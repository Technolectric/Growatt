import os
import time
import json
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

# Weather Caching & Debug
WEATHER_CACHE_FILE = "weather_cache.json"
weather_cooldown_until = None
weather_debug = {
    "status": "Initializing",
    "last_attempt": None,
    "error": None,
    "url_used": None,
    "source": "None"
}

# East African Timezone
EAT = timezone(timedelta(hours=3))

# ----------------------------
# Weather Functions (Robust Caching & 429 Handling)
# ----------------------------
def load_weather_cache():
    """Load weather from local JSON file to prevent API spam on restart"""
    try:
        if os.path.exists(WEATHER_CACHE_FILE):
            with open(WEATHER_CACHE_FILE, 'r') as f:
                data = json.load(f)
            
            # Check if cache is valid (less than 2 hours old)
            cached_time = datetime.fromisoformat(data['timestamp'])
            if datetime.now(EAT) - cached_time < timedelta(hours=2):
                print(f"✓ Loaded valid weather cache from {data['timestamp']}")
                return data['forecast']
            else:
                print("ℹ️ Weather cache expired")
    except Exception as e:
        print(f"ℹ️ Cache load failed (normal on first run): {e}")
    return None

def save_weather_cache(forecast):
    """Save weather to local JSON file"""
    try:
        data = {
            'timestamp': datetime.now(EAT).isoformat(),
            'forecast': forecast
        }
        with open(WEATHER_CACHE_FILE, 'w') as f:
            json.dump(data, f)
        print("✓ Weather cache saved to disk")
    except Exception as e:
        print(f"⚠️ Failed to save weather cache: {e}")

def get_weather_forecast():
    """Get weather forecast with 429 protection and caching"""
    global weather_debug, weather_cooldown_until
    
    now = datetime.now(EAT)
    
    # 1. Check Cooldown (Anti-429)
    if weather_cooldown_until and now < weather_cooldown_until:
        remaining = int((weather_cooldown_until - now).total_seconds() / 60)
        msg = f"Rate Limited. Cooling down for {remaining} min."
        print(f"⚠️ {msg}")
        weather_debug["status"] = "Rate Limited (429)"
        weather_debug["error"] = msg
        
        # Try to return cached data if API is blocked
        return load_weather_cache()

    # 2. Headers to look like a browser
    weather_headers = {
        "User-Agent": "Mozilla/5.0 (compatible; SolarMonitor/1.0; +http://tulia.house)",
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate"
    }
    
    url = f"https://api.open-meteo.com/v1/forecast?latitude={LATITUDE}&longitude={LONGITUDE}&hourly=cloud_cover,shortwave_radiation,direct_radiation&timezone=Africa/Nairobi&forecast_days=2"
    fallback_url = f"https://api.open-meteo.com/v1/forecast?latitude={LATITUDE}&longitude={LONGITUDE}&hourly=cloud_cover,shortwave_radiation,direct_radiation&forecast_days=2"
    
    weather_debug["last_attempt"] = now.strftime("%H:%M:%S")
    
    try:
        print(f"🌤️ Requesting weather data...")
        weather_debug["url_used"] = url
        
        # Attempt 1: Primary URL
        try:
            response = requests.get(url, headers=weather_headers, timeout=20)
            response.raise_for_status()
        except requests.exceptions.HTTPError as e:
            # CRITICAL: Handle 429 specifically
            if e.response.status_code == 429:
                print("⛔ 429 TOO MANY REQUESTS - Stopping retries for 60 mins")
                weather_cooldown_until = now + timedelta(minutes=60)
                weather_debug["error"] = "HTTP 429: Too Many Requests (Cooling Down)"
                weather_debug["status"] = "Rate Limited"
                return load_weather_cache() # Return cache if possible
            raise e # Re-raise other errors to trigger fallback
            
        except Exception as e:
            print(f"⚠️ Primary weather fetch failed: {e}. Trying fallback...")
            weather_debug["error"] = f"Primary failed: {str(e)}"
            
            # Attempt 2: Fallback URL
            weather_debug["url_used"] = fallback_url
            response = requests.get(fallback_url, headers=weather_headers, timeout=20)
            response.raise_for_status()

        data = response.json()
        
        if 'hourly' not in data:
            raise ValueError("Invalid API response: 'hourly' key missing")

        forecast = {
            'times': data['hourly']['time'],
            'cloud_cover': data['hourly']['cloud_cover'],
            'solar_radiation': data['hourly']['shortwave_radiation'],
            'direct_radiation': data['hourly']['direct_radiation']
        }
        
        print(f"✓ Weather forecast updated: {len(forecast['times'])} hours data")
        weather_debug["status"] = "Success"
        weather_debug["error"] = None
        weather_debug["source"] = "API"
        
        # Save to cache
        save_weather_cache(forecast)
        
        return forecast
        
    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)}"
        print(f"✗ Weather fetch completely failed: {error_msg}")
        weather_debug["status"] = "Failed"
        weather_debug["error"] = error_msg
        
        # Fallback to disk cache if API fails completely
        return load_weather_cache()

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
                clean_time = time_str.replace('Z', '')
                forecast_time = datetime.fromisoformat(clean_time)
                
                if forecast_time.tzinfo is None:
                    # If using fallback URL (UTC), adjust to EAT
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
            except Exception:
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
    
    return None

# ----------------------------
# Helper Functions
# ----------------------------
def get_backup_voltage_status(voltage):
    if voltage >= BACKUP_VOLTAGE_GOOD: return "Good", "green"
    elif voltage >= BACKUP_VOLTAGE_MEDIUM: return "Medium", "orange"
    else: return "Low", "red"

def check_generator_running(backup_inverter_data):
    if not backup_inverter_data: return False
    v_ac_input = float(backup_inverter_data.get('vac', 0) or 0)
    p_ac_input = float(backup_inverter_data.get('pAcInPut', 0) or 0)
    return v_ac_input > 100 or p_ac_input > 50

# ----------------------------
# Email function
# ----------------------------
def send_email(subject, html_content, alert_type="general"):
    global last_alert_time, alert_history
    if not all([RESEND_API_KEY, SENDER_EMAIL, RECIPIENT_EMAIL]):
        print("✗ Error: Missing email credentials")
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
    
    email_data = {"from": SENDER_EMAIL, "to": [RECIPIENT_EMAIL], "subject": subject, "html": html_content}
    
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
            print(f"✗ Email failed {response.status_code}")
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
    
    if not all([inv1, inv2, inv3_backup]): return
    
    primary_capacity = min(inv1['Capacity'], inv2['Capacity'])
    backup_voltage = inv3_backup['vBat']
    backup_active = inv3_backup['OutputPower'] > 50
    
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
# Growatt Polling Loop
# ----------------------------
def poll_growatt():
    global latest_data, load_history, battery_history, weather_forecast, last_communication
    
    # 1. Try to load cache first
    print("🌤️ Loading initial weather...")
    weather_forecast = load_weather_cache()
    
    # 2. If no cache, try fetch (unless rate limited)
    if not weather_forecast:
        weather_forecast = get_weather_forecast()
        
    last_weather_update = datetime.now(EAT) if weather_forecast else None
    
    while True:
        try:
            # Weather Update Logic
            should_update_weather = False
            now = datetime.now(EAT)
            
            # If we have no data, try every loop (unless cooldown active)
            if weather_forecast is None:
                should_update_weather = True
            # If we have data, only update every 45 mins to be safe
            elif last_weather_update and (now - last_weather_update > timedelta(minutes=45)):
                should_update_weather = True
                
            if should_update_weather:
                new_forecast = get_weather_forecast()
                if new_forecast:
                    weather_forecast = new_forecast
                    last_weather_update = now
            
            # Inverter Polling
            total_output_power = 0
            total_battery_discharge_W = 0
            total_solar_input_W = 0
            inverter_data = []
            
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
    """Manual trigger to refresh weather"""
    global weather_forecast, weather_cooldown_until
    # Reset cooldown on manual force
    weather_cooldown_until = None
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
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ 
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(rgba(0, 0, 0, 0.4), rgba(0, 0, 0, 0.6)), 
                        url('https://images.unsplash.com/photo-1582268611958-ebfd161ef9cf?w=1600&q=80') center/cover fixed;
            min-height: 100vh; padding: 20px;
        }}
        .header {{ text-align: center; color: white; margin-bottom: 30px; text-shadow: 2px 2px 4px rgba(0,0,0,0.7); }}
        .header h1 {{ font-size: 2.5em; margin-bottom: 5px; font-weight: 300; letter-spacing: 2px; }}
        .container {{ max-width: 1400px; margin: 0 auto; }}
        .card {{ background: rgba(255, 255, 255, 0.95); backdrop-filter: blur(10px); padding: 25px; border-radius: 15px; box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3); margin-bottom: 20px; }}
        .timestamp {{ text-align: center; color: #666; font-size: 0.95em; margin-bottom: 20px; }}
        .system-status {{ color: white; padding: 20px; border-radius: 10px; margin-bottom: 20px; text-align: center; }}
        .system-status.normal {{ background: linear-gradient(135deg, #11998e 0%, #38ef7d 100%); }}
        .system-status.warning {{ background: linear-gradient(135deg, #f77f00 0%, #fcbf49 100%); }}
        .system-status.critical {{ background: linear-gradient(135deg, #eb3349 0%, #f45c43 100%); }}
        .weather-alert {{ padding: 20px; border-radius: 10px; margin-bottom: 20px; color: white; }}
        .weather-alert.good {{ background: linear-gradient(135deg, #11998e 0%, #38ef7d 100%); }}
        .weather-alert.poor {{ background: linear-gradient(135deg, #f77f00 0%, #fcbf49 100%); }}
        .weather-alert.error {{ background: linear-gradient(135deg, #757F9A 0%, #D7DDE8 100%); color: #333; border-left: 5px solid #dc3545; }}
        .metrics-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 15px; margin-bottom: 30px; }}
        .metric {{ padding: 20px; border-radius: 10px; color: white; box-shadow: 0 4px 15px rgba(0, 0, 0, 0.2); }}
        .metric.green {{ background: linear-gradient(135deg, #11998e 0%, #38ef7d 100%); }}
        .metric.orange {{ background: linear-gradient(135deg, #f77f00 0%, #fcbf49 100%); }}
        .metric.red {{ background: linear-gradient(135deg, #eb3349 0%, #f45c43 100%); }}
        .metric.blue {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); }}
        .metric.gray {{ background: linear-gradient(135deg, #757F9A 0%, #D7DDE8 100%); }}
        .metric-value {{ font-size: 1.8em; font-weight: bold; }}
        .inverter-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; margin: 20px 0; }}
        .inverter-card {{ background: white; padding: 20px; border-radius: 10px; border-left: 5px solid #667eea; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
        .inverter-card.backup {{ border-left-color: #f77f00; background: #fff9f0; }}
        .inverter-card.offline {{ border-left-color: #dc3545; background: #fff0f0; }}
        .retry-btn {{ background: #667eea; color: white; border: none; padding: 8px 15px; border-radius: 5px; cursor: pointer; margin-top: 10px; }}
        .footer {{ text-align: center; color: white; margin-top: 30px; font-size: 0.9em; text-shadow: 1px 1px 3px rgba(0,0,0,0.5); }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>TULIA HOUSE</h1>
            <div class="subtitle">☀️ Solar Energy Monitoring System</div>
        </div>
        
        <div class="card">
            <div class="timestamp"><strong>Last Updated:</strong> {latest_data.get('timestamp', 'N/A')}</div>
            
            <div class="system-status {'critical' if generator_running or backup_voltage < 51.2 else 'warning' if backup_active else 'normal'}">
                <h2>{'🚨 GENERATOR RUNNING' if generator_running else '🚨 GENERATOR STARTING' if backup_voltage < 51.2 else '⚠️ BACKUP SYSTEM ACTIVE' if backup_active else '✓ NORMAL OPERATION'}</h2>
            </div>
"""
    
    if solar_conditions:
        alert_class = "poor" if solar_conditions['poor_conditions'] else "good"
        period = solar_conditions.get('analysis_period', 'Next 10 Hours')
        html += f"""
            <div class="weather-alert {alert_class}">
                <h3>{'☁️ Poor Solar Conditions Ahead' if solar_conditions['poor_conditions'] else '☀️ Good Solar Conditions Expected'}</h3>
                <p><strong>{period}:</strong> Cloud Cover: {solar_conditions['avg_cloud_cover']:.0f}% | Solar: {solar_conditions['avg_solar_radiation']:.0f} W/m²</p>
                <p style="font-size:0.8em; margin-top:5px;">Source: {weather_debug.get('source', 'Unknown')}</p>
            </div>
"""
    else:
        html += f"""
            <div class="weather-alert error">
                <h3>⚠️ Weather Data Unavailable</h3>
                <p><strong>Status:</strong> {weather_debug['status']}</p>
                <p><strong>Error:</strong> {weather_debug['error']}</p>
                <p><strong>Last Attempt:</strong> {weather_debug['last_attempt']}</p>
                <button class="retry-btn" onclick="fetch('/refresh_weather').then(r => window.location.reload())">🔄 Force Retry (Resets Cooldown)</button>
            </div>
"""
    
    html += f"""
            <div class="metrics-grid">
                <div class="metric {primary_color}">
                    <div class="metric-label">Primary Batteries</div>
                    <div class="metric-value">{primary_battery:.0f}%</div>
                </div>
                <div class="metric {backup_voltage_color}">
                    <div class="metric-label">Backup Battery</div>
                    <div class="metric-value">{backup_voltage:.1f}V</div>
                </div>
                <div class="metric blue">
                    <div class="metric-label">Total Load</div>
                    <div class="metric-value">{total_load:.0f}W</div>
                </div>
                <div class="metric gray">
                    <div class="metric-label">Solar Input</div>
                    <div class="metric-value">{total_solar:.0f}W</div>
                </div>
                <div class="metric {'green' if generator_running else 'orange' if backup_active else 'green'}">
                    <div class="metric-label">Generator</div>
                    <div class="metric-value">{'ON' if generator_running else 'OFF'}</div>
                </div>
            </div>
            
            <h2>Inverter Details</h2>
            <div class="inverter-grid">
"""
    
    for inv in latest_data.get("inverters", []):
        cls = 'offline' if inv.get('communication_lost') else ('backup' if inv.get('Type') == 'backup' else '')
        html += f"""
                <div class="inverter-card {cls}">
                    <h3>{inv.get('Label')}</h3>
                    <p><strong>Power:</strong> {inv.get('OutputPower', 0):.0f}W</p>
                    <p><strong>Battery:</strong> {inv.get('Capacity', 0):.0f}% / {inv.get('vBat', 0):.1f}V</p>
                    <p><strong>Solar:</strong> {inv.get('ppv', 0):.0f}W</p>
                    <p><strong>Temp:</strong> {inv.get('temperature', 0):.1f}°C</p>
                </div>
"""
    
    html += """
            </div>
        </div>
        
        <div class="card">
            <div class="chart-container">
                <h2>Power Monitoring - Last 12 Hours</h2>
                <canvas id="powerChart"></canvas>
            </div>
        </div>
        
        <script>
            const ctx = document.getElementById('powerChart').getContext('2d');
            new Chart(ctx, {
                type: 'line',
                data: {
                    labels: """ + str(times) + """,
                    datasets: [
                        { label: 'Load (W)', data: """ + str(load_values) + """, borderColor: 'rgb(102, 126, 234)', fill: false },
                        { label: 'Discharge (W)', data: """ + str(battery_values) + """, borderColor: 'rgb(235, 51, 73)', fill: false }
                    ]
                },
                options: { responsive: true }
            });
            setTimeout(() => window.location.reload(), 300000);
        </script>
        
        <div class="footer">10kW Solar System • Cascade Battery Architecture • Solar-Aware Monitoring</div>
    </div>
</body>
</html>
"""
    return render_template_string(html)

Thread(target=poll_growatt, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
