import os
import time
import requests
from datetime import datetime, timedelta, timezone
from threading import Thread, Lock
from flask import Flask, redirect, url_for

# ----------------------------
# 1. Initialization & Config
# ----------------------------
app = Flask(__name__)
data_lock = Lock()

# Growatt API Endpoints
API_URL = "https://openapi.growatt.com/v1/device/storage/storage_last_data"
HISTORY_URL = "https://openapi.growatt.com/v1/device/storage/storage_history"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"

# Env Vars (Make sure these are set in Render)
TOKEN = os.getenv("API_TOKEN")
SERIAL_NUMBERS = os.getenv("SERIAL_NUMBERS", "RKG3B0400T,KAM4N5W0AG,JNK1CDR0KQ").split(",")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_MINUTES", 5))

# Local Inverter Setup
INVERTER_CONFIG = {
    "RKG3B0400T": {"label": "Inverter 1", "type": "primary"},
    "KAM4N5W0AG": {"label": "Inverter 2", "type": "primary"},
    "JNK1CDR0KQ": {"label": "Inverter 3 (Backup)", "type": "backup"}
}

# Timezone (Nairobi)
EAT = timezone(timedelta(hours=3))

# Global Storage
headers = {"token": TOKEN, "Content-Type": "application/x-www-form-urlencoded"}
latest_data = {}
load_history = [] 
weather_forecast = {}

# ----------------------------
# 2. Data Logic
# ----------------------------

def get_weather_forecast():
    """Fetches high-accuracy solar radiation forecast for Nairobi coordinates."""
    try:
        params = {
            "latitude": -1.8524,
            "longitude": 36.7768,
            "hourly": "shortwave_radiation,temperature_2m,weather_code",
            "timezone": "Africa/Nairobi",
            "forecast_days": 2
        }
        r = requests.get(WEATHER_URL, params=params, timeout=10)
        return r.json().get('hourly', {})
    except Exception as e:
        print(f"Weather API Error: {e}")
        return {}

def fetch_system_data():
    """Syncs current inverter status and weather forecast."""
    global latest_data, weather_forecast, load_history
    with data_lock:
        try:
            # Refresh weather first
            weather_forecast = get_weather_forecast()
            
            temp_inverters = []
            total_load = 0
            now = datetime.now(EAT)
            
            for sn in SERIAL_NUMBERS:
                r = requests.post(API_URL, data={"storage_sn": sn}, headers=headers, timeout=15)
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

            temp_inverters.sort(key=lambda x: x['Label'])
            
            latest_data = {
                "timestamp": now.strftime("%H:%M:%S"),
                "total_load": total_load,
                "primary_min": min(temp_inverters[0]['Capacity'], temp_inverters[1]['Capacity']) if len(temp_inverters) > 1 else 0,
                "backup_v": temp_inverters[2]['vBat'] if len(temp_inverters) > 2 else 0,
                "backup_active": temp_inverters[2]['OutputPower'] > 50 if len(temp_inverters) > 2 else False,
                "inverters": temp_inverters
            }
            
            # Update history (12h rolling window)
            load_history.append((now, total_load))
            load_history = [(t, p) for t, p in load_history if t >= now - timedelta(hours=12)]
            return True
        except Exception as e:
            print(f"Growatt Sync Error: {e}")
            return False

def load_initial_history():
    """Warm up the chart by pulling Growatt history on startup."""
    global load_history
    today = datetime.now(EAT).strftime("%Y-%m-%d")
    try:
        # Summing load from primary inverters for history
        for sn in SERIAL_NUMBERS[:2]:
            r = requests.post(HISTORY_URL, data={"storage_sn": sn, "date": today, "start": 0}, headers=headers, timeout=15)
            data_points = r.json().get("data", {}).get("datas", [])
            for pt in data_points:
                ts = pt.get("time")
                if ts:
                    dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=EAT)
                    if dt > datetime.now(EAT) - timedelta(hours=12):
                        load_history.append((dt, float(pt.get("outPutPower", 0))))
        load_history.sort()
    except: pass

def poller():
    while True:
        fetch_system_data()
        time.sleep(POLL_INTERVAL * 60)

# ----------------------------
# 3. Web Interface
# ----------------------------

@app.route("/refresh")
def manual_refresh():
    fetch_system_data()
    return redirect(url_for('home'))

@app.route("/")
def home():
    if not latest_data: 
        return '<div style="text-align:center;padding:50px;"><h2>Waking up sensors...</h2><p>Wait 5 seconds and refresh.</p></div>'
    
    # Color logic
    p_color = "red" if latest_data['primary_min'] < 40 else "orange" if latest_data['primary_min'] < 50 else "green"
    b_color = "red" if latest_data['backup_v'] < 51.2 else "orange" if latest_data['backup_v'] < 52 else "green"
    
    # Weather Row Logic (Next 10 Hours)
    weather_rows = ""
    if weather_forecast:
        now_eat = datetime.now(EAT)
        count = 0
        for i, t_iso in enumerate(weather_forecast.get('time', [])):
            ft = datetime.fromisoformat(t_iso).replace(tzinfo=EAT)
            if ft >= now_eat.replace(minute=0) and count < 10:
                rad = weather_forecast['shortwave_radiation'][i]
                temp = weather_forecast['temperature_2m'][i]
                rad_color = '#eb3349' if rad < 150 and (8 <= ft.hour <= 17) else '#28a745'
                weather_rows += f"<tr><td>{ft.strftime('%H:%M')}</td><td>{temp}°C</td><td style='color:{rad_color}; font-weight:bold;'>{rad} W/m²</td></tr>"
                count += 1

    inv_cards = "".join([f"""
        <div class="inv-card {'backup' if i['Type'] == 'backup' else ''}">
            <h3>{i['Label']}</h3>
            <div class="stat"><span>Capacity</span><b>{i['Capacity']:.0f}%</b></div>
            <div class="stat"><span>Voltage</span><b>{i['vBat']:.1f}V</b></div>
            <div class="stat"><span>Output</span><b>{i['OutputPower']:.0f}W</b></div>
        </div>""" for i in latest_data['inverters']])

    return f"""
<!DOCTYPE html>
<html>
<head>
    <title>Tulia House Solar</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        * {{ margin:0; padding:0; box-sizing:border-box; }}
        body {{ font-family: 'Segoe UI', sans-serif; background: #121212; color: white; padding: 15px; }}
        .container {{ max-width: 1000px; margin: 0 auto; }}
        .header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }}
        .btn-refresh {{ padding: 10px 18px; background: #3a86ff; color: white; text-decoration: none; border-radius: 8px; font-size: 0.9em; }}
        .card {{ background: #1e1e1e; border-radius: 12px; padding: 20px; margin-bottom: 15px; box-shadow: 0 4px 15px rgba(0,0,0,0.3); }}
        .metrics {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-bottom: 20px; }}
        .m-item {{ padding: 15px; border-radius: 10px; text-align: center; }}
        .green {{ background: #2d6a4f; }} .orange {{ background: #9d4200; }} .red {{ background: #780000; }} .blue {{ background: #00509d; }}
        .grid {{ display: grid; grid-template-columns: 1.2fr 1fr; gap: 20px; }}
        @media (max-width: 600px) {{ .grid {{ grid-template-columns: 1fr; }} }}
        .inv-card {{ background: #252525; padding: 12px; border-radius: 8px; border-left: 4px solid #3a86ff; margin-bottom: 10px; }}
        .inv-card.backup {{ border-left-color: #fb8500; }}
        .stat {{ display: flex; justify-content: space-between; font-size: 0.9em; padding: 5px 0; border-bottom: 1px solid #333; }}
        table {{ width: 100%; border-collapse: collapse; font-size: 0.85em; }}
        td, th {{ padding: 10px; text-align: left; border-bottom: 1px solid #333; }}
        .chart-container {{ height: 200px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div><h1>TULIA SOLAR</h1><p style="color:#888;">{latest_data['timestamp']} EAT</p></div>
            <a href="/refresh" class="btn-refresh" onclick="this.innerHTML='Syncing...';">Manual Refresh</a>
        </div>
        
        <div class="metrics">
            <div class="m-item {p_color}"><h3>{latest_data['primary_min']:.0f}%</h3><small>Primary Bat</small></div>
            <div class="m-item {b_color}"><h3>{latest_data['backup_v']:.1f}V</h3><small>Backup Bat</small></div>
            <div class="m-item blue"><h3>{latest_data['total_load']:.0f}W</h3><small>Current Load</small></div>
            <div class="m-item {'orange' if latest_data['backup_active'] else 'green'}"><h3>{'ON' if latest_data['backup_active'] else 'OFF'}</h3><small>Backup</small></div>
        </div>

        <div class="grid">
            <div class="card"><h3>Inverters</h3>{inv_cards}</div>
            <div class="card">
                <h3>10h Forecast</h3>
                <table>
                    <thead><tr><th>Time</th><th>Temp</th><th>Sun</th></tr></thead>
                    <tbody>{weather_rows or "<tr><td colspan='3'>Awaiting forecast...</td></tr>"}</tbody>
                </table>
            </div>
        </div>

        <div class="card">
            <h3>Power Load (12h)</h3>
            <div class="chart-container"><canvas id="loadChart"></canvas></div>
        </div>
    </div>
    <script>
        new Chart(document.getElementById('loadChart'), {{
            type: 'line',
            data: {{
                labels: {str([t.strftime('%H:%M') for t, p in load_history])},
                datasets: [{{ 
                    label: 'Watts', 
                    data: {str([p for t, p in load_history])}, 
                    borderColor: '#3a86ff', 
                    backgroundColor: 'rgba(58,134,255,0.1)',
                    fill: true, tension: 0.4, pointRadius: 0 
                }}]
            }},
            options: {{ responsive: true, maintainAspectRatio: false }}
        }});
    </script>
</body>
</html>
"""

if __name__ == "__main__":
    load_initial_history()
    Thread(target=poller, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
