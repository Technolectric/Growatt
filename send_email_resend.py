import os
import time
import requests
from datetime import datetime, timedelta, timezone
from threading import Thread
from flask import Flask, render_template_string, request, jsonify
import numpy as np
from collections import deque

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
PRIMARY_BATTERY_THRESHOLD = 40
BACKUP_VOLTAGE_THRESHOLD = 51.2
TOTAL_SOLAR_CAPACITY_KW = 10
PRIMARY_INVERTER_CAPACITY_W = 10000
BACKUP_INVERTER_CAPACITY_W = 5000

# Backup battery voltage status thresholds
BACKUP_VOLTAGE_GOOD = 53.0
BACKUP_VOLTAGE_MEDIUM = 52.3
BACKUP_VOLTAGE_LOW = 52.0

# Temperature thresholds
INVERTER_TEMP_WARNING = 60
INVERTER_TEMP_CRITICAL = 70

# Communication timeout
COMMUNICATION_TIMEOUT_MINUTES = 10

# Battery Specifications (LiFePO4)
PRIMARY_BATTERY_CAPACITY_WH = 30000
PRIMARY_BATTERY_USABLE_WH = 18000  # 60% usable (down to 40%)
BACKUP_BATTERY_CAPACITY_WH = 30000
BACKUP_BATTERY_DEGRADED_WH = 21000  # 70% remaining after 5 years
BACKUP_BATTERY_USABLE_WH = 14700  # 70% of degraded capacity

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
weather_source = "Initializing..."
solar_conditions_cache = None
alert_history = []
last_communication = {}

# Solar forecast globals
solar_forecast = []
solar_generation_pattern = deque(maxlen=48)
load_demand_pattern = deque(maxlen=48)
SOLAR_EFFICIENCY_FACTOR = 0.85
FORECAST_HOURS = 12

# East African Timezone
EAT = timezone(timedelta(hours=3))

# ----------------------------
# Multi-Source Weather Forecast Functions
# ----------------------------
def get_weather_from_openmeteo():
    """Try Open-Meteo API (Primary source)"""
    try:
        url = f"https://api.open-meteo.com/v1/forecast?latitude={LATITUDE}&longitude={LONGITUDE}&hourly=cloud_cover,shortwave_radiation&timezone=Africa/Nairobi&forecast_days=2"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        return {
            'times': data['hourly']['time'],
            'cloud_cover': data['hourly']['cloud_cover'],
            'solar_radiation': data['hourly']['shortwave_radiation'],
            'source': 'Open-Meteo'
        }
    except Exception as e:
        print(f"✗ Open-Meteo failed: {e}")
        return None

def get_weather_from_weatherapi():
    """Try WeatherAPI.com (Fallback 1)"""
    try:
        WEATHERAPI_KEY = os.getenv("WEATHERAPI_KEY", "demo")
        url = f"http://api.weatherapi.com/v1/forecast.json?key={WEATHERAPI_KEY}&q={LATITUDE},{LONGITUDE}&days=2"
        response = requests.get(url, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            times, cloud_cover, solar_radiation = [], [], []
            
            for day in data.get('forecast', {}).get('forecastday', []):
                for hour in day.get('hour', []):
                    times.append(hour['time'])
                    cloud_cover.append(hour['cloud'])
                    uv = hour.get('uv', 0)
                    solar_radiation.append(uv * 120)
            
            if times:
                return {
                    'times': times,
                    'cloud_cover': cloud_cover,
                    'solar_radiation': solar_radiation,
                    'source': 'WeatherAPI'
                }
        return None
    except Exception as e:
        print(f"✗ WeatherAPI failed: {e}")
        return None

def get_weather_from_7timer():
    """Try 7Timer.info (Fallback 2)"""
    try:
        url = f"http://www.7timer.info/bin/api.pl?lon={LONGITUDE}&lat={LATITUDE}&product=civil&output=json"
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        times, cloud_cover, solar_radiation = [], [], []
        base_time = datetime.now(EAT)
        
        for item in data.get('dataseries', [])[:48]:
            hour_offset = item.get('timepoint', 0)
            forecast_time = base_time + timedelta(hours=hour_offset)
            times.append(forecast_time.strftime('%Y-%m-%dT%H:%M'))
            
            cloud_val = item.get('cloudcover', 5)
            cloud_pct = min((cloud_val * 12), 100)
            cloud_cover.append(cloud_pct)
            
            solar_est = max(800 * (1 - cloud_pct/100), 0)
            solar_radiation.append(solar_est)
        
        if times:
            return {
                'times': times,
                'cloud_cover': cloud_cover,
                'solar_radiation': solar_radiation,
                'source': '7Timer'
            }
        return None
    except Exception as e:
        print(f"✗ 7Timer failed: {e}")
        return None

def get_fallback_weather():
    """Synthetic Clear Sky Weather if Internet Fails"""
    print("⚠️ Using Synthetic Fallback Weather")
    times, clouds, rads = [], [], []
    now = datetime.now(EAT).replace(minute=0, second=0, microsecond=0)
    
    for i in range(48):
        t = now + timedelta(hours=i)
        times.append(t.isoformat())
        clouds.append(20)
        
        h = t.hour
        if 6 <= h <= 18:
            dist = abs(12 - h)
            rads.append(max(0, 1000 - (dist * 150)))
        else:
            rads.append(0)
    
    return {
        'times': times,
        'cloud_cover': clouds,
        'solar_radiation': rads,
        'source': 'Synthetic (Offline)'
    }

def get_weather_forecast():
    """Try multiple weather sources with fallback"""
    global weather_source
    print("🌤️ Fetching weather forecast...")
    
    sources = [
        ("Open-Meteo", get_weather_from_openmeteo),
        ("WeatherAPI", get_weather_from_weatherapi),
        ("7Timer", get_weather_from_7timer)
    ]
    
    for source_name, fetch_func in sources:
        print(f"   Trying {source_name}...")
        forecast = fetch_func()
        if forecast and len(forecast.get('times', [])) > 0:
            weather_source = forecast['source']
            print(f"✓ Weather forecast from {weather_source}: {len(forecast['times'])} hours")
            return forecast
    
    print("✗ All weather sources failed, using synthetic fallback")
    weather_source = "Synthetic (Offline)"
    return get_fallback_weather()

def analyze_solar_conditions(forecast):
    """Analyze upcoming solar conditions - FIXED version"""
    if not forecast:
        print("⚠️ No forecast data for analysis")
        return None
    
    try:
        now = datetime.now(EAT)
        current_hour = now.hour
        is_nighttime = current_hour < 6 or current_hour >= 18
        
        # Determine analysis period
        if is_nighttime:
            analysis_period = "Tomorrow's Daylight"
        else:
            analysis_period = "Today's Remaining Daylight"
        
        avg_cloud_cover, avg_solar_radiation, count = 0, 0, 0
        
        for i, time_str in enumerate(forecast['times']):
            try:
                # Parse time - handle multiple formats
                if 'T' in time_str:
                    forecast_time = datetime.fromisoformat(time_str.replace('Z', ''))
                else:
                    forecast_time = datetime.strptime(time_str, '%Y-%m-%d %H:%M')
                
                if forecast_time.tzinfo is None:
                    forecast_time = forecast_time.replace(tzinfo=EAT)
                else:
                    forecast_time = forecast_time.astimezone(EAT)
                
                # Only include FUTURE daylight hours
                if forecast_time > now and 6 <= forecast_time.hour <= 18:
                    avg_cloud_cover += forecast['cloud_cover'][i]
                    avg_solar_radiation += forecast['solar_radiation'][i]
                    count += 1
                    if count >= 12:
                        break
            except Exception as parse_error:
                continue
        
        if count > 0:
            avg_cloud_cover /= count
            avg_solar_radiation /= count
            poor_conditions = avg_cloud_cover > 70 or avg_solar_radiation < 200
            
            print(f"✓ Solar analysis: {count} hours, Cloud: {avg_cloud_cover:.0f}%, Solar: {avg_solar_radiation:.0f}W/m²")
            
            return {
                'avg_cloud_cover': avg_cloud_cover,
                'avg_solar_radiation': avg_solar_radiation,
                'poor_conditions': poor_conditions,
                'hours_analyzed': count,
                'analysis_period': analysis_period,
                'is_nighttime': is_nighttime
            }
        else:
            print("⚠️ No valid forecast hours found")
    except Exception as e:
        print(f"✗ Error analyzing solar: {e}")
        import traceback
        traceback.print_exc()
    
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
# Solar Forecast Helper Functions
# ----------------------------
def analyze_historical_solar_pattern():
    """Analyze historical solar generation patterns"""
    if len(solar_generation_pattern) < 3:
        return None
    
    hour_map = {}
    for data in solar_generation_pattern:
        h = data['hour']
        if h not in hour_map:
            hour_map[h] = []
        if data['max_possible'] > 0:
            hour_map[h].append(data['generation'] / data['max_possible'])
    
    pattern = []
    for h, vals in hour_map.items():
        if vals:
            pattern.append((h, np.mean(vals)))
    
    return pattern if pattern else None

def analyze_historical_load_pattern():
    """Analyze historical load demand patterns"""
    if len(load_demand_pattern) < 3:
        return None
    
    hour_map = {}
    for data in load_demand_pattern:
        h = data['hour']
        if h not in hour_map:
            hour_map[h] = []
        hour_map[h].append(data['load'])
    
    pattern = []
    for h, vals in hour_map.items():
        if vals:
            pattern.append((h, 0, np.mean(vals)))
    
    return pattern if pattern else None

def apply_solar_curve(generation, hour_of_day):
    """Apply solar curve: 0W at night, sin² during day"""
    if hour_of_day < 6 or hour_of_day >= 19:
        return 0.0
    
    solar_hour = (hour_of_day - 6) / 13.0
    curve_factor = (np.sin(solar_hour * np.pi) ** 2)
    
    # Reduce early/late hours
    if hour_of_day <= 7 or hour_of_day >= 18:
        curve_factor *= 0.7
    
    return generation * curve_factor

def generate_solar_forecast(weather_forecast_data, historical_pattern):
    """Generate solar generation forecast"""
    forecast = []
    now = datetime.now(EAT)
    max_possible = TOTAL_SOLAR_CAPACITY_KW * 1000
    
    # Parse weather data into lookup
    weather_lookup = {}
    if weather_forecast_data:
        for i, t_str in enumerate(weather_forecast_data['times']):
            try:
                if 'T' in t_str:
                    dt = datetime.fromisoformat(t_str.replace('Z', ''))
                else:
                    dt = datetime.strptime(t_str, '%Y-%m-%d %H:%M')
                
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=EAT)
                else:
                    dt = dt.astimezone(EAT)
                
                key = (dt.day, dt.hour)
                weather_lookup[key] = {
                    'cloud_cover': weather_forecast_data['cloud_cover'][i],
                    'solar_radiation': weather_forecast_data['solar_radiation'][i]
                }
            except:
                continue
    
    # Generate forecast for next FORECAST_HOURS
    for i in range(FORECAST_HOURS):
        forecast_time = now + timedelta(hours=i)
        hour = forecast_time.hour
        day = forecast_time.day
        
        cloud_cover = 50
        theoretical_generation = 0
        
        # Get weather data
        w_data = weather_lookup.get((day, hour))
        if w_data:
            cloud_cover = w_data['cloud_cover']
            theoretical_generation = (w_data['solar_radiation'] / 1000) * max_possible * SOLAR_EFFICIENCY_FACTOR
        
        # Apply night restriction
        if hour < 6 or hour >= 19:
            blended_generation = 0.0
        else:
            curve_adjusted = apply_solar_curve(theoretical_generation, hour)
            blended_generation = curve_adjusted
            
            # Blend with historical pattern
            if historical_pattern:
                for ph, norm in historical_pattern:
                    if ph == hour:
                        blended_generation = (curve_adjusted * 0.6 + (norm * max_possible) * 0.4)
                        break
        
        forecast.append({
            'time': forecast_time,
            'hour': hour,
            'estimated_generation': max(0, blended_generation),
            'theoretical_max': theoretical_generation,
            'cloud_cover': cloud_cover
        })
    
    return forecast

def generate_load_forecast(historical_pattern):
    """Generate load demand forecast"""
    forecast = []
    now = datetime.now(EAT)
    
    for hour_offset in range(FORECAST_HOURS):
        forecast_time = now + timedelta(hours=hour_offset)
        hour = forecast_time.hour
        
        # Default load pattern
        if 18 <= hour <= 22:
            load = 2800
        elif hour < 6:
            load = 800
        else:
            load = 1500
        
        # Override with historical if available
        if historical_pattern:
            for ph, _, al in historical_pattern:
                if ph == hour:
                    load = al
                    break
        
        forecast.append({
            'time': forecast_time,
            'hour': hour,
            'estimated_load': load
        })
    
    return forecast

def calculate_battery_cascade(solar_forecast_data, load_forecast_data, current_primary_percent, backup_active=False):
    """Simulate battery cascade: Primary → Backup → Generator"""
    if not solar_forecast_data or not load_forecast_data:
        return None
    
    current_primary_wh = (current_primary_percent / 100) * PRIMARY_BATTERY_USABLE_WH
    current_backup_wh = BACKUP_BATTERY_USABLE_WH * 0.8
    
    trace_primary = [current_primary_wh / 1000]
    trace_backup = [current_backup_wh / 1000]
    trace_genset = [0]
    trace_deficit = [0]
    
    acc_genset = 0
    
    limit = min(len(solar_forecast_data), len(load_forecast_data))
    
    for i in range(limit):
        solar = solar_forecast_data[i]['estimated_generation']
        load = load_forecast_data[i]['estimated_load']
        
        net_watts = load - solar
        trace_deficit.append(max(0, net_watts))
        
        energy_step_wh = net_watts * 1.0
        
        if energy_step_wh > 0:
            # Drain primary
            if current_primary_wh >= energy_step_wh:
                current_primary_wh -= energy_step_wh
                energy_step_wh = 0
            else:
                energy_step_wh -= current_primary_wh
                current_primary_wh = 0
            
            # Drain backup
            if energy_step_wh > 0:
                if current_backup_wh >= energy_step_wh:
                    current_backup_wh -= energy_step_wh
                    energy_step_wh = 0
                else:
                    energy_step_wh -= current_backup_wh
                    current_backup_wh = 0
            
            # Generator needed
            if energy_step_wh > 0:
                acc_genset += energy_step_wh
        else:
            # Charge
            surplus = abs(energy_step_wh)
            space_p = PRIMARY_BATTERY_USABLE_WH - current_primary_wh
            
            if surplus <= space_p:
                current_primary_wh += surplus
            else:
                current_primary_wh = PRIMARY_BATTERY_USABLE_WH
                surplus -= space_p
                current_backup_wh = min(current_backup_wh + surplus, BACKUP_BATTERY_USABLE_WH)
        
        trace_primary.append(current_primary_wh / 1000)
        trace_backup.append(current_backup_wh / 1000)
        trace_genset.append(acc_genset / 1000)
    
    return {
        'primary_trace': trace_primary,
        'backup_trace': trace_backup,
        'genset_trace': trace_genset,
        'deficit_trace': trace_deficit,
        'will_need_generator': acc_genset > 0
    }

def update_solar_pattern(current_generation):
    """Update solar generation pattern with noise filtering"""
    now = datetime.now(EAT)
    h = now.hour
    
    # Filter night noise
    if h < 6 or h >= 19:
        clean_gen = 0.0
    else:
        clean_gen = current_generation
    
    max_w = TOTAL_SOLAR_CAPACITY_KW * 1000
    solar_generation_pattern.append({
        'hour': h,
        'generation': clean_gen,
        'max_possible': max_w
    })

def update_load_pattern(current_load):
    """Update load demand pattern"""
    load_demand_pattern.append({
        'hour': datetime.now(EAT).hour,
        'load': current_load
    })

# ----------------------------
# Email function
# ----------------------------
def send_email(subject, html_content, alert_type="general"):
    global last_alert_time, alert_history
    if not all([RESEND_API_KEY, SENDER_EMAIL, RECIPIENT_EMAIL]):
        print("✗ Missing email credentials")
        return False
    
    cooldown_map = {
        "critical": 60,
        "very_high_load": 30,
        "backup_active": 120,
        "high_load": 60,
        "moderate_load": 120,
        "warning": 120,
        "communication_lost": 60,
        "fault_alarm": 30,
        "high_temperature": 60,
        "test": 0,
        "general": 120
    }
    
    cooldown_minutes = cooldown_map.get(alert_type, 120)
    
    if alert_type in last_alert_time and cooldown_minutes > 0:
        if datetime.now(EAT) - last_alert_time[alert_type] < timedelta(minutes=cooldown_minutes):
            print(f"⚠️ Alert cooldown active for {alert_type}")
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
            now = datetime.now(EAT)
            print(f"✓ Email sent: {subject}")
            last_alert_time[alert_type] = now
            
            alert_history.append({
                "timestamp": now,
                "type": alert_type,
                "subject": subject
            })
            
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
    """Smart alert logic"""
    inv1 = next((i for i in inverter_data if i['SN'] == 'RKG3B0400T'), None)
    inv2 = next((i for i in inverter_data if i['SN'] == 'KAM4N5W0AG'), None)
    inv3_backup = next((i for i in inverter_data if i['SN'] == 'JNK1CDR0KQ'), None)
    
    if not all([inv1, inv2, inv3_backup]):
        return
    
    primary_capacity = min(inv1['Capacity'], inv2['Capacity'])
    backup_voltage = inv3_backup['vBat']
    backup_active = inv3_backup['OutputPower'] > 50
    total_load = inv1['OutputPower'] + inv2['OutputPower'] + inv3_backup['OutputPower']
    
    # Communication loss alerts
    for inv in inverter_data:
        if inv.get('communication_lost', False):
            send_email(
                subject=f"⚠️ Communication Lost: {inv['Label']}",
                html_content=f"<p>No data from {inv['Label']} for over 10 minutes.</p>",
                alert_type="communication_lost"
            )
    
    # Fault alerts
    for inv in inverter_data:
        if inv.get('has_fault', False):
            send_email(
                subject=f"🚨 FAULT: {inv['Label']}",
                html_content=f"<p>Inverter {inv['Label']} has reported a fault.</p>",
                alert_type="fault_alarm"
            )
    
    # Temperature alerts
    for inv in inverter_data:
        if inv.get('high_temperature', False):
            temp = inv.get('temperature', 0)
            send_email(
                subject=f"🌡️ HIGH TEMP: {inv['Label']}",
                html_content=f"<p>{inv['Label']} temperature: {temp:.1f}°C</p>",
                alert_type="high_temperature"
            )
    
    # Critical: Generator
    if generator_running or backup_voltage < BACKUP_VOLTAGE_THRESHOLD:
        send_email(
            subject="🚨 CRITICAL: Generator Running",
            html_content=f"<p>Backup voltage: {backup_voltage:.1f}V. Generator {'running' if generator_running else 'should start'}.</p>",
            alert_type="critical"
        )
        return
    
    # Backup active
    if backup_active and primary_capacity < PRIMARY_BATTERY_THRESHOLD:
        send_email(
            subject="⚠️ Backup Active",
            html_content=f"<p>Backup supplying power. Primary: {primary_capacity:.0f}%</p>",
            alert_type="backup_active"
        )
        return
    
    # Battery warnings
    if 40 < primary_capacity < 50:
        send_email(
            subject="⚠️ Primary Battery Low",
            html_content=f"<p>Primary batteries: {primary_capacity:.0f}%</p>",
            alert_type="warning"
        )
    
    # Solar-aware discharge alerts
    if total_battery_discharge >= 2500:
        send_email(
            subject="🚨 Very High Battery Discharge",
            html_content=f"<p>Battery discharge: {total_battery_discharge:.0f}W</p>",
            alert_type="very_high_load"
        )
    elif 1500 <= total_battery_discharge < 2500:
        send_email(
            subject="⚠️ High Battery Discharge",
            html_content=f"<p>Battery discharge: {total_battery_discharge:.0f}W</p>",
            alert_type="high_load"
        )
    elif 1000 <= total_battery_discharge < 1500 and primary_capacity < 50:
        send_email(
            subject="ℹ️ Moderate Discharge",
            html_content=f"<p>Battery discharge: {total_battery_discharge:.0f}W</p>",
            alert_type="moderate_load"
        )

# ----------------------------
# Growatt Polling Loop
# ----------------------------
def poll_growatt():
    global latest_data, load_history, battery_history, weather_forecast, last_communication, solar_conditions_cache, solar_forecast
    
    print("🌤️ Fetching initial weather forecast...")
    weather_forecast = get_weather_forecast()
    if weather_forecast:
        solar_conditions_cache = analyze_solar_conditions(weather_forecast)
    last_weather_update = datetime.now(EAT)
    
    while True:
        try:
            # Update weather every 30 minutes
            if datetime.now(EAT) - last_weather_update > timedelta(minutes=30):
                weather_forecast = get_weather_forecast()
                if weather_forecast:
                    solar_conditions_cache = analyze_solar_conditions(weather_forecast)
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
                    ppv = float(data.get("ppv") or 0)
                    ppv2 = float(data.get("ppv2") or 0)
                    
                    inv_temp = float(data.get("invTemperature") or 0)
                    dcdc_temp = float(data.get("dcDcTemperature") or 0)
                    temp = max(inv_temp, dcdc_temp, float(data.get("temperature") or 0))
                    
                    error_code = int(data.get("errorCode") or 0)
                    fault_code = int(data.get("faultCode") or 0)
                    warn_code = int(data.get("warnCode") or 0)
                    has_fault = error_code != 0 or fault_code != 0 or warn_code != 0
                    
                    vac = float(data.get("vac") or 0)
                    pac_input = float(data.get("pAcInPut") or 0)
                    
                    total_output_power += out_power
                    total_solar_input_W += (ppv + ppv2)
                    
                    if p_bat > 0:
                        total_battery_discharge_W += p_bat
                    
                    inv_info = {
                        "SN": sn,
                        "Label": config['label'],
                        "Type": config['type'],
                        "DataLog": config['datalog'],
                        "DisplayOrder": config['display_order'],
                        "OutputPower": out_power,
                        "Capacity": capacity,
                        "vBat": v_bat,
                        "pBat": p_bat,
                        "ppv": ppv,
                        "ppv2": ppv2,
                        "temperature": temp,
                        "invTemperature": inv_temp,
                        "dcDcTemperature": dcdc_temp,
                        "high_temperature": temp >= INVERTER_TEMP_WARNING,
                        "Status": data.get("statusText", "Unknown"),
                        "has_fault": has_fault,
                        "fault_info": {"errorCode": error_code, "faultCode": fault_code, "warnCode": warn_code},
                        "vac": vac,
                        "pAcInput": pac_input,
                        "communication_lost": False,
                        "last_seen": now.strftime("%Y-%m-%d %H:%M:%S")
                    }
                    
                    inverter_data.append(inv_info)
                    
                    if config['type'] == 'primary':
                        if capacity > 0:
                            primary_capacities.append(capacity)
                    elif config['type'] == 'backup':
                        backup_data = inv_info
                        if vac > 100 or pac_input > 50:
                            generator_running = True
                
                except Exception as e:
                    print(f"❌ Error polling {sn}: {e}")
                    if sn in last_communication:
                        if now - last_communication[sn] > timedelta(minutes=COMMUNICATION_TIMEOUT_MINUTES):
                            config = INVERTER_CONFIG.get(sn, {})
                            inverter_data.append({
                                "SN": sn,
                                "Label": config.get('label', sn),
                                "Type": config.get('type', 'unknown'),
                                "DisplayOrder": config.get('display_order', 99),
                                "communication_lost": True,
                                "last_seen": last_communication[sn].strftime("%Y-%m-%d %H:%M:%S")
                            })
            
            inverter_data.sort(key=lambda x: x.get('DisplayOrder', 99))
            
            # Update patterns
            update_solar_pattern(total_solar_input_W)
            update_load_pattern(total_output_power)
            
            # Analyze patterns
            solar_pattern = analyze_historical_solar_pattern()
            load_pattern = analyze_historical_load_pattern()
            
            # Generate forecasts
            solar_forecast = generate_solar_forecast(weather_forecast, solar_pattern)
            load_forecast = generate_load_forecast(load_pattern)
            
            # Calculate metrics
            primary_battery_min = min(primary_capacities) if primary_capacities else 0
            backup_battery_voltage = backup_data['vBat'] if backup_data else 0
            backup_voltage_status, backup_voltage_color = get_backup_voltage_status(backup_battery_voltage)
            backup_active = backup_data['OutputPower'] > 50 if backup_data else False
            
            # Battery cascade prediction
            battery_life_prediction = calculate_battery_cascade(
                solar_forecast,
                load_forecast,
                primary_battery_min,
                backup_active
            )
            
            # Save data
            latest_data = {
                "timestamp": now.strftime("%Y-%m-%d %H:%M:%S EAT"),
                "total_output_power": total_output_power,
                "total_battery_discharge_W": total_battery_discharge_W,
                "total_solar_input_W": total_solar_input_W,
                "primary_battery_min": primary_battery_min,
                "backup_battery_voltage": backup_battery_voltage,
                "backup_voltage_status": backup_voltage_status,
                "backup_voltage_color": backup_voltage_color,
                "backup_active": backup_active,
                "generator_running": generator_running,
                "inverters": inverter_data,
                "solar_forecast": solar_forecast,
                "load_forecast": load_forecast,
                "battery_life_prediction": battery_life_prediction,
                "historical_pattern_count": len(solar_generation_pattern),
                "load_pattern_count": len(load_demand_pattern)
            }
            
            load_history.append((now, total_output_power))
            load_history = [(t, p) for t, p in load_history if t >= now - timedelta(hours=12)]
            
            battery_history.append((now, total_battery_discharge_W))
            battery_history = [(t, p) for t, p in battery_history if t >= now - timedelta(hours=12)]
            
            print(f"{latest_data['timestamp']} | Load={total_output_power:.0f}W | Solar={total_solar_input_W:.0f}W | Discharge={total_battery_discharge_W:.0f}W | Primary={primary_battery_min:.0f}% | Backup={backup_battery_voltage:.1f}V | Gen={'ON' if generator_running else 'OFF'}")
            
            check_and_send_alerts(inverter_data, solar_conditions_cache, total_solar_input_W, total_battery_discharge_W, generator_running)
        
        except Exception as e:
            print(f"❌ Error in polling: {e}")
            import traceback
            traceback.print_exc()
        
        time.sleep(POLL_INTERVAL_MINUTES * 60)

# ----------------------------
# Flask Route - COMPLETE DASHBOARD
# ----------------------------
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
    
    solar_forecast_data = latest_data.get("solar_forecast", [])
    load_forecast_data = latest_data.get("load_forecast", [])
    battery_life_prediction = latest_data.get("battery_life_prediction")
    historical_pattern_count = latest_data.get("historical_pattern_count", 0)
    load_pattern_count = latest_data.get("load_pattern_count", 0)
    
    forecast_times = []
    if solar_forecast_data:
        forecast_times = [sf['time'].strftime('%H:%M') for sf in solar_forecast_data]
    
    sim_times, trace_primary, trace_backup, trace_genset, trace_deficit = [], [], [], [], []
    pred_class, pred_message = "good", "Initializing..."
    
    if battery_life_prediction:
        sim_times = ["Now"] + forecast_times
        trace_primary = battery_life_prediction.get('primary_trace', [])
        trace_backup = battery_life_prediction.get('backup_trace', [])
        trace_genset = battery_life_prediction.get('genset_trace', [])
        trace_deficit = battery_life_prediction.get('deficit_trace', [])
        
        if battery_life_prediction.get('will_need_generator'):
            pred_class = "critical"
            pred_message = "🚨 CRITICAL: Generator will be needed!"
        elif trace_primary and min(trace_primary) <= 0:
            pred_class = "warning"
            pred_message = "⚠️ WARNING: Primary battery will deplete."
        else:
            pred_class = "good"
            pred_message = "✓ OK: Battery sufficient for forecast period."
    
    solar_conditions = solar_conditions_cache
    
    html = f"""
<!DOCTYPE html>
<html>
<head>
    <title>Tulia House - Solar Monitor</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <meta http-equiv="refresh" content="300">
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ 
            font-family: 'Segoe UI', Tahoma, sans-serif;
            background: linear-gradient(rgba(0,0,0,0.4), rgba(0,0,0,0.6)), 
                        url('https://images.unsplash.com/photo-1582268611958-ebfd161ef9cf?w=1600&q=80') center/cover fixed;
            min-height: 100vh; padding: 20px;
        }}
        .header {{ text-align: center; color: white; margin-bottom: 30px; text-shadow: 2px 2px 4px rgba(0,0,0,0.7); }}
        .header h1 {{ font-size: 2.5em; font-weight: 300; letter-spacing: 2px; }}
        .header .subtitle {{ font-size: 1.1em; opacity: 0.9; }}
        .header .specs {{ font-size: 0.9em; opacity: 0.8; margin-top: 10px; }}
        .container {{ max-width: 1400px; margin: 0 auto; }}
        .card {{ background: rgba(255,255,255,0.95); backdrop-filter: blur(10px); padding: 25px; border-radius: 15px; 
                 box-shadow: 0 8px 32px rgba(0,0,0,0.3); margin-bottom: 20px; }}
        .timestamp {{ text-align: center; color: #666; font-size: 0.95em; margin-bottom: 20px; }}
        .system-status {{ color: white; padding: 20px; border-radius: 10px; margin-bottom: 20px; text-align: center; }}
        .system-status.normal {{ background: linear-gradient(135deg, #11998e 0%, #38ef7d 100%); }}
        .system-status.warning {{ background: linear-gradient(135deg, #f77f00 0%, #fcbf49 100%); }}
        .system-status.critical {{ background: linear-gradient(135deg, #dc3545 0%, #f45c43 100%); }}
        .system-status h2 {{ margin-bottom: 10px; font-size: 1.5em; }}
        .weather-alert, .battery-prediction {{ padding: 20px; border-radius: 10px; margin-bottom: 20px; color: white; }}
        .weather-alert.good, .battery-prediction.good {{ background: linear-gradient(135deg, #11998e 0%, #38ef7d 100%); }}
        .weather-alert.poor, .battery-prediction.warning {{ background: linear-gradient(135deg, #f77f00 0%, #fcbf49 100%); }}
        .battery-prediction.critical {{ background: linear-gradient(135deg, #dc3545 0%, #f45c43 100%); }}
        h2 {{ color: #333; margin-bottom: 20px; font-size: 1.5em; font-weight: 500; }}
        .metrics-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 15px; margin-bottom: 30px; }}
        .metric {{ padding: 20px; border-radius: 10px; color: white; box-shadow: 0 4px 15px rgba(0,0,0,0.2); transition: transform 0.2s; }}
        .metric:hover {{ transform: translateY(-5px); }}
        .metric.green {{ background: linear-gradient(135deg, #11998e 0%, #38ef7d 100%); }}
        .metric.orange {{ background: linear-gradient(135deg, #f77f00 0%, #fcbf49 100%); }}
        .metric.red {{ background: linear-gradient(135deg, #dc3545 0%, #f45c43 100%); }}
        .metric.blue {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); }}
        .metric.gray {{ background: linear-gradient(135deg, #757F9A 0%, #D7DDE8 100%); }}
        .metric-label {{ font-size: 0.85em; opacity: 0.9; margin-bottom: 8px; }}
        .metric-value {{ font-size: 1.8em; font-weight: bold; }}
        .metric-subtext {{ font-size: 0.75em; opacity: 0.8; margin-top: 5px; }}
        .inverter-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; margin: 20px 0; }}
        .inverter-card {{ background: white; padding: 20px; border-radius: 10px; border-left: 5px solid #667eea; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
        .inverter-card.backup {{ border-left-color: #f77f00; background: #fff9f0; }}
        .inverter-card.offline {{ border-left-color: #dc3545; background: #fff0f0; }}
        .inverter-card h3 {{ color: #333; margin-bottom: 10px; font-size: 1.4em; font-weight: 600; }}
        .inverter-card .inv-label {{ font-size: 0.85em; color: #666; margin-bottom: 15px; padding: 8px; 
                                     background: #f5f5f5; border-radius: 5px; font-family: monospace; }}
        .inverter-card .inv-stat {{ display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #eee; }}
        .inverter-card .inv-stat:last-child {{ border-bottom: none; }}
        .inverter-card .inv-stat-label {{ color: #666; }}
        .inverter-card .inv-stat-value {{ font-weight: bold; color: #333; }}
        .temp-warning {{ color: #ff9800; font-weight: bold; }}
        .temp-critical {{ color: #dc3545; font-weight: bold; }}
        .chart-container {{ margin: 30px 0; background: white; padding: 20px; border-radius: 10px; }}
        canvas {{ max-height: 400px; }}
        .footer {{ text-align: center; color: white; margin-top: 30px; font-size: 0.9em; text-shadow: 1px 1px 3px rgba(0,0,0,0.5); }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>TULIA HOUSE</h1>
            <div class="subtitle">☀️ Solar Energy Monitoring System with AI Forecasting</div>
            <div class="specs">📍 Champagne Ridge, Kajiado | 🔆 10kW Solar | 🔋 30kWh Primary + 21kWh Backup (5yo)</div>
        </div>
        
        <div class="card">
            <div class="timestamp"><strong>Last Updated:</strong> {latest_data.get('timestamp', 'N/A')}</div>
            
            <div class="system-status {'critical' if generator_running else 'warning' if backup_active else 'normal'}">
                <h2>{'🚨 GENERATOR RUNNING' if generator_running else '⚠️ BACKUP ACTIVE' if backup_active else '✓ NORMAL OPERATION'}</h2>
                <div class="status-text">
                    {'Generator running. Critical power situation.' if generator_running else 
                     f'Backup supplying {latest_data.get("inverters", [{}])[2].get("OutputPower", 0) if len(latest_data.get("inverters", [])) > 2 else 0:.0f}W.' if backup_active else
                     'All systems on primary batteries.'}
                </div>
            </div>
"""
    
    # Weather forecast
    if solar_conditions:
        alert_class = "poor" if solar_conditions['poor_conditions'] else "good"
        period = solar_conditions.get('analysis_period', 'Next Hours')
        is_night = solar_conditions.get('is_nighttime', False)
        
        html += f"""
            <div class="weather-alert {alert_class}">
                <h3>{'☁️ Poor Solar Conditions' if solar_conditions['poor_conditions'] else '☀️ Good Solar Expected'}</h3>
                <p><strong>{period}:</strong> Cloud: {solar_conditions['avg_cloud_cover']:.0f}% | Solar: {solar_conditions['avg_solar_radiation']:.0f} W/m²</p>
                {'<p>⚠️ Limited recharge expected.</p>' if solar_conditions['poor_conditions'] else '<p>✓ Good recharge expected.</p>'}
                {f'<p style="font-size:0.9em;opacity:0.8">🌙 Nighttime - analyzing tomorrow</p>' if is_night else ''}
                <p style="font-size:0.8em;opacity:0.7;margin-top:10px">📡 {weather_source}</p>
            </div>
"""
    else:
        html += f"""
            <div class="weather-alert poor">
                <h3>🌤️ Weather Forecast</h3>
                <p><strong>Status:</strong> {weather_source}</p>
                <p>Weather data loading... Trying multiple sources.</p>
            </div>
"""
    
    # Battery prediction
    html += f"""
            <div class="battery-prediction {pred_class}">
                <h3>🔋 Battery Life Prediction (Next {FORECAST_HOURS} Hours)</h3>
                <p><strong>{pred_message}</strong></p>
                <p><strong>Primary:</strong> {primary_battery:.0f}% = {(primary_battery/100)*18:.1f}kWh of 18kWh usable</p>
                <p><strong>Backup:</strong> {BACKUP_BATTERY_USABLE_WH/1000:.0f}kWh available (5yo LiFePO4)</p>
                <p style="font-size:0.85em;opacity:0.8">Based on {historical_pattern_count} solar + {load_pattern_count} load patterns</p>
            </div>
            
            <div class="metrics-grid">
                <div class="metric {primary_color}">
                    <div class="metric-label">Primary Batteries</div>
                    <div class="metric-value">{primary_battery:.0f}%</div>
                    <div class="metric-subtext">{(primary_battery/100)*18:.1f}kWh of 18kWh</div>
                </div>
                <div class="metric {backup_voltage_color}">
                    <div class="metric-label">Backup Battery</div>
                    <div class="metric-value">{backup_voltage:.1f}V</div>
                    <div class="metric-subtext">{backup_voltage_status} | 14.7kWh capacity</div>
                </div>
                <div class="metric blue">
                    <div class="metric-label">Total Load</div>
                    <div class="metric-value">{total_load:.0f}W</div>
                    <div class="metric-subtext">All Inverters</div>
                </div>
                <div class="metric gray">
                    <div class="metric-label">Solar Input</div>
                    <div class="metric-value">{total_solar:.0f}W</div>
                    <div class="metric-subtext">Current Generation</div>
                </div>
                <div class="metric {'red' if total_battery_discharge > 2000 else 'orange' if total_battery_discharge > 1000 else 'green'}">
                    <div class="metric-label">Battery Discharge</div>
                    <div class="metric-value">{total_battery_discharge:.0f}W</div>
                    <div class="metric-subtext">From Battery</div>
                </div>
                <div class="metric {'red' if generator_running else 'green'}">
                    <div class="metric-label">Generator</div>
                    <div class="metric-value">{'ON' if generator_running else 'OFF'}</div>
                    <div class="metric-subtext">{'Running' if generator_running else 'Standby'}</div>
                </div>
            </div>
            
            <h2>Inverter Details</h2>
            <div class="inverter-grid">
"""
    
    # Inverters
    for inv in latest_data.get("inverters", []):
        is_backup = inv.get('Type') == 'backup'
        is_offline = inv.get('communication_lost', False)
        card_class = 'offline' if is_offline else ('backup' if is_backup else '')
        
        html += f"""
                <div class="inverter-card {card_class}">
                    <h3>{inv.get('Label', 'Unknown')}</h3>
                    <div class="inv-label">{inv.get('SN', 'N/A')} ({inv.get('DataLog', 'N/A')})</div>
"""
        
        if is_offline:
            html += f"""
                    <div style="color:#dc3545;padding:10px;background:#fff0f0;border-radius:5px">
                        <strong>⚠️ COMMUNICATION LOST</strong><br>Last seen: {inv.get('last_seen', 'Unknown')}
                    </div>
"""
        else:
            temp = inv.get('temperature', 0)
            temp_class = 'temp-critical' if temp >= INVERTER_TEMP_CRITICAL else ('temp-warning' if temp >= INVERTER_TEMP_WARNING else '')
            
            html += f"""
                    <div class="inv-stat">
                        <span class="inv-stat-label">Battery {'Voltage' if is_backup else 'Capacity'}</span>
                        <span class="inv-stat-value">{'%.1fV' % inv.get('vBat', 0) if is_backup else '%.0f%%' % inv.get('Capacity', 0)}</span>
                    </div>
"""
            if not is_backup:
                html += f"""
                    <div class="inv-stat">
                        <span class="inv-stat-label">Voltage</span>
                        <span class="inv-stat-value">{inv.get('vBat', 0):.1f}V</span>
                    </div>
"""
            
            html += f"""
                    <div class="inv-stat">
                        <span class="inv-stat-label">Output Power</span>
                        <span class="inv-stat-value">{inv.get('OutputPower', 0):.0f}W</span>
                    </div>
                    <div class="inv-stat">
                        <span class="inv-stat-label">Solar Input</span>
                        <span class="inv-stat-value">{(inv.get('ppv', 0) + inv.get('ppv2', 0)):.0f}W</span>
                    </div>
                    <div class="inv-stat">
                        <span class="inv-stat-label">Temperature</span>
                        <span class="inv-stat-value {temp_class}">{temp:.1f}°C</span>
                    </div>
"""
            
            if inv.get('has_fault'):
                html += """
                    <div style="color:#dc3545;padding:10px;background:#fff0f0;border-radius:5px;margin-top:10px">
                        <strong>⚠️ FAULT DETECTED</strong>
                    </div>
"""
        
        html += """
                </div>
"""
    
    html += """
            </div>
        </div>
"""
    
    # Cascade chart
    if sim_times and trace_primary:
        html += f"""
        <div class="card">
            <div class="chart-container">
                <h2>🔋 Battery Cascade Prediction - Next {FORECAST_HOURS} Hours</h2>
                <p style="color:#666;margin-bottom:15px;font-size:0.95em">
                    <strong>Simulation:</strong> Primary → Backup → Generator. Gray bars = Power Deficit (Load-Solar).
                </p>
                <canvas id="cascadeChart"></canvas>
            </div>
            <script>
                new Chart(document.getElementById('cascadeChart'), {{
                    type: 'line',
                    data: {{
                        labels: {sim_times},
                        datasets: [
                            {{ type: 'bar', label: 'Deficit (W)', data: {trace_deficit}, backgroundColor: 'rgba(100,100,100,0.2)', 
                               yAxisID: 'y_power', order: 4 }},
                            {{ label: 'Generator (kWh)', data: {trace_genset}, backgroundColor: 'rgba(231,76,60,0.6)', 
                               borderColor: '#c0392b', fill: true, yAxisID: 'y_energy', order: 1 }},
                            {{ label: 'Backup (kWh)', data: {trace_backup}, backgroundColor: 'rgba(230,126,34,0.5)', 
                               borderColor: '#d35400', fill: true, yAxisID: 'y_energy', order: 2 }},
                            {{ label: 'Primary (kWh)', data: {trace_primary}, backgroundColor: 'rgba(46,204,113,0.5)', 
                               borderColor: '#27ae60', fill: true, yAxisID: 'y_energy', order: 3 }}
                        ]
                    }},
                    options: {{
                        responsive: true,
                        interaction: {{ mode: 'index', intersect: false }},
                        scales: {{
                            y_energy: {{ type: 'linear', position: 'left', title: {{ display: true, text: 'Energy (kWh)' }}, 
                                         beginAtZero: true, suggestedMax: 30 }},
                            y_power: {{ type: 'linear', position: 'right', title: {{ display: true, text: 'Deficit (W)' }}, 
                                       grid: {{ drawOnChartArea: false }} }}
                        }}
                    }}
                }});
            </script>
        </div>
"""
    
    # Historical chart
    html += f"""
        <div class="card">
            <div class="chart-container">
                <h2>Power Monitoring - Last 12 Hours</h2>
                <canvas id="powerChart"></canvas>
            </div>
            <script>
                new Chart(document.getElementById('powerChart'), {{
                    type: 'line',
                    data: {{
                        labels: {times},
                        datasets: [
                            {{ label: 'Total Load (W)', data: {load_values}, borderColor: 'rgb(102,126,234)', 
                               backgroundColor: 'rgba(102,126,234,0.1)', borderWidth: 3, tension: 0.4, fill: true }},
                            {{ label: 'Battery Discharge (W)', data: {battery_values}, borderColor: 'rgb(235,51,73)', 
                               backgroundColor: 'rgba(235,51,73,0.1)', borderWidth: 3, tension: 0.4, fill: true }}
                        ]
                    }},
                    options: {{
                        responsive: true,
                        interaction: {{ mode: 'index', intersect: false }},
                        scales: {{ y: {{ title: {{ display: true, text: 'Power (W)' }} }} }}
                    }}
                }});
            </script>
        </div>
        
        <div class="footer">
            10kW Solar • 30kWh Primary (18kWh usable) • 21kWh Backup (5yo) • LiFePO4 • AI Forecasting • Managed by YourHost
        </div>
    </div>
</body>
</html>
"""
    
    return render_template_string(html)

# ----------------------------
# Start
# ----------------------------
Thread(target=poll_growatt, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
