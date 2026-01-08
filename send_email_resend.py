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
    <title>Growatt Monitor</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }}
        .container {{ max-width: 1200px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        h1 {{ color: #333; }}
        .metric {{ display: inline-block; margin: 15px 20px; padding: 15px; border-radius: 5px; background: #f9f9f9; }}
        .metric.red {{ border-left: 4px solid red; }}
        .metric.green {{ border-left: 4px solid green; }}
        table {{ width: 100%; border-collapse: collapse; margin: 20px 0; }}
        th, td {{ padding: 10px; text-align: left; border-bottom: 1px solid #ddd; }}
        th {{ background: #333; color: white; }}
        .chart-container {{ margin: 30px 0; }}
        canvas {{ max-height: 400px; }}
        button {{ background: #007bff; color: white; border: none; padding: 10px 20px; border-radius: 5px; cursor: pointer; margin: 10px 0; }}
        button:hover {{ background: #0056b3; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>🔋 Growatt Monitor</h1>
        <p><strong>Last updated:</strong> {latest_data.get('timestamp', 'N/A')}</p>
        
        <div class="metric {load_color}">
            <strong>Total Output Power:</strong> {latest_data.get('total_output_power', 'N/A')} W
        </div>
        
        <div class="metric {battery_color}">
            <strong>Total Battery Discharge:</strong> {latest_data.get('total_battery_discharge_W', 'N/A')} W
        </div>
        
        <h2>Per Inverter Data</h2>
        <table>
            <tr>
                <th>SN</th>
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
"""
    
    # Auto-refresh every 5 minutes
    html += """
        <meta http-equiv="refresh" content="300">
"""
    
    # Merged Chart
    html += f"""
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
                            borderColor: 'rgb(75, 192, 192)',
                            backgroundColor: 'rgba(75, 192, 192, 0.1)',
                            borderWidth: 2,
                            tension: 0.1,
                            yAxisID: 'y'
                        }},
                        {{
                            label: 'Battery Discharge (W)',
                            data: {battery_values},
                            borderColor: 'rgb(255, 99, 132)',
                            backgroundColor: 'rgba(255, 99, 132, 0.1)',
                            borderWidth: 2,
                            tension: 0.1,
                            yAxisID: 'y'
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
                                text: 'Power (W)'
                            }}
                        }}
                    }},
                    plugins: {{
                        legend: {{
                            display: true,
                            position: 'top'
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
        
        <form method="POST" action="/test_alert">
            <button type="submit">Send Test Alert Email</button>
        </form>
    </div>
</body>
</html>
"""
    
    return render_template_string(html)

@app.route("/test_alert", methods=["POST"])
def test_alert():
    send_email(
        subject="🔔 Growatt Test Alert",
        html_content="<p>This is a test alert from your Growatt monitor.</p>"
    )
    return '<p>Test alert sent! ✅ <a href="/">Back</a></p>'

# ----------------------------
# Start background polling thread
# ----------------------------
Thread(target=poll_growatt, daemon=True).start()

# ----------------------------
# Run Flask
# ----------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
