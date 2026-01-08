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
BATTERY_DISCHARGE_THRESHOLD_W = int(os.getenv("BATTERY_DISCHARGE_THRESHOLD_W", 1000))
HISTORY_HOURS = 12

# ----------------------------
# Location Config (Kajiado, Kenya)
# ----------------------------
LATITUDE = -1.85238
LONGITUDE = 36.77683

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
load_history = []
battery_history = []
weather_forecast = {}
solar_forecast = {}

# East African Timezone
EAT = timezone(timedelta(hours=3))

# ----------------------------
# Weather & Solar Forecast Functions
# ----------------------------
def get_weather_forecast():
    """Get weather forecast from Open-Meteo (free, no API key)"""
    try:
        url = f"https://api.open-meteo.com/v1/forecast?latitude={LATITUDE}&longitude={LONGITUDE}&hourly=cloud_cover,cloud_cover_low,cloud_cover_mid,cloud_cover_high,shortwave_radiation,direct_radiation&timezone=Africa/Nairobi&forecast_days=2"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        # Parse forecast data
        forecast = {
            'times': data['hourly']['time'],
            'cloud_cover': data['hourly']['cloud_cover'],
            'solar_radiation': data['hourly']['shortwave_radiation'],
            'direct_radiation': data['hourly']['direct_radiation']
        }
        
        print(f"✓ Weather forecast updated: {len(forecast['times'])} hours")
        return forecast
    except Exception as e:
        print(f"✗ Error fetching weather forecast: {e}")
        return None

def analyze_solar_conditions(forecast, hours_ahead=10):
    """Analyze upcoming solar conditions for the next N hours"""
    if not forecast:
        return None
    
    try:
        now = datetime.now(EAT)
        future_time = now + timedelta(hours=hours_ahead)
        
        # Get next 10 hours of forecast
        avg_cloud_cover = 0
        avg_solar_radiation = 0
        count = 0
        
        for i, time_str in enumerate(forecast['times']):
            forecast_time = datetime.fromisoformat(time_str.replace('Z', '+00:00')).astimezone(EAT)
            if now <= forecast_time <= future_time:
                avg_cloud_cover += forecast['cloud_cover'][i]
                avg_solar_radiation += forecast['solar_radiation'][i]
                count += 1
        
        if count > 0:
            avg_cloud_cover /= count
            avg_solar_radiation /= count
            
            # Determine if conditions are poor (high clouds, low radiation)
            poor_conditions = avg_cloud_cover > 70 or avg_solar_radiation < 200
            
            return {
                'avg_cloud_cover': avg_cloud_cover,
                'avg_solar_radiation': avg_solar_radiation,
                'poor_conditions': poor_conditions,
                'hours_analyzed': count
            }
    except Exception as e:
        print(f"✗ Error analyzing solar conditions: {e}")
    
    return None

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

def can_send_alert():
    global last_alert_time
    if last_alert_time is None:
        return True
    return datetime.now(EAT) - last_alert_time > timedelta(minutes=60)

# ----------------------------
# Growatt Polling Loop
# ----------------------------
def poll_growatt():
    global latest_data, load_history, battery_history, weather_forecast
    
    # Update weather forecast every 30 minutes
    last_weather_update = None
    
    while True:
        try:
            # Update weather forecast every 30 minutes
            if last_weather_update is None or datetime.now(EAT) - last_weather_update > timedelta(minutes=30):
                weather_forecast = get_weather_forecast()
                last_weather_update = datetime.now(EAT)
            
            total_output_power = 0
            total_battery_discharge_W = 0
            total_battery_capacity = 0
            inverter_data = []
            now = datetime.now(EAT)
            
            battery_capacities = []
            
            for sn in SERIAL_NUMBERS:
                response = requests.post(
                    API_URL,
                    data={"storage_sn": sn},
                    headers=headers,
                    timeout=20
                )
                response.raise_for_status()
                data = response.json().get("data", {})
                
                # Total output power
                out_power = float(data.get("outPutPower") or 0)
                total_output_power += out_power
                
                # Total battery discharge using pBat
                if "pBat" in data and data["pBat"]:
                    bat_power = float(data["pBat"])
                    if bat_power > 0:
                        total_battery_discharge_W += bat_power
                
                # Battery capacity - collect from specific inverters only
                capacity = float(data.get("capacity") or 0)
                if sn in ["KAM4N5W0AG", "RKG3B0400T"]:
                    if capacity > 0:  # Only add valid readings
                        battery_capacities.append(capacity)
                
                inverter_data.append({
                    "SN": sn,
                    "OutputPower": out_power,
                    "pBat": data.get("pBat", 0),
                    "Capacity": capacity
                })
            
            # Use minimum battery capacity from the specified inverters
            total_battery_capacity = min(battery_capacities) if battery_capacities else 0
            
            # Save latest readings
            latest_data = {
                "timestamp": now.strftime("%Y-%m-%d %H:%M:%S EAT"),
                "total_output_power": total_output_power,
                "total_battery_discharge_W": total_battery_discharge_W,
                "battery_capacity": total_battery_capacity,
                "inverters": inverter_data
            }
            
            # Append to history
            load_history.append((now, total_output_power))
            load_history = [(t, p) for t, p in load_history if t >= now - timedelta(hours=HISTORY_HOURS)]
            
            battery_history.append((now, total_battery_discharge_W))
            battery_history = [(t, p) for t, p in battery_history if t >= now - timedelta(hours=HISTORY_HOURS)]
            
            print(f"{latest_data['timestamp']} | Load={total_output_power}W | Battery={total_battery_discharge_W}W | Capacity={total_battery_capacity}%")
            
            # --- Intelligent Alert Logic ---
            solar_conditions = analyze_solar_conditions(weather_forecast, hours_ahead=10)
            
            # Alert if high load + low battery + poor upcoming solar conditions
            if total_output_power >= LOAD_THRESHOLD_WATTS and total_battery_capacity < 50:
                if solar_conditions and solar_conditions['poor_conditions']:
                    if can_send_alert():
                        send_email(
                            subject="⚠️ URGENT: High Load + Low Battery + Poor Solar Forecast",
                            html_content=f"""
                            <h2>⚠️ Critical Power Alert - Tulia House</h2>
                            <p><strong>Current Status:</strong></p>
                            <ul>
                                <li>Load: {total_output_power} W (Threshold: {LOAD_THRESHOLD_WATTS} W)</li>
                                <li>Battery: {total_battery_capacity}% (Low!)</li>
                                <li>Battery Discharge: {total_battery_discharge_W} W</li>
                            </ul>
                            <p><strong>Weather Forecast (Next 10 Hours):</strong></p>
                            <ul>
                                <li>Cloud Cover: {solar_conditions['avg_cloud_cover']:.0f}% (High!)</li>
                                <li>Solar Radiation: {solar_conditions['avg_solar_radiation']:.0f} W/m²</li>
                            </ul>
                            <p><strong>⚠️ WARNING:</strong> Poor solar conditions expected. Generator may be needed soon. Please reduce power consumption!</p>
                            """
                        )
            
            # Standard high load alert
            elif total_output_power >= LOAD_THRESHOLD_WATTS:
                if can_send_alert():
                    alert_msg = f"<p>Total Load: {total_output_power} W (Threshold: {LOAD_THRESHOLD_WATTS} W)</p>"
                    alert_msg += f"<p>Battery: {total_battery_capacity}%</p>"
                    
                    if solar_conditions:
                        if solar_conditions['poor_conditions']:
                            alert_msg += f"<p>⚠️ Poor solar conditions ahead (Cloud: {solar_conditions['avg_cloud_cover']:.0f}%). Consider reducing usage.</p>"
                        else:
                            alert_msg += f"<p>✓ Good solar conditions expected (Cloud: {solar_conditions['avg_cloud_cover']:.0f}%).</p>"
                    
                    send_email(
                        subject="⚠️ Tulia House: High Load Alert",
                        html_content=alert_msg
                    )
            
            # Battery discharge alert
            if total_battery_discharge_W >= BATTERY_DISCHARGE_THRESHOLD_W:
                if can_send_alert():
                    send_email(
                        subject="⚠️ Tulia House: High Battery Discharge",
                        html_content=f"<p>Battery discharge: {total_battery_discharge_W:.0f} W (Threshold: {BATTERY_DISCHARGE_THRESHOLD_W} W)</p><p>Battery Level: {total_battery_capacity}%</p>"
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
    battery_capacity = latest_data.get("battery_capacity", 0)
    battery_capacity_color = "red" if battery_capacity < 30 else ("orange" if battery_capacity < 50 else "green")
    
    # Prepare chart data
    times = [t.strftime('%H:%M') for t, p in load_history]
    load_values = [p for t, p in load_history]
    battery_values = [p for t, p in battery_history]
    
    # Analyze solar conditions
    solar_conditions = analyze_solar_conditions(weather_forecast, hours_ahead=10)
    
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
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-bottom: 30px;
        }}
        
        .metric {{ 
            padding: 20px;
            border-radius: 10px;
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
        
        .metric.orange {{
            background: linear-gradient(135deg, #f77f00 0%, #fcbf49 100%);
        }}
        
        .metric.red {{ 
            background: linear-gradient(135deg, #eb3349 0%, #f45c43 100%);
        }}
        
        .metric-label {{
            font-size: 0.85em;
            opacity: 0.9;
            margin-bottom: 8px;
        }}
        
        .metric-value {{
            font-size: 1.8em;
            font-weight: bold;
        }}
        
        .weather-alert {{
            background: linear-gradient(135deg, #f77f00 0%, #fcbf49 100%);
            color: white;
            padding: 20px;
            border-radius: 10px;
            margin-bottom: 20px;
            box-shadow: 0 4px 15px rgba(247, 127, 0, 0.3);
        }}
        
        .weather-alert.good {{
            background: linear-gradient(135deg, #11998e 0%, #38ef7d 100%);
        }}
        
        .weather-alert h3 {{
            margin-bottom: 10px;
            font-size: 1.2em;
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
            <div class="subtitle">🔋 Solar Energy Monitoring System</div>
            <div class="location">📍 Champagne Ridge, Kajiado • Rift Valley Views</div>
        </div>
        
        <div class="card">
            <div class="timestamp">
                <strong>Last Updated:</strong> {latest_data.get('timestamp', 'N/A')}
            </div>
"""
    
    # Solar forecast alert
    if solar_conditions:
        if solar_conditions['poor_conditions']:
            html += f"""
            <div class="weather-alert">
                <h3>☁️ Weather Alert - Poor Solar Conditions Ahead</h3>
                <p><strong>Next 10 Hours Forecast:</strong></p>
                <p>• Cloud Cover: {solar_conditions['avg_cloud_cover']:.0f}% (High)</p>
                <p>• Solar Radiation: {solar_conditions['avg_solar_radiation']:.0f} W/m² (Low)</p>
                <p><strong>⚠️ Recommendation:</strong> Reduce power consumption to preserve battery. Generator may be needed.</p>
            </div>
"""
        else:
            html += f"""
            <div class="weather-alert good">
                <h3>☀️ Good Solar Conditions Expected</h3>
                <p><strong>Next 10 Hours Forecast:</strong></p>
                <p>• Cloud Cover: {solar_conditions['avg_cloud_cover']:.0f}%</p>
                <p>• Solar Radiation: {solar_conditions['avg_solar_radiation']:.0f} W/m²</p>
                <p>✓ Batteries should recharge well</p>
            </div>
"""
    
    html += f"""
            <div class="metrics">
                <div class="metric {load_color}">
                    <div class="metric-label">Total Load</div>
                    <div class="metric-value">{latest_data.get('total_output_power', 'N/A')} W</div>
                </div>
                
                <div class="metric {battery_color}">
                    <div class="metric-label">Battery Discharge</div>
                    <div class="metric-value">{latest_data.get('total_battery_discharge_W', 'N/A')} W</div>
                </div>
                
                <div class="metric {battery_capacity_color}">
                    <div class="metric-label">Battery Level</div>
                    <div class="metric-value">{battery_capacity:.0f}%</div>
                </div>
            </div>
            
            <h2>Inverter Status</h2>
            <table>
                <tr>
                    <th>Serial Number</th>
                    <th>Output (W)</th>
                    <th>Battery (W)</th>
                    <th>Capacity (%)</th>
                </tr>
"""
    
    for inv in latest_data.get("inverters", []):
        html += f"""
                <tr>
                    <td>{inv['SN']}</td>
                    <td>{inv['OutputPower']}</td>
                    <td>{inv['pBat']}</td>
                    <td>{inv['Capacity']}</td>
                </tr>
"""
    
    html += """
            </table>
        </div>
        
        <meta http-equiv="refresh" content="300">
        
        <div class="card">
            <div class="chart-container">
                <h2>Power Monitoring - Last 12 Hours</h2>
                <canvas id="mergedChart"></canvas>
            </div>
            
            <script>
                const ctx = document.getElementById('mergedChart').getContext('2d');
                new Chart(ctx, {
                    type: 'line',
                    data: {
                        labels: """ + str(times) + """,
                        datasets: [
                            {
                                label: 'Total Load (W)',
                                data: """ + str(load_values) + """,
                                borderColor: 'rgb(17, 153, 142)',
                                backgroundColor: 'rgba(17, 153, 142, 0.1)',
                                borderWidth: 3,
                                tension: 0.4,
                                yAxisID: 'y',
                                fill: true
                            },
                            {
                                label: 'Battery Discharge (W)',
                                data: """ + str(battery_values) + """,
                                borderColor: 'rgb(235, 51, 73)',
                                backgroundColor: 'rgba(235, 51, 73, 0.1)',
                                borderWidth: 3,
                                tension: 0.4,
                                yAxisID: 'y',
                                fill: true
                            }
                        ]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: true,
                        interaction: {
                            mode: 'index',
                            intersect: false
                        },
                        scales: {
                            y: {
                                type: 'linear',
                                display: true,
                                position: 'left',
                                title: {
                                    display: true,
                                    text: 'Power (W)',
                                    font: {
                                        size: 14,
                                        weight: 'bold'
                                    }
                                },
                                grid: {
                                    color: 'rgba(0, 0, 0, 0.1)'
                                }
                            },
                            x: {
                                grid: {
                                    color: 'rgba(0, 0, 0, 0.05)'
                                }
                            }
                        },
                        plugins: {
                            legend: {
                                display: true,
                                position: 'top',
                                labels: {
                                    font: {
                                        size: 13
                                    },
                                    padding: 15
                                }
                            },
                            tooltip: {
                                callbacks: {
                                    label: function(context) {
                                        return context.dataset.label + ': ' + context.parsed.y.toFixed(0) + ' W';
                                    }
                                }
                            }
                        }
                    }
                });
            </script>
            
            <form method="POST" action="/test_alert" style="text-align: center; margin-top: 30px;">
                <button type="submit">🔔 Send Test Alert</button>
            </form>
        </div>
        
        <div class="footer">
            Powered by Growatt Solar • Weather by Open-Meteo • Managed by YourHost
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
        html_content="<p>This is a test alert from Tulia House solar monitoring system.</p>"
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
