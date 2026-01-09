import os
import time
import requests
import traceback
from datetime import datetime, timedelta, timezone
from threading import Thread
from flask import Flask, render_template_string, request, jsonify

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
# Inverter Configuration (Display order: 1, 2, 3)
# ----------------------------
INVERTER_CONFIG = {
    "RKG3B0400T": {"label": "Inverter 1", "type": "primary", "datalog": "DDD0B021CC", "display_order": 1},
    "KAM4N5W0AG": {"label": "Inverter 2", "type": "primary", "datalog": "DDD0B02121", "display_order": 2},
    "JNK1CDR0KQ": {"label": "Inverter 3 (Backup)", "type": "backup", "datalog": "DDD0B0221H", "display_order": 3}
}

# Alert thresholds
PRIMARY_BATTERY_THRESHOLD = 40  # When backup kicks in
BACKUP_VOLTAGE_THRESHOLD = 51.2  # When generator starts
TOTAL_SOLAR_CAPACITY_KW = 10
PRIMARY_INVERTER_CAPACITY_W = 10000  # 10kW combined
BACKUP_INVERTER_CAPACITY_W = 5000    # 5kW

# Backup battery voltage status thresholds
BACKUP_VOLTAGE_GOOD = 53.0   # Good status
BACKUP_VOLTAGE_MEDIUM = 52.3  # Medium status  
BACKUP_VOLTAGE_LOW = 52.0     # Low status

# Temperature thresholds
INVERTER_TEMP_WARNING = 60  # °C
INVERTER_TEMP_CRITICAL = 70  # °C

# Communication timeout
COMMUNICATION_TIMEOUT_MINUTES = 10

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
last_alert_time = {}
latest_data = {}
load_history = []
battery_history = []
weather_forecast = {}
alert_history = []
last_communication = {}

# Debug info for weather
weather_debug = {
    "status": "Initializing",
    "last_attempt": None,
    "error": None,
    "url_used": None
}

# East African Timezone
EAT = timezone(timedelta(hours=3))

# ----------------------------
# Weather & Solar Forecast Functions
# ----------------------------
def get_weather_forecast():
    """Get weather forecast from Open-Meteo with robust error handling"""
    global weather_debug
    
    # Headers to look like a browser/legitimate app
    weather_headers = {
        "User-Agent": "Mozilla/5.0 (compatible; SolarMonitor/1.0; +http://tulia.house)",
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate"
    }
    
    # Primary URL (Specific Timezone)
    url = f"https://api.open-meteo.com/v1/forecast?latitude={LATITUDE}&longitude={LONGITUDE}&hourly=cloud_cover,shortwave_radiation,direct_radiation&timezone=Africa/Nairobi&forecast_days=2"
    
    # Fallback URL (UTC - simpler if timezone fails)
    fallback_url = f"https://api.open-meteo.com/v1/forecast?latitude={LATITUDE}&longitude={LONGITUDE}&hourly=cloud_cover,shortwave_radiation,direct_radiation&forecast_days=2"
    
    weather_debug["last_attempt"] = datetime.now(EAT).strftime("%H:%M:%S")
    
    try:
        print(f"🌤️ Requesting weather data from: {url}")
        weather_debug["url_used"] = url
        
        # Attempt 1: Primary URL
        try:
            response = requests.get(url, headers=weather_headers, timeout=20)
            response.raise_for_status()
        except Exception as e:
            print(f"⚠️ Primary weather fetch failed: {e}. Trying fallback...")
            weather_debug["error"] = f"Primary failed: {str(e)}"
            # Attempt 2: Fallback URL
            weather_debug["url_used"] = fallback_url
            response = requests.get(fallback_url, headers=weather_headers, timeout=20)
            response.raise_for_status()

        data = response.json()
        
        # Validate data structure
        if 'hourly' not in data:
            raise ValueError("Invalid API response: 'hourly' key missing")

        forecast = {
            'times': data['hourly']['time'],
            'cloud_cover': data['hourly']['cloud_cover'],
            'solar_radiation': data['hourly']['shortwave_radiation'],
            'direct_radiation': data['hourly']['direct_radiation']
        }
        
        print(f"✓ Weather forecast updated: {len(forecast['times'])} hours data received")
        weather_debug["status"] = "Success"
        weather_debug["error"] = None
        return forecast
        
    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)}"
        print(f"✗ Weather fetch completely failed: {error_msg}")
        weather_debug["status"] = "Failed"
        weather_debug["error"] = error_msg
        return None

def analyze_solar_conditions(forecast):
    """Analyze upcoming solar conditions"""
    if not forecast:
        return None
    
    try:
        now = datetime.now(EAT)
        current_hour = now.hour
        
        is_nighttime = current_hour < 6 or current_hour >= 18
        
        if is_nighttime:
            tomorrow = now + timedelta(days=1)
            start_time = tomorrow.replace(hour=6, minute=0, second=0, microsecond=0)
            end_time = tomorrow.replace(hour=18, minute=0, second=0, microsecond=0)
            analysis_label = "Tomorrow's Daylight"
        else:
            start_time = now
            end_time = now.replace(hour=18, minute=0, second=0, microsecond=0)
            analysis_label = "Today's Remaining Daylight"
        
        avg_cloud_cover = 0
        avg_solar_radiation = 0
        count = 0
        
        for i, time_str in enumerate(forecast['times']):
            try:
                # Handle ISO format variations
                clean_time = time_str.replace('Z', '')
                forecast_time = datetime.fromisoformat(clean_time)
                
                # Adjust timezone if naive
                if forecast_time.tzinfo is None:
                    # If we used the fallback URL (UTC), we need to adjust
                    if "Africa/Nairobi" not in weather_debug.get("url_used", ""):
                        forecast_time = forecast_time.replace(tzinfo=timezone.utc).astimezone(EAT)
                    else:
                        forecast_time = forecast_time.replace(tzinfo=EAT)
                else:
                    forecast_time = forecast_time.astimezone(EAT)
                
                if start_time <= forecast_time <= end_time:
                    hour = forecast_time.hour
                    if 6 <= hour <= 18:
                        avg_cloud_cover += forecast['cloud_cover'][i]
                        avg_solar_radiation += forecast['solar_radiation'][i]
                        count += 1
            except Exception as ve:
                continue
        
        if count > 0:
            avg_cloud_cover /= count
            avg_solar_radiation /= count
            
            poor_conditions = avg_cloud_cover > 70 or avg_solar_radiation < 200
            
            return {
                'avg_cloud_cover': avg_cloud_cover,
                'avg_solar_radiation': avg_solar_radiation,
                'poor_conditions': poor_conditions,
                'hours_analyzed': count,
                'analysis_period': analysis_label,
                'is_nighttime': is_nighttime
            }
    except Exception as e:
        print(f"✗ Error analyzing solar conditions: {e}")
        weather_debug["error"] = f"Analysis Error: {str(e)}"
    
    return None

# ----------------------------
# Helper Functions
# ----------------------------
def get_backup_voltage_status(voltage):
    if voltage >= BACKUP_VOLTAGE_GOOD:
        return "Good", "green"
    elif voltage >= BACKUP_VOLTAGE_MEDIUM:
        return "Medium", "orange"
    else:
        return "Low", "red"

def check_generator_running(backup_inverter_data):
    if not backup_inverter_data:
        return False
    v_ac_input = float(backup_inverter_data.get('vac', 0) or 0)
    p_ac_input = float(backup_inverter_data.get('pAcInPut', 0) or 0)
    return v_ac_input > 100 or p_ac_input > 50

# ----------------------------
# Email function
# ----------------------------
def send_email(subject, html_content, alert_type="general"):
    global last_alert_time, alert_history
    if not all([RESEND_API_KEY, SENDER_EMAIL, RECIPIENT_EMAIL]):
        print("✗ Error: Missing email credentials in env")
        return False
    
    cooldown_map = {
        "critical": 60, "very_high_load": 30, "backup_active": 120,
        "high_load": 60, "moderate_load": 120, "warning": 120,
        "communication_lost": 60, "fault_alarm": 30, "high_temperature": 60,
        "test": 0, "general": 120
    }
    
    cooldown_minutes = cooldown_map.get(alert_type, 120)
    
    if alert_type in last_alert_time and cooldown_minutes > 0:
        if datetime.now(EAT) - last_alert_time[alert_type] < timedelta(minutes=cooldown_minutes):
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
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json=email_data
        )
        if response.status_code == 200:
            now = datetime.now(EAT)
            print(f"✓ Email sent: {subject}")
            last_alert_time[alert_type] = now
            alert_history.append({"timestamp": now, "type": alert_type, "subject": subject})
            cutoff = now - timedelta(hours=12)
            alert_history[:] = [a for a in alert_history if a['timestamp'] >= cutoff]
            return True
        else:
            print(f"✗ Email failed {response.status_code}: {response.text}")
            return False
    except Exception as e:
        print(f"✗ Error sending email: {e}")
        return False

# ----------------------------
# Alert Logic
# ----------------------------
def check_and_send_alerts(inverter_data, solar_conditions, total_solar_input, total_battery_discharge, generator_running):
    inv1 = next((i for i in inverter_data if i['SN'] == 'RKG3B0400T'), None)
    inv2 = next((i for i in inverter_data if i['SN'] == 'KAM4N5W0AG'), None)
    inv3_backup = next((i for i in inverter_data if i['SN'] == 'JNK1CDR0KQ'), None)
    
    if not all([inv1, inv2, inv3_backup]):
        return
    
    primary_capacity = min(inv1['Capacity'], inv2['Capacity'])
    backup_voltage = inv3_backup['vBat']
    backup_active = inv3_backup['OutputPower'] > 50
    total_load = inv1['OutputPower'] + inv2['OutputPower'] + inv3_backup['OutputPower']
    
    # 1. Comm Loss
    for inv in inverter_data:
        if inv.get('communication_lost', False):
            send_email(f"⚠️ Communication Lost: {inv['Label']}", f"Inverter {inv['Label']} offline.", "communication_lost")

    # 2. Faults
    for inv in inverter_data:
        if inv.get('has_fault', False):
            send_email(f"🚨 FAULT: {inv['Label']}", f"Fault detected on {inv['Label']}.", "fault_alarm")

    # 3. Temp
    for inv in inverter_data:
        if inv.get('high_temperature', False):
            send_email(f"🌡️ High Temp: {inv['Label']}", f"Temp: {inv['temperature']}C", "high_temperature")

    # 4. Generator / Critical
    if generator_running or backup_voltage < BACKUP_VOLTAGE_THRESHOLD:
        send_email("🚨 CRITICAL: Generator/Backup", f"Generator: {generator_running}, Backup V: {backup_voltage}", "critical")
        return
    
    # 5. Backup Active
    if backup_active and primary_capacity < PRIMARY_BATTERY_THRESHOLD:
        send_email("⚠️ Backup Active", f"Backup supplying power. Primary: {primary_capacity}%", "backup_active")
        return

    # 6. High Discharge
    if total_battery_discharge >= 2500:
        send_email("🚨 High Discharge", f"Battery Discharge: {total_battery_discharge}W", "very_high_load")
    elif 1500 <= total_battery_discharge < 2500:
        send_email("⚠️ High Discharge", f"Battery Discharge: {total_battery_discharge}W", "high_load")

# ----------------------------
# Polling Loop
# ----------------------------
def poll_growatt():
    global latest_data, load_history, battery_history, weather_forecast, last_communication
    
    print("🌤️ Initial weather fetch...")
    weather_forecast = get_weather_forecast()
    last_weather_update = datetime.now(EAT) if weather_forecast else None
    
    while True:
        try:
            # Retry weather more aggressively if it failed (every loop), otherwise every 30 mins
            should_update_weather = False
            if weather_forecast is None:
                should_update_weather = True
            elif last_weather_update and (datetime.now(EAT) - last_weather_update > timedelta(minutes=30)):
                should_update_weather = True
                
            if should_update_weather:
                new_forecast = get_weather_forecast()
                if new_forecast:
                    weather_forecast = new_forecast
                    last_weather_update = datetime.now(EAT)
            
            total_output_power = 0
            total_battery_discharge_W = 0
            total_solar_input_W = 0
            inverter_data = []
            now = datetime.now(EAT)
            
            primary_capacities = []
            backup_data = None
            generator_running = False
            
            for sn in SERIAL_NUMBERS:
                try:
                    response = requests.post(API_URL, data={"storage_sn": sn}, headers=headers, timeout=20)
                    response.raise_for_status()
                    data = response.json().get("data", {})
                    last_communication[sn] = now
                    
                    config = INVERTER_CONFIG.get(sn, {"label": sn, "type": "unknown", "datalog": "N/A", "display_order": 99})
                    
                    out_power = float(data.get("outPutPower") or 0)
                    capacity = float(data.get("capacity") or 0)
                    v_bat = float(data.get("vBat") or 0)
                    p_bat = float(data.get("pBat") or 0)
                    ppv = float(data.get("ppv") or 0) + float(data.get("ppv2") or 0)
                    temp = max(float(data.get("invTemperature") or 0), float(data.get("dcDcTemperature") or 0), float(data.get("temperature") or 0))
                    
                    vac = float(data.get("vac") or 0)
                    pac_input = float(data.get("pAcInPut") or 0)
                    
                    total_output_power += out_power
                    total_solar_input_W += ppv
                    if p_bat > 0: total_battery_discharge_W += p_bat
                    
                    inv_info = {
                        "SN": sn, "Label": config['label'], "Type": config['type'], "DataLog": config['datalog'],
                        "DisplayOrder": config['display_order'], "OutputPower": out_power, "Capacity": capacity,
                        "vBat": v_bat, "pBat": p_bat, "ppv": ppv, "temperature": temp,
                        "high_temperature": temp >= INVERTER_TEMP_WARNING,
                        "Status": data.get("statusText", "Unknown"),
                        "has_fault": int(data.get("errorCode") or 0) != 0,
                        "fault_info": {"errorCode": int(data.get("errorCode") or 0), "faultCode": int(data.get("faultCode") or 0)},
                        "vac": vac, "communication_lost": False, "last_seen": now.strftime("%Y-%m-%d %H:%M:%S")
                    }
                    inverter_data.append(inv_info)
                    
                    if config['type'] == 'primary' and capacity > 0: primary_capacities.append(capacity)
                    elif config['type'] == 'backup':
                        backup_data = inv_info
                        if vac > 100 or pac_input > 50: generator_running = True
                
                except Exception as e:
                    print(f"❌ Error polling {sn}: {e}")
                    if sn in last_communication and (now - last_communication[sn] > timedelta(minutes=COMMUNICATION_TIMEOUT_MINUTES)):
                        config = INVERTER_CONFIG.get(sn, {})
                        inverter_data.append({"SN": sn, "Label": config.get('label', sn), "Type": config.get('type', 'unknown'), "DisplayOrder": config.get('display_order', 99), "communication_lost": True, "last_seen": last_communication[sn].strftime("%Y-%m-%d %H:%M:%S")})

            inverter_data.sort(key=lambda x: x.get('DisplayOrder', 99))
            
            latest_data = {
                "timestamp": now.strftime("%Y-%m-%d %H:%M:%S EAT"),
                "total_output_power": total_output_power,
                "total_battery_discharge_W": total_battery_discharge_W,
                "total_solar_input_W": total_solar_input_W,
                "primary_battery_min": min(primary_capacities) if primary_capacities else 0,
                "backup_battery_voltage": backup_data['vBat'] if backup_data else 0,
                "backup_voltage_status": get_backup_voltage_status(backup_data['vBat'] if backup_data else 0)[0],
                "backup_voltage_color": get_backup_voltage_status(backup_data['vBat'] if backup_data else 0)[1],
                "backup_active": backup_data['OutputPower'] > 50 if backup_data else False,
                "generator_running": generator_running,
                "inverters": inverter_data
            }
            
            load_history.append((now, total_output_power))
            load_history = [(t, p) for t, p in load_history if t >= now - timedelta(hours=12)]
            battery_history.append((now, total_battery_discharge_W))
            battery_history = [(t, p) for t, p in battery_history if t >= now - timedelta(hours=12)]
            
            print(f"{latest_data['timestamp']} | Load={total_output_power:.0f}W | Solar={total_solar_input_W:.0f}W")
            
            solar_conditions = analyze_solar_conditions(weather_forecast)
            check_and_send_alerts(inverter_data, solar_conditions, total_solar_input_W, total_battery_discharge_W, generator_running)
        
        except Exception as e:
            print(f"❌ Error in polling loop: {e}")
            traceback.print_exc()
        
        time.sleep(POLL_INTERVAL_MINUTES * 60)

# ----------------------------
# Flask Web Routes
# ----------------------------
@app.route("/refresh_weather")
def refresh_weather():
    global weather_forecast
    weather_forecast = get_weather_forecast()
    return jsonify(weather_debug)

@app.route("/")
def home():
    primary_battery = latest_data.get("primary_battery_min", 0)
    backup_voltage = latest_data.get("backup_battery_voltage", 0)
    backup_voltage_status = latest_data.get("backup_voltage_status", "Unknown")
    backup_voltage_color = latest_data.get("backup_voltage_color", "gray")
    backup_active = latest_data.get("backup_active", False)
    generator_running = latest_data.get("generator_running", False)
    total_load = latest_data.get("total_output_power", 0)
    total_solar = latest_data.get("total_solar_input_W", 0)
    total_battery_discharge = latest_data.get("total_battery_discharge_W", 0)
    
    primary_color = "red" if primary_battery < 40 else ("orange" if primary_battery < 50 else "green")
    
    times = [t.strftime('%H:%M') for t, p in load_history]
    load_values = [p for t, p in load_history]
    battery_values = [p for t, p in battery_history]
    
    solar_conditions = analyze_solar_conditions(weather_forecast)
    
    html = f"""
<!DOCTYPE html>
<html>
<head>
    <title>Tulia House - Solar Monitor</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body {{ font-family: 'Segoe UI', sans-serif; background: #f0f2f5; padding: 20px; }}
        .container {{ max-width: 1200px; margin: 0 auto; }}
        .card {{ background: white; padding: 20px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); margin-bottom: 20px; }}
        .header {{ text-align: center; margin-bottom: 30px; }}
        .metric-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 15px; }}
        .metric {{ padding: 15px; border-radius: 8px; color: white; text-align: center; }}
        .metric.green {{ background: #28a745; }}
        .metric.orange {{ background: #fd7e14; }}
        .metric.red {{ background: #dc3545; }}
        .metric.blue {{ background: #007bff; }}
        .metric.gray {{ background: #6c757d; }}
        .metric-val {{ font-size: 1.5em; font-weight: bold; }}
        .weather-alert {{ padding: 15px; border-radius: 8px; color: white; margin-bottom: 20px; }}
        .weather-alert.good {{ background: #28a745; }}
        .weather-alert.poor {{ background: #fd7e14; }}
        .weather-alert.error {{ background: #6c757d; }}
        .inv-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 15px; }}
        .inv-card {{ border: 1px solid #ddd; padding: 15px; border-radius: 8px; }}
        .inv-card.backup {{ background: #fff3cd; border-color: #ffeeba; }}
        .inv-card.offline {{ background: #f8d7da; border-color: #f5c6cb; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>TULIA HOUSE SOLAR</h1>
            <p>Last Updated: {latest_data.get('timestamp', 'Waiting for data...')}</p>
        </div>

        <div class="card">
            <div class="metric-grid">
                <div class="metric {primary_color}">
                    <div>Primary Battery</div>
                    <div class="metric-val">{primary_battery:.0f}%</div>
                </div>
                <div class="metric {backup_voltage_color}">
                    <div>Backup Battery</div>
                    <div class="metric-val">{backup_voltage:.1f}V</div>
                </div>
                <div class="metric blue">
                    <div>Total Load</div>
                    <div class="metric-val">{total_load:.0f}W</div>
                </div>
                <div class="metric gray">
                    <div>Solar Input</div>
                    <div class="metric-val">{total_solar:.0f}W</div>
                </div>
                <div class="metric {'green' if generator_running else 'gray'}">
                    <div>Generator</div>
                    <div class="metric-val">{'ON' if generator_running else 'OFF'}</div>
                </div>
            </div>
        </div>
"""

    if solar_conditions:
        alert_class = "poor" if solar_conditions['poor_conditions'] else "good"
        html += f"""
        <div class="weather-alert {alert_class}">
            <h3>{'☁️ Poor Solar Conditions' if solar_conditions['poor_conditions'] else '☀️ Good Solar Conditions'}</h3>
            <p>Cloud: {solar_conditions['avg_cloud_cover']:.0f}% | Solar: {solar_conditions['avg_solar_radiation']:.0f} W/m²</p>
            <p>{'Limited recharge expected.' if solar_conditions['poor_conditions'] else 'Batteries should recharge well.'}</p>
        </div>
        """
    else:
        # Detailed Error Display
        html += f"""
        <div class="weather-alert error">
            <h3>⚠️ Weather Data Unavailable</h3>
            <p><strong>Status:</strong> {weather_debug['status']}</p>
            <p><strong>Last Error:</strong> {weather_debug['error']}</p>
            <p><strong>Last Attempt:</strong> {weather_debug['last_attempt']}</p>
            <p><strong>URL Used:</strong> <span style="font-size:0.8em">{weather_debug['url_used']}</span></p>
            <div style="margin-top:10px;">
                <button onclick="fetch('/refresh_weather').then(r => window.location.reload())" style="padding:5px 10px; cursor:pointer;">Force Retry</button>
            </div>
        </div>
        """

    html += """
        <div class="card">
            <h3>Inverter Status</h3>
            <div class="inv-grid">
    """
    
    for inv in latest_data.get("inverters", []):
        cls = 'offline' if inv.get('communication_lost') else ('backup' if inv.get('Type') == 'backup' else '')
        html += f"""
            <div class="inv-card {cls}">
                <h4>{inv.get('Label')}</h4>
                <p>Power: <strong>{inv.get('OutputPower', 0):.0f}W</strong></p>
                <p>Battery: <strong>{inv.get('Capacity', 0):.0f}%</strong> / {inv.get('vBat', 0):.1f}V</p>
                <p>Solar: {inv.get('ppv', 0):.0f}W</p>
                <p>Temp: {inv.get('temperature', 0):.1f}°C</p>
            </div>
        """
        
    html += f"""
            </div>
        </div>
        
        <div class="card">
            <canvas id="pChart"></canvas>
        </div>
    </div>
    
    <script>
        new Chart(document.getElementById('pChart'), {{
            type: 'line',
            data: {{
                labels: {times},
                datasets: [
                    {{ label: 'Load (W)', data: {load_values}, borderColor: 'blue', fill: false }},
                    {{ label: 'Discharge (W)', data: {battery_values}, borderColor: 'red', fill: false }}
                ]
            }},
            options: {{ responsive: true }}
        }});
        
        setTimeout(() => window.location.reload(), 300000);
    </script>
</body>
</html>
"""
    return render_template_string(html)

Thread(target=poll_growatt, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
