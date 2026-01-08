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

# ----------------------------
# Inverter Configuration
# ----------------------------
INVERTER_CONFIG = {
    "RKG3B0400T": {"label": "Inverter 1", "type": "primary", "datalog": "DDD0B021CC"},
    "KAM4N5W0AG": {"label": "Inverter 2", "type": "primary", "datalog": "DDD0B02121"},
    "JNK1CDR0KQ": {"label": "Inverter 3 (Backup)", "type": "backup", "datalog": "DDD0B0221H"}
}

# Alert thresholds
PRIMARY_BATTERY_THRESHOLD = 40  # When backup kicks in
BACKUP_VOLTAGE_THRESHOLD = 51.2  # When generator starts
BACKUP_CAPACITY_WARNING = 30  # Warn when backup battery is getting low
TOTAL_SOLAR_CAPACITY_KW = 10

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
last_alert_time = {}  # Track different alert types separately
latest_data = {}
load_history = []
battery_history = []
weather_forecast = {}

# East African Timezone
EAT = timezone(timedelta(hours=3))

# ----------------------------
# Weather & Solar Forecast Functions
# ----------------------------
def get_weather_forecast():
    """Get weather forecast from Open-Meteo (free, no API key)"""
    try:
        url = f"https://api.open-meteo.com/v1/forecast?latitude={LATITUDE}&longitude={LONGITUDE}&hourly=cloud_cover,shortwave_radiation,direct_radiation&timezone=Africa/Nairobi&forecast_days=2"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        
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
# Email function with alert type tracking
# ----------------------------
def send_email(subject, html_content, alert_type="general"):
    global last_alert_time
    if not all([RESEND_API_KEY, SENDER_EMAIL, RECIPIENT_EMAIL]):
        print("✗ Error: Missing email credentials in env")
        return False
    
    # Rate limit: different cooldowns for different alert types
    cooldown_minutes = 60 if alert_type != "critical" else 30  # Critical alerts can send more frequently
    
    if alert_type in last_alert_time:
        if datetime.now(EAT) - last_alert_time[alert_type] < timedelta(minutes=cooldown_minutes):
            print(f"⚠️ Alert cooldown active for {alert_type}, skipping email")
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
            last_alert_time[alert_type] = datetime.now(EAT)
            return True
        else:
            print(f"✗ Email failed {response.status_code}: {response.text}")
            return False
    except Exception as e:
        print(f"✗ Error sending email: {e}")
        return False

# ----------------------------
# Intelligent Alert Logic
# ----------------------------
def check_and_send_alerts(inverter_data, solar_conditions):
    """Smart alert logic based on cascade inverter architecture"""
    
    # Extract data for each inverter
    inv1 = next((i for i in inverter_data if i['SN'] == 'RKG3B0400T'), None)
    inv2 = next((i for i in inverter_data if i['SN'] == 'KAM4N5W0AG'), None)
    inv3_backup = next((i for i in inverter_data if i['SN'] == 'JNK1CDR0KQ'), None)
    
    if not all([inv1, inv2, inv3_backup]):
        print("⚠️ Not all inverters reporting data")
        return
    
    # Calculate primary battery levels (minimum of Inv1 and Inv2)
    primary_capacity = min(inv1['Capacity'], inv2['Capacity'])
    backup_capacity = inv3_backup['Capacity']
    backup_voltage = inv3_backup['vBat']
    
    # Total load across all inverters
    total_load = inv1['OutputPower'] + inv2['OutputPower'] + inv3_backup['OutputPower']
    
    # Check backup inverter status
    backup_active = inv3_backup['OutputPower'] > 50  # Backup is supplying power
    
    # ============================================
    # CRITICAL ALERT: Generator about to start
    # ============================================
    if backup_voltage < 51.2:
        send_email(
            subject="🚨 CRITICAL: Generator Starting - Backup Battery Critical",
            html_content=f"""
            <div style="font-family: Arial; padding: 20px; background: #fff3cd; border-left: 5px solid #dc3545;">
                <h2 style="color: #dc3545;">🚨 GENERATOR ACTIVATION IMMINENT</h2>
                
                <p><strong>Backup Inverter (Inv 3) Status:</strong></p>
                <ul>
                    <li>Battery Voltage: <strong style="color: #dc3545;">{backup_voltage:.1f}V</strong> (Threshold: 51.2V)</li>
                    <li>Battery Capacity: {backup_capacity:.0f}%</li>
                    <li>Status: Generator starting or already running</li>
                </ul>
                
                <p><strong>Primary Inverters Status:</strong></p>
                <ul>
                    <li>Inverter 1 Battery: {inv1['Capacity']:.0f}%</li>
                    <li>Inverter 2 Battery: {inv2['Capacity']:.0f}%</li>
                    <li>Combined Load: {total_load:.0f}W</li>
                </ul>
                
                <p><strong>⚠️ ACTION REQUIRED:</strong></p>
                <p>The backup battery voltage has dropped below 51.2V. The generator should be starting automatically. 
                <strong>Reduce all non-essential loads immediately.</strong></p>
                
                {f'<p style="color: #dc3545;"><strong>Weather Alert:</strong> Poor solar conditions expected (Cloud: {solar_conditions["avg_cloud_cover"]:.0f}%). Generator may run for extended period.</p>' if solar_conditions and solar_conditions['poor_conditions'] else ''}
            </div>
            """,
            alert_type="critical"
        )
    
    # ============================================
    # HIGH ALERT: Backup inverter active
    # ============================================
    elif backup_active and primary_capacity < PRIMARY_BATTERY_THRESHOLD:
        send_email(
            subject="⚠️ HIGH ALERT: Backup Inverter Active - Primary Batteries Low",
            html_content=f"""
            <div style="font-family: Arial; padding: 20px; background: #fff3cd; border-left: 5px solid #ff9800;">
                <h2 style="color: #ff9800;">⚠️ BACKUP SYSTEM ACTIVATED</h2>
                
                <p><strong>System Status:</strong></p>
                <ul>
                    <li>Backup Inverter (Inv 3): <strong style="color: #ff9800;">ACTIVE</strong> - Supplying {inv3_backup['OutputPower']:.0f}W</li>
                    <li>Backup Battery: {backup_capacity:.0f}% ({backup_voltage:.1f}V)</li>
                    <li>Primary Batteries: <strong style="color: #dc3545;">{primary_capacity:.0f}%</strong> (Below 40% threshold)</li>
                </ul>
                
                <p><strong>Individual Inverter Status:</strong></p>
                <ul>
                    <li>Inverter 1: {inv1['Capacity']:.0f}% | {inv1['OutputPower']:.0f}W</li>
                    <li>Inverter 2: {inv2['Capacity']:.0f}% | {inv2['OutputPower']:.0f}W</li>
                    <li>Total Load: {total_load:.0f}W</li>
                </ul>
                
                <p><strong>⚠️ WARNING:</strong></p>
                <p>Primary inverter batteries are below 40%. Backup inverter is now supplying power to the primary inverters. 
                If backup voltage drops below 51.2V, the generator will start automatically.</p>
                
                {f'<p style="color: #dc3545;"><strong>Weather Alert:</strong> Poor solar conditions ahead (Cloud: {solar_conditions["avg_cloud_cover"]:.0f}%). Limited recharge expected. Consider reducing load.</p>' if solar_conditions and solar_conditions['poor_conditions'] else f'<p style="color: #28a745;"><strong>Weather:</strong> Good solar conditions expected (Cloud: {solar_conditions["avg_cloud_cover"]:.0f}%). Batteries should recharge.</p>' if solar_conditions else ''}
            </div>
            """,
            alert_type="backup_active"
        )
    
    # ============================================
    # WARNING: Primary batteries approaching 40%
    # ============================================
    elif 40 < primary_capacity < 50:
        send_email(
            subject="⚠️ WARNING: Primary Battery Low - Backup Activation Soon",
            html_content=f"""
            <div style="font-family: Arial; padding: 20px; background: #fff9e6; border-left: 5px solid #ffc107;">
                <h2 style="color: #ffc107;">⚠️ PRIMARY BATTERY WARNING</h2>
                
                <p><strong>System Status:</strong></p>
                <ul>
                    <li>Primary Batteries: <strong style="color: #ff9800;">{primary_capacity:.0f}%</strong> (Approaching 40% threshold)</li>
                    <li>Inverter 1: {inv1['Capacity']:.0f}%</li>
                    <li>Inverter 2: {inv2['Capacity']:.0f}%</li>
                    <li>Backup Battery: {backup_capacity:.0f}% ({backup_voltage:.1f}V)</li>
                    <li>Total Load: {total_load:.0f}W</li>
                </ul>
                
                <p><strong>Next Stage:</strong></p>
                <p>When primary batteries drop below 40%, Backup Inverter (Inv 3) will activate to supply power to the primary inverters.</p>
                
                {f'<p style="color: #dc3545;"><strong>Weather Alert:</strong> Poor solar forecast (Cloud: {solar_conditions["avg_cloud_cover"]:.0f}%). Consider reducing non-essential loads now.</p>' if solar_conditions and solar_conditions['poor_conditions'] else f'<p style="color: #28a745;"><strong>Weather:</strong> Good conditions expected. Batteries should recover.</p>' if solar_conditions else ''}
                
                <p><strong>Recommendation:</strong> Monitor usage and consider reducing high-power loads (AC, water heater).</p>
            </div>
            """,
            alert_type="warning"
        )
    
    # ============================================
    # INFO: High load but good battery levels
    # ============================================
    elif total_load > 2000:  # High total load
        send_email(
            subject="ℹ️ INFO: High Power Usage Detected",
            html_content=f"""
            <div style="font-family: Arial; padding: 20px; background: #e7f3ff; border-left: 5px solid #2196F3;">
                <h2 style="color: #2196F3;">ℹ️ HIGH POWER USAGE</h2>
                
                <p><strong>Current Status:</strong></p>
                <ul>
                    <li>Total Load: <strong>{total_load:.0f}W</strong></li>
                    <li>Primary Batteries: {primary_capacity:.0f}% (Good)</li>
                    <li>Backup Battery: {backup_capacity:.0f}%</li>
                </ul>
                
                <p><strong>Individual Loads:</strong></p>
                <ul>
                    <li>Inverter 1: {inv1['OutputPower']:.0f}W</li>
                    <li>Inverter 2: {inv2['OutputPower']:.0f}W</li>
                    <li>Inverter 3: {inv3_backup['OutputPower']:.0f}W</li>
                </ul>
                
                {f'<p style="color: #28a745;"><strong>Weather:</strong> Good solar conditions expected. System should handle load well.</p>' if solar_conditions and not solar_conditions['poor_conditions'] else f'<p style="color: #ff9800;"><strong>Weather:</strong> Poor solar ahead. Consider moderating usage.</p>' if solar_conditions else ''}
            </div>
            """,
            alert_type="high_load"
        )

# ----------------------------
# Growatt Polling Loop
# ----------------------------
def poll_growatt():
    global latest_data, load_history, battery_history, weather_forecast
    
    last_weather_update = None
    
    while True:
        try:
            # Update weather forecast every 30 minutes
            if last_weather_update is None or datetime.now(EAT) - last_weather_update > timedelta(minutes=30):
                weather_forecast = get_weather_forecast()
                last_weather_update = datetime.now(EAT)
            
            total_output_power = 0
            total_battery_discharge_W = 0
            inverter_data = []
            now = datetime.now(EAT)
            
            primary_capacities = []
            backup_data = None
            
            for sn in SERIAL_NUMBERS:
                response = requests.post(
                    API_URL,
                    data={"storage_sn": sn},
                    headers=headers,
                    timeout=20
                )
                response.raise_for_status()
                data = response.json().get("data", {})
                
                # Get configuration
                config = INVERTER_CONFIG.get(sn, {"label": sn, "type": "unknown", "datalog": "N/A"})
                
                # Extract key metrics
                out_power = float(data.get("outPutPower") or 0)
                capacity = float(data.get("capacity") or 0)
                v_bat = float(data.get("vBat") or 0)
                p_bat = float(data.get("pBat") or 0)
                
                total_output_power += out_power
                
                if p_bat > 0:
                    total_battery_discharge_W += p_bat
                
                inv_info = {
                    "SN": sn,
                    "Label": config['label'],
                    "Type": config['type'],
                    "DataLog": config['datalog'],
                    "OutputPower": out_power,
                    "Capacity": capacity,
                    "vBat": v_bat,
                    "pBat": p_bat,
                    "Status": data.get("statusText", "Unknown")
                }
                
                inverter_data.append(inv_info)
                
                # Track primary vs backup
                if config['type'] == 'primary':
                    if capacity > 0:
                        primary_capacities.append(capacity)
                elif config['type'] == 'backup':
                    backup_data = inv_info
            
            # Calculate system-wide metrics
            primary_battery_min = min(primary_capacities) if primary_capacities else 0
            backup_battery = backup_data['Capacity'] if backup_data else 0
            backup_voltage = backup_data['vBat'] if backup_data else 0
            backup_active = backup_data['OutputPower'] > 50 if backup_data else False
            
            # Save latest readings
            latest_data = {
                "timestamp": now.strftime("%Y-%m-%d %H:%M:%S EAT"),
                "total_output_power": total_output_power,
                "total_battery_discharge_W": total_battery_discharge_W,
                "primary_battery_min": primary_battery_min,
                "backup_battery": backup_battery,
                "backup_voltage": backup_voltage,
                "backup_active": backup_active,
                "inverters": inverter_data
            }
            
            # Append to history
            load_history.append((now, total_output_power))
            load_history = [(t, p) for t, p in load_history if t >= now - timedelta(hours=12)]
            
            battery_history.append((now, total_battery_discharge_W))
            battery_history = [(t, p) for t, p in battery_history if t >= now - timedelta(hours=12)]
            
            print(f"{latest_data['timestamp']} | Load={total_output_power:.0f}W | Primary={primary_battery_min:.0f}% | Backup={backup_battery:.0f}% ({backup_voltage:.1f}V) | Backup Active={backup_active}")
            
            # Check alerts with solar conditions
            solar_conditions = analyze_solar_conditions(weather_forecast, hours_ahead=10)
            check_and_send_alerts(inverter_data, solar_conditions)
        
        except Exception as e:
            print(f"❌ Error polling Growatt: {e}")
            import traceback
            traceback.print_exc()
        
        time.sleep(POLL_INTERVAL_MINUTES * 60)

# ----------------------------
# Flask Web Routes
# ----------------------------
@app.route("/")
def home():
    primary_battery = latest_data.get("primary_battery_min", 0)
    backup_battery = latest_data.get("backup_battery", 0)
    backup_voltage = latest_data.get("backup_voltage", 0)
    backup_active = latest_data.get("backup_active", False)
    total_load = latest_data.get("total_output_power", 0)
    
    # Color coding
    primary_color = "red" if primary_battery < 40 else ("orange" if primary_battery < 50 else "green")
    backup_color = "red" if backup_voltage < 51.2 else ("orange" if backup_battery < 30 else "green")
    
    # Chart data
    times = [t.strftime('%H:%M') for t, p in load_history]
    load_values = [p for t, p in load_history]
    battery_values = [p for t, p in battery_history]
    
    # Solar conditions
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
        
        .header .specs {{
            font-size: 0.9em;
            opacity: 0.8;
            margin-top: 10px;
        }}
        
        .container {{ 
            max-width: 1400px; 
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
        
        .system-status {{
            background: {'linear-gradient(135deg, #eb3349 0%, #f45c43 100%)' if backup_voltage < 51.2 else 'linear-gradient(135deg, #f77f00 0%, #fcbf49 100%)' if backup_active else 'linear-gradient(135deg, #11998e 0%, #38ef7d 100%)'};
            color: white;
            padding: 20px;
            border-radius: 10px;
            margin-bottom: 20px;
            text-align: center;
        }}
        
        .system-status h2 {{
            margin-bottom: 10px;
            font-size: 1.5em;
        }}
        
        .system-status .status-text {{
            font-size: 1.1em;
        }}
        
        .metrics-grid {{
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
        
        .metric.blue {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
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
        
        .metric-subtext {{
            font-size: 0.75em;
            opacity: 0.8;
            margin-top: 5px;
        }}
        
        .weather-alert {{
            padding: 20px;
            border-radius: 10px;
            margin-bottom: 20px;
            color: white;
        }}
        
        .weather-alert.good {{
            background: linear-gradient(135deg, #11998e 0%, #38ef7d 100%);
        }}
        
        .weather-alert.poor {{
            background: linear-gradient(135deg, #f77f00 0%, #fcbf49 100%);
        }}
        
        h2 {{ 
            color: #333;
            margin-bottom: 20px;
            font-size: 1.5em;
            font-weight: 500;
        }}
        
        .inverter-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
            margin: 20px 0;
        }}
        
        .inverter-card {{
            background: white;
            padding: 20px;
            border-radius: 10px;
            border-left: 5px solid #667eea;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }}
        
        .inverter-card.backup {{
            border-left-color: #f77f00;
            background: #fff9f0;
        }}
        
        .inverter-card h3 {{
            color: #333;
            margin-bottom: 10px;
            font-size: 1.4em;
            font-weight: 600;
        }}
        
        .inverter-card .inv-label {{
            font-size: 0.85em;
            color: #666;
            margin-bottom: 15px;
            padding: 8px;
            background: #f5f5f5;
            border-radius: 5px;
            font-family: monospace;
        }}
        
        .inverter-card .inv-stat {{
            display: flex;
            justify-content: space-between;
            padding: 8px 0;
            border-bottom: 1px solid #eee;
        }}
        
        .inverter-card .inv-stat:last-child {{
            border-bottom: none;
        }}
        
        .inverter-card .inv-stat-label {{
            color: #666;
        }}
        
        .inverter-card .inv-stat-value {{
            font-weight: bold;
            color: #333;
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
            <div class="subtitle">☀️ Solar Energy Monitoring System</div>
            <div class="specs">📍 Champagne Ridge, Kajiado | 🔆 10kW Solar Array | 🔋 Cascade Battery System</div>
        </div>
        
        <div class="card">
            <div class="timestamp">
                <strong>Last Updated:</strong> {latest_data.get('timestamp', 'N/A')}
            </div>
            
            <div class="system-status">
                <h2>{'🚨 GENERATOR ACTIVE / STARTING' if backup_voltage < 51.2 else '⚠️ BACKUP SYSTEM ACTIVE' if backup_active else '✓ NORMAL OPERATION'}</h2>
                <div class="status-text">
                    {
                        'Backup battery voltage critical. Generator should be running.' if backup_voltage < 51.2 else
                        f'Primary batteries below 40%. Backup inverter supplying {latest_data.get("inverters", [{}])[2].get("OutputPower", 0) if len(latest_data.get("inverters", [])) > 2 else 0:.0f}W to system.' if backup_active else
                        'All systems operating on primary batteries.'
                    }
                </div>
            </div>
"""
    
    # Weather alert
    if solar_conditions:
        alert_class = "poor" if solar_conditions['poor_conditions'] else "good"
        html += f"""
            <div class="weather-alert {alert_class}">
                <h3>{'☁️ Poor Solar Conditions Ahead' if solar_conditions['poor_conditions'] else '☀️ Good Solar Conditions Expected'}</h3>
                <p><strong>Next 10 Hours:</strong> Cloud Cover: {solar_conditions['avg_cloud_cover']:.0f}% | Solar Radiation: {solar_conditions['avg_solar_radiation']:.0f} W/m²</p>
                {'<p>⚠️ Limited recharge expected. Monitor battery levels closely.</p>' if solar_conditions['poor_conditions'] else '<p>✓ Batteries should recharge well during daylight hours.</p>'}
            </div>
"""
    
    html += f"""
            <div class="metrics-grid">
                <div class="metric {primary_color}">
                    <div class="metric-label">Primary Batteries</div>
                    <div class="metric-value">{primary_battery:.0f}%</div>
                    <div class="metric-subtext">Inverters 1 & 2 (Min)</div>
                </div>
                
                <div class="metric {backup_color}">
                    <div class="metric-label">Backup Battery</div>
                    <div class="metric-value">{backup_battery:.0f}%</div>
                    <div class="metric-subtext">{backup_voltage:.1f}V {'(Generator at 51.2V)' if backup_voltage > 51.2 else '(GENERATOR THRESHOLD!)'}</div>
                </div>
                
                <div class="metric blue">
                    <div class="metric-label">Total System Load</div>
                    <div class="metric-value">{total_load:.0f}W</div>
                    <div class="metric-subtext">All Inverters</div>
                </div>
                
                <div class="metric {'orange' if backup_active else 'green'}">
                    <div class="metric-label">Backup Status</div>
                    <div class="metric-value">{'ACTIVE' if backup_active else 'Standby'}</div>
                    <div class="metric-subtext">{'Supplying Power' if backup_active else 'Ready'}</div>
                </div>
            </div>
            
            <h2>Inverter Details</h2>
            <div class="inverter-grid">
"""
    
    # Display each inverter
    for inv in latest_data.get("inverters", []):
        is_backup = inv.get('Type') == 'backup'
        label = inv.get('Label', 'Unknown')
        sn = inv.get('SN', 'N/A')
        datalog = inv.get('DataLog', 'N/A')
        
        html += f"""
                <div class="inverter-card {'backup' if is_backup else ''}">
                    <h3>{label}</h3>
                    <div class="inv-label">{sn} ({datalog})</div>
                    <div class="inv-stat">
                        <span class="inv-stat-label">Battery Capacity</span>
                        <span class="inv-stat-value">{inv.get('Capacity', 0):.0f}%</span>
                    </div>
                    <div class="inv-stat">
                        <span class="inv-stat-label">Battery Voltage</span>
                        <span class="inv-stat-value">{inv.get('vBat', 0):.1f}V</span>
                    </div>
                    <div class="inv-stat">
                        <span class="inv-stat-label">Output Power</span>
                        <span class="inv-stat-value">{inv.get('OutputPower', 0):.0f}W</span>
                    </div>
                    <div class="inv-stat">
                        <span class="inv-stat-label">Battery Power</span>
                        <span class="inv-stat-value">{inv.get('pBat', 0):.0f}W</span>
                    </div>
                    <div class="inv-stat">
                        <span class="inv-stat-label">Status</span>
                        <span class="inv-stat-value">{inv.get('Status', 'Unknown')}</span>
                    </div>
                </div>
"""
    
    html += """
            </div>
        </div>
        
        <meta http-equiv="refresh" content="300">
        
        <div class="card">
            <div class="chart-container">
                <h2>Power Monitoring - Last 12 Hours</h2>
                <canvas id="powerChart"></canvas>
            </div>
            
            <script>
                const ctx = document.getElementById('powerChart').getContext('2d');
                new Chart(ctx, {
                    type: 'line',
                    data: {
                        labels: """ + str(times) + """,
                        datasets: [
                            {
                                label: 'Total Load (W)',
                                data: """ + str(load_values) + """,
                                borderColor: 'rgb(102, 126, 234)',
                                backgroundColor: 'rgba(102, 126, 234, 0.1)',
                                borderWidth: 3,
                                tension: 0.4,
                                fill: true
                            },
                            {
                                label: 'Battery Discharge (W)',
                                data: """ + str(battery_values) + """,
                                borderColor: 'rgb(235, 51, 73)',
                                backgroundColor: 'rgba(235, 51, 73, 0.1)',
                                borderWidth: 3,
                                tension: 0.4,
                                fill: true
                            }
                        ]
                    },
                    options: {
                        responsive: true,
                        interaction: {
                            mode: 'index',
                            intersect: false
                        },
                        scales: {
                            y: {
                                type: 'linear',
                                display: true,
                                title: {
                                    display: true,
                                    text: 'Power (W)',
                                    font: { size: 14, weight: 'bold' }
                                }
                            }
                        },
                        plugins: {
                            legend: {
                                display: true,
                                position: 'top'
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
            10kW Solar System • Cascade Battery Architecture • Managed by YourHost
        </div>
    </div>
</body>
</html>
"""
    
    return render_template_string(html)

@app.route("/test_alert", methods=["POST"])
def test_alert():
    send_email(
        subject="🔔 Tulia House Test Alert",
        html_content="<p>This is a test alert from Tulia House solar monitoring system.</p>",
        alert_type="test"
    )
    return '<html><body style="font-family: Arial; text-align: center; padding: 50px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white;"><h1>✅ Test Alert Sent!</h1><p><a href="/" style="color: white;">← Back</a></p></body></html>'

# ----------------------------
# Start
# ----------------------------
Thread(target=poll_growatt, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
