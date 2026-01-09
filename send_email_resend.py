import os
import time
import requests
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

# Tiered Load Alert System (SOLAR-AWARE - only alerts on battery discharge)
# TIER 1: Moderate battery discharge (1000-1500W) + Low Battery - 120 min cooldown
# TIER 2: High battery discharge (1500-2500W) - 60 min cooldown
# TIER 3: Very High battery discharge (>2500W) - 30 min cooldown
# Note: Alerts only if power is coming from BATTERY, not if solar is covering load

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
weather_source = "Initializing..."  # Track which weather service is working
solar_conditions_cache = None  # Cache analyzed solar conditions
alert_history = []
last_communication = {}  # Track last successful communication per inverter

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
    """Try WeatherAPI.com (Fallback 1 - uses demo key)"""
    try:
        # Using lat,lon format which works without API key for basic forecast
       WEATHERAPI_KEY = os.getenv("WEATHERAPI_KEY")  # put your key in environment variable
url = f"http://api.weatherapi.com/v1/forecast.json?key={WEATHERAPI_KEY}&q={LATITUDE},{LONGITUDE}&days=2"
        response = requests.get(url, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            
            times = []
            cloud_cover = []
            solar_radiation = []
            
            for day in data.get('forecast', {}).get('forecastday', []):
                for hour in day.get('hour', []):
                    times.append(hour['time'])
                    cloud_cover.append(hour['cloud'])
                    # Estimate solar from UV index
                    uv = hour.get('uv', 0)
                    solar_radiation.append(uv * 100)
            
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
    """Try 7Timer.info (Fallback 2 - always free, no key)"""
    try:
        url = f"http://www.7timer.info/bin/api.pl?lon={LONGITUDE}&lat={LATITUDE}&product=civil&output=json"
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        times = []
        cloud_cover = []
        solar_radiation = []
        
        base_time = datetime.now(EAT)
        
        for item in data.get('dataseries', [])[:48]:
            hour_offset = item.get('timepoint', 0)
            forecast_time = base_time + timedelta(hours=hour_offset)
            times.append(forecast_time.strftime('%Y-%m-%dT%H:%M'))
            
            # Cloud cover: 1-9 scale
            cloud_val = item.get('cloudcover', 5)
            cloud_pct = min((cloud_val * 12), 100)
            cloud_cover.append(cloud_pct)
            
            # Estimate solar
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

def get_weather_forecast():
    """Try multiple weather sources with fallback"""
    global weather_source
    
    print("🌤️ Fetching weather forecast...")
    
    # Try sources in order
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
    
    print("✗ All weather sources failed")
    weather_source = "All sources failed"
    return None

def analyze_solar_conditions(forecast):
    """Analyze upcoming solar conditions - smart daytime-only analysis"""
    if not forecast:
        print("⚠️ analyze_solar_conditions: No forecast data")
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
                # Handle multiple time formats from different weather sources
                if 'T' in time_str:
                    # ISO format: 2026-01-09T21:00 or 2026-01-09T21:00:00Z
                    forecast_time = datetime.fromisoformat(time_str.replace('Z', ''))
                else:
                    # Other formats
                    forecast_time = datetime.strptime(time_str, '%Y-%m-%d %H:%M')
                
                # Ensure timezone aware
                if forecast_time.tzinfo is None:
                    forecast_time = forecast_time.replace(tzinfo=EAT)
                else:
                    forecast_time = forecast_time.astimezone(EAT)
                
                if start_time <= forecast_time <= end_time:
                    hour = forecast_time.hour
                    if 6 <= hour <= 18:
                        avg_cloud_cover += forecast['cloud_cover'][i]
                        avg_solar_radiation += forecast['solar_radiation'][i]
                        count += 1
            except Exception as parse_error:
                # Skip this time entry if parsing fails
                continue
        
        if count > 0:
            avg_cloud_cover /= count
            avg_solar_radiation /= count
            
            poor_conditions = avg_cloud_cover > 70 or avg_solar_radiation < 200
            
            print(f"✓ Solar analysis: {count} hours analyzed, Cloud: {avg_cloud_cover:.0f}%, Solar: {avg_solar_radiation:.0f}W/m²")
            
            return {
                'avg_cloud_cover': avg_cloud_cover,
                'avg_solar_radiation': avg_solar_radiation,
                'poor_conditions': poor_conditions,
                'hours_analyzed': count,
                'analysis_period': analysis_label,
                'is_nighttime': is_nighttime
            }
        else:
            print(f"⚠️ Solar analysis: No valid hours found in forecast window")
    except Exception as e:
        print(f"✗ Error analyzing solar conditions: {e}")
        import traceback
        traceback.print_exc()
    
    return None

# ----------------------------
# Helper Functions
# ----------------------------
def get_backup_voltage_status(voltage):
    """Get backup battery status based on voltage"""
    if voltage >= BACKUP_VOLTAGE_GOOD:
        return "Good", "green"
    elif voltage >= BACKUP_VOLTAGE_MEDIUM:
        return "Medium", "orange"
    else:
        return "Low", "red"

def check_generator_running(backup_inverter_data):
    """Check if generator is running based on input voltage"""
    if not backup_inverter_data:
        return False
    
    # Generator is running if there's AC input to the backup inverter
    v_ac_input = float(backup_inverter_data.get('vac', 0) or 0)
    p_ac_input = float(backup_inverter_data.get('pAcInPut', 0) or 0)
    
    # If there's voltage or power on AC input, generator is running
    return v_ac_input > 100 or p_ac_input > 50

# ----------------------------
# Email function with alert history tracking
# ----------------------------
def send_email(subject, html_content, alert_type="general"):
    global last_alert_time, alert_history
    if not all([RESEND_API_KEY, SENDER_EMAIL, RECIPIENT_EMAIL]):
        print("✗ Error: Missing email credentials in env")
        return False
    
    # Increased cooldowns to reduce email frequency
    cooldown_map = {
        "critical": 60,           # Generator starting
        "very_high_load": 30,     # >2500W from battery
        "backup_active": 120,     # Backup supplying power
        "high_load": 60,          # 1500-2500W from battery
        "moderate_load": 120,     # 1000-1500W with low battery
        "warning": 120,           # Primary battery warning
        "communication_lost": 60, # Inverter offline
        "fault_alarm": 30,        # Inverter fault
        "high_temperature": 60,   # High temperature
        "test": 0,                # Test alerts
        "general": 120            # Default
    }
    
    cooldown_minutes = cooldown_map.get(alert_type, 120)
    
    if alert_type in last_alert_time and cooldown_minutes > 0:
        if datetime.now(EAT) - last_alert_time[alert_type] < timedelta(minutes=cooldown_minutes):
            print(f"⚠️ Alert cooldown active for {alert_type} ({cooldown_minutes} min)")
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
            
            # Add to alert history
            alert_history.append({
                "timestamp": now,
                "type": alert_type,
                "subject": subject
            })
            
            # Keep only last 12 hours
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
# Intelligent Alert Logic
# ----------------------------
def check_and_send_alerts(inverter_data, solar_conditions, total_solar_input, total_battery_discharge, generator_running):
    """Smart alert logic - solar-aware, temperature monitoring, fault detection"""
    
    # Extract data for each inverter
    inv1 = next((i for i in inverter_data if i['SN'] == 'RKG3B0400T'), None)
    inv2 = next((i for i in inverter_data if i['SN'] == 'KAM4N5W0AG'), None)
    inv3_backup = next((i for i in inverter_data if i['SN'] == 'JNK1CDR0KQ'), None)
    
    if not all([inv1, inv2, inv3_backup]):
        print("⚠️ Not all inverters reporting data")
        return
    
    # Calculate system metrics
    primary_capacity = min(inv1['Capacity'], inv2['Capacity'])
    backup_voltage = inv3_backup['vBat']
    backup_active = inv3_backup['OutputPower'] > 50
    total_load = inv1['OutputPower'] + inv2['OutputPower'] + inv3_backup['OutputPower']
    
    # ============================================
    # COMMUNICATION LOSS ALERTS
    # ============================================
    for inv in inverter_data:
        if inv.get('communication_lost', False):
            send_email(
                subject=f"⚠️ Communication Lost: {inv['Label']}",
                html_content=f"""
                <div style="font-family: Arial; padding: 20px; background: #fff3cd; border-left: 5px solid #ff9800;">
                    <h2 style="color: #ff9800;">⚠️ INVERTER COMMUNICATION LOST</h2>
                    <p><strong>Inverter:</strong> {inv['Label']} ({inv['SN']})</p>
                    <p><strong>Last Seen:</strong> {inv.get('last_seen', 'Unknown')}</p>
                    <p>No data received from this inverter for over 10 minutes.</p>
                    <p><strong>Action Required:</strong> Check inverter and network connection.</p>
                </div>
                """,
                alert_type="communication_lost"
            )
    
    # ============================================
    # FAULT/ERROR ALERTS
    # ============================================
    for inv in inverter_data:
        if inv.get('has_fault', False):
            fault_info = inv.get('fault_info', {})
            send_email(
                subject=f"🚨 FAULT ALARM: {inv['Label']}",
                html_content=f"""
                <div style="font-family: Arial; padding: 20px; background: #fff3cd; border-left: 5px solid #dc3545;">
                    <h2 style="color: #dc3545;">🚨 INVERTER FAULT DETECTED</h2>
                    <p><strong>Inverter:</strong> {inv['Label']} ({inv['SN']})</p>
                    <p><strong>Error Code:</strong> {fault_info.get('errorCode', 0)}</p>
                    <p><strong>Fault Code:</strong> {fault_info.get('faultCode', 0)}</p>
                    <p><strong>Warning Code:</strong> {fault_info.get('warnCode', 0)}</p>
                    <p><strong>Status:</strong> {inv.get('Status', 'Unknown')}</p>
                    <p><strong>⚠️ IMMEDIATE ACTION REQUIRED</strong></p>
                    <p>Inverter has reported a fault condition. System may not be operating correctly.</p>
                </div>
                """,
                alert_type="fault_alarm"
            )
    
    # ============================================
    # TEMPERATURE ALERTS
    # ============================================
    for inv in inverter_data:
        if inv.get('high_temperature', False):
            temp = inv.get('temperature', 0)
            send_email(
                subject=f"🌡️ HIGH TEMPERATURE: {inv['Label']}",
                html_content=f"""
                <div style="font-family: Arial; padding: 20px; background: #fff3cd; border-left: 5px solid #ff9800;">
                    <h2 style="color: #ff9800;">🌡️ HIGH TEMPERATURE WARNING</h2>
                    <p><strong>Inverter:</strong> {inv['Label']} ({inv['SN']})</p>
                    <p><strong>Temperature:</strong> {temp:.1f}°C (Threshold: {INVERTER_TEMP_WARNING}°C)</p>
                    <p>Inverter temperature is elevated. Ensure adequate ventilation.</p>
                    <p>{'<strong style="color: #dc3545;">⚠️ CRITICAL: Temperature above 70°C!</strong>' if temp >= INVERTER_TEMP_CRITICAL else ''}</p>
                </div>
                """,
                alert_type="high_temperature"
            )
    
    # ============================================
    # CRITICAL: Generator Running
    # ============================================
    if generator_running or backup_voltage < BACKUP_VOLTAGE_THRESHOLD:
        send_email(
            subject="🚨 CRITICAL: Generator Running - Backup Battery Critical",
            html_content=f"""
            <div style="font-family: Arial; padding: 20px; background: #fff3cd; border-left: 5px solid #dc3545;">
                <h2 style="color: #dc3545;">🚨 GENERATOR ACTIVATION</h2>
                
                <p><strong>Backup Inverter Status:</strong></p>
                <ul>
                    <li>Battery Voltage: <strong style="color: #dc3545;">{backup_voltage:.1f}V</strong></li>
                    <li>Generator: <strong>{'RUNNING' if generator_running else 'Should be starting'}</strong></li>
                    <li>Input Detected: {'Yes' if generator_running else 'No'}</li>
                </ul>
                
                <p><strong>Primary Inverters:</strong></p>
                <ul>
                    <li>Inverter 1: {inv1['Capacity']:.0f}%</li>
                    <li>Inverter 2: {inv2['Capacity']:.0f}%</li>
                    <li>Total Load: {total_load:.0f}W</li>
                </ul>
                
                <p><strong>⚠️ ACTION REQUIRED:</strong></p>
                <ol>
                    <li>Turn OFF all Ovens</li>
                    <li>Turn OFF water heater (if used)</li>
                    <li>Unplug non-essential devices</li>
                    <li>Use only critical loads (lights, fridge)</li>
                </ol>
                
                {f'<p style="color: #dc3545;"><strong>Weather:</strong> Poor solar ahead (Cloud: {solar_conditions["avg_cloud_cover"]:.0f}%). Generator may run extended period.</p>' if solar_conditions and solar_conditions['poor_conditions'] else ''}
            </div>
            """,
            alert_type="critical"
        )
        return  # Don't check other alerts if generator is running
    
    # ============================================
    # HIGH ALERT: Backup inverter active
    # ============================================
    if backup_active and primary_capacity < PRIMARY_BATTERY_THRESHOLD:
        send_email(
            subject="⚠️ HIGH ALERT: Backup Inverter Active",
            html_content=f"""
            <div style="font-family: Arial; padding: 20px; background: #fff3cd; border-left: 5px solid #ff9800;">
                <h2 style="color: #ff9800;">⚠️ BACKUP SYSTEM ACTIVATED</h2>
                
                <p><strong>System Status:</strong></p>
                <ul>
                    <li>Backup Inverter: <strong>ACTIVE</strong> - Supplying {inv3_backup['OutputPower']:.0f}W</li>
                    <li>Backup Voltage: {backup_voltage:.1f}V</li>
                    <li>Primary Batteries: <strong>{primary_capacity:.0f}%</strong> (Below 40%)</li>
                </ul>
                
                <p><strong>Action Required:</strong></p>
                <ul>
                    <li>Turn OFF Oven immediately</li>
                    <li>Turn OFF water heater (if used) / Kettle</li>
                    <li>Minimize all non-essential loads</li>
                    <li>Use only lighting, fridge, essential devices</li>
                </ul>
                
                {f'<p style="color: #dc3545;"><strong>Weather:</strong> Poor solar ahead (Cloud: {solar_conditions["avg_cloud_cover"]:.0f}%).</p>' if solar_conditions and solar_conditions['poor_conditions'] else ''}
            </div>
            """,
            alert_type="backup_active"
        )
        return
    
    # ============================================
    # WARNING: Primary batteries 40-50%
    # ============================================
    if 40 < primary_capacity < 50:
        send_email(
            subject="⚠️ WARNING: Primary Battery Low",
            html_content=f"""
            <div style="font-family: Arial; padding: 20px; background: #fff9e6; border-left: 5px solid #ffc107;">
                <h2 style="color: #ffc107;">⚠️ PRIMARY BATTERY WARNING</h2>
                
                <p><strong>System Status:</strong></p>
                <ul>
                    <li>Primary Batteries: <strong>{primary_capacity:.0f}%</strong> (Approaching 40%)</li>
                    <li>Inverter 1: {inv1['Capacity']:.0f}%</li>
                    <li>Inverter 2: {inv2['Capacity']:.0f}%</li>
                    <li>Backup Voltage: {backup_voltage:.1f}V</li>
                </ul>
                
                <p><strong>Recommendation:</strong> Reduce high-power loads (Oven, water heater (if used), kettle).</p>
                
                {f'<p style="color: #dc3545;"><strong>Weather:</strong> Poor solar forecast. Consider reducing loads.</p>' if solar_conditions and solar_conditions['poor_conditions'] else ''}
            </div>
            """,
            alert_type="warning"
        )
    
    # ============================================
    # SOLAR-AWARE BATTERY DISCHARGE ALERTS
    # Only alert if power is coming from BATTERY, not solar
    # ============================================
    
    # TIER 3: Very High Battery Discharge (>2500W)
    if total_battery_discharge >= 2500:
        send_email(
            subject="🚨 URGENT: Very High Battery Discharge",
            html_content=f"""
            <div style="font-family: Arial; padding: 20px; background: #fff3cd; border-left: 5px solid #dc3545;">
                <h2 style="color: #dc3545;">🚨 URGENT: HIGH BATTERY DISCHARGE</h2>
                
                <p><strong>Power Status:</strong></p>
                <ul>
                    <li>Battery Discharge: <strong style="color: #dc3545;">{total_battery_discharge:.0f}W</strong> (Critical!)</li>
                    <li>Solar Input: {total_solar_input:.0f}W</li>
                    <li>Total Load: {total_load:.0f}W</li>
                    <li>Primary Batteries: {primary_capacity:.0f}%</li>
                </ul>
                
                <p><strong>⚠️ IMMEDIATE ACTION:</strong></p>
                <ul>
                    <li>Turn OFF Oven immediately</li>
                    <li>Turn OFF water heater (if used)</li>
                    <li>Turn OFF kettle</li>
                    <li>Reduce to essential loads only</li>
                </ul>
                
                {f'<p style="color: #dc3545;"><strong>Weather:</strong> Poor solar conditions (Cloud: {solar_conditions["avg_cloud_cover"]:.0f}%). Battery recovery will be limited.</p>' if solar_conditions and solar_conditions['poor_conditions'] else ''}
            </div>
            """,
            alert_type="very_high_load"
        )
    
    # TIER 2: High Battery Discharge (1500-2500W)
    elif 1500 <= total_battery_discharge < 2500:
        send_email(
            subject="⚠️ WARNING: High Battery Discharge",
            html_content=f"""
            <div style="font-family: Arial; padding: 20px; background: #fff9e6; border-left: 5px solid #ff9800;">
                <h2 style="color: #ff9800;">⚠️ HIGH BATTERY DISCHARGE</h2>
                
                <p><strong>Power Status:</strong></p>
                <ul>
                    <li>Battery Discharge: <strong>{total_battery_discharge:.0f}W</strong></li>
                    <li>Solar Input: {total_solar_input:.0f}W</li>
                    <li>Total Load: {total_load:.0f}W</li>
                    <li>Primary Batteries: {primary_capacity:.0f}%</li>
                </ul>
                
                <p><strong>Recommendation:</strong> Reduce load to preserve battery.</p>
                
                {f'<p style="color: #dc3545;"><strong>Weather:</strong> Poor solar ahead. Consider reducing usage.</p>' if solar_conditions and solar_conditions['poor_conditions'] else ''}
            </div>
            """,
            alert_type="high_load"
        )
    
    # TIER 1: Moderate Battery Discharge (1000-1500W) with Low Battery
    elif 1000 <= total_battery_discharge < 1500 and primary_capacity < 50:
        send_email(
            subject="ℹ️ INFO: Moderate Battery Discharge - Low Battery",
            html_content=f"""
            <div style="font-family: Arial; padding: 20px; background: #e7f3ff; border-left: 5px solid #2196F3;">
                <h2 style="color: #2196F3;">ℹ️ MODERATE BATTERY DISCHARGE</h2>
                
                <p><strong>Power Status:</strong></p>
                <ul>
                    <li>Battery Discharge: {total_battery_discharge:.0f}W</li>
                    <li>Solar Input: {total_solar_input:.0f}W</li>
                    <li>Primary Batteries: <strong>{primary_capacity:.0f}%</strong> (Below 50%)</li>
                </ul>
                
                <p><strong>Advisory:</strong> Consider conserving energy.</p>
            </div>
            """,
            alert_type="moderate_load"
        )

# ----------------------------
# Growatt Polling Loop
# ----------------------------
def poll_growatt():
    global latest_data, load_history, battery_history, weather_forecast, last_communication, solar_conditions_cache
    
    # Fetch weather immediately on startup
    print("🌤️ Fetching initial weather forecast...")
    weather_forecast = get_weather_forecast()
    if weather_forecast:
        solar_conditions_cache = analyze_solar_conditions(weather_forecast)
    last_weather_update = datetime.now(EAT) if weather_forecast else None
    
    while True:
        try:
            # Update weather forecast every 30 minutes
            if last_weather_update is None or datetime.now(EAT) - last_weather_update > timedelta(minutes=30):
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
                    response = requests.post(
                        API_URL,
                        data={"storage_sn": sn},
                        headers=headers,
                        timeout=20
                    )
                    response.raise_for_status()
                    data = response.json().get("data", {})
                    
                    # Update last communication time
                    last_communication[sn] = now
                    
                    # Get configuration
                    config = INVERTER_CONFIG.get(sn, {"label": sn, "type": "unknown", "datalog": "N/A", "display_order": 99})
                    
                    # Extract metrics
                    out_power = float(data.get("outPutPower") or 0)
                    capacity = float(data.get("capacity") or 0)
                    v_bat = float(data.get("vBat") or 0)
                    p_bat = float(data.get("pBat") or 0)
                    ppv = float(data.get("ppv") or 0)
                    ppv2 = float(data.get("ppv2") or 0)
                    
                    # Temperature monitoring
                    inv_temp = float(data.get("invTemperature") or 0)
                    dcdc_temp = float(data.get("dcDcTemperature") or 0)
                    temp = max(inv_temp, dcdc_temp, float(data.get("temperature") or 0))
                    
                    # Fault detection
                    error_code = int(data.get("errorCode") or 0)
                    fault_code = int(data.get("faultCode") or 0)
                    warn_code = int(data.get("warnCode") or 0)
                    has_fault = error_code != 0 or fault_code != 0 or warn_code != 0
                    
                    # AC input (for generator detection)
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
                        "fault_info": {
                            "errorCode": error_code,
                            "faultCode": fault_code,
                            "warnCode": warn_code
                        },
                        "vac": vac,
                        "pAcInput": pac_input,
                        "communication_lost": False,
                        "last_seen": now.strftime("%Y-%m-%d %H:%M:%S")
                    }
                    
                    inverter_data.append(inv_info)
                    
                    # Track primary vs backup
                    if config['type'] == 'primary':
                        if capacity > 0:
                            primary_capacities.append(capacity)
                    elif config['type'] == 'backup':
                        backup_data = inv_info
                        # Check if generator is running
                        if vac > 100 or pac_input > 50:
                            generator_running = True
                
                except Exception as e:
                    print(f"❌ Error polling {sn}: {e}")
                    # Check for communication timeout
                    if sn in last_communication:
                        time_since_last = now - last_communication[sn]
                        if time_since_last > timedelta(minutes=COMMUNICATION_TIMEOUT_MINUTES):
                            # Add placeholder with communication lost flag
                            config = INVERTER_CONFIG.get(sn, {})
                            inverter_data.append({
                                "SN": sn,
                                "Label": config.get('label', sn),
                                "Type": config.get('type', 'unknown'),
                                "DisplayOrder": config.get('display_order', 99),
                                "communication_lost": True,
                                "last_seen": last_communication[sn].strftime("%Y-%m-%d %H:%M:%S")
                            })
            
            # Sort inverters by display order
            inverter_data.sort(key=lambda x: x.get('DisplayOrder', 99))
            
            # Calculate system metrics
            primary_battery_min = min(primary_capacities) if primary_capacities else 0
            backup_battery_voltage = backup_data['vBat'] if backup_data else 0
            backup_voltage_status, backup_voltage_color = get_backup_voltage_status(backup_battery_voltage)
            backup_active = backup_data['OutputPower'] > 50 if backup_data else False
            
            # Save latest data
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
                "inverters": inverter_data
            }
            
            # Append to history
            load_history.append((now, total_output_power))
            load_history = [(t, p) for t, p in load_history if t >= now - timedelta(hours=12)]
            
            battery_history.append((now, total_battery_discharge_W))
            battery_history = [(t, p) for t, p in battery_history if t >= now - timedelta(hours=12)]
            
            print(f"{latest_data['timestamp']} | Load={total_output_power:.0f}W | Solar={total_solar_input_W:.0f}W | Battery Discharge={total_battery_discharge_W:.0f}W | Primary={primary_battery_min:.0f}% | Backup={backup_battery_voltage:.1f}V | Gen={'ON' if generator_running else 'OFF'}")
            
            # Check alerts with solar-aware logic (use cached solar_conditions)
            check_and_send_alerts(inverter_data, solar_conditions_cache, total_solar_input_W, total_battery_discharge_W, generator_running)
        
        except Exception as e:
            print(f"❌ Error in polling loop: {e}")
            import traceback
            traceback.print_exc()
        
        time.sleep(POLL_INTERVAL_MINUTES * 60)

# ----------------------------
# Flask Web Routes
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
    
    # Color coding
    primary_color = "red" if primary_battery < 40 else ("orange" if primary_battery < 50 else "green")
    
    # Chart data
    times = [t.strftime('%H:%M') for t, p in load_history]
    load_values = [p for t, p in load_history]
    battery_values = [p for t, p in battery_history]
    
    # Use cached solar conditions (calculated in polling loop)
    solar_conditions = solar_conditions_cache
    
    
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
            color: white;
            padding: 20px;
            border-radius: 10px;
            margin-bottom: 20px;
            text-align: center;
        }}
        
        .system-status.normal {{
            background: linear-gradient(135deg, #11998e 0%, #38ef7d 100%);
        }}
        
        .system-status.warning {{
            background: linear-gradient(135deg, #f77f00 0%, #fcbf49 100%);
        }}
        
        .system-status.critical {{
            background: linear-gradient(135deg, #eb3349 0%, #f45c43 100%);
        }}
        
        .system-status h2 {{
            margin-bottom: 10px;
            font-size: 1.5em;
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
        
        .metrics-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
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
        
        .metric.gray {{
            background: linear-gradient(135deg, #757F9A 0%, #D7DDE8 100%);
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
        
        .inverter-card.offline {{
            border-left-color: #dc3545;
            background: #fff0f0;
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
        
        .temp-warning {{
            color: #ff9800;
            font-weight: bold;
        }}
        
        .temp-critical {{
            color: #dc3545;
            font-weight: bold;
        }}
        
        .alert-history {{
            margin: 20px 0;
        }}
        
        .alert-item {{
            display: grid;
            grid-template-columns: 160px 150px 1fr;
            gap: 15px;
            align-items: center;
            padding: 15px;
            border-bottom: 1px solid #eee;
            transition: background 0.2s;
        }}
        
        .alert-item:hover {{
            background: #f8f9fa;
        }}
        
        .alert-item:last-child {{
            border-bottom: none;
        }}
        
        .alert-time {{
            color: #666;
            font-size: 0.9em;
            font-family: monospace;
        }}
        
        .alert-badge {{
            display: inline-block;
            padding: 6px 12px;
            border-radius: 20px;
            color: white;
            font-size: 0.85em;
            font-weight: 500;
            text-align: center;
        }}
        
        .alert-subject {{
            color: #333;
            font-size: 0.95em;
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
        
        .footer {{
            text-align: center;
            color: white;
            margin-top: 30px;
            font-size: 0.9em;
            text-shadow: 1px 1px 3px rgba(0,0,0,0.5);
        }}
        
        @media (max-width: 768px) {{
            .alert-item {{
                grid-template-columns: 1fr;
                gap: 8px;
            }}
            
            .alert-badge {{
                width: fit-content;
            }}
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
            
            <div class="system-status {'critical' if generator_running or backup_voltage < 51.2 else 'warning' if backup_active else 'normal'}">
                <h2>{'🚨 GENERATOR RUNNING' if generator_running else '🚨 GENERATOR STARTING' if backup_voltage < 51.2 else '⚠️ BACKUP SYSTEM ACTIVE' if backup_active else '✓ NORMAL OPERATION'}</h2>
                <div class="status-text">
                    {
                        'Generator is running. Backup battery critical.' if generator_running else
                        'Backup voltage critical. Generator should be starting.' if backup_voltage < 51.2 else
                        f'Backup inverter supplying {latest_data.get("inverters", [{}])[2].get("OutputPower", 0) if len(latest_data.get("inverters", [])) > 2 else 0:.0f}W to system.' if backup_active else
                        'All systems operating on primary batteries.'
                    }
                </div>
            </div>
"""
    
    # Weather alert - ALWAYS SHOW
    if solar_conditions:
        alert_class = "poor" if solar_conditions['poor_conditions'] else "good"
        period = solar_conditions.get('analysis_period', 'Next 10 Hours')
        is_night = solar_conditions.get('is_nighttime', False)
        
        html += f"""
            <div class="weather-alert {alert_class}">
                <h3>{'☁️ Poor Solar Conditions Ahead' if solar_conditions['poor_conditions'] else '☀️ Good Solar Conditions Expected'}</h3>
                <p><strong>{period}:</strong> Cloud Cover: {solar_conditions['avg_cloud_cover']:.0f}% | Solar Radiation: {solar_conditions['avg_solar_radiation']:.0f} W/m²</p>
                {'<p>⚠️ Limited recharge expected. Monitor battery levels closely.</p>' if solar_conditions['poor_conditions'] else '<p>✓ Batteries should recharge well during daylight hours.</p>'}
                {f'<p style="font-size: 0.9em; opacity: 0.8;">🌙 Currently nighttime - analyzing tomorrow\'s solar potential</p>' if is_night else ''}
                <p style="font-size: 0.8em; opacity: 0.7; margin-top: 10px;">📡 Data from: {weather_source}</p>
            </div>
"""
    else:
        # Show status when weather unavailable
        html += f"""
            <div class="weather-alert poor">
                <h3>🌤️ Weather Forecast</h3>
                <p><strong>Status:</strong> {weather_source}</p>
                <p>Unable to fetch weather data at this time. Trying multiple sources (Open-Meteo, WeatherAPI, 7Timer).</p>
                <p style="font-size: 0.85em; opacity: 0.7;">Updates attempted every 30 minutes. System will keep trying.</p>
            </div>
"""
    
    html += f"""
            <div class="metrics-grid">
                <div class="metric {primary_color}">
                    <div class="metric-label">Primary Batteries</div>
                    <div class="metric-value">{primary_battery:.0f}%</div>
                    <div class="metric-subtext">Inverters 1 & 2 (Min)</div>
                </div>
                
                <div class="metric {backup_voltage_color}">
                    <div class="metric-label">Backup Battery</div>
                    <div class="metric-value">{backup_voltage:.1f}V</div>
                    <div class="metric-subtext">{backup_voltage_status} | Gen at 51.2V</div>
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
                
                <div class="metric {'green' if generator_running else 'orange' if backup_active else 'green'}">
                    <div class="metric-label">Generator</div>
                    <div class="metric-value">{'ON' if generator_running else 'OFF'}</div>
                    <div class="metric-subtext">{'Running' if generator_running else 'Standby'}</div>
                </div>
            </div>
            
            <h2>Inverter Details</h2>
            <div class="inverter-grid">
"""
    
    # Display inverters (already sorted by display_order in polling loop)
    for inv in latest_data.get("inverters", []):
        is_backup = inv.get('Type') == 'backup'
        is_offline = inv.get('communication_lost', False)
        label = inv.get('Label', 'Unknown')
        sn = inv.get('SN', 'N/A')
        datalog = inv.get('DataLog', 'N/A')
        
        card_class = 'offline' if is_offline else ('backup' if is_backup else '')
        
        html += f"""
                <div class="inverter-card {card_class}">
                    <h3>{label}</h3>
                    <div class="inv-label">{sn} ({datalog})</div>
"""
        
        if is_offline:
            html += f"""
                    <div style="color: #dc3545; padding: 10px; background: #fff0f0; border-radius: 5px; margin-bottom: 10px;">
                        <strong>⚠️ COMMUNICATION LOST</strong><br>
                        Last seen: {inv.get('last_seen', 'Unknown')}
                    </div>
"""
        else:
            # Show normal stats
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
                        <span class="inv-stat-label">Battery Voltage</span>
                        <span class="inv-stat-value">{inv.get('vBat', 0):.1f}V</span>
                    </div>
"""
            
            html += f"""
                    <div class="inv-stat">
                        <span class="inv-stat-label">Output Power</span>
                        <span class="inv-stat-value">{inv.get('OutputPower', 0):.0f}W</span>
                    </div>
                    <div class="inv-stat">
                        <span class="inv-stat-label">Battery Power</span>
                        <span class="inv-stat-value">{inv.get('pBat', 0):.0f}W</span>
                    </div>
                    <div class="inv-stat">
                        <span class="inv-stat-label">Solar Input</span>
                        <span class="inv-stat-value">{(inv.get('ppv', 0) + inv.get('ppv2', 0)):.0f}W</span>
                    </div>
                    <div class="inv-stat">
                        <span class="inv-stat-label">Temperature</span>
                        <span class="inv-stat-value {temp_class}">{temp:.1f}°C</span>
                    </div>
                    <div class="inv-stat">
                        <span class="inv-stat-label">Status</span>
                        <span class="inv-stat-value">{inv.get('Status', 'Unknown')}</span>
                    </div>
"""
            
            if inv.get('has_fault'):
                fault_info = inv.get('fault_info', {})
                html += f"""
                    <div style="color: #dc3545; padding: 10px; background: #fff0f0; border-radius: 5px; margin-top: 10px;">
                        <strong>⚠️ FAULT DETECTED</strong><br>
                        Error: {fault_info.get('errorCode', 0)} | Fault: {fault_info.get('faultCode', 0)}
                    </div>
"""
        
        html += """
                </div>
"""
    
    html += """
            </div>
        </div>
        
        <meta http-equiv="refresh" content="300">
"""
    
    # Alert History Section
    if alert_history:
        html += """
        <div class="card">
            <h2>📧 Alert History - Last 12 Hours</h2>
            <div class="alert-history">
"""
        
        def get_alert_badge(alert_type):
            badges = {
                "critical": ("🔴", "Critical", "#dc3545"),
                "very_high_load": ("🔴", "Very High Discharge", "#dc3545"),
                "backup_active": ("🟠", "Backup Active", "#ff9800"),
                "high_load": ("🟠", "High Discharge", "#ff9800"),
                "warning": ("🟡", "Warning", "#ffc107"),
                "moderate_load": ("🔵", "Moderate Discharge", "#2196F3"),
                "communication_lost": ("⚠️", "Comm Lost", "#ff9800"),
                "fault_alarm": ("🚨", "Fault", "#dc3545"),
                "high_temperature": ("🌡️", "High Temp", "#ff9800"),
                "test": ("⚪", "Test", "#9e9e9e"),
                "general": ("⚪", "Info", "#9e9e9e")
            }
            return badges.get(alert_type, ("⚪", "Unknown", "#9e9e9e"))
        
        for alert in reversed(alert_history):
            icon, badge_text, color = get_alert_badge(alert['type'])
            time_str = alert['timestamp'].strftime("%H:%M:%S")
            date_str = alert['timestamp'].strftime("%Y-%m-%d")
            
            html += f"""
                <div class="alert-item">
                    <div class="alert-time">{date_str} {time_str}</div>
                    <div class="alert-badge" style="background: {color};">{icon} {badge_text}</div>
                    <div class="alert-subject">{alert['subject']}</div>
                </div>
"""
        
        html += """
            </div>
        </div>
"""
    else:
        html += """
        <div class="card">
            <h2>📧 Alert History - Last 12 Hours</h2>
            <div class="alert-history">
                <p style="text-align: center; color: #666; padding: 20px;">
                    ✓ No alerts sent in the last 12 hours
                </p>
            </div>
        </div>
"""
    
    html += """
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
        </div>
        
        <div class="footer">
            10kW Solar System • Cascade Battery Architecture • Solar-Aware Monitoring • Managed by YourHost
        </div>
    </div>
</body>
</html>
"""
    
    return render_template_string(html)

# ----------------------------
# Start background polling thread
# ----------------------------
Thread(target=poll_growatt, daemon=True).start()

# ----------------------------
# Run Flask
# ----------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
