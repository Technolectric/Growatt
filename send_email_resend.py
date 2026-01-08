import os
import time
import requests
from datetime import datetime, timedelta, timezone
from threading import Thread
from flask import Flask, render_template_string, request

# ----------------------------
# Flask app
# ----------------------------
app = Flask(__name__)

# ----------------------------
# Growatt Config (from env)
# ----------------------------
API_URL = "https://openapi.growatt.com/v1/device/storage/storage_last_data"
TOKEN = os.getenv("API_TOKEN")
SERIAL_NUMBERS = os.getenv("SERIAL_NUMBERS", "").split(",")
POLL_INTERVAL_MINUTES = int(os.getenv("POLL_INTERVAL_MINUTES", 5))

# ----------------------------
# Inverter Configuration
# ----------------------------
INVERTER_CONFIG = {
    "RKG3B0400T": {"label": "Inverter 1", "type": "primary", "datalog": "DDD0B021CC"},
    "KAM4N5W0AG": {"label": "Inverter 2", "type": "primary", "datalog": "DDD0B02121"},
    "JNK1CDR0KQ": {"label": "Inverter 3 (Backup)", "type": "backup", "datalog": "DDD0B0221H"}
}

PRIMARY_BATTERY_THRESHOLD = 40
BACKUP_VOLTAGE_THRESHOLD = 51.2
BACKUP_CAPACITY_WARNING = 30
TOTAL_SOLAR_CAPACITY_KW = 10

# ----------------------------
# Location Config
# ----------------------------
LATITUDE = -1.85238
LONGITUDE = 36.77683

# ----------------------------
# Email Config
# ----------------------------
RESEND_API_KEY = os.getenv('RESEND_API_KEY')
SENDER_EMAIL = os.getenv('SENDER_EMAIL')
RECIPIENT_EMAIL = os.getenv('RECIPIENT_EMAIL')

# ----------------------------
# Globals
# ----------------------------
headers = {"token": TOKEN, "Content-Type": "application/x-www-form-urlencoded"}
last_alert_time = {}
latest_data = {}
load_history = []
battery_history = []
weather_forecast = {}

EAT = timezone(timedelta(hours=3))

# ----------------------------
# Weather
# ----------------------------
def get_weather_forecast():
    try:
        url = f"https://api.open-meteo.com/v1/forecast?latitude={LATITUDE}&longitude={LONGITUDE}&hourly=cloud_cover,shortwave_radiation,direct_radiation&timezone=Africa/Nairobi&forecast_days=2"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        d = r.json()
        return {
            "times": d["hourly"]["time"],
            "cloud_cover": d["hourly"]["cloud_cover"],
            "solar_radiation": d["hourly"]["shortwave_radiation"],
            "direct_radiation": d["hourly"]["direct_radiation"],
        }
    except Exception as e:
        print(e)
        return None

def analyze_solar_conditions(forecast, hours_ahead=10):
    if not forecast:
        return None
    now = datetime.now(EAT)
    future = now + timedelta(hours=hours_ahead)
    cloud = solar = count = 0
    for i, t in enumerate(forecast["times"]):
        ft = datetime.fromisoformat(t.replace("Z","+00:00")).astimezone(EAT)
        if now <= ft <= future:
            cloud += forecast["cloud_cover"][i]
            solar += forecast["solar_radiation"][i]
            count += 1
    if count == 0:
        return None
    cloud /= count
    solar /= count
    return {
        "avg_cloud_cover": cloud,
        "avg_solar_radiation": solar,
        "poor_conditions": cloud > 70 or solar < 200
    }

# ----------------------------
# Email
# ----------------------------
def send_email(subject, html, alert_type="general"):
    global last_alert_time
    if not all([RESEND_API_KEY, SENDER_EMAIL, RECIPIENT_EMAIL]):
        return False
    cooldown = 30 if alert_type == "critical" else 60
    if alert_type in last_alert_time:
        if datetime.now(EAT) - last_alert_time[alert_type] < timedelta(minutes=cooldown):
            return False
    r = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
        json={"from": SENDER_EMAIL, "to": [RECIPIENT_EMAIL], "subject": subject, "html": html}
    )
    if r.status_code == 200:
        last_alert_time[alert_type] = datetime.now(EAT)
        return True
    return False

# ----------------------------
# Polling
# ----------------------------
def poll_growatt():
    global latest_data, load_history, battery_history, weather_forecast
    last_weather = None

    while True:
        try:
            if not last_weather or datetime.now(EAT) - last_weather > timedelta(minutes=30):
                weather_forecast = get_weather_forecast()
                last_weather = datetime.now(EAT)

            total_output_power = 0
            total_battery_discharge_W = 0
            inverter_data = []
            now = datetime.now(EAT)
            primary_caps = []
            backup = None

            for sn in SERIAL_NUMBERS:
                r = requests.post(API_URL, headers=headers, data={"storage_sn": sn}, timeout=20)
                d = r.json().get("data", {})
                cfg = INVERTER_CONFIG.get(sn, {"label": sn, "type": "unknown", "datalog": "N/A"})

                out_power = float(d.get("outPutPower") or 0)
                capacity = float(d.get("capacity") or 0)
                vbat = float(d.get("vBat") or 0)
                pbat = float(d.get("pBat") or 0)

                total_output_power += out_power
                if pbat > 0:
                    total_battery_discharge_W += pbat

                inv = {
                    "SN": sn,
                    "Label": cfg["label"],
                    "Type": cfg["type"],
                    "DataLog": cfg["datalog"],
                    "OutputPower": out_power,
                    "Capacity": capacity,
                    "vBat": vbat,
                    "pBat": pbat,
                    "Status": d.get("statusText", "Unknown")
                }
                inverter_data.append(inv)

                if cfg["type"] == "primary":
                    primary_caps.append(capacity)
                if cfg["type"] == "backup":
                    backup = inv

            latest_data = {
                "timestamp": now.strftime("%Y-%m-%d %H:%M:%S EAT"),
                "total_output_power": total_output_power,
                "total_battery_discharge_W": total_battery_discharge_W,
                "primary_battery_min": min(primary_caps) if primary_caps else 0,
                "backup_battery": backup["Capacity"] if backup else 0,
                "backup_voltage": backup["vBat"] if backup else 0,
                "backup_active": backup["OutputPower"] > 50 if backup else False,
                "inverters": inverter_data
            }

            load_history.append((now, total_output_power))
            load_history[:] = [(t,p) for t,p in load_history if t >= now - timedelta(hours=12)]
            battery_history.append((now, total_battery_discharge_W))
            battery_history[:] = [(t,p) for t,p in battery_history if t >= now - timedelta(hours=12)]

        except Exception as e:
            print(e)

        time.sleep(POLL_INTERVAL_MINUTES * 60)

# ----------------------------
# Web
# ----------------------------
@app.route("/")
def home():
    return "Growatt monitor running ✅"

# ----------------------------
# Start
# ----------------------------
if __name__ == "__main__":
    Thread(target=poll_growatt, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)
