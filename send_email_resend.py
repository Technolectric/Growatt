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

# Alert thresholds
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
load_history = []  # Restored for the chart
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
                    "Label": cfg['label'], "SN": sn, "Type": cfg['type'], "DataLog": cfg['datalog'],
                    "Capacity": float(d.get("capacity") or 0),
                    "vBat": float(d.get("vBat") or 0),
                    "pBat": float(d.get("pBat") or 0),
                    "OutputPower": float(d.get("outPutPower") or 0),
                    "Status": d.get("statusText", "Offline")
                }
                temp_inverters.append(inv_obj)
                total_load += inv_obj["OutputPower"]

            # Sort 1, 2, 3
            temp_inverters.sort(key=lambda x: x['Label'])
            
            # Extract main metrics (Safe access)
            inv1 = temp_inverters[0] if len(temp_inverters) > 0 else {}
            inv2 = temp_inverters[1] if len(temp_inverters) > 1 else {}
            inv3 = temp_inverters[2] if len(temp_inverters) > 2 else {}
            
            primary_cap = min(inv1.get('Capacity', 0), inv2.get('Capacity', 0))
            
            latest_data = {
                "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
                "total_load": total_load,
                "primary_min": primary_cap,
                "backup_v": inv3.get('vBat', 0),
                "backup_active": inv3.get('OutputPower', 0) > 50,
                "inverters": temp_inverters
            }

            # Update Chart History
            load_history.append((now, total_load))
            load_history = [(t, p) for t, p in load_history if t >= now - timedelta(hours=12)]

            # Alert Check
            if inv3:
                b_stat = "GOOD" if inv3['vBat'] >= 53 else "MEDIUM" if inv3['vBat'] >= 52 else "LOW"
                if inv3['vBat'] < GEN_START_VOLTAGE:
                    send_email("🚨 CRITICAL: Gen Starting", f"Backup vBat: {inv3['vBat']}V ({b_stat})", "critical")
                elif latest_data['backup_active'] and primary_cap < PRIMARY_BAT_THRESHOLD:
                    send_email("⚠️ Backup Active", f"Status: {b_stat} | Primary: {primary_cap}%", "backup")

        except Exception as e: 
            print(f"Poll Error: {e}")
            import traceback
            traceback.print_exc()
            
        time.sleep(POLL_INTERVAL_MINUTES * 60)

# ----------------------------
# UI
# ----------------------------
@app.route("/")
def home():
    if not latest_data: return '<div style="color:white; text-align:center; padding:50px; font-family:sans-serif;">System Starting... Please refresh in 1 minute.</div>'
    
    # 1. Colors
    p_color = "red" if latest_data['primary_min'] < 40 else "orange" if latest_data['primary_min'] < 50 else "green"
    b_color = "red" if latest_data['backup_v'] < 51.2 else "orange" if latest_data['backup_v'] < 52 else "green"
    
    # 2. Weather Rows
    weather_rows = ""
    if weather_forecast:
        now = datetime.now(EAT)
        for i, t_str in enumerate(weather_forecast.get('time', [])):
            ft = datetime.fromisoformat(t_str.replace('Z', '+00:00')).astimezone(EAT)
            if now <= ft <= now + timedelta(hours=10):
                rad = weather_forecast['shortwave_radiation'][i]
                # Highlight low solar radiation
                rad_style = 'color: #dc3545; font-weight: bold;' if rad < 200 else 'color: #28a745;'
                
                weather_rows += f"""
                <tr>
                    <td>{ft.strftime('%H:%M')}</td>
                    <td>{get_weather_desc(weather_forecast['weather_code'][i])}</td>
                    <td>{weather_forecast['temperature_2m'][i]}°C</td>
                    <td style="{rad_style}">{rad} W/m²</td>
                </tr>"""

    # 3. Inverter Cards
    inverter_html = ""
    for inv in latest_data['inverters']:
        is_backup = inv['Type'] == 'backup'
        if is_backup:
            v = inv['vBat']
            if v >= 53: b_disp = '<span style="color: #28a745; font-weight:bold;">GOOD</span>'
            elif v >= 52: b_disp = '<span style="color: #ffc107; font-weight:bold;">MEDIUM</span>'
            else: b_disp = '<span style="color: #dc3545; font-weight:bold;">LOW</span>'
        else:
            b_disp = f"<b>{inv['Capacity']:.0f}%</b>"

        inverter_html += f"""
                <div class="inverter-card {'backup' if is_backup else ''}">
                    <h3>{inv['Label']}</h3>
                    <div class="inv-label">SN: {inv['SN']}</div>
                    <div class="inv-stat"><span class="inv-stat-label">Battery</span> <span class="inv-stat-value">{b_disp}</span></div>
                    <div class="inv-stat"><span class="inv-stat-label">Voltage</span> <span class="inv-stat-value">{inv['vBat']:.1f}V</span></div>
                    <div class="inv-stat"><span class="inv-stat-label">Output</span> <span class="inv-stat-value">{inv['OutputPower']:.0f}W</span></div>
                    <div class="inv-stat"><span class="inv-stat-label">Status</span> <span class="inv-stat-value">{inv['Status']}</span></div>
                </div>"""

    return f"""
<!DOCTYPE html>
<html>
<head>
    <title>Tulia House - Solar Monitor</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ 
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(rgba(0, 0, 0, 0.5), rgba(0, 0, 0, 0.7)), 
                        url('https://images.unsplash.com/photo-1582268611958-ebfd161ef9cf?w=1600&q=80') center/cover fixed;
            min-height: 100vh;
            padding: 20px;
            color: #333;
        }}
        .container {{ max-width: 1400px; margin: 0 auto; }}
        
        /* Headers */
        .header {{ text-align: center; color: white; margin-bottom: 30px; text-shadow: 2px 2px 4px rgba(0,0,0,0.8); }}
        .header h1 {{ font-size: 2.5em; font-weight: 300; letter-spacing: 2px; margin-bottom: 5px; }}
        .header .subtitle {{ font-size: 1.1em; opacity: 0.9; }}
        
        /* Cards */
        .card {{
            background: rgba(255, 255, 255, 0.95);
            backdrop-filter: blur(10px);
            padding: 25px;
            border-radius: 15px;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
            margin-bottom: 20px;
        }}
        
        /* Metrics */
        .metrics-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-bottom: 30px; }}
        .metric {{ padding: 20px; border-radius: 12px; color: white; box-shadow: 0 4px 15px rgba(0, 0, 0, 0.2); text-align:center; transition: transform 0.2s; }}
        .metric:hover {{ transform: translateY(-5px); }}
        .metric-value {{ font-size: 1.8em; font-weight: bold; display:block; }}
        .metric-label {{ font-size: 0.85em; opacity: 0.9; display:block; margin-bottom:5px; }}
        .metric-subtext {{ font-size: 0.75em; opacity: 0.8; margin-top: 5px; }}
        
        .metric.green {{ background: linear-gradient(135deg, #11998e 0%, #38ef7d 100%); }}
        .metric.orange {{ background: linear-gradient(135deg, #f77f00 0%, #fcbf49 100%); }}
        .metric.red {{ background: linear-gradient(135deg, #eb3349 0%, #f45c43 100%); }}
        .metric.blue {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); }}
        
        /* Status Banner */
        .system-status {{
            background: {'linear-gradient(135deg, #eb3349 0%, #f45c43 100%)' if latest_data['backup_v'] < 51.2 else 'linear-gradient(135deg, #f77f00 0%, #fcbf49 100%)' if latest_data['backup_active'] else 'linear-gradient(135deg, #11998e 0%, #38ef7d 100%)'};
            color: white; padding: 20px; border-radius: 12px; margin-bottom: 20px; text-align: center;
            box-shadow: 0 4px 15px rgba(0,0,0,0.2);
        }}
        
        /* Inverter Grid */
        .inverter-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; margin-top: 20px; }}
        .inverter-card {{ background: white; padding: 20px; border-radius: 12px; border-left: 5px solid #667eea; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
        .inverter-card.backup {{ border-left-color: #f77f00; }}
        .inverter-card h3 {{ color: #333; margin-bottom: 5px; }}
        .inv-label {{ font-size: 0.8em; color: #777; margin-bottom: 15px; }}
        .inv-stat {{ display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #eee; }}
        
        /* Table */
        table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
        th, td {{ text-align: left; padding: 12px; border-bottom: 1px solid #eee; }}
        th {{ color: #666; font-size: 0.85em; text-transform:uppercase; letter-spacing:1px; }}
        tr:last-child td {{ border-bottom: none; }}
        
        /* Chart */
        .chart-container {{ position: relative; height: 300px; width: 100%; }}
        
        h2 {{ color: #444; margin-bottom: 15px; font-weight: 500; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>TULIA HOUSE</h1>
            <div class="subtitle">Solar Energy Monitor</div>
        </div>
        
        <div class="card">
            <div class="system-status">
                <h2>{'🚨 GENERATOR STARTING' if latest_data['backup_v'] < 51.2 else '⚠️ BACKUP ACTIVE' if latest_data['backup_active'] else '✓ SYSTEM NORMAL'}</h2>
                <div>{'Running on Generator/Backup' if latest_data['backup_active'] else 'Running on Solar/Primary Batteries'}</div>
            </div>

            <div class="metrics-grid">
                <div class="metric {p_color}">
                    <span class="metric-label">Primary Batteries</span>
                    <span class="metric-value">{latest_data['primary_min']:.0f}%</span>
                    <span class="metric-subtext">Inv 1 & 2 (Min)</span>
                </div>
                <div class="metric {b_color}">
                    <span class="metric-label">Backup Battery</span>
                    <span class="metric-value">{latest_data['backup_v']:.1f}V</span>
                    <span class="metric-subtext">Gen Start @ 51.2V</span>
                </div>
                <div class="metric blue">
                    <span class="metric-label">Total Load</span>
                    <span class="metric-value">{latest_data['total_load']:.0f}W</span>
                    <span class="metric-subtext">Current Usage</span>
                </div>
                <div class="metric {'orange' if latest_data['backup_active'] else 'green'}">
                    <span class="metric-label">Backup Status</span>
                    <span class="metric-value">{'ACTIVE' if latest_data['backup_active'] else 'Standby'}</span>
                </div>
            </div>
            
            <div class="inverter-grid" style="margin-top:0;">
                <div style="grid-column: span 2;"> <h2>Inverter Details</h2>
                     <div class="inverter-grid" style="margin-top:0;">
                        {inverter_html}
                     </div>
                </div>
                <div>
                    <h2>10-Hour Forecast</h2>
                    <div style="background:white; border-radius:12px; padding:15px; border:1px solid #eee;">
                        <table>
                            <thead><tr><th>Time</th><th>Sky</th><th>Temp</th><th>Solar</th></tr></thead>
                            <tbody>{weather_rows}</tbody>
                        </table>
                    </div>
                </div>
            </div>
        </div>

        <div class="card">
            <h2>Power Consumption (12 Hours)</h2>
            <div class="chart-container">
                <canvas id="loadChart"></canvas>
            </div>
        </div>
        
        <div style="text-align:center; color:white; margin-top:20px; font-size:0.9em; opacity:0.8;">
            Updated: {latest_data['timestamp']} | Poll Interval: {POLL_INTERVAL_MINUTES}m
        </div>
    </div>

    <script>
        const ctx = document.getElementById('loadChart').getContext('2d');
        new Chart(ctx, {{
            type: 'line',
            data: {{
                labels: {str([t.strftime('%H:%M') for t, p in load_history])},
                datasets: [{{
                    label: 'Load (Watts)',
                    data: {str([p for t, p in load_history])},
                    borderColor: '#667eea',
                    backgroundColor: 'rgba(102, 126, 234, 0.2)',
                    fill: true,
                    tension: 0.4,
                    pointRadius: 0
                }}]
            }},
            options: {{
                responsive: true,
                maintainAspectRatio: false,
                scales: {{ y: {{ beginAtZero: true, grid: {{ color: '#f0f0f0' }} }}, x: {{ grid: {{ display: false }} }} }}
            }}
        }});
    </script>
</body>
</html>
"""

# Health Check for Render (Keep Alive)
@app.route("/health")
def health():
    return "OK", 200

if __name__ == "__main__":
    Thread(target=poll_growatt, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
