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
LOAD_THRESHOLD_WATTS = int(os.getenv("LOAD_THRESHOLD_WATTS", 1000))
BATTERY_DISCHARGE_THRESHOLD_W = int(os.getenv("BATTERY_DISCHARGE_THRESHOLD_W", 1000))  # default 1000 W
HISTORY_HOURS = 12  # 12-hour history

# ----------------------------
# Email (Resend) Config
# ----------------------------
RESEND_API_KEY = os.getenv('RESEND_API_KEY')
SENDER_EMAIL = os.getenv('SENDER_EMAIL')
RECIPIENT_EMAIL = os.getenv('RECIPIENT_EMAIL')

# ----------------------------
# Globals
# ----------------------------
headers = {"token": TOKEN, "Content-Type": "application/x-www-form-urlencoded"}
last_alert_time = None
latest_data = {}
load_history = []  # (timestamp, total_output_power)
battery_history = []  # (timestamp, total_battery_discharge_W)

# East African Timezone
EAT = timezone(timedelta(hours=3))

# ----------------------------
# Email function
# ----------------------------
def send_email(subject, html_content):
    global last_alert_time
    if not all([RESEND_API_KEY, SENDER_EMAIL, RECIPIENT_EMAIL]):
        print("✗ Error: Missing email credentials in env")
        return False
    
    # Rate limit: 1 email/hour
    if last_alert_time and datetime.now(EAT) - last_alert_time < timedelta(minutes=60):
        print("⚠️ Alert cooldown active, skipping email")
        return False
    
    email_data = {
        "from": SENDER_EMAIL,
        "to": [RECIPIENT_EMAIL],
        "subject": subject,
        "html": html_content
    }
    
    try:
        response = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json"
            },
            json=email_data
        )
        if response.status_code == 200:
            print(f"✓ Email sent: {subject}")
            last_alert_time = datetime.now(EAT)
            return True
        else:
            print(f"✗ Email failed {response.status_code}: {response.text}")
            return False
    except Exception as e:
        print(f"✗ Error sending email: {e}")
        return False

# ----------------------------
# Can send alert helper
# ----------------------------
def can_send_alert():
    global last_alert_time
    if last_alert_time is None:
        return True
    return datetime.now(EAT) - last_alert_time > timedelta(minutes=60)

# ----------------------------
# Growatt Polling Loop
# ----------------------------
def poll_growatt():
    global latest_data, load_history, battery_history
    
    while True:
        try:
            total_output_power = 0
            total_battery_discharge_W = 0
            inverter_data = []
            now = datetime.now(EAT)
            
            for sn in SERIAL_NUMBERS:
                response = requests.post(
                    API_URL,
                    data={"storage_sn": sn},
                    headers=headers,
                    timeout=20
                )
                response.raise_for_status()
                data = response.json().get("data", {})
                
                # Total output power for charts
                out_power = float(data.get("outPutPower") or 0)
                total_output_power += out_power
                
                # Total battery discharge using pBat (actual battery power)
                if "pBat" in data and data["pBat"]:
                    bat_power = float(data["pBat"])
                    # Only count if battery is discharging (positive value means discharge)
                    if bat_power > 0:
                        total_battery_discharge_W += bat_power
                
                inverter_data.append({
                    "SN": sn,
                    "OutputPower": out_power,
                    "pBat": data.get("pBat", 0)
                })
            
            # Save latest readings
            latest_data = {
                "timestamp": now.strftime("%Y-%m-%d %H:%M:%S EAT"),
                "total_output_power": total_output_power,
                "total_battery_discharge_W": total_battery_discharge_W,
                "inverters": inverter_data
            }
            
            # Append to history (12 hours)
            load_history.append((now, total_output_power))
            load_history = [(t, p) for t, p in load_history if t >= now - timedelta(hours=HISTORY_HOURS)]
            
            battery_history.append((now, total_battery_discharge_W))
            battery_history = [(t, p) for t, p in battery_history if t >= now - timedelta(hours=HISTORY_HOURS)]
            
            print(f"{latest_data['timestamp']} | Load={total_output_power}W | Battery Discharge={total_battery_discharge_W}W")
            
            # --- Load alert ---
            if total_output_power >= LOAD_THRESHOLD_WATTS:
                if can_send_alert():
                    send_email(
                        subject="⚠️ Growatt Alert: High Load",
                        html_content=f"<p>Total Load has been above {LOAD_THRESHOLD_WATTS}W. Current: {total_output_power} W</p>"
                    )
            
            # --- Battery discharge alert ---
            if total_battery_discharge_W >= BATTERY_DISCHARGE_THRESHOLD_W:
                if can_send_alert():
                    send_email(
                        subject="⚠️ Growatt Alert: High Battery Discharge",
                        html_content=f"<p>Total battery discharge is {total_battery_discharge_W:.0f} W, exceeding the threshold of {BATTERY_DISCHARGE_THRESHOLD_W} W.</p>"
                    )
        
        except Exception as e:
            print(f"❌ Error polling Growatt: {e}")
        
        time.sleep(POLL_INTERVAL_MINUTES * 60)

# ----------------------------
# Flask Web Routes
# ----------------------------
@app.route("/")
def home():
    load_color = "red" if latest_data.get("total_output_power", 0) >= LOAD_THRESHOLD_WATTS else "green"
    battery_color = "red" if latest_data.get("total_battery_discharge_W", 0) >= BATTERY_DISCHARGE_THRESHOLD_W else "green"
    
    # Prepare data for merged chart
    times = [t.strftime('%H:%M') for t, p in load_history]
    load_values = [p for t, p in load_history]
    battery_values = [p for t, p in battery_history]
    
    html = f"""
<!DOCTYPE html>
<html>
<head>
    <title>Tulia House - Solar Monitor</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        
        body {{ 
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(rgba(0, 0, 0, 0.4), rgba(0, 0, 0, 0.6)), 
                        url('https://images.unsplash.com/photo-1582268611958-ebfd161ef9cf?w=1600&q=80') center/cover fixed;
            min-height: 100vh;
            padding: 20px;
        }}
        
        .header {{
            text-align: center;
            color: white;
            margin-bottom: 30px;
            text-shadow: 2px 2px 4px rgba(0,0,0,0.7);
        }}
        
        .header h1 {{
            font-size: 2.5em;
            margin-bottom: 5px;
            font-weight: 300;
            letter-spacing: 2px;
        }}
        
        .header .subtitle {{
            font-size: 1.1em;
            opacity: 0.9;
            font-weight: 300;
        }}
        
        .header .location {{
            font-size: 0.9em;
            opacity: 0.8;
            margin-top: 5px;
        }}
        
        .container {{ 
            max-width: 1200px; 
            margin: 0 auto;
        }}
        
        .card {{
            background: rgba(255, 255, 255, 0.95);
            backdrop-filter: blur(10px);
            padding: 25px;
            border-radius: 15px;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
            margin-bottom: 20px;
        }}
        
        .timestamp {{
            text-align: center;
            color: #666;
            font-size: 0.95em;
            margin-bottom: 20px;
        }}
        
        .metrics {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }}
        
        .metric {{ 
            padding: 20px;
            border-radius: 10px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            box-shadow: 0 4px 15px rgba(0, 0, 0, 0.2);
            transition: transform 0.2s;
        }}
        
        .metric:hover {{
            transform: translateY(-5px);
        }}
        
        .metric.green {{ 
            background: linear-gradient(135deg, #11998e 0%, #38ef7d 100%);
        }}
        
        .metric.red {{ 
            background: linear-gradient(135deg, #eb3349 0%, #f45c43 100%);
        }}
        
        .metric-label {{
            font-size: 0.9em;
            opacity: 0.9;
            margin-bottom: 8px;
        }}
        
        .metric-value {{
            font-size: 2em;
            font-weight: bold;
        }}
        
        h2 {{ 
            color: #333;
            margin-bottom: 20px;
            font-size: 1.5em;
            font-weight: 500;
        }}
        
        table {{ 
            width: 100%;
            border-collapse: collapse;
            margin: 20px 0;
            background: white;
            border-radius: 8px;
            overflow: hidden;
        }}
        
        th, td {{ 
            padding: 15px;
            text-align: left;
            border-bottom: 1px solid #eee;
        }}
        
        th {{ 
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            font-weight: 500;
        }}
        
        tr:hover {{
            background: #f8f9fa;
        }}
        
        .chart-container {{ 
            margin: 30px 0;
            background: white;
            padding: 20px;
            border-radius: 10px;
        }}
        
        canvas {{ 
            max-height: 400px;
        }}
        
        button {{ 
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            padding: 12px 30px;
            border-radius: 25px;
            cursor: pointer;
            font-size: 1em;
            font-weight: 500;
            box-shadow: 0 4px 15px rgba(102, 126, 234, 0.4);
            transition: all 0.3s;
        }}
        
        button:hover {{ 
            transform: translateY(-2px);
            box-shadow: 0 6px 20px rgba(102, 126, 234, 0.6);
        }}
        
        .footer {{
            text-align: center;
            color: white;
            margin-top: 30px;
            font-size: 0.9em;
            text-shadow: 1px 1px 3px rgba(0,0,0,0.5);
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>TULIA HOUSE</h1>
            <div class="subtitle">Solar Energy Monitoring System</div>
            <div class="location">📍 Champagne Ridge, Kajiado • Rift Valley Views</div>
        </div>
        
        <div class="card">
            <div class="timestamp">
                <strong>Last Updated:</strong> {latest_data.get('timestamp', 'N/A')}
            </div>
            
            <div class="metrics">
                <div class="metric {load_color}">
                    <div class="metric-label">Total Output Power</div>
                    <div class="metric-value">{latest_data.get('total_output_power', 'N/A')} W</div>
                </div>
                
                <div class="metric {battery_color}">
                    <div class="metric-label">Battery Discharge</div>
                    <div class="metric-value">{latest_data.get('total_battery_discharge_W', 'N/A')} W</div>
                </div>
            </div>
            
            <h2>Inverter Status</h2>
            <table>
                <tr>
                    <th>Serial Number</th>
                    <th>Output Power (W)</th>
                    <th>Battery Power (W)</th>
                </tr>
"""
    
    for inv in latest_data.get("inverters", []):
        html += f"""
                <tr>
                    <td>{inv['SN']}</td>
                    <td>{inv['OutputPower']}</td>
                    <td>{inv['pBat']}</td>
                </tr>
"""
    
    html += """
            </table>
        </div>
"""
    
    # Auto-refresh every 5 minutes
    html += """
        <meta http-equiv="refresh" content="300">
"""
    
    # Merged Chart
    html += f"""
        <div class="card">
            <div class="chart-container">
                <h2>Power Monitoring - Last 12 Hours</h2>
                <canvas id="mergedChart"></canvas>
            </div>
            
            <script>
                const ctx = document.getElementById('mergedChart').getContext('2d');
                new Chart(ctx, {{
                    type: 'line',
                    data: {{
                        labels: {times},
                        datasets: [
                            {{
                                label: 'Total Load (W)',
                                data: {load_values},
                                borderColor: 'rgb(17, 153, 142)',
                                backgroundColor: 'rgba(17, 153, 142, 0.1)',
                                borderWidth: 3,
                                tension: 0.4,
                                yAxisID: 'y',
                                fill: true
                            }},
                            {{
                                label: 'Battery Discharge (W)',
                                data: {battery_values},
                                borderColor: 'rgb(235, 51, 73)',
                                backgroundColor: 'rgba(235, 51, 73, 0.1)',
                                borderWidth: 3,
                                tension: 0.4,
                                yAxisID: 'y',
                                fill: true
                            }}
                        ]
                    }},
                    options: {{
                        responsive: true,
                        maintainAspectRatio: true,
                        interaction: {{
                            mode: 'index',
                            intersect: false
                        }},
                        scales: {{
                            y: {{
                                type: 'linear',
                                display: true,
                                position: 'left',
                                title: {{
                                    display: true,
                                    text: 'Power (W)',
                                    font: {{
                                        size: 14,
                                        weight: 'bold'
                                    }}
                                }},
                                grid: {{
                                    color: 'rgba(0, 0, 0, 0.1)'
                                }}
                            }},
                            x: {{
                                grid: {{
                                    color: 'rgba(0, 0, 0, 0.05)'
                                }}
                            }}
                        }},
                        plugins: {{
                            legend: {{
                                display: true,
                                position: 'top',
                                labels: {{
                                    font: {{
                                        size: 13
                                    }},
                                    padding: 15
                                }}
                            }},
                            tooltip: {{
                                callbacks: {{
                                    label: function(context) {{
                                        return context.dataset.label + ': ' + context.parsed.y.toFixed(0) + ' W';
                                    }}
                                }}
                            }}
                        }}
                    }}
                }});
            </script>
            
            <form method="POST" action="/test_alert" style="text-align: center; margin-top: 30px;">
                <button type="submit">🔔 Send Test Alert</button>
            </form>
        </div>
        
        <div class="footer">
            Powered by Growatt Solar System • Managed by YourHost
        </div>
    </div>
</body>
</html>
"""
    
    return render_template_string(html)

@app.route("/test_alert", methods=["POST"])
def test_alert():
    send_email(
        subject="🔔 Tulia House Solar Alert Test",
        html_content="<p>This is a test alert from Tulia House solar monitoring system in Champagne Ridge, Kajiado.</p>"
    )
    return '<html><body style="font-family: Arial; text-align: center; padding: 50px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white;"><h1>✅ Test Alert Sent!</h1><p><a href="/" style="color: white; text-decoration: underline;">← Back to Dashboard</a></p></body></html>'

# ----------------------------
# Start background polling thread
# ----------------------------
Thread(target=poll_growatt, daemon=True).start()

# ----------------------------
# Run Flask
# ----------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
