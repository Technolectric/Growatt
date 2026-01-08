import os
import time
import requests
from datetime import datetime, timedelta, timezone
from threading import Thread
from flask import Flask

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

# Thresholds
PRIMARY_BAT_THRESHOLD = 40
GEN_START_VOLTAGE = 51.2

# Location (Kajiado, Kenya)
LATITUDE = -1.85238
LONGITUDE = 36.77683
EAT = timezone(timedelta(hours=3))

# Email (Resend)
RESEND_API_KEY = os.getenv('RESEND_API_KEY')
SENDER_EMAIL = os.getenv('SENDER_EMAIL')
RECIPIENT_EMAIL = os.getenv('RECIPIENT_EMAIL')

# Globals
headers = {"token": TOKEN, "Content-Type": "application/x-www-form-urlencoded"}
latest_data = {}
weather_forecast = {}
last_alert_time = {}

# ----------------------------
# Helpers
# ----------------------------
def get_weather_desc(code):
    mapping = {
        0: "Clear", 1: "Mainly Clear", 2: "Partly Cloudy", 3: "Overcast",
        45: "Fog", 51: "Drizzle", 61: "Rain", 80: "Showers"
    }
    return mapping.get(code, "Cloudy")

def get_weather_forecast():
    try:
        url = f"https://api.open-meteo.com/v1/forecast?latitude={LATITUDE}&longitude={LONGITUDE}&hourly=cloud_cover,shortwave_radiation,weather_code,temperature_2m&timezone=Africa/Nairobi&forecast_days=2"
        response = requests.get(url, timeout=10)
        return response.json().get('hourly', {})
    except Exception as e:
        print(f"Weather Error: {e}")
        return {}

def send_email(subject, html_content, alert_type="general"):
    if not all([RESEND_API_KEY, SENDER_EMAIL, RECIPIENT_EMAIL]): return
    cooldown = 60 if alert_type != "critical" else 30
    if alert_type in last_alert_time and datetime.now(EAT) - last_alert_time[alert_type] < timedelta(minutes=cooldown):
        return
    try:
        requests.post("https://api.resend.com/emails", 
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={"from": SENDER_EMAIL, "to": [RECIPIENT_EMAIL], "subject": subject, "html": html_content})
        last_alert_time[alert_type] = datetime.now(EAT)
    except: pass

# ----------------------------
# Core Logic
# ----------------------------
def poll_growatt():
    global latest_data, weather_forecast
    while True:
        try:
            weather_forecast = get_weather_forecast()
            temp_inverters = []
            total_load = 0
            
            for sn in SERIAL_NUMBERS:
                r = requests.post(API_URL, data={"storage_sn": sn}, headers=headers, timeout=20)
                d = r.json().get("data", {})
                cfg = INVERTER_CONFIG.get(sn, {"label": sn, "type": "unknown"})
                
                inv_obj = {
                    "Label": cfg['label'], "SN": sn, "Type": cfg['type'],
                    "Capacity": float(d.get("capacity") or 0),
                    "vBat": float(d.get("vBat") or 0),
                    "OutputPower": float(d.get("outPutPower") or 0),
                    "Status": d.get("statusText", "Offline")
                }
                temp_inverters.append(inv_obj)
                total_load += inv_obj["OutputPower"]

            # Sort 1, 2, 3
            temp_inverters.sort(key=lambda x: x['Label'])
            
            # Extract main metrics
            inv1, inv2, inv3 = temp_inverters[0], temp_inverters[1], temp_inverters[2]
            
            latest_data = {
                "timestamp": datetime.now(EAT).strftime("%H:%M:%S"),
                "total_load": total_load,
                "primary_min": min(inv1['Capacity'], inv2['Capacity']),
                "backup_v": inv3['vBat'],
                "backup_active": inv3['OutputPower'] > 50,
                "inverters": temp_inverters
            }

            # Alert Check
            b_stat = "GOOD" if inv3['vBat'] >= 53 else "MEDIUM" if inv3['vBat'] >= 52 else "LOW"
            if inv3['vBat'] < GEN_START_VOLTAGE:
                send_email("🚨 CRITICAL: Gen Starting", f"Backup vBat: {inv3['vBat']}V ({b_stat})", "critical")
            elif latest_data['backup_active'] and latest_data['primary_min'] < PRIMARY_BAT_THRESHOLD:
                send_email("⚠️ Backup Active", f"Status: {b_stat} | Primary: {latest_data['primary_min']}%", "backup")

        except Exception as e: print(f"Poll Error: {e}")
        time.sleep(POLL_INTERVAL_MINUTES * 60)

# ----------------------------
# UI
# ----------------------------
@app.route("/")
def home():
    if not latest_data: return "Loading data..."
    
    # Weather rows
    weather_rows = ""
    if weather_forecast:
        now = datetime.now(EAT)
        for i, t_str in enumerate(weather_forecast.get('time', [])):
            ft = datetime.fromisoformat(t_str.replace('Z', '+00:00')).astimezone(EAT)
            if now <= ft <= now + timedelta(hours=10):
                weather_rows += f"<tr><td>{ft.strftime('%H:%M')}</td><td>{get_weather_desc(weather_forecast['weather_code'][i])}</td><td>{weather_forecast['temperature_2m'][i]}°C</td><td>{weather_forecast['shortwave_radiation'][i]}W/m²</td></tr>"

    html = f"""
    <!DOCTYPE html><html><head><title>Tulia Solar</title>
    <style>
        body {{ font-family: sans-serif; background: #f0f2f5; padding: 20px; }}
        .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; }}
        .card {{ background: white; padding: 20px; border-radius: 12px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
        .metric {{ font-size: 2em; font-weight: bold; margin: 10px 0; }}
        .stat {{ display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #eee; }}
        table {{ width: 100%; border-collapse: collapse; }}
        td, th {{ padding: 8px; text-align: left; border-bottom: 1px solid #eee; }}
    </style></head><body>
    <h1>Tulia House Solar</h1>
    <p>Updated: {latest_data['timestamp']} EAT</p>
    
    <div class="grid">
        <div class="card">
            <h3>System Load</h3>
            <div class="metric" style="color:#007bff">{latest_data['total_load']:.0f}W</div>
            <div class="stat"><span>Primary Battery</span> <span>{latest_data['primary_min']}%</span></div>
            <div class="stat"><span>Backup Status</span> <span style="color:{'orange' if latest_data['backup_active'] else 'green'}">{'ACTIVE' if latest_data['backup_active'] else 'Standby'}</span></div>
        </div>

        <div class="card">
            <h3>Weather Forecast (10h)</h3>
            <table><tr><th>Time</th><th>Sky</th><th>Temp</th><th>Solar</th></tr>{weather_rows}</table>
        </div>
    </div>

    <h2 style="margin-top:30px">Inverters</h2>
    <div class="grid">
    """
    
    for inv in latest_data['inverters']:
        # Special Logic for Inverter 3
        if "3" in inv['Label']:
            v = inv['vBat']
            if v >= 53: b_disp = '<b style="color:green">GOOD</b>'
            elif v >= 52: b_disp = '<b style="color:orange">MEDIUM</b>'
            else: b_disp = '<b style="color:red">LOW</b>'
        else:
            b_disp = f"<b>{inv['Capacity']:.0f}%</b>"

        html += f"""
        <div class="card">
            <h3>{inv['Label']}</h3>
            <div class="stat"><span>Battery</span> {b_disp}</div>
            <div class="stat"><span>Voltage</span> <b>{inv['vBat']:.1f}V</b></div>
            <div class="stat"><span>Load</span> <b>{inv['OutputPower']:.0f}W</b></div>
            <div class="stat"><span>Status</span> <span>{inv['Status']}</span></div>
        </div>"""

    return html + "</div></body></html>"

if __name__ == "__main__":
    Thread(target=poll_growatt, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
