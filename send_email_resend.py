import os
import time
import requests
from datetime import datetime, timedelta, timezone
from threading import Thread
from flask import Flask

# ----------------------------
# Flask app configuration
# ----------------------------
app = Flask(__name__)

# ----------------------------
# Growatt Config (via Environment Variables)
# ----------------------------
# API Endpoints
API_URL = "https://openapi.growatt.com/v1/device/storage/storage_last_data"
HISTORY_URL = "https://openapi.growatt.com/v1/device/storage/storage_history"

TOKEN = os.getenv("API_TOKEN")
# Expects a comma-separated string: "SN1,SN2,SN3"
SERIAL_NUMBERS = os.getenv("SERIAL_NUMBERS", "").split(",")
POLL_INTERVAL_MINUTES = int(os.getenv("POLL_INTERVAL_MINUTES", 5))

# Inverter Mapping (Strictly following your 1, 2, 3 order)
INVERTER_CONFIG = {
    "RKG3B0400T": {"label": "Inverter 1", "type": "primary", "datalog": "DDD0B021CC"},
    "KAM4N5W0AG": {"label": "Inverter 2", "type": "primary", "datalog": "DDD0B02121"},
    "JNK1CDR0KQ": {"label": "Inverter 3 (Backup)", "type": "backup", "datalog": "DDD0B0221H"}
}

# Thresholds
PRIMARY_BAT_THRESHOLD = 40
GEN_START_VOLTAGE = 51.2
EAT = timezone(timedelta(hours=3))

# Email (Resend)
RESEND_API_KEY = os.getenv('RESEND_API_KEY')
SENDER_EMAIL = os.getenv('SENDER_EMAIL')
RECIPIENT_EMAIL = os.getenv('RECIPIENT_EMAIL')

# Globals
headers = {"token": TOKEN, "Content-Type": "application/x-www-form-urlencoded"}
latest_data = {}
load_history = []  # List of (datetime, power) tuples
weather_forecast = {}
last_alert_time = {}

# ----------------------------
# Weather Helpers
# ----------------------------
def get_weather_desc(code):
    mapping = {0: "Clear", 1: "Mainly Clear", 2: "Partly Cloudy", 3: "Overcast", 45: "Fog", 51: "Drizzle", 61: "Rain", 80: "Showers"}
    return mapping.get(code, "Cloudy")

def get_weather_forecast():
    try:
        # Requesting Nairobi timezone ensures Open-Meteo returns EAT strings
        url = f"https://api.open-meteo.com/v1/forecast?latitude=-1.85238&longitude=36.77683&hourly=cloud_cover,shortwave_radiation,weather_code,temperature_2m&timezone=Africa/Nairobi&forecast_days=2"
        response = requests.get(url, timeout=10)
        return response.json().get('hourly', {})
    except: return {}

# ----------------------------
# Historical Data Loader (The Fix for Render Restarts)
# ----------------------------
def load_api_history():
    """Hits the history API for the last 12 hours so the chart is never empty."""
    global load_history
    print("Pre-loading historical data from Growatt...")
    today_str = datetime.now(EAT).strftime("%Y-%m-%d")
    temp_history = {}

    try:
        # We fetch history for the two primary inverters to sum their load
        for sn in SERIAL_NUMBERS[:2]:
            r = requests.post(HISTORY_URL, data={"storage_sn": sn, "date": today_str, "start": 0}, headers=headers, timeout=20)
            data = r.json().get("data", {}).get("datas", [])
            for entry in data:
                t_str = entry.get("time")
                if not t_str: continue
                dt = datetime.strptime(t_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=EAT)
                if dt < datetime.now(EAT) - timedelta(hours=12): continue
                
                power = float(entry.get("outPutPower", 0))
                temp_history[t_str] = temp_history.get(t_str, 0) + power

        sorted_times = sorted(temp_history.keys())
        load_history = [(datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=EAT), temp_history[ts]) for ts in sorted_times]
        print(f"Loaded {len(load_history)} historical points.")
    except Exception as e:
        print(f"History Load Error: {e}")

# ----------------------------
# Background Polling
# ----------------------------
def poll_growatt():
    global latest_data, weather_forecast, load_history
    while True:
        try:
            weather_forecast = get_weather_forecast()
            temp_inverters = []
            total_load = 0
            now = datetime.now(EAT)
            
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

            temp_inverters.sort(key=lambda x: x['Label']) # Keep 1, 2, 3 order
            
            latest_data = {
                "timestamp": now.strftime("%Y-%m-%d %H:%M:%S EAT"),
                "total_load": total_load,
                "primary_min": min(temp_inverters[0]['Capacity'], temp_inverters[1]['Capacity']) if len(temp_inverters) > 1 else 0,
                "backup_v": temp_inverters[2]['vBat'] if len(temp_inverters) > 2 else 0,
                "backup_active": temp_inverters[2]['OutputPower'] > 50 if len(temp_inverters) > 2 else False,
                "inverters": temp_inverters
            }

            load_history.append((now, total_load))
            load_history = [(t, p) for t, p in load_history if t >= now - timedelta(hours=12)]

        except Exception as e: print(f"Poll Error: {e}")
        time.sleep(POLL_INTERVAL_MINUTES * 60)

# ----------------------------
# UI Route
# ----------------------------
@app.route("/")
def home():
    if not latest_data: return '<div style="text-align:center;padding:50px;">Initializing System... Refresh in 30 seconds.</div>'
    
    # Logic for UI colors
    p_color = "red" if latest_data['primary_min'] < 40 else "orange" if latest_data['primary_min'] < 50 else "green"
    b_color = "red" if latest_data['backup_v'] < 51.2 else "orange" if latest_data['backup_v'] < 52 else "green"
    
    # Fix for Weather Table
    weather_rows = ""
    if weather_forecast and 'time' in weather_forecast:
        now = datetime.now(EAT)
        for i, t_str in enumerate(weather_forecast['time']):
            ft = datetime.fromisoformat(t_str).replace(tzinfo=EAT) # Force EAT
            if now <= ft <= now + timedelta(hours=10):
                rad = weather_forecast['shortwave_radiation'][i]
                rad_style = 'color: #dc3545; font-weight: bold;' if rad < 200 and (6 <= ft.hour <= 18) else 'color: #28a745;'
                weather_rows += f"<tr><td>{ft.strftime('%H:%M')}</td><td>{get_weather_desc(weather_forecast['weather_code'][i])}</td><td>{weather_forecast['temperature_2m'][i]}°C</td><td style='{rad_style}'>{rad} W/m²</td></tr>"

    inverter_html = ""
    for inv in latest_data['inverters']:
        is_backup = inv['Type'] == 'backup'
        b_disp = f"<b>{inv['Capacity']:.0f}%</b>" if not is_backup else ('GOOD' if inv['vBat'] >= 53 else 'MEDIUM' if inv['vBat'] >= 52 else 'LOW')
        inverter_html += f'<div class="inverter-card {"backup" if is_backup else ""}"><h3>{inv["Label"]}</h3><div class="inv-label">SN: {inv["SN"]}</div><div class="inv-stat"><span>Battery</span><span>{b_disp}</span></div><div class="inv-stat"><span>Voltage</span><span>{inv["vBat"]:.1f}V</span></div><div class="inv-stat"><span>Output</span><span>{inv["OutputPower"]:.0f}W</span></div><div class="inv-stat"><span>Status</span><span>{inv["Status"]}</span></div></div>'

    return f"""
<!DOCTYPE html>
<html>
<head>
    <title>Tulia House Solar</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: 'Segoe UI', sans-serif; background: linear-gradient(rgba(0,0,0,0.5), rgba(0,0,0,0.7)), url('https://images.unsplash.com/photo-1582268611958-ebfd161ef9cf?w=1600') center/cover fixed; min-height: 100vh; padding: 20px; color: #333; }}
        .container {{ max-width: 1400px; margin: 0 auto; }}
        .header {{ text-align: center; color: white; margin-bottom: 30px; text-shadow: 2px 2px 4px rgba(0,0,0,0.8); }}
        .card {{ background: rgba(255, 255, 255, 0.95); backdrop-filter: blur(10px); padding: 25px; border-radius: 15px; box-shadow: 0 8px 32px rgba(0,0,0,0.3); margin-bottom: 20px; }}
        .metrics-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-bottom: 30px; }}
        .metric {{ padding: 20px; border-radius: 12px; color: white; text-align:center; }}
        .metric-value {{ font-size: 1.8em; font-weight: bold; display:block; }}
        .metric.green {{ background: linear-gradient(135deg, #11998e, #38ef7d); }}
        .metric.orange {{ background: linear-gradient(135deg, #f77f00, #fcbf49); }}
        .metric.red {{ background: linear-gradient(135deg, #eb3349, #f45c43); }}
        .metric.blue {{ background: linear-gradient(135deg, #667eea, #764ba2); }}
        .system-status {{ background: {'linear-gradient(135deg, #eb3349, #f45c43)' if latest_data['backup_v'] < 51.2 else 'linear-gradient(135deg, #f77f00, #fcbf49)' if latest_data['backup_active'] else 'linear-gradient(135deg, #11998e, #38ef7d)'}; color: white; padding: 20px; border-radius: 12px; margin-bottom: 20px; text-align: center; }}
        .inverter-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; }}
        .inverter-card {{ background: white; padding: 20px; border-radius: 12px; border-left: 5px solid #667eea; }}
        .inverter-card.backup {{ border-left-color: #f77f00; }}
        .inv-stat {{ display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #eee; }}
        table {{ width: 100%; border-collapse: collapse; }}
        th, td {{ padding: 12px; border-bottom: 1px solid #eee; text-align: left; }}
        .chart-container {{ height: 300px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header"><h1>TULIA HOUSE</h1><p>Solar Energy Monitor</p></div>
        <div class="card">
            <div class="system-status"><h2>{'🚨 GENERATOR STARTING' if latest_data['backup_v'] < 51.2 else '⚠️ BACKUP ACTIVE' if latest_data['backup_active'] else '✓ SYSTEM NORMAL'}</h2></div>
            <div class="metrics-grid">
                <div class="metric {p_color}"><span class="metric-value">{latest_data['primary_min']:.0f}%</span><span>Primary Bat</span></div>
                <div class="metric {b_color}"><span class="metric-value">{latest_data['backup_v']:.1f}V</span><span>Backup Bat</span></div>
                <div class="metric blue"><span class="metric-value">{latest_data['total_load']:.0f}W</span><span>System Load</span></div>
                <div class="metric {'orange' if latest_data['backup_active'] else 'green'}"><span class="metric-value">{'ACTIVE' if latest_data['backup_active'] else 'Standby'}</span><span>Backup Status</span></div>
            </div>
            <div class="inverter-grid">
                <div style="grid-column: span 2;"><h2>Inverter Details</h2><div class="inverter-grid">{inverter_html}</div></div>
                <div><h2>10h Forecast</h2><div style="background:white; border-radius:12px; padding:10px;"><table>{weather_rows}</table></div></div>
            </div>
        </div>
        <div class="card"><h2>Power Consumption (12 Hours)</h2><div class="chart-container"><canvas id="loadChart"></canvas></div></div>
        <div style="text-align:center; color:white; font-size:0.8em;">Last Update: {latest_data['timestamp']}</div>
    </div>
    <script>
        new Chart(document.getElementById('loadChart'), {{
            type: 'line',
            data: {{
                labels: {str([t.strftime('%H:%M') for t, p in load_history])},
                datasets: [{{ label: 'Watts', data: {str([p for t, p in load_history])}, borderColor: '#667eea', backgroundColor: 'rgba(102,126,234,0.2)', fill: true, tension: 0.4, pointRadius: 4 }}]
            }},
            options: {{ responsive: true, maintainAspectRatio: false }}
        }});
    </script>
</body>
</html>
"""

@app.route("/health")
def health(): return "OK", 200

if __name__ == "__main__":
    load_api_history() # PRE-FILL CHART
    Thread(target=poll_growatt, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
