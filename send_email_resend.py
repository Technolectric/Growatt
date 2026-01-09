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

# Debug info for weather (New)
weather_debug = {
    "status": "Initializing",
    "last_attempt": None,
    "error": None,
    "url_used": None
}

# East African Timezone
EAT = timezone(timedelta(hours=3))

# ----------------------------
# Weather & Solar Forecast Functions (FIXED)
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
                    <p>{'<strong style="color: #dc3545;">
