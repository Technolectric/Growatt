import os
import time
import requests
from datetime import datetime, timedelta, timezone
from flask import Flask
from threading import Thread

# ----------------------------
# Flask app
# ----------------------------
app = Flask(__name__)

# ----------------------------
# Growatt Config
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

INVERTER_ORDER = [
    "RKG3B0400T",
    "KAM4N5W0AG",
    "JNK1CDR0KQ"
]

PRIMARY_BATTERY_THRESHOLD = 40
BACKUP_VOLTAGE_THRESHOLD = 51.2

# ----------------------------
# Location (Kajiado)
# ----------------------------
LATITUDE = -1.85238
LONGITUDE = 36.77683

# ----------------------------
# Globals
# ----------------------------
headers = {"token": TOKEN, "Content-Type": "application/x-www-form-urlencoded"}
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
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={LATITUDE}&longitude={LONGITUDE}"
            f"&hourly=cloud_cover,shortwave_radiation"
            f"&timezone=Africa/Nairobi&forecast_days=2"
        )
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        return {
            "times": data["hourly"]["time"],
            "cloud": data["hourly"]["cloud_cover"],
            "solar": data["hourly"]["shortwave_radiation"],
        }
    except Exception as e:
        print("Weather error:", e)
        return None

def get_next_12h_forecast(forecast):
    if not forecast:
        return []

    now = datetime.now(EAT)
    rows = []

    for i, t in enumerate(forecast["times"]):
        ft = datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(EAT)
        if now <= ft <= now + timedelta(hours=12):
            rows.append({
                "time": ft.strftime("%H:%M"),
                "cloud": forecast["cloud"][i],
                "solar": forecast["solar"][i],
            })
    return rows

# ----------------------------
# Growatt Polling
# ----------------------------
def poll_growatt():
    global latest_data, weather_forecast, load_history, battery_history

    last_weather = None

    while True:
        try:
            if not last_weather or datetime.now(EAT) - last_weather > timedelta(minutes=30):
                weather_forecast = get_weather_forecast()
                last_weather = datetime.now(EAT)

            inverter_data = []
            total_load = 0
            total_discharge = 0
            now = datetime.now(EAT)

            primary_caps = []
            backup = None

            for sn in SERIAL_NUMBERS:
                r = requests.post(API_URL, headers=headers, data={"storage_sn": sn}, timeout=20)
                r.raise_for_status()
                data = r.json().get("data", {})

                cfg = INVERTER_CONFIG.get(sn, {})
                out_power = float(data.get("outPutPower") or 0)
                capacity = float(data.get("capacity") or 0)
                vbat = float(data.get("vBat") or 0)

                # ✅ Correct battery logic
                p_charge = float(data.get("pCharge") or 0)
                p_discharge = float(data.get("pDischarge") or 0)

                total_load += out_power
                total_discharge += p_discharge

                inv = {
                    "SN": sn,
                    "Label": cfg.get("label", sn),
                    "Type": cfg.get("type", "unknown"),
                    "DataLog": cfg.get("datalog", ""),
                    "OutputPower": out_power,
                    "Capacity": capacity,
                    "vBat": vbat,
                    "pCharge": p_charge,
                    "pDischarge": p_discharge,
                    "Status": data.get("statusText", "Unknown"),
                }

                inverter_data.append(inv)

                if inv["Type"] == "primary":
                    primary_caps.append(capacity)
                if inv["Type"] == "backup":
                    backup = inv

            # ✅ Force inverter order
            inverter_data.sort(key=lambda x: INVERTER_ORDER.index(x["SN"]))

            primary_min = min(primary_caps) if primary_caps else 0
            backup_voltage = backup["vBat"] if backup else 0
            backup_active = backup["OutputPower"] > 50 if backup else False

            latest_data = {
                "timestamp": now.strftime("%Y-%m-%d %H:%M:%S EAT"),
                "total_output_power": total_load,
                "total_battery_discharge_W": total_discharge,
                "primary_battery_min": primary_min,
                "backup_voltage": backup_voltage,
                "backup_active": backup_active,
                "inverters": inverter_data,
            }

            load_history.append((now, total_load))
            load_history[:] = [(t, p) for t, p in load_history if t > now - timedelta(hours=12)]

            battery_history.append((now, total_discharge))
            battery_history[:] = [(t, p) for t, p in battery_history if t > now - timedelta(hours=12)]

            print(f"{latest_data['timestamp']} | Load {total_load:.0f}W | Discharge {total_discharge:.0f}W")

        except Exception as e:
            print("Growatt polling error:", e)

        time.sleep(POLL_INTERVAL_MINUTES * 60)

# ----------------------------
# Web UI
# ----------------------------
@app.route("/")
def home():
    forecast_12h = get_next_12h_forecast(weather_forecast)

    html = "<h1>TULIA HOUSE – Solar Monitor</h1>"
    html += f"<p>Last Update: {latest_data.get('timestamp','')}</p>"

    html += "<h2>Inverters</h2>"
    for inv in latest_data.get("inverters", []):
        html += f"""
        <b>{inv['Label']}</b><br>
        Battery: {inv['Capacity']}% | {inv['vBat']}V<br>
        Output: {inv['OutputPower']}W<br>
        Discharge: {inv['pDischarge']}W | Charge: {inv['pCharge']}W<br>
        Status: {inv['Status']}<hr>
        """

    html += "<h2>Next 12 Hours Solar Forecast</h2><table border=1 cellpadding=5>"
    html += "<tr><th>Time</th><th>Cloud %</th><th>Solar W/m²</th></tr>"
    for h in forecast_12h:
        html += f"<tr><td>{h['time']}</td><td>{h['cloud']}</td><td>{h['solar']}</td></tr>"
    html += "</table>"

    return html

# ----------------------------
# Start
# ----------------------------
if __name__ == "__main__":
    Thread(target=poll_growatt, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)
