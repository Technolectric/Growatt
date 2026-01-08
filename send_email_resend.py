import os
import time
import requests
from datetime import datetime, timedelta, timezone
from threading import Thread, Lock
from flask import Flask, redirect, url_for

# ----------------------------
# Flask app configuration
# ----------------------------
app = Flask(__name__)
data_lock = Lock() # Prevents issues if manual refresh and auto-poll happen at once

# ----------------------------
# Growatt Config
# ----------------------------
API_URL = "https://openapi.growatt.com/v1/device/storage/storage_last_data"
HISTORY_URL = "https://openapi.growatt.com/v1/device/storage/storage_history"

TOKEN = os.getenv("API_TOKEN")
SERIAL_NUMBERS = os.getenv("SERIAL_NUMBERS", "RKG3B0400T,KAM4N5W0AG,JNK1CDR0KQ").split(",")
POLL_INTERVAL_MINUTES = int(os.getenv("POLL_INTERVAL_MINUTES", 5))

# Inverter Mapping
INVERTER_CONFIG = {
    "RKG3B0400T": {"label": "Inverter 1", "type": "primary"},
    "KAM4N5W0AG": {"label": "Inverter 2", "type": "primary"},
    "JNK1CDR0KQ": {"label": "Inverter 3 (Backup)", "type": "backup"}
}

EAT = timezone(timedelta(hours=3))

# Globals
headers = {"token": TOKEN, "Content-Type": "application/x-www-form-urlencoded"}
latest_data = {}
load_history = [] 
weather_forecast = {}

# ----------------------------
# Logic Functions
# ----------------------------
def get_weather_forecast():
    try:
        url = f"https://api.open-meteo.com/v1/forecast?latitude=-1.85238&longitude=36.77683&hourly=shortwave_radiation,weather_code,temperature_2m&timezone=Africa/Nairobi&forecast_days=2"
        response = requests.get(url, timeout=10)
        return response.json().get('hourly', {})
    except: return {}

def fetch_all_data():
    """The core engine that pulls current data and updates the UI state."""
    global latest_data, weather_forecast, load_history
    with data_lock:
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

            temp_inverters.sort(key=lambda x: x['Label'])
            
            latest_data = {
                "timestamp": now.strftime("%H:%M:%S"),
                "total_load": total_load,
                "primary_min": min(temp_inverters[0]['Capacity'], temp_inverters[1]['Capacity']) if len(temp_inverters) > 1 else 0,
                "backup_v": temp_inverters[2]['vBat'] if len(temp_inverters) > 2 else 0,
                "backup_active": temp_inverters[2]['OutputPower'] > 50 if len(temp_inverters) > 2 else False,
                "inverters": temp_inverters
            }
            
            # Update history list
            load_history.append((now, total_load))
            load_history = [(t, p) for t, p in load_history if t >= now - timedelta(hours=12)]
            return True
        except Exception as e:
            print(f"Fetch Error: {e}")
            return False

def load_api_history():
    """Initial pre-fill of the chart so it isn't empty on restart."""
    global load_history
    print("Pre-loading historical chart data...")
    today_str = datetime.now(EAT).strftime("%Y-%m-%d")
    temp_history = {}
    try:
        for sn in SERIAL_NUMBERS[:2]:
            r = requests.post(HISTORY_URL, data={"storage_sn": sn, "date": today_str, "start": 0}, headers=headers, timeout=20)
            data = r.json().get("data", {}).get("datas", [])
            for entry in data:
                t_str = entry.get("time")
                if not t_str: continue
                dt = datetime.strptime(t_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=EAT)
                if dt < datetime.now(EAT) - timedelta(hours=12): continue
                temp_history[t_str] = temp_history.get(t_str, 0) + float(entry.get("outPutPower", 0))

        sorted_times = sorted(temp_history.keys())
        load_history = [(datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=EAT), temp_history[ts]) for ts in sorted_times]
    except: pass

def poll_loop():
    while True:
        fetch_all_data()
        time.sleep(POLL_INTERVAL_MINUTES * 60)

# ----------------------------
# Routes
# ----------------------------
@app.route("/refresh")
def refresh():
    fetch_all_data()
    return redirect(url_for('home'))

@app.route("/")
def home():
    if not latest_data: return '<div style="text-align:center;padding:50px;font-family:sans-serif;"><h2>Connecting to Inverters...</h2><p>Please wait 10 seconds and refresh.</p></div>'
    
    p_color = "red" if latest_data['primary_min'] < 40 else "orange" if latest_data['primary_min'] < 50 else "green"
    b_color = "red" if latest_data['backup_v'] < 51.2 else "orange" if latest_data['backup_v'] < 52 else "green"
    
    weather_rows = ""
    if weather_forecast:
        now = datetime.now(EAT)
        for i, t_str in enumerate(weather_forecast.get('time', [])):
            ft = datetime.fromisoformat(t_str).replace(tzinfo=EAT)
            if now <= ft <= now + timedelta(hours=8):
                rad = weather_forecast['shortwave_radiation'][i]
                rad_style = 'color: #dc3545;' if rad < 200 and (7 < ft.hour < 17) else 'color: #28a745;'
                weather_rows += f"<tr><td>{ft.strftime('%H:%M')}</td><td>{weather_forecast['temperature_2m'][i]}°C</td><td style='{rad_style}'>{rad} W/m²</td></tr>"

    inv_cards = ""
    for inv in latest_data['inverters']:
        is_b = inv['Type'] == 'backup'
        stat_val = f"{inv['Capacity']:.0f}%" if not is_b else ('GOOD' if inv['vBat']>=53 else 'MED' if inv['vBat']>=52 else 'LOW')
        inv_cards += f'<div class="inv-card {"backup" if is_b else ""}"><h3>{inv["Label"]}</h3><div class="stat"><span>Battery</span><b>{stat_val}</b></div><div class="stat"><span>Voltage</span><b>{inv["vBat"]:.1f}V</b></div><div class="stat"><span>Output</span><b>{inv["OutputPower"]:.0f}W</b></div></div>'

    return f"""
<!DOCTYPE html>
<html>
<head>
    <title>Tulia Solar</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        * {{ margin:0; padding:0; box-sizing:border-box; }}
        body {{ font-family: 'Segoe UI', sans-serif; background: linear-gradient(rgba(0,0,0,0.6), rgba(0,0,0,0.8)), url('https://images.unsplash.com/photo-1508514177221-188b1cf16e9d?w=1600') center/cover fixed; color: #333; padding: 20px; }}
        .container {{ max-width: 1200px; margin: 0 auto; }}
        .header {{ display: flex; justify-content: space-between; align-items: center; color: white; margin-bottom: 20px; }}
        .btn-refresh {{ padding: 10px 20px; background: #667eea; color: white; text-decoration: none; border-radius: 8px; font-weight: bold; transition: 0.3s; }}
        .btn-refresh:hover {{ background: #764ba2; transform: scale(1.05); }}
        .card {{ background: rgba(255,255,255,0.95); border-radius: 15px; padding: 20px; box-shadow: 0 10px 30px rgba(0,0,0,0.5); margin-bottom: 20px; }}
        .metrics {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-bottom: 20px; }}
        .m-item {{ padding: 20px; border-radius: 12px; color: white; text-align: center; }}
        .green {{ background: linear-gradient(135deg, #11998e, #38ef7d); }}
        .orange {{ background: linear-gradient(135deg, #f77f00, #fcbf49); }}
        .red {{ background: linear-gradient(135deg, #eb3349, #f45c43); }}
        .blue {{ background: linear-gradient(135deg, #667eea, #764ba2); }}
        .grid-main {{ display: grid; grid-template-columns: 2fr 1fr; gap: 20px; }}
        .inv-card {{ background: white; padding: 15px; border-radius: 10px; border-left: 5px solid #667eea; margin-bottom: 10px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }}
        .inv-card.backup {{ border-left-color: #f77f00; }}
        .stat {{ display: flex; justify-content: space-between; padding: 5px 0; border-bottom: 1px solid #eee; }}
        table {{ width: 100%; border-collapse: collapse; }}
        td, th {{ padding: 10px; border-bottom: 1px solid #eee; font-size: 0.9em; }}
        .chart-box {{ height: 250px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div><h1>TULIA HOUSE</h1><p>System Updated: {latest_data['timestamp']}</p></div>
            <a href="/refresh" class="btn-refresh" onclick="this.innerHTML='Updating...';">Refresh Data</a>
        </div>

        <div class="card">
            <div class="metrics">
                <div class="m-item {p_color}"><h2>{latest_data['primary_min']:.0f}%</h2><p>Primary Battery</p></div>
                <div class="m-item {b_color}"><h2>{latest_data['backup_v']:.1f}V</h2><p>Backup Battery</p></div>
                <div class="m-item blue"><h2>{latest_data['total_load']:.0f}W</h2><p>Current Load</p></div>
                <div class="m-item {'orange' if latest_data['backup_active'] else 'green'}"><h2>{'ACTIVE' if latest_data['backup_active'] else 'Standby'}</h2><p>Backup Status</p></div>
            </div>

            <div class="grid-main">
                <div><h3>Inverter Status</h3>{inv_cards}</div>
                <div><h3>Solar Forecast</h3><table>{weather_rows}</table></div>
            </div>
        </div>

        <div class="card">
            <h3>Power Usage (Last 12h)</h3>
            <div class="chart-box"><canvas id="loadChart"></canvas></div>
        </div>
    </div>
    <script>
        new Chart(document.getElementById('loadChart'), {{
            type: 'line',
            data: {{
                labels: {str([t.strftime('%H:%M') for t, p in load_history])},
                datasets: [{{ label: 'Watts', data: {str([p for t, p in load_history])}, borderColor: '#667eea', backgroundColor: 'rgba(102,126,234,0.1)', fill: true, tension: 0.4, pointRadius: 3 }}]
            }},
            options: {{ responsive: true, maintainAspectRatio: false }}
        }});
    </script>
</body>
</html>
"""

if __name__ == "__main__":
    load_api_history()
    Thread(target=poll_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
