import os
import time
import requests
from datetime import datetime, timedelta, timezone
from threading import Thread, Lock
from flask import Flask

# ----------------------------
# 1. Initialization & Config
# ----------------------------
app = Flask(__name__)
data_lock = Lock()

# Growatt API Endpoints
API_URL = "https://openapi.growatt.com/v1/device/storage/storage_last_data"
HISTORY_URL = "https://openapi.growatt.com/v1/device/storage/storage_history"

# Config from Environment Variables
TOKEN = os.getenv("API_TOKEN")
SERIAL_NUMBERS = os.getenv("SERIAL_NUMBERS", "RKG3B0400T,KAM4N5W0AG,JNK1CDR0KQ").split(",")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_MINUTES", 5))
EAT = timezone(timedelta(hours=3))

# Inverter Mapping (Matches your specific hardware)
INVERTER_CONFIG = {
    "RKG3B0400T": {"label": "Inverter 1", "type": "primary"},
    "KAM4N5W0AG": {"label": "Inverter 2", "type": "primary"},
    "JNK1CDR0KQ": {"label": "Inverter 3 (Backup)", "type": "backup"}
}

# Global Data Store
latest_data = {}
history_log = []  # List of tuples: (datetime, load, discharge)
weather_forecast = {}
headers = {"token": TOKEN, "Content-Type": "application/x-www-form-urlencoded"}

# ----------------------------
# 2. Data Fetching Engines
# ----------------------------

def fetch_current_status():
    """Polls real-time data and updates the global state."""
    global latest_data, weather_forecast, history_log
    with data_lock:
        try:
            # 1. Weather Update (Nairobi Coordinates)
            w_url = "https://api.open-meteo.com/v1/forecast?latitude=-1.8524&longitude=36.7768&hourly=shortwave_radiation,temperature_2m&timezone=Africa/Nairobi&forecast_days=1"
            weather_forecast = requests.get(w_url, timeout=10).json().get('hourly', {})

            # 2. Real-time Growatt Update
            temp_invs = []
            total_load = 0
            total_discharge = 0
            now = datetime.now(EAT)

            for sn in SERIAL_NUMBERS:
                r = requests.post(API_URL, data={"storage_sn": sn}, headers=headers, timeout=15).json().get("data", {})
                cfg = INVERTER_CONFIG.get(sn, {"label": sn, "type": "unknown"})
                
                inv_obj = {
                    "Label": cfg['label'], "SN": sn, "Type": cfg['type'],
                    "SOC": float(r.get("capacity") or 0),
                    "vBat": float(r.get("vBat") or 0),
                    "Load": float(r.get("outPutPower") or 0),
                    "Discharge": float(r.get("pDischarge") or 0),
                    "Status": r.get("statusText", "Offline")
                }
                temp_invs.append(inv_obj)
                total_load += inv_obj["Load"]
                total_discharge += inv_obj["Discharge"]

            temp_invs.sort(key=lambda x: x['Label'])
            latest_data = {
                "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
                "total_load": total_load,
                "total_discharge": total_discharge,
                "soc": min(temp_invs[0]['SOC'], temp_invs[1]['SOC']) if len(temp_invs) > 1 else 0,
                "backup_v": temp_invs[2]['vBat'] if len(temp_invs) > 2 else 0,
                "backup_active": temp_invs[2]['Load'] > 50 if len(temp_invs) > 2 else False,
                "inverters": temp_invs
            }
            
            # Append new point to history and trim to 12 hours
            history_log.append((now, total_load, total_discharge))
            history_log = [(t, l, d) for t, l, d in history_log if t >= now - timedelta(hours=12)]
        except Exception as e:
            print(f"Polling Error: {e}")

def load_storage_history():
    """Queries historical records to pre-fill the chart on startup."""
    global history_log
    now = datetime.now(EAT)
    today_str = now.strftime("%Y-%m-%d")
    temp_map = {}
    
    print("Pre-loading 12-hour storage history...")
    try:
        # Query primary inverters for load and discharge history
        for sn in SERIAL_NUMBERS[:2]:
            r = requests.post(HISTORY_URL, data={"storage_sn": sn, "date": today_str}, headers=headers, timeout=20).json()
            data_points = r.get("data", {}).get("datas", [])
            
            for pt in data_points:
                t_str = pt.get("time")
                if not t_str: continue
                
                dt = datetime.strptime(t_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=EAT)
                if dt > now - timedelta(hours=12):
                    entry = temp_map.get(t_str, [dt, 0, 0])
                    entry[1] += float(pt.get("outPutPower") or 0)
                    entry[2] += float(pt.get("pDischarge") or 0)
                    temp_map[t_str] = entry
        
        with data_lock:
            history_log = sorted(temp_map.values())
        print(f"Loaded {len(history_log)} historical data points.")
    except Exception as e:
        print(f"Historical Load Error: {e}")

def poller():
    while True:
        fetch_current_status()
        time.sleep(POLL_INTERVAL * 60)

# ----------------------------
# 3. Glassmorphism UI
# ----------------------------

@app.route("/")
def home():
    if not latest_data:
        return '<div style="background:#111;color:white;text-align:center;padding:100px;height:100vh;font-family:sans-serif;"><h2>Connecting to Tulia House Sensors...</h2></div>'
    
    p_color = "red" if latest_data['soc'] < 40 else "orange" if latest_data['soc'] < 50 else "green"
    b_color = "red" if latest_data['backup_v'] < 51.2 else "orange" if latest_data['backup_v'] < 52 else "green"
    
    # Weather Forecast Generation
    w_rows = ""
    if weather_forecast and 'time' in weather_forecast:
        now_h = datetime.now(EAT).hour
        count = 0
        for i, t_iso in enumerate(weather_forecast['time']):
            f_h = int(t_iso.split('T')[1].split(':')[0])
            if f_h >= now_h and count < 8:
                rad = weather_forecast['shortwave_radiation'][i]
                w_rows += f"<tr><td>{f_h}:00</td><td>{weather_forecast['temperature_2m'][i]}°C</td><td>{rad} W/m²</td></tr>"
                count += 1

    inv_html = "".join([f"""
        <div class="inv-card {'backup' if inv['Type'] == 'backup' else ''}">
            <h3>{inv['Label']}</h3>
            <div class="stat-line"><span>Battery SOC</span><b>{inv['SOC']:.0f}%</b></div>
            <div class="stat-line"><span>Voltage</span><b>{inv['vBat']:.1f}V</b></div>
            <div class="stat-line"><span>Status</span><b>{inv['Status']}</b></div>
        </div>""" for inv in latest_data['inverters']])

    return f"""
<!DOCTYPE html>
<html>
<head>
    <title>Tulia House Solar</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ 
            font-family: 'Segoe UI', sans-serif;
            background: linear-gradient(rgba(0,0,0,0.7), rgba(0,0,0,0.8)), url('https://images.unsplash.com/photo-1508514177221-188b1cf16e9d?w=1600') center/cover fixed;
            min-height: 100vh; padding: 20px; color: #333;
        }}
        .container {{ max-width: 1200px; margin: 0 auto; }}
        .header {{ text-align: center; color: white; margin-bottom: 30px; text-shadow: 2px 2px 4px rgba(0,0,0,0.5); }}
        .glass-card {{
            background: rgba(255, 255, 255, 0.9); backdrop-filter: blur(15px);
            border-radius: 20px; padding: 25px; box-shadow: 0 8px 32px rgba(0,0,0,0.3);
            margin-bottom: 20px;
        }}
        .metrics {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-bottom: 25px; }}
        .metric {{ padding: 20px; border-radius: 15px; color: white; text-align: center; }}
        .green {{ background: linear-gradient(135deg, #11998e, #38ef7d); }}
        .orange {{ background: linear-gradient(135deg, #f77f00, #fcbf49); }}
        .red {{ background: linear-gradient(135deg, #eb3349, #f45c43); }}
        .blue {{ background: linear-gradient(135deg, #667eea, #764ba2); }}
        .layout {{ display: grid; grid-template-columns: 2fr 1fr; gap: 20px; }}
        @media (max-width: 800px) {{ .layout {{ grid-template-columns: 1fr; }} }}
        .inv-card {{ background: white; padding: 20px; border-radius: 15px; border-left: 6px solid #667eea; margin-bottom: 15px; }}
        .inv-card.backup {{ border-left-color: #f77f00; }}
        .stat-line {{ display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #eee; }}
        table {{ width: 100%; border-collapse: collapse; }}
        th, td {{ padding: 12px; border-bottom: 1px solid #eee; text-align: left; }}
        .chart-box {{ height: 300px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header"><h1>TULIA HOUSE SOLAR</h1><p>Real-Time Energy Analytics</p></div>

        <div class="glass-card">
            <div class="metrics">
                <div class="metric {p_color}"><h2>{latest_data['soc']:.0f}%</h2><p>Primary Battery</p></div>
                <div class="metric {b_color}"><h2>{latest_data['backup_v']:.1f}V</h2><p>Backup Battery</p></div>
                <div class="metric blue"><h2>{latest_data['total_load']:.0f}W</h2><p>House Load</p></div>
                <div class="metric {'orange' if latest_data['backup_active'] else 'green'}"><h2>{'ACTIVE' if latest_data['backup_active'] else 'Standby'}</h2><p>Backup Status</p></div>
            </div>

            <div class="layout">
                <div><h3>Inverter Health</h3><div style="margin-top:15px;">{inv_html}</div></div>
                <div>
                    <h3>Solar Forecast</h3>
                    <div style="background:white; border-radius:15px; padding:10px; margin-top:10px;">
                        <table>
                            <thead><tr><th>Time</th><th>Temp</th><th>Solar</th></tr></thead>
                            <tbody>{w_rows or "<tr><td colspan='3'>Loading forecast...</td></tr>"}</tbody>
                        </table>
                    </div>
                </div>
            </div>
        </div>

        <div class="glass-card">
            <h3>12-Hour Energy & Storage Profile</h3>
            <div class="chart-box"><canvas id="mainChart"></canvas></div>
        </div>
        <p style="text-align:center; color:white; font-size:0.8em; opacity:0.7;">Last Sync: {latest_data['ts']} EAT</p>
    </div>

    <script>
        new Chart(document.getElementById('mainChart'), {{
            type: 'line',
            data: {{
                labels: {str([t.strftime('%H:%M') for t, l, d in history_log])},
                datasets: [
                    {{ 
                        label: 'House Load (W)', 
                        data: {str([l for t, l, d in history_log])}, 
                        borderColor: '#3a86ff', 
                        backgroundColor: 'rgba(58,134,255,0.1)', 
                        fill: true, tension: 0.4, pointRadius: 0 
                    }},
                    {{ 
                        label: 'Battery Discharge (W)', 
                        data: {str([d for t, l, d in history_log])}, 
                        borderColor: '#eb3349', 
                        borderDash: [5, 5],
                        fill: false, tension: 0.4, pointRadius: 0 
                    }}
                ]
            }},
            options: {{ 
                responsive: true, maintainAspectRatio: false,
                scales: {{ y: {{ beginAtZero: true }} }},
                plugins: {{ legend: {{ position: 'bottom' }} }}
            }}
        }});
    </script>
</body>
</html>
"""

if __name__ == "__main__":
    # Load historical storage data first
    load_storage_history()
    # Start background synchronization
    Thread(target=poller, daemon=True).start()
    # Start Flask
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
