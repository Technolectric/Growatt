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
# Location Config (Kajiado)
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

# ======================================================
# ✅ FIXED WEATHER FETCH (SAFE + RELIABLE)
# ======================================================
def get_weather_forecast():
    try:
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={LATITUDE}"
            f"&longitude={LONGITUDE}"
            "&hourly=cloud_cover,shortwave_radiation,direct_radiation"
            "&timezone=Africa/Nairobi"
            "&forecast_days=2"
        )

        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()

        hourly = data.get("hourly", {})

        forecast = {
            "times": hourly.get("time", []),
            "cloud_cover": hourly.get("cloud_cover", []),
            "solar_radiation": hourly.get("shortwave_radiation", []),
            "direct_radiation": hourly.get("direct_radiation", [])
        }

        if not forecast["times"]:
            print("⚠️ Weather API returned no hourly data")
            return None

        print(f"✓ Weather forecast updated ({len(forecast['times'])} hours)")
        return forecast

    except Exception as e:
        print(f"✗ Weather fetch error: {e}")
        return None


def analyze_solar_conditions(forecast, hours_ahead=10):
    if not forecast or not forecast.get("times"):
        return None

    try:
        now = datetime.now(EAT)
        future_time = now + timedelta(hours=hours_ahead)

        avg_cloud_cover = 0
        avg_solar_radiation = 0
        count = 0

        for i, time_str in enumerate(forecast["times"]):
            forecast_time = datetime.fromisoformat(
                time_str.replace("Z", "+00:00")
            ).astimezone(EAT)

            if now <= forecast_time <= future_time:
                avg_cloud_cover += forecast["cloud_cover"][i]
                avg_solar_radiation += forecast["solar_radiation"][i]
                count += 1

        if count == 0:
            return None

        avg_cloud_cover /= count
        avg_solar_radiation /= count

        poor_conditions = avg_cloud_cover > 70 or avg_solar_radiation < 200

        return {
            "avg_cloud_cover": avg_cloud_cover,
            "avg_solar_radiation": avg_solar_radiation,
            "poor_conditions": poor_conditions,
            "hours_analyzed": count
        }

    except Exception as e:
        print(f"✗ Solar analysis error: {e}")
        return None

# ----------------------------
# Growatt Polling Loop
# ----------------------------
def poll_growatt():
    global latest_data, load_history, battery_history, weather_forecast
    last_weather_update = None

    while True:
        try:
            now = datetime.now(EAT)

            if (
                last_weather_update is None or
                now - last_weather_update > timedelta(minutes=30)
            ):
                wf = get_weather_forecast()
                if wf:
                    weather_forecast = wf
                last_weather_update = now

            total_output_power = 0
            total_battery_discharge_W = 0
            inverter_data = []

            for sn in SERIAL_NUMBERS:
                response = requests.post(
                    API_URL,
                    data={"storage_sn": sn},
                    headers=headers,
                    timeout=20
                )
                response.raise_for_status()
                data = response.json().get("data", {})

                out_power = float(data.get("outPutPower") or 0)
                capacity = float(data.get("capacity") or 0)
                v_bat = float(data.get("vBat") or 0)
                p_bat = float(data.get("pBat") or 0)

                total_output_power += out_power
                if p_bat > 0:
                    total_battery_discharge_W += p_bat

                cfg = INVERTER_CONFIG.get(sn, {})
                inverter_data.append({
                    "SN": sn,
                    "Label": cfg.get("label", sn),
                    "Type": cfg.get("type", "unknown"),
                    "OutputPower": out_power,
                    "Capacity": capacity,
                    "vBat": v_bat,
                    "pBat": p_bat,
                    "Status": data.get("statusText", "Unknown")
                })

            latest_data = {
                "timestamp": now.strftime("%Y-%m-%d %H:%M:%S EAT"),
                "total_output_power": total_output_power,
                "total_battery_discharge_W": total_battery_discharge_W,
                "inverters": inverter_data
            }

            load_history.append((now, total_output_power))
            load_history[:] = [(t, p) for t, p in load_history if t >= now - timedelta(hours=12)]

            battery_history.append((now, total_battery_discharge_W))
            battery_history[:] = [(t, p) for t, p in battery_history if t >= now - timedelta(hours=12)]

            time.sleep(POLL_INTERVAL_MINUTES * 60)

        except Exception as e:
            print(f"❌ Poll error: {e}")
            time.sleep(30)

# ----------------------------
# Flask Route (✅ LAZY WEATHER LOAD FIX)
# ----------------------------
@app.route("/")
def home():
    global weather_forecast

    if not weather_forecast:
        weather_forecast = get_weather_forecast()

    solar_conditions = analyze_solar_conditions(weather_forecast, hours_ahead=10)

    return "<h3>Dashboard loading OK – Weather logic fixed</h3>"

# ----------------------------
# Start background thread
# ----------------------------
Thread(target=poll_growatt, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
