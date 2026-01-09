import os
import time
import requests
from datetime import datetime, timedelta, timezone
from threading import Thread, Lock
from flask import Flask, render_template_string, request
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
# Inverter Configuration (Ordered)
# ----------------------------
INVERTER_CONFIG = {
    "RKG3B0400T": {"label": "Inverter 1", "type": "primary", "datalog": "DDD0B021CC", "order": 1},
    "KAM4N5W0AG": {"label": "Inverter 2", "type": "primary", "datalog": "DDD0B02121", "order": 2},
    "JNK1CDR0KQ": {"label": "Inverter 3 (Backup)", "type": "backup", "datalog": "DDD0B0221H", "order": 3}
}

# Alert thresholds
PRIMARY_BATTERY_THRESHOLD = 40  # When backup kicks in
BACKUP_VOLTAGE_THRESHOLD = 51.2  # When generator starts
BACKUP_VOLTAGE_WARNING = 52.3  # Warning threshold
BACKUP_VOLTAGE_GOOD = 53.0  # Good threshold

# Load alert thresholds - now consider battery discharge
TIER3_LOAD_THRESHOLD = 5000  # >5000W with battery discharge >2000W
TIER2_LOAD_THRESHOLD = 3000  # 3000-5000W with battery discharge >1000W
TIER1_LOAD_THRESHOLD = 2000  # 2000-3000W with battery discharge >500W

TOTAL_SOLAR_CAPACITY_KW = 10

# Inverter status codes from Growatt API
INVERTER_STATUS_CODES = {
    0: "Standby",
    1: "Grid-connected",
    2: "Battery Discharging",
    3: "Fault",
    4: "Flash update",
    5: "PV charging",
    6: "AC charging",
    7: "Combined charging"
}

# Fault codes that indicate issues
FAULT_CODES = {
    0: "No fault",
    1: "Grid voltage high",
    2: "Grid voltage low",
    3: "Grid frequency high",
    4: "Grid frequency low",
    5: "Grid loss",
    6: "Islanding",
    7: "Output overload",
    8: "Temperature high",
    9: "PV voltage high",
    10: "AC voltage high",
    11: "Battery voltage high",
    12: "Battery voltage low",
    13: "Battery over current",
    14: "Ground fault",
    15: "GFCI fault",
    16: "Communication error",
    17: "Hardware error",
    18: "Battery temperature high"
}

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
alert_history = []  # Track all alerts sent in last 12 hours: (timestamp, type, subject)
inverter_status = {}  # Track inverter communication status and faults
inverter_last_seen = {}  # Track when each inverter was last seen
data_lock = Lock()

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

def analyze_solar_conditions(forecast):
    """Analyze upcoming solar conditions - smart daytime-only analysis"""
    if not forecast:
        return None
    
    try:
        now = datetime.now(EAT)
        current_hour = now.hour
        
        # Determine if it's currently nighttime (6 PM to 6 AM)
        is_nighttime = current_hour < 6 or current_hour >= 18
        
        if is_nighttime:
            # During night: analyze tomorrow's full daytime (6 AM to 6 PM)
            tomorrow = now + timedelta(days=1)
            start_time = tomorrow.replace(hour=6, minute=0, second=0, microsecond=0)
            end_time = tomorrow.replace(hour=18, minute=0, second=0, microsecond=0)
            analysis_label = "Tomorrow's Daylight"
        else:
            # During day: analyze remaining daylight today (now until 6 PM)
            start_time = now
            end_time = now.replace(hour=18, minute=0, second=0, microsecond=0)
            analysis_label = "Today's Remaining Daylight"
        
        avg_cloud_cover = 0
        avg_solar_radiation = 0
        count = 0
        
        for i, time_str in enumerate(forecast['times']):
            forecast_time = datetime.fromisoformat(time_str.replace('Z', '+00:00')).astimezone(EAT)
            
            # Only include times within our analysis window
            if start_time <= forecast_time <= end_time:
                # During daytime hours only (6 AM to 6 PM)
                hour = forecast_time.hour
                if 6 <= hour <= 18:
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
                'hours_analyzed': count,
                'analysis_period': analysis_label,
                'is_nighttime': is_nighttime
            }
    except Exception as e:
        print(f"✗ Error analyzing solar conditions: {e}")
    
    return None

# ----------------------------
# Email function with alert type tracking
# ----------------------------
def send_email(subject, html_content, alert_type="general"):
    global last_alert_time, alert_history
    if not all([RESEND_API_KEY, SENDER_EMAIL, RECIPIENT_EMAIL]):
        print("✗ Error: Missing email credentials in env")
        return False
    
    # Rate limit: different cooldowns for different alert types
    cooldown_map = {
        "critical": 30,           # Generator starting - every 30 min
        "very_high_load": 30,     # >5000W with heavy battery discharge
        "backup_active": 60,      # Backup supplying power - every 60 min
        "high_load": 60,          # 3000-5000W with battery discharge
        "moderate_load": 120,     # 2000-3000W with battery discharge
        "warning": 120,           # Primary battery warning
        "fault": 60,              # Inverter fault
        "comms_loss": 30,         # Communication loss
        "test": 0,                # Test alerts - no cooldown
        "general": 120            # Default - every 120 min
    }
    
    cooldown_minutes = cooldown_map.get(alert_type, 120)
    
    if alert_type in last_alert_time and cooldown_minutes > 0:
        if datetime.now(EAT) - last_alert_time[alert_type] < timedelta(minutes=cooldown_minutes):
            print(f"⚠️ Alert cooldown active for {alert_type}, skipping email ({cooldown_minutes} min cooldown)")
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
            
            # Keep only last 12 hours of alerts
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
# Backup Battery Status Helper
# ----------------------------
def get_backup_battery_status(voltage):
    """Get backup battery status based on voltage"""
    if voltage >= BACKUP_VOLTAGE_GOOD:
        return "Good", "green"
    elif voltage >= BACKUP_VOLTAGE_WARNING:
        return "Medium", "orange"
    elif voltage >= BACKUP_VOLTAGE_THRESHOLD:
        return "Low", "red"
    else:
        return "Critical", "darkred"

# ----------------------------
# Fault Detection Functions
# ----------------------------
def check_inverter_faults(inverter_data):
    """Check for inverter faults and communication issues"""
    now = datetime.now(EAT)
    
    for inv in inverter_data:
        sn = inv['SN']
        inverter_last_seen[sn] = now
        
        # Check for communication loss
        if inv.get('lost', False) or inv.get('errorCode', 0) != 0 or inv.get('faultCode', 0) != 0:
            fault_code = inv.get('faultCode', 0)
            error_code = inv.get('errorCode', 0)
            status_code = inv.get('status', 2)  # Default to discharge
            
            # Only send alert if we haven't sent one recently for this inverter
            alert_key = f"fault_{sn}"
            if alert_key not in last_alert_time or (now - last_alert_time[alert_key]) > timedelta(minutes=60):
                
                # Get fault description
                fault_desc = FAULT_CODES.get(fault_code, f"Unknown fault code: {fault_code}")
                
                send_email(
                    subject=f"⚠️ INVERTER FAULT: {inv['Label']}",
                    html_content=f"""
                    <div style="font-family: Arial; padding: 20px; background: #fff3cd; border-left: 5px solid #dc3545;">
                        <h2 style="color: #dc3545;">⚠️ INVERTER FAULT DETECTED</h2>
                        
                        <p><strong>Inverter Details:</strong></p>
                        <ul>
                            <li>Inverter: {inv['Label']} ({sn})</li>
                            <li>Status: {INVERTER_STATUS_CODES.get(status_code, 'Unknown')}</li>
                            <li>Fault Code: {fault_code} - {fault_desc}</li>
                            <li>Error Code: {error_code}</li>
                            <li>Battery Voltage: {inv.get('vBat', 0):.1f}V</li>
                            <li>Output Power: {inv.get('OutputPower', 0):.0f}W</li>
                        </ul>
                        
                        <p><strong>⚠️ ACTION REQUIRED:</strong></p>
                        <p>Check the inverter immediately. This may indicate hardware failure or communication issues.</p>
                    </div>
                    """,
                    alert_type="fault"
                )
                last_alert_time[alert_key] = now
    
    # Check for communication loss (inverter not responding for >15 minutes)
    for sn in SERIAL_NUMBERS:
        if sn in inverter_last_seen:
            time_since_last_seen = now - inverter_last_seen[sn]
            if time_since_last_seen > timedelta(minutes=15):
                alert_key = f"comms_loss_{sn}"
                if alert_key not in last_alert_time or (now - last_alert_time[alert_key]) > timedelta(minutes=30):
                    config = INVERTER_CONFIG.get(sn, {"label": f"Inverter {sn}"})
                    send_email(
                        subject=f"🚨 COMMUNICATION LOST: {config['label']}",
                        html_content=f"""
                        <div style="font-family: Arial; padding: 20px; background: #fff3cd; border-left: 5px solid #dc3545;">
                            <h2 style="color: #dc3545;">🚨 INVERTER COMMUNICATION LOST</h2>
                            
                            <p><strong>Inverter Details:</strong></p>
                            <ul>
                                <li>Inverter: {config['label']} ({sn})</li>
                                <li>Last Seen: {inverter_last_seen[sn].strftime('%Y-%m-%d %H:%M:%S')}</li>
                                <li>Time Since Last Communication: {time_since_last_seen.seconds // 60} minutes</li>
                            </ul>
                            
                            <p><strong>⚠️ ACTION REQUIRED:</strong></p>
                            <p>The inverter has stopped communicating. Check:</p>
                            <ol>
                                <li>Power supply to the inverter</li>
                                <li>Network connectivity</li>
                                <li>Inverter status lights</li>
                                <li>Data logger connection</li>
                            </ol>
                        </div>
                        """,
                        alert_type="comms_loss"
                    )
                    last_alert_time[alert_key] = now

# ----------------------------
# Intelligent Alert Logic
# ----------------------------
def check_and_send_alerts(inverter_data, solar_conditions, total_battery_discharge):
    """Smart alert logic based on cascade inverter architecture"""
    
    # Check for inverter faults first
    check_inverter_faults(inverter_data)
    
    # Extract data for each inverter
    inv1 = next((i for i in inverter_data if i['SN'] == 'RKG3B0400T'), None)
    inv2 = next((i for i in inverter_data if i['SN'] == 'KAM4N5W0AG'), None)
    inv3_backup = next((i for i in inverter_data if i['SN'] == 'JNK1CDR0KQ'), None)
    
    if not all([inv1, inv2, inv3_backup]):
        print("⚠️ Not all inverters reporting data")
        return
    
    # Calculate primary battery levels (minimum of Inv1 and Inv2)
    primary_capacity = min(inv1['Capacity'], inv2['Capacity'])
    backup_voltage = inv3_backup['vBat']
    
    # Total load across all inverters
    total_load = inv1['OutputPower'] + inv2['OutputPower'] + inv3_backup['OutputPower']
    
    # Check backup inverter status
    backup_active = inv3_backup['OutputPower'] > 50  # Backup is supplying power
    
    # Backup battery status
    backup_status, _ = get_backup_battery_status(backup_voltage)
    
    # ============================================
    # CRITICAL ALERT: Generator about to start
    # ============================================
    if backup_voltage < BACKUP_VOLTAGE_THRESHOLD:
        send_email(
            subject="🚨 CRITICAL: Generator Starting - Backup Battery Critical",
            html_content=f"""
            <div style="font-family: Arial; padding: 20px; background: #fff3cd; border-left: 5px solid #dc3545;">
                <h2 style="color: #dc3545;">🚨 GENERATOR ACTIVATION IMMINENT</h2>
                
                <p><strong>Backup Inverter (Inv 3) Status:</strong></p>
                <ul>
                    <li>Battery Voltage: <strong style="color: #dc3545;">{backup_voltage:.1f}V</strong> (Threshold: {BACKUP_VOLTAGE_THRESHOLD}V)</li>
                    <li>Status: Generator starting or already running</li>
                </ul>
                
                <p><strong>Primary Inverters Status:</strong></p>
                <ul>
                    <li>Inverter 1 Battery: {inv1['Capacity']:.0f}%</li>
                    <li>Inverter 2 Battery: {inv2['Capacity']:.0f}%</li>
                    <li>Combined Load: {total_load:.0f}W</li>
                    <li>Battery Discharge: {total_battery_discharge:.0f}W</li>
                </ul>
                
                <p><strong>⚠️ ACTION REQUIRED:</strong></p>
                <p>The backup battery voltage has dropped below {BACKUP_VOLTAGE_THRESHOLD}V. The generator should be starting automatically.</p>
                <p><strong>REDUCE LOADS NOW:</strong></p>
                <ol>
                    <li>Turn OFF all Ovens</li>
                    <li>Turn OFF water heater (if used)</li>
                    <li>Unplug non-essential devices</li>
                    <li>Use only critical loads (lights, fridge)</li>
                </ol>
                
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
                    <li>Backup Battery: {backup_status} ({backup_voltage:.1f}V)</li>
                    <li>Primary Batteries: <strong style="color: #dc3545;">{primary_capacity:.0f}%</strong> (Below {PRIMARY_BATTERY_THRESHOLD}% threshold)</li>
                    <li>Battery Discharge: {total_battery_discharge:.0f}W</li>
                </ul>
                
                <p><strong>Individual Inverter Status:</strong></p>
                <ul>
                    <li>Inverter 1: {inv1['Capacity']:.0f}% | {inv1['OutputPower']:.0f}W</li>
                    <li>Inverter 2: {inv2['Capacity']:.0f}% | {inv2['OutputPower']:.0f}W</li>
                    <li>Total Load: {total_load:.0f}W</li>
                </ul>
                
                <p><strong>⚠️ WARNING:</strong></p>
                <p>Primary inverter batteries are below {PRIMARY_BATTERY_THRESHOLD}%. Backup inverter is now supplying power to the primary inverters. 
                If backup voltage drops below {BACKUP_VOLTAGE_THRESHOLD}V, the generator will start automatically.</p>
                
                <p><strong>Action Required:</strong></p>
                <ul>
                    <li>Turn OFF Oven immediately</li>
                    <li>Turn OFF water heater (if used) / Kettle</li>
                    <li>Minimize all non-essential loads</li>
                    <li>Use only lighting, fridge, essential devices</li>
                </ul>
                
                {f'<p style="color: #dc3545;"><strong>Weather Alert:</strong> Poor solar conditions ahead (Cloud: {solar_conditions["avg_cloud_cover"]:.0f}%). Limited recharge expected. Consider reducing load.</p>' if solar_conditions and solar_conditions['poor_conditions'] else f'<p style="color: #28a745;"><strong>Weather:</strong> Good solar conditions expected (Cloud: {solar_conditions["avg_cloud_cover"]:.0f}%). Batteries should recharge.</p>' if solar_conditions else ''}
            </div>
            """,
            alert_type="backup_active"
        )
    
    # ============================================
    # WARNING: Primary batteries approaching threshold
    # ============================================
    elif PRIMARY_BATTERY_THRESHOLD < primary_capacity < 50 and total_battery_discharge > 500:
        send_email(
            subject="⚠️ WARNING: Primary Battery Low - Backup Activation Soon",
            html_content=f"""
            <div style="font-family: Arial; padding: 20px; background: #fff9e6; border-left: 5px solid #ffc107;">
                <h2 style="color: #ffc107;">⚠️ PRIMARY BATTERY WARNING</h2>
                
                <p><strong>System Status:</strong></p>
                <ul>
                    <li>Primary Batteries: <strong style="color: #ff9800;">{primary_capacity:.0f}%</strong> (Approaching {PRIMARY_BATTERY_THRESHOLD}% threshold)</li>
                    <li>Battery Discharge: {total_battery_discharge:.0f}W</li>
                    <li>Inverter 1: {inv1['Capacity']:.0f}%</li>
                    <li>Inverter 2: {inv2['Capacity']:.0f}%</li>
                    <li>Backup Battery: {backup_status} ({backup_voltage:.1f}V)</li>
                    <li>Total Load: {total_load:.0f}W</li>
                </ul>
                
                <p><strong>Next Stage:</strong></p>
                <p>When primary batteries drop below {PRIMARY_BATTERY_THRESHOLD}%, Backup Inverter (Inv 3) will activate to supply power to the primary inverters.</p>
                
                {f'<p style="color: #dc3545;"><strong>Weather Alert:</strong> Poor solar forecast (Cloud: {solar_conditions["avg_cloud_cover"]:.0f}%). Consider reducing non-essential loads now.</p>' if solar_conditions and solar_conditions['poor_conditions'] else f'<p style="color: #28a745;"><strong>Weather:</strong> Good conditions expected. Batteries should recover.</p>' if solar_conditions else ''}
                
                <p><strong>Recommendation:</strong> Monitor usage and consider reducing high-power loads (Oven, water heater (if used), kettle).</p>
            </div>
            """,
            alert_type="warning"
        )
    
    # ============================================
    # TIERED HIGH LOAD ALERTS (Based on battery discharge)
    # Only alert when battery is being heavily discharged
    # ============================================
    
    # TIER 3: Very High Load (>5000W) with heavy battery discharge
    if total_load >= TIER3_LOAD_THRESHOLD and total_battery_discharge > 2000:
        send_email(
            subject="🚨 URGENT: Very High Power Usage Draining Battery",
            html_content=f"""
            <div style="font-family: Arial; padding: 20px; background: #fff3cd; border-left: 5px solid #dc3545;">
                <h2 style="color: #dc3545;">🚨 URGENT: HIGH LOAD DRAINING BATTERY</h2>
                
                <p><strong>Current Status:</strong></p>
                <ul>
                    <li>Total Load: <strong style="color: #dc3545;">{total_load:.0f}W</strong> (Above {TIER3_LOAD_THRESHOLD}W)</li>
                    <li>Battery Discharge: <strong style="color: #dc3545;">{total_battery_discharge:.0f}W</strong> (CRITICAL)</li>
                    <li>Primary Batteries: {primary_capacity:.0f}%</li>
                    <li>Backup Battery: {backup_status} ({backup_voltage:.1f}V)</li>
                    <li>Solar Generation: {(inv1.get('ppv', 0) + inv2.get('ppv', 0) + inv3_backup.get('ppv', 0)):.0f}W</li>
                </ul>
                
                <p><strong>Individual Loads:</strong></p>
                <ul>
                    <li>Inverter 1: {inv1['OutputPower']:.0f}W ({inv1['Capacity']:.0f}%)</li>
                    <li>Inverter 2: {inv2['OutputPower']:.0f}W ({inv2['Capacity']:.0f}%)</li>
                    <li>Inverter 3: {inv3_backup['OutputPower']:.0f}W</li>
                </ul>
                
                <p><strong>⚠️ IMMEDIATE ACTION REQUIRED:</strong></p>
                <p>Power consumption is critically high and draining batteries rapidly.</p>
                <ul>
                    <li>Turn OFF Oven immediately</li>
                    <li>Turn OFF water heater (if used)</li>
                    <li>Turn OFF kettle</li>
                    <li>Check for multiple high-power devices running simultaneously</li>
                    <li>Reduce to essential loads only</li>
                </ul>
                
                {f'<p style="color: #dc3545;"><strong>Weather Alert:</strong> Poor solar conditions expected (Cloud: {solar_conditions["avg_cloud_cover"]:.0f}%). Battery recovery will be limited.</p>' if solar_conditions and solar_conditions['poor_conditions'] else f'<p style="color: #28a745;"><strong>Weather:</strong> Good solar expected (Cloud: {solar_conditions["avg_cloud_cover"]:.0f}%). Reduce load to allow battery recovery.</p>' if solar_conditions else ''}
            </div>
            """,
            alert_type="very_high_load"
        )
    
    # TIER 2: High Load (3000-5000W) with battery discharge
    elif TIER2_LOAD_THRESHOLD <= total_load < TIER3_LOAD_THRESHOLD and total_battery_discharge > 1000:
        send_email(
            subject="⚠️ WARNING: High Power Usage Draining Battery",
            html_content=f"""
            <div style="font-family: Arial; padding: 20px; background: #fff9e6; border-left: 5px solid #ff9800;">
                <h2 style="color: #ff9800;">⚠️ HIGH POWER USAGE DRAINING BATTERY</h2>
                
                <p><strong>Current Status:</strong></p>
                <ul>
                    <li>Total Load: <strong style="color: #ff9800;">{total_load:.0f}W</strong> (Above {TIER2_LOAD_THRESHOLD}W)</li>
                    <li>Battery Discharge: <strong style="color: #ff9800;">{total_battery_discharge:.0f}W</strong> (High)</li>
                    <li>Primary Batteries: {primary_capacity:.0f}%</li>
                    <li>Backup Battery: {backup_status} ({backup_voltage:.1f}V)</li>
                    <li>Solar Generation: {(inv1.get('ppv', 0) + inv2.get('ppv', 0) + inv3_backup.get('ppv', 0)):.0f}W</li>
                </ul>
                
                <p><strong>Individual Loads:</strong></p>
                <ul>
                    <li>Inverter 1: {inv1['OutputPower']:.0f}W</li>
                    <li>Inverter 2: {inv2['OutputPower']:.0f}W</li>
                    <li>Inverter 3: {inv3_backup['OutputPower']:.0f}W</li>
                </ul>
                
                <p><strong>Recommendation:</strong></p>
                <p>Power usage is high and draining batteries. Consider reducing load.</p>
                <ul>
                    <li>Avoid using multiple high-power devices simultaneously</li>
                    <li>Turn off Oven if cooking is complete</li>
                    <li>Monitor battery levels</li>
                </ul>
                
                {f'<p style="color: #dc3545;"><strong>Weather Alert:</strong> Poor solar forecast (Cloud: {solar_conditions["avg_cloud_cover"]:.0f}%). Consider reducing usage.</p>' if solar_conditions and solar_conditions['poor_conditions'] else f'<p style="color: #28a745;"><strong>Weather:</strong> Good solar conditions expected. Reduce load to allow battery recovery.</p>' if solar_conditions else ''}
            </div>
            """,
            alert_type="high_load"
        )
    
    # TIER 1: Moderate Load (2000-3000W) with battery discharge and low battery
    elif TIER1_LOAD_THRESHOLD <= total_load < TIER2_LOAD_THRESHOLD and total_battery_discharge > 500 and primary_capacity < 50:
        send_email(
            subject="ℹ️ INFO: Moderate Load with Battery Drain",
            html_content=f"""
            <div style="font-family: Arial; padding: 20px; background: #e7f3ff; border-left: 5px solid #2196F3;">
                <h2 style="color: #2196F3;">ℹ️ MODERATE POWER USAGE - BATTERY DRAIN</h2>
                
                <p><strong>Current Status:</strong></p>
                <ul>
                    <li>Total Load: {total_load:.0f}W (Moderate)</li>
                    <li>Battery Discharge: {total_battery_discharge:.0f}W</li>
                    <li>Primary Batteries: <strong style="color: #ff9800;">{primary_capacity:.0f}%</strong> (Below 50%)</li>
                    <li>Backup Battery: {backup_status} ({backup_voltage:.1f}V)</li>
                    <li>Solar Generation: {(inv1.get('ppv', 0) + inv2.get('ppv', 0) + inv3_backup.get('ppv', 0)):.0f}W</li>
                </ul>
                
                <p><strong>Advisory:</strong></p>
                <p>Current power usage is moderate but batteries are below 50% and discharging. Consider conserving energy.</p>
                
                {f'<p style="color: #ff9800;"><strong>Weather:</strong> Poor solar ahead (Cloud: {solar_conditions["avg_cloud_cover"]:.0f}%). Battery recovery may be slow.</p>' if solar_conditions and solar_conditions['poor_conditions'] else f'<p style="color: #28a745;"><strong>Weather:</strong> Good solar expected. Batteries should recover if load is reduced.</p>' if solar_conditions else ''}
            </div>
            """,
            alert_type="moderate_load"
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
                try:
                    response = requests.post(
                        API_URL,
                        data={"storage_sn": sn},
                        headers=headers,
                        timeout=20
                    )
                    response.raise_for_status()
                    json_data = response.json()
                    
                    if json_data.get("code") != 0:
                        print(f"✗ API error for {sn}: {json_data.get('message', 'Unknown error')}")
                        continue
                    
                    # Parse the new data structure
                    storage_data = json_data.get("data", {}).get("storage", [])
                    if not storage_data:
                        print(f"✗ No storage data for {sn}")
                        continue
                    
                    data = storage_data[0]
                    
                    # Get configuration
                    config = INVERTER_CONFIG.get(sn, {"label": sn, "type": "unknown", "datalog": "N/A", "order": 99})
                    
                    # Extract key metrics using the correct field names from the API
                    out_power = float(data.get("outPutPower") or 0)
                    capacity = float(data.get("capacity") or 0)
                    v_bat = float(data.get("vBat") or 0)
                    
                    # Calculate battery power: pDischarge is positive when discharging, pCharge is positive when charging
                    p_discharge = float(data.get("pDischarge") or 0)
                    p_charge = float(data.get("pCharge") or 0)
                    p_bat = p_discharge - p_charge  # Positive = discharging, Negative = charging
                    
                    # Get status and fault information
                    status_num = int(data.get("status", 2))
                    fault_code = int(data.get("faultCode", 0))
                    error_code = int(data.get("errorCode", 0))
                    warn_code = int(data.get("warnCode", 0))
                    
                    # Get solar generation
                    p_pv = float(data.get("ppv") or 0)
                    
                    total_output_power += out_power
                    
                    if p_bat > 0:
                        total_battery_discharge_W += p_bat
                    
                    inv_info = {
                        "SN": sn,
                        "Label": config['label'],
                        "Type": config['type'],
                        "Order": config['order'],
                        "DataLog": config['datalog'],
                        "OutputPower": out_power,
                        "Capacity": capacity,
                        "vBat": v_bat,
                        "pBat": p_bat,
                        "ppv": p_pv,
                        "status": status_num,
                        "faultCode": fault_code,
                        "errorCode": error_code,
                        "warnCode": warn_code,
                        "lost": data.get("lost", False),
                        "StatusText": data.get("statusText", "Unknown"),
                        "time": data.get("time", "")
                    }
                    
                    inverter_data.append(inv_info)
                    
                    # Track primary vs backup
                    if config['type'] == 'primary':
                        if capacity > 0:
                            primary_capacities.append(capacity)
                    elif config['type'] == 'backup':
                        backup_data = inv_info
                        
                except Exception as e:
                    print(f"✗ Error polling inverter {sn}: {e}")
                    import traceback
                    traceback.print_exc()
                    continue
            
            # Sort inverters by order (1, 2, 3)
            inverter_data.sort(key=lambda x: x.get('Order', 99))
            
            # Calculate system-wide metrics
            primary_battery_min = min(primary_capacities) if primary_capacities else 0
            backup_voltage = backup_data['vBat'] if backup_data else 0
            backup_active = backup_data['OutputPower'] > 50 if backup_data else False
            backup_status, backup_color = get_backup_battery_status(backup_voltage)
            
            # Check if any inverter has a fault
            has_fault = any(inv.get('faultCode', 0) != 0 or inv.get('errorCode', 0) != 0 for inv in inverter_data)
            
            # Save latest readings
            latest_data = {
                "timestamp": now.strftime("%Y-%m-%d %H:%M:%S EAT"),
                "total_output_power": total_output_power,
                "total_battery_discharge_W": total_battery_discharge_W,
                "primary_battery_min": primary_battery_min,
                "backup_voltage": backup_voltage,
                "backup_status": backup_status,
                "backup_active": backup_active,
                "has_fault": has_fault,
                "inverters": inverter_data,
                "solar_generation": sum(inv.get('ppv', 0) for inv in inverter_data)
            }
            
            # Append to history
            load_history.append((now, total_output_power))
            load_history = [(t, p) for t, p in load_history if t >= now - timedelta(hours=12)]
            
            battery_history.append((now, total_battery_discharge_W))
            battery_history = [(t, p) for t, p in battery_history if t >= now - timedelta(hours=12)]
            
            # Print status with more details
            status_summary = f"{latest_data['timestamp']} | Load={total_output_power:.0f}W | Solar={latest_data['solar_generation']:.0f}W | "
            status_summary += f"BattDis={total_battery_discharge_W:.0f}W | Primary={primary_battery_min:.0f}% | "
            status_summary += f"Backup={backup_voltage:.1f}V ({backup_status}) | Backup Active={backup_active}"
            if has_fault:
                status_summary += " | ⚠️ FAULT DETECTED"
            print(status_summary)
            
            # Check alerts with solar conditions
            solar_conditions = analyze_solar_conditions(weather_forecast)
            check_and_send_alerts(inverter_data, solar_conditions, total_battery_discharge_W)
        
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
    backup_voltage = latest_data.get("backup_voltage", 0)
    backup_status = latest_data.get("backup_status", "Unknown")
    backup_active = latest_data.get("backup_active", False)
    total_load = latest_data.get("total_output_power", 0)
    solar_generation = latest_data.get("solar_generation", 0)
    has_fault = latest_data.get("has_fault", False)
    
    # Color coding for primary battery
    primary_color = "red" if primary_battery < PRIMARY_BATTERY_THRESHOLD else ("orange" if primary_battery < 50 else "green")
    
    # Color coding for backup battery based on voltage
    if backup_voltage >= BACKUP_VOLTAGE_GOOD:
        backup_color = "green"
    elif backup_voltage >= BACKUP_VOLTAGE_WARNING:
        backup_color = "orange"
    elif backup_voltage >= BACKUP_VOLTAGE_THRESHOLD:
        backup_color = "red"
    else:
        backup_color = "darkred"
    
    # Chart data
    times = [t.strftime('%H:%M') for t, p in load_history[-48:]]  # Last 48 data points
    load_values = [p for t, p in load_history[-48:]]
    battery_values = [p for t, p in battery_history[-48:]]
    
    # Solar conditions
    solar_conditions = analyze_solar_conditions(weather_forecast)
    
    # Get inverter status for display
    inverters_sorted = sorted(latest_data.get("inverters", []), key=lambda x: x.get('Order', 99))
    
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
            background: {'linear-gradient(135deg, #8B0000 0%, #DC143C 100%)' if backup_voltage < BACKUP_VOLTAGE_THRESHOLD else 
                        'linear-gradient(135deg, #f77f00 0%, #fcbf49 100%)' if backup_active else 
                        'linear-gradient(135deg, #11998e 0%, #38ef7d 100%)' if not has_fault else
                        'linear-gradient(135deg, #8B0000 0%, #DC143C 100%)'};
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
        
        .metric.darkred {{
            background: linear-gradient(135deg, #8B0000 0%, #DC143C 100%);
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
        
        .inverter-card.fault {{
            border-left-color: #dc3545;
            background: #fff3f3;
            animation: pulse 2s infinite;
        }}
        
        @keyframes pulse {{
            0% {{ box-shadow: 0 0 0 0 rgba(220, 53, 69, 0.4); }}
            70% {{ box-shadow: 0 0 0 10px rgba(220, 53, 69, 0); }}
            100% {{ box-shadow: 0 0 0 0 rgba(220, 53, 69, 0); }}
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
        
        @media (max-width: 768px) {{
            .alert-item {{
                grid-template-columns: 1fr;
                gap: 8px;
            }}
            
            .alert-badge {{
                width: fit-content;
            }}
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
        
        .fault-indicator {{
            display: inline-block;
            background: #dc3545;
            color: white;
            padding: 5px 15px;
            border-radius: 20px;
            font-size: 0.9em;
            margin-left: 10px;
            animation: blink 1s infinite;
        }}
        
        @keyframes blink {{
            0% {{ opacity: 1; }}
            50% {{ opacity: 0.5; }}
            100% {{ opacity: 1; }}
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
                <h2>
                    {'🚨 GENERATOR ACTIVE / STARTING' if backup_voltage < BACKUP_VOLTAGE_THRESHOLD else 
                     '⚠️ BACKUP SYSTEM ACTIVE' if backup_active else 
                     '⚠️ SYSTEM FAULT DETECTED' if has_fault else 
                     '✓ NORMAL OPERATION'}
                     {has_fault and backup_voltage >= BACKUP_VOLTAGE_THRESHOLD and not backup_active and '<span class="fault-indicator">FAULT</span>' or ''}
                </h2>
                <div class="status-text">
                    {f'Backup battery voltage critical ({backup_voltage:.1f}V). Generator should be running.' if backup_voltage < BACKUP_VOLTAGE_THRESHOLD else
                     f'Primary batteries below {PRIMARY_BATTERY_THRESHOLD}%. Backup inverter supplying {inverters_sorted[2].get("OutputPower", 0) if len(inverters_sorted) > 2 else 0:.0f}W to system.' if backup_active else
                     '⚠️ One or more inverters reporting faults. Check inverter details.' if has_fault else
                     'All systems operating on primary batteries.'}
                </div>
            </div>
"""
    
    # Weather alert
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
            </div>
"""
    
    # Load analysis note
    load_note = ""
    if total_load > 2000 and latest_data.get('total_battery_discharge_W', 0) < 500:
        load_note = f"<div style='background: #e7f3ff; padding: 10px; border-radius: 5px; margin-bottom: 15px;'><strong>ℹ️ Note:</strong> High load ({total_load:.0f}W) is being supported by solar ({solar_generation:.0f}W), not batteries.</div>"
    elif total_load > 2000:
        load_note = f"<div style='background: #fff3cd; padding: 10px; border-radius: 5px; margin-bottom: 15px;'><strong>⚠️ Warning:</strong> High load ({total_load:.0f}W) is draining batteries ({latest_data.get('total_battery_discharge_W', 0):.0f}W discharge).</div>"
    
    html += load_note
    
    html += f"""
            <div class="metrics-grid">
                <div class="metric {primary_color}">
                    <div class="metric-label">Primary Batteries</div>
                    <div class="metric-value">{primary_battery:.0f}%</div>
                    <div class="metric-subtext">Inverters 1 & 2 (Minimum)</div>
                </div>
                
                <div class="metric {backup_color}">
                    <div class="metric-label">Backup Battery</div>
                    <div class="metric-value">{backup_voltage:.1f}V</div>
                    <div class="metric-subtext">{backup_status} {'(Generator threshold: 51.2V)' if backup_voltage >= BACKUP_VOLTAGE_THRESHOLD else '(GENERATOR STARTING!)'}</div>
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
                
                <div class="metric {'red' if solar_generation < 100 else 'green'}">
                    <div class="metric-label">Solar Generation</div>
                    <div class="metric-value">{solar_generation:.0f}W</div>
                    <div class="metric-subtext">Total PV Power</div>
                </div>
                
                <div class="metric {'orange' if latest_data.get('total_battery_discharge_W', 0) > 500 else 'green'}">
                    <div class="metric-label">Battery Discharge</div>
                    <div class="metric-value">{latest_data.get('total_battery_discharge_W', 0):.0f}W</div>
                    <div class="metric-subtext">Total from all batteries</div>
                </div>
            </div>
            
            <h2>Inverter Details</h2>
            <div class="inverter-grid">
"""
    
    # Display each inverter in order
    for inv in inverters_sorted:
        is_backup = inv.get('Type') == 'backup'
        has_fault = inv.get('faultCode', 0) != 0 or inv.get('errorCode', 0) != 0
        label = inv.get('Label', 'Unknown')
        sn = inv.get('SN', 'N/A')
        datalog = inv.get('DataLog', 'N/A')
        status_text = inv.get('StatusText', 'Unknown')
        fault_code = inv.get('faultCode', 0)
        error_code = inv.get('errorCode', 0)
        
        html += f"""
                <div class="inverter-card {'backup' if is_backup else ''} {'fault' if has_fault else ''}">
                    <h3>{label} {has_fault and '🔴' or ''}</h3>
                    <div class="inv-label">{sn} ({datalog})</div>
                    {has_fault and f'<div style="background: #ffebee; color: #c62828; padding: 8px; border-radius: 5px; margin-bottom: 10px; font-size: 0.9em;">⚠️ FAULT: Code {fault_code} | Error: {error_code}</div>' or ''}
                    <div class="inv-stat">
                        <span class="inv-stat-label">Status</span>
                        <span class="inv-stat-value">{status_text}</span>
                    </div>
        """
        
        if is_backup:
            # For backup inverter, show voltage and status
            backup_status, _ = get_backup_battery_status(inv.get('vBat', 0))
            html += f"""
                    <div class="inv-stat">
                        <span class="inv-stat-label">Battery Voltage</span>
                        <span class="inv-stat-value">{inv.get('vBat', 0):.1f}V ({backup_status})</span>
                    </div>
            """
        else:
            # For primary inverters, show capacity
            html += f"""
                    <div class="inv-stat">
                        <span class="inv-stat-label">Battery Capacity</span>
                        <span class="inv-stat-value">{inv.get('Capacity', 0):.0f}%</span>
                    </div>
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
                        <span class="inv-stat-label">Solar Power</span>
                        <span class="inv-stat-value">{inv.get('ppv', 0):.0f}W</span>
                    </div>
                    <div class="inv-stat">
                        <span class="inv-stat-label">Last Update</span>
                        <span class="inv-stat-value">{inv.get('time', 'Unknown')}</span>
                    </div>
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
        
        # Get alert type styling
        def get_alert_badge(alert_type):
            badges = {
                "critical": ("🔴", "Critical", "#dc3545"),
                "very_high_load": ("🔴", "Very High Load", "#dc3545"),
                "backup_active": ("🟠", "Backup Active", "#ff9800"),
                "high_load": ("🟠", "High Load", "#ff9800"),
                "warning": ("🟡", "Warning", "#ffc107"),
                "moderate_load": ("🔵", "Moderate Load", "#2196F3"),
                "fault": ("🔴", "Fault", "#dc3545"),
                "comms_loss": ("🔴", "Comms Loss", "#8B0000"),
                "test": ("⚪", "Test", "#9e9e9e"),
                "general": ("⚪", "Info", "#9e9e9e")
            }
            return badges.get(alert_type, ("⚪", "Unknown", "#9e9e9e"))
        
        # Display alerts newest first
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
