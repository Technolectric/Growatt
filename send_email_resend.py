import os
import time
import requests
import json
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
# ----------------------------12000) 
    
    b_wh_total = BACKUP_BATTERY_DE
API_URL = "https://openapi.growatt.com/v1/device/storage/storage_GRADED_WH * 0.9 
    b_usable_wh = max(0, b_last_data"
TOKEN = os.getenv("API_TOKEN")
SERIAL_NUMBERS = os.getenvwh_total - 4200) 
    
    current_system_wh = p_usable("SERIAL_NUMBERS", "").split(",")
POLL_INTERVAL_MINUTES = int(os.getenv("_daily_wh + b_usable_wh
    trace_total_pct = [(current_system_whPOLL_INTERVAL_MINUTES", 5))

# ----------------------------
# Inverter Configuration (Display order / TOTAL_SYSTEM_USABLE_WH) * 100]
    
    limit = min(: 1, 2, 3)
# ----------------------------
INVERTER_CONFIG = {
    "RKG3B0400T": {"label": "Inverter 1", "type":len(solar_forecast_data), len(load_forecast_data))
    generator_needed = False
 "primary", "datalog": "DDD0B021CC", "display_order": 1},    time_empty = None
    switchover_occurred = False
    accumulated_genset_wh = 0
    
    for i in range(limit):
        solar = solar_forecast_data[
    "KAM4N5W0AG": {"label": "Inverter 2", "type": "i]['estimated_generation']
        load = load_forecast_data[i]['estimated_load']
        primary", "datalog": "DDD0B02121", "display_order": 2},time_str = solar_forecast_data[i]['time'].strftime("%I:%M %p")
        
    "JNK1CDR0KQ": {"label": "Inverter 3 (Backup)", "type":net_watts = load - solar
        energy_step_wh = net_watts * 1.0  "backup", "datalog": "DDD0B0221H", "display_order": 3
        
        if energy_step_wh > 0:
            if p_usable_daily_wh}
}

# Alert thresholds
PRIMARY_BATTERY_THRESHOLD = 40  # When backup kicks in >= energy_step_wh:
                p_usable_daily_wh -= energy_step_wh
            else:
                remaining_deficit
BACKUP_VOLTAGE_THRESHOLD = 51.2  # When generator starts
TOTAL_SOLAR_CAPACITY_KW = 10
PRIMARY_INVERTER_CAPACITY_W = 10 = energy_step_wh - p_usable_daily_wh
                p_usable_daily_wh = 0
                switchover_000  # 10kW combined
BACKUP_INVERTER_CAPACITY_W = occurred = True
                if b_usable_wh >= remaining_deficit:
                    b_usable_wh5000    # 5kW

# Backup battery voltage status thresholds
BACKUP_VOLTAGE_GOOD -= remaining_deficit
                else:
                    b_usable_wh = 0
                    generator_needed = = 53.0   # Good status
BACKUP_VOLTAGE_MEDIUM = 52.3 True
                    accumulated_genset_wh += (remaining_deficit - b_usable_wh)
                      # Medium status  
BACKUP_VOLTAGE_LOW = 52.0     # Low status

if time_empty is None: time_empty = time_str
        else:
            surplus = abs# Temperature thresholds
INVERTER_TEMP_WARNING = 60  # °C
INVERTER_(energy_step_wh)
            space_p = 18000 - p_usable_TEMP_CRITICAL = 70  # °C

# Communication timeout
COMMUNICATION_TIMEOUT_MINdaily_wh
            if surplus <= space_p: p_usable_daily_wh += surplus
            elseUTES = 10

# Battery Specifications (LiFePO4)
# Primary: 30kWh Total:
                p_usable_daily_wh = 18000
                surplus -= space_ Physical
# 0-20% (6kWh): Hard Cutoff (Unusable)
# 2p
                space_b = 16800 - b_usable_wh
                if surplus <=0-40% (6kWh): Emergency Reserve (Manual/Emergency)
# 40-10 space_b: b_usable_wh += surplus
                else: b_usable_wh = 16800
        
        pct = ((p_usable_daily_wh + b_usable_wh)0% (18kWh): Daily Automated Cycling
PRIMARY_BATTERY_CAPACITY_WH = 30000 
PRIMARY_DAILY_MIN_PCT = 40 

# Backup: 21 / TOTAL_SYSTEM_USABLE_WH) * 100
        trace_total_pct.appendkWh Total Physical (Degraded)
# 0-20% (4.2kWh): Hard Cutoff(pct)
        
    return {
        'trace_total_pct': trace_total_pct,
        'generator_needed':
# 20-100% (16.8kWh): Usable
BACKUP_BATTERY_DEGRADED_WH = 21000   
BACKUP_CUTOFF_PCT = generator_needed,
        'time_empty': time_empty,
        'switchover_occurred': switchover_occurred,
        'genset_hours': accumulated_genset_wh / 50 20

# Total System "Automated" Capacity (for 0-100% chart)
00 
    }

def update_solar_pattern(current_generation):
    now = datetime.# 18kWh (Primary Daily) + 16.8kWh (Backup Usable) = 34.8kWh
TOTAL_SYSTEM_USABLE_WH = 34800 

# now(EAT)
    hour = now.hour
    clean_generation = 0.0 if (----------------------------
# Location Config (Kajiado, Kenya)
# ----------------------------
LATITUDE = -1.85238
LONGITUDE = 36.77683

# ----------------------------hour < 6 or hour >= 19) else current_generation
    solar_generation_pattern.append({'timestamp': now, 'hour': hour, 'generation': clean_generation, 'max_possible': TOTAL_
# Email (Resend) Config
# ----------------------------
RESEND_API_KEY = os.getenvSOLAR_CAPACITY_KW * 1000})

def update_load_pattern(current_('RESEND_API_KEY')
SENDER_EMAIL = os.getenv('SENDER_EMAIL')
load):
    now = datetime.now(EAT)
    load_demand_pattern.append({'timestampRECIPIENT_EMAIL = os.getenv('RECIPIENT_EMAIL')

# ----------------------------
': now, 'hour': now.hour, 'load': current_load})

# ----------------------------
## Globals
# ----------------------------
headers = {"token": TOKEN, "Content-Type": "application/ Email function
# ----------------------------
def send_email(subject, html_content, alert_type="general", send_via_email=True):
    global last_alert_time, alert_history
    cox-www-form-urlencoded"}
last_alert_time = {}
latest_data = {}
loadoldown_map = {
        "critical": 60, "very_high_load": 30_history = []
battery_history = []
weather_forecast = {}
weather_source = "Initializing...", "backup_active": 120, "high_load": 60,
        "moderate
solar_conditions_cache = None
alert_history = []
last_communication = {}

# Solar forecast_load": 120, "warning": 120, "communication_lost": 60 globals
solar_forecast = []
solar_generation_pattern = deque(maxlen=48)
load_, "fault_alarm": 30,
        "high_temperature": 60, "test":demand_pattern = deque(maxlen=48)
SOLAR_EFFICIENCY_FACTOR = 0.8 0, "general": 120
    }
    cooldown_minutes = cooldown_map.5
FORECAST_HOURS = 12

# East African Timezone
EAT = timezone(timedelta(hours=3))

# ----------------------------
# Weather & Analysis Functions
# ----------------------------
def getget(alert_type, 120)
    
    if alert_type in last_alert__weather_from_openmeteo():
    try:
        url = f"https://api.opentime and cooldown_minutes > 0:
        if datetime.now(EAT) - last_alert_-meteo.com/v1/forecast?latitude={LATITUDE}&longitude={LONGITUDE}&hourly=cloudtime[alert_type] < timedelta(minutes=cooldown_minutes):
            return False
    
    _cover,shortwave_radiation&timezone=Africa/Nairobi&forecast_days=2"
        responseaction_successful = False
    if send_via_email and all([RESEND_API_KEY, S = requests.get(url, timeout=10)
        response.raise_for_status()
        ENDER_EMAIL, RECIPIENT_EMAIL]):
        try:
            response = requests.post(
data = response.json()
        return {
            'times': data['hourly']['time'],
            '                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {REScloud_cover': data['hourly']['cloud_cover'],
            'solar_radiation': data['hourly']['shortEND_API_KEY}", "Content-Type": "application/json"},
                json={"from": SENDERwave_radiation'],
            'source': 'Open-Meteo'
        }
    except Exception:_EMAIL, "to": [RECIPIENT_EMAIL], "subject": subject, "html": html_
        return None

def get_weather_from_weatherapi():
    try:
        WEATHERAPI_content}
            )
            if response.status_code == 200: action_successful = True
        except Exception as e: print(f"✗ Error sending email: {e}")
    else:
KEY = os.getenv("WEATHERAPI_KEY") 
        url = f"http://api.weatherapi.com/v1/forecast.json?key={WEATHERAPI_KEY}&q={LATITUDE},{LONGITUDE        print(f"ℹ️ Alert logged to Dashboard: {subject}")
        action_successful = True

    }&days=2"
        response = requests.get(url, timeout=10)
        if responseif action_successful:
        now = datetime.now(EAT)
        last_alert_time[.status_code == 200:
            data = response.json()
            times, cloud_alert_type] = now
        alert_history.append({"timestamp": now, "type": alert_typecover, solar_radiation = [], [], []
            for day in data.get('forecast', {}).get(', "subject": subject})
        cutoff = now - timedelta(hours=12)
        alert_historyforecastday', []):
                for hour in day.get('hour', []):
                    times.append([:] = [a for a in alert_history if a['timestamp'] >= cutoff]
        return True
    hour['time'])
                    cloud_cover.append(hour['cloud'])
                    uv = hour.get('return False

# ----------------------------
# Alert Logic
# ----------------------------
def check_and_send_uv', 0)
                    solar_radiation.append(uv * 120) 
            ifalerts(inverter_data, solar_conditions, total_solar_input, total_battery_discharge, generator times:
                return {'times': times, 'cloud_cover': cloud_cover, 'solar_radiation': solar_radiation, 'source': 'WeatherAPI'}
        return None
    except Exception:
        return None_running):
    inv1 = next((i for i in inverter_data if i['SN'] == '
        
def get_weather_from_7timer():
    try:
        url = f"httpRKG3B0400T'), None)
    inv2 = next((i for i in inverter_data if i['SN'] == 'KAM4N5W0AG'), None)
    inv3_://www.7timer.info/bin/api.pl?lon={LONGITUDE}&lat={LATITUDE}&backup = next((i for i in inverter_data if i['SN'] == 'JNK1CDR0KQproduct=civil&output=json"
        response = requests.get(url, timeout=15)
'), None)
    if not all([inv1, inv2, inv3_backup]): return
    
        response.raise_for_status()
        data = response.json()
        times, cloud_cover    primary_capacity = min(inv1['Capacity'], inv2['Capacity'])
    backup_voltage = inv, solar_radiation = [], [], []
        base_time = datetime.now(EAT)
        for3_backup['vBat']
    backup_active = inv3_backup['OutputPower'] > 5 item in data.get('dataseries', [])[:48]:
            hour_offset = item.get0
    total_load = inv1['OutputPower'] + inv2['OutputPower'] + inv3_('timepoint', 0)
            forecast_time = base_time + timedelta(hours=hour_offset)backup['OutputPower']
    
    for inv in inverter_data:
        if inv.get('communication
            times.append(forecast_time.strftime('%Y-%m-%dT%H:%M'))
            _lost', False): send_email(f"⚠️ Communication Lost: {inv['Label']}", "Check invertercloud_val = item.get('cloudcover', 5)
            cloud_pct = min((cloud_.", "communication_lost")
        if inv.get('has_fault', False): send_email(fval * 12), 100)
            cloud_cover.append(cloud_pct)
"🚨 FAULT ALARM: {inv['Label']}", "Inverter fault.", "fault_alarm")
            solar_est = max(800 * (1 - cloud_pct/100), 0        if inv.get('high_temperature', False): send_email(f"🌡️ HIGH TEMPERATURE: {)
            solar_radiation.append(solar_est)
        if times:
            return {'times':inv['Label']}", f"Temp: {inv.get('temperature',0)}", "high_temperature") times, 'cloud_cover': cloud_cover, 'solar_radiation': solar_radiation, 'source': '
    
    if generator_running or backup_voltage < BACKUP_VOLTAGE_THRESHOLD:
        send7Timer'}
        return None
    except Exception:
        return None

def get_fallback_weather():_email("🚨 CRITICAL: Generator Running - Backup Battery Critical", "Action Required.", "critical")
        return
    times, clouds, rads = [], [], []
    now = datetime.now(EAT).replace
    
    if backup_active and primary_capacity < PRIMARY_BATTERY_THRESHOLD:
        send_(minute=0, second=0, microsecond=0)
    for i in range(48):email("⚠️ HIGH ALERT: Backup Inverter Active", "Reduce Load.", "backup_active")
        return

        t = now + timedelta(hours=i)
        times.append(t.isoformat())
    
    if 40 < primary_capacity < 50:
        send_email("⚠️ WARNING: Primary Battery Low", "Reduce Load.", "warning", send_via_email=backup_active)
            clouds.append(20)
        h = t.hour
        rads.append(max(0, 1000 - (abs(12 - h) * 150)) if 
    if total_battery_discharge >= 4500:
        send_email("🚨 URGENT: Very High Battery Discharge", "Critical Discharge.", "very_high_load", send_via_email=backup_active)
    elif 2500 <= total_battery_discharge < 35006 <= h <= 18 else 0)
    return {'times': times, 'cloud_cover'::
        send_email("⚠️ WARNING: High Battery Discharge", "High Discharge.", "high_load", send_via_email=backup_active)
    elif 1500 <= total_battery_discharge < 2000 and primary_capacity < 50:
        send_email("ℹ️ INFO: clouds, 'solar_radiation': rads, 'source': 'Synthetic (Offline)'}

def get_weather Moderate Battery Discharge - Low Battery", "Moderate Discharge.", "moderate_load", send_via_email=backup__forecast():
    global weather_source
    print("🌤️ Fetching weather forecast...")
    sources =active)

# ----------------------------
# Polling Loop
# ----------------------------
def poll_growatt(): [("Open-Meteo", get_weather_from_openmeteo), ("WeatherAPI", get_weather_from_weatherapi), ("7Timer", get_weather_from_7timer)]
    for source_name, fetch_func in sources:
        forecast = fetch_func()
        if forecast and len(forecast.get('times', [])) > 0:
            weather_source = forecast['source']
            return forecast
    weather_source = "Synthetic (Offline)"
    return get_fallback_weather()

def
    global latest_data, load_history, battery_history, weather_forecast, last_communication, solar analyze_solar_conditions(forecast):
    if not forecast: return None
    try:
        now =_conditions_cache
    weather_forecast = get_weather_forecast()
    if weather_forecast: solar datetime.now(EAT)
        current_hour = now.hour
        is_nighttime = current_conditions_cache = analyze_solar_conditions(weather_forecast)
    last_weather_update = datetime_hour < 6 or current_hour >= 18
        if is_nighttime:
            tomorrow.now(EAT)
    
    while True:
        try:
            now = datetime.now = now + timedelta(days=1)
            start_time = tomorrow.replace(hour=6, minute(EAT)
            cutoff_time = now - timedelta(hours=12)
            alert_history=0, second=0, microsecond=0)
            end_time = tomorrow.replace(hour=[:] = [a for a in alert_history if a['timestamp'] >= cutoff_time]
            
            18, minute=0, second=0, microsecond=0)
            analysis_label = "Tomorrowif now - last_weather_update > timedelta(minutes=30):
                weather_forecast = get_'s Daylight"
        else:
            start_time = now
            end_time = now.replaceweather_forecast()
                if weather_forecast: solar_conditions_cache = analyze_solar_conditions(weather(hour=18, minute=0, second=0, microsecond=0)
            analysis_label_forecast)
                last_weather_update = now
            
            total_output_power, total_ = "Today's Remaining Daylight"
        
        avg_cloud_cover, avg_solar_radiation, count = 0, 0, 0
        for i, time_str in enumerate(forecast['timesbattery_discharge_W, total_solar_input_W = 0, 0, 0
            inverter_data, primary_']):
            try:
                forecast_time = datetime.fromisoformat(time_str.replace('Zcapacities = [], []
            backup_data, generator_running = None, False
            
            for sn', '')) if 'T' in time_str else datetime.strptime(time_str, '%Y-%m in SERIAL_NUMBERS:
                try:
                    response = requests.post(API_URL, data={"storage_sn": sn},-%d %H:%M')
                if forecast_time.tzinfo is None: forecast_time = forecast headers=headers, timeout=20)
                    response.raise_for_status()
                    data = response_time.replace(tzinfo=EAT)
                else: forecast_time = forecast_time.ast.json().get("data", {})
                    last_communication[sn] = now
                    config = INVERimezone(EAT)
                
                if start_time <= forecast_time <= end_time and TER_CONFIG.get(sn, {"label": sn, "type": "unknown", "display_order":6 <= forecast_time.hour <= 18:
                    avg_cloud_cover += forecast['cloud_ 99})
                    
                    out_power = float(data.get("outPutPower") or cover'][i]
                    avg_solar_radiation += forecast['solar_radiation'][i]
                    count +=0)
                    capacity = float(data.get("capacity") or 0)
                    v_bat = 1
            except: continue
        
        if count > 0:
            avg_cloud_cover float(data.get("vBat") or 0)
                    p_bat = float(data.get /= count
            avg_solar_radiation /= count
            return {
                'avg_cloud_cover':("pBat") or 0)
                    ppv = float(data.get("ppv") or  avg_cloud_cover,
                'avg_solar_radiation': avg_solar_radiation,
                'poor_conditions': avg_cloud_cover > 70 or avg_solar_radiation < 2000) + float(data.get("ppv2") or 0)
                    
                    temp = max,
                'analysis_period': analysis_label,
                'is_nighttime': is_nighttime(float(data.get("invTemperature") or 0), float(data.get("dcDcTemperature")
            }
    except Exception: pass
    return None

# ----------------------------
# Helper Functions
# or 0), float(data.get("temperature") or 0))
                    
                    has_fault = ----------------------------
def get_backup_voltage_status(voltage):
    if voltage >= BACKUP_VOL int(data.get("errorCode") or 0) != 0 or int(data.get("faultCodeTAGE_GOOD: return "Good", "green"
    elif voltage >= BACKUP_VOLTAGE_MEDIUM:") or 0) != 0
                    
                    vac = float(data.get("vac") or  return "Medium", "orange"
    else: return "Low", "red"

def check_generator_running(backup_inverter_data):
    if not backup_inverter_data: return False
    0)
                    pac_input = float(data.get("pAcInPut") or 0)
                    
                    total_output_power += out_power
                    total_solar_input_W += ppvreturn float(backup_inverter_data.get('vac', 0) or 0) > 1
                    if p_bat > 0: total_battery_discharge_W += p_bat
                    
00 or float(backup_inverter_data.get('pAcInPut', 0) or                     inv_info = {
                        "SN": sn, "Label": config['label'], "Type": config0) > 50

def analyze_historical_solar_pattern():
    if len(solar_generation['type'], "DisplayOrder": config['display_order'],
                        "OutputPower": out_power, "_pattern) < 3: return None
    pattern = []
    hour_map = {}
    forCapacity": capacity, "vBat": v_bat, "pBat": p_bat, "ppv": hour_data in solar_generation_pattern:
        hour = hour_data['hour']
        max_ ppv,
                        "temperature": temp, "high_temperature": temp >= INVERTER_TEMP_WARNINGpossible = hour_data.get('max_possible', TOTAL_SOLAR_CAPACITY_KW * 1, "Status": data.get("statusText", "Unknown"),
                        "has_fault": has_fault000)
        if hour not in hour_map: hour_map[hour] = []
        , "fault_info": {"errorCode": int(data.get("errorCode") or 0), "faultCodeif max_possible > 0: hour_map[hour].append(hour_data['generation'] / max": int(data.get("faultCode") or 0)},
                        "communication_lost": False, "_possible)
    for hour, values in hour_map.items(): pattern.append((hour, np.last_seen": now.strftime("%Y-%m-%d %H:%M:%S")
                    }
mean(values)))
    return pattern

def analyze_historical_load_pattern():
    if len(load                    inverter_data.append(inv_info)
                    
                    if config['type'] == 'primary_demand_pattern) < 3: return None
    pattern = []
    hour_map = {}
' and capacity > 0: primary_capacities.append(capacity)
                    elif config['type'] ==    for hour_data in load_demand_pattern:
        hour = hour_data['hour']
         'backup':
                        backup_data = inv_info
                        if vac > 100 or pac_if hour not in hour_map: hour_map[hour] = []
        hour_map[hour].input > 50: generator_running = True
                except:
                    if sn in last_communication andappend(hour_data['load'])
    for hour, values in hour_map.items(): pattern.append now - last_communication[sn] > timedelta(minutes=COMMUNICATION_TIMEOUT_MINUTES):
                        ((hour, 0, np.mean(values)))
    return pattern

def get_hourly_weather_config = INVERTER_CONFIG.get(sn, {})
                        inverter_data.append({"SN":forecast(weather_data, num_hours=12):
    hourly_forecast = []
    now = sn, "Label": config.get('label', sn), "Type": config.get('type'), "Display datetime.now(EAT)
    if not weather_data: return hourly_forecast
    weather_times = []
    for i, time_str in enumerate(weather_data['times']):
        try:
Order": config.get('display_order', 99), "communication_lost": True})
            
            inverter_data            forecast_time = datetime.fromisoformat(time_str.replace('Z', '')) if 'T.sort(key=lambda x: x.get('DisplayOrder', 99))
            update_solar' in time_str else datetime.strptime(time_str, '%Y-%m-%d %H:%M')
            if forecast_time.tzinfo is None: forecast_time = forecast_time.replace(tzinfo=EAT)
            else: forecast_time = forecast_time.astimezone(EAT)
            weather_times.append({
                'time': forecast_time,
                'cloud_cover': weather_data['cloud_cover'][i] if i < len(weather_data['cloud_cover']) else 50,
                '_pattern(total_solar_input_W)
            update_load_pattern(total_output_power)
            
            load_history.append((now, total_output_power))
            load_history[:] = [(t, p) for t, p in load_history if t >= now - timedelta(hours=12)]
            battery_history.append((now, total_battery_discharge_W))
            battery_history[:] = [(t, p) for t, p in battery_history if t >= now - timedelta(solar_radiation': weather_data['solar_radiation'][i] if i < len(weather_data['solarhours=12)]
            
            solar_pattern = analyze_historical_solar_pattern()
            load_pattern = analyze_historical_load_pattern()
            
            solar_forecast = generate_solar_forecast_radiation']) else 0
            })
        except: continue
    
    if not weather_times: return hourly_forecast
    weather_times.sort(key=lambda x: x['time'])
    for(weather_forecast, solar_pattern)
            moving_avg_load = calculate_moving_average_load hour_offset in range(num_hours):
        forecast_time = now + timedelta(hours=hour_(45)
            load_forecast = generate_load_forecast(load_pattern, moving_avg_load)
            
            primary_battery_min = min(primary_capacities) if primary_capacitiesoffset)
        closest = min(weather_times, key=lambda x: abs(x['time'] - forecast_time))
        hourly_forecast.append({
            'time': forecast_time,
            'hour': forecast_time.hour,
            'cloud_cover': closest['cloud_cover'],
            ' else 0
            backup_battery_voltage = backup_data['vBat'] if backup_data else solar_radiation': closest['solar_radiation']
        })
    return hourly_forecast

def apply_solar0
            backup_voltage_status, backup_voltage_color = get_backup_voltage_status(backup_curve(generation, hour_of_day):
    if hour_of_day < 6 or hour_of_day >= 19: return 0.0
    solar_day_length = 1_battery_voltage)
            backup_active = backup_data['OutputPower'] > 50 if backup_data else False
            
            backup_percent_calc = max(0, min(100,3.0
    solar_hour = (hour_of_day - 6) / solar_day_length
    curve_factor = np.sin(solar_hour * np.pi) ** 2
    if hour_of_day <= 7 or hour_of_day >= 18: curve_factor *= 0.7
    return generation * curve_factor

def generate_solar_forecast(weather_forecast_ (backup_battery_voltage - 51.0) / 2.0 * 100))
            backup_kwh_calc = (backup_percent_calc / 100) * (BACKUP_BATTERY_DEGRADED_WH / 1000)
            
            battery_life_prediction = calculate_battery_cascade(solar_forecast, load_forecast, primary_battery_min, backup_active)
            
            latest_data = {
                "timestamp": now.strftime("%Y-%m-%d %H:%M:%S EAT"),
                "total_output_power": total_output_power,
                "total_battery_discharge_W": total_battery_discharge_W,
                "data, historical_pattern):
    forecast = []
    hourly_weather = get_hourly_weather_forecast(weather_forecast_data, FORECAST_HOURS)
    max_possible_generation = TOTAL_SOLtotal_solar_input_W": total_solar_input_W,
                "primary_battery_minAR_CAPACITY_KW * 1000
    
    for hour_data in hourly_weather:
        hour_of_day = hour_data['hour']
        if hour_of_day <": primary_battery_min,
                "backup_battery_voltage": backup_battery_voltage,
                "backup_voltage_status": backup_voltage_status,
                "backup_voltage_color": backup_voltage_color,
                " 6 or hour_of_day >= 19:
            blended_generation = 0.0
            cloud_factor = 0.0
            cloud_cover = 100
            solar_backup_active": backup_active,
                "backup_percent_calc": backup_percent_calc,
radiation = 0
        else:
            cloud_cover = hour_data['cloud_cover']
                            "backup_kwh_calc": backup_kwh_calc,
                "generator_running": generatorsolar_radiation = hour_data['solar_radiation']
            cloud_factor = max(0.1,_running,
                "inverters": inverter_data,
                "solar_forecast": solar_forecast,
                "load_forecast": load_forecast,
                "battery_life_prediction": battery_life_ (1 - (cloud_cover / 100)) ** 1.5)
            theoretical_generationprediction,
                "historical_pattern_count": len(solar_generation_pattern),
                "load_ = (solar_radiation / 1000) * max_possible_generation * SOLAR_EFFICIENCY_pattern_count": len(load_demand_pattern)
            }
            
            print(f"{latestFACTOR
            curve_adjusted_generation = apply_solar_curve(theoretical_generation, hour_of_day_data['timestamp']} | Load={total_output_power:.0f}W | Solar={total_solar)
            
            if historical_pattern:
                pattern_factor = 0.0
                for pattern_hour, normalized in historical_pattern:
                    if pattern_hour == hour_of_day:
                        _input_W:.0f}W")
            check_and_send_alerts(inverter_data, solar_conditions_cache, total_solar_input_W, total_battery_discharge_W, generatorpattern_factor = normalized
                        break
                blended_generation = (curve_adjusted_generation * 0_running)
        except Exception as e: print(f"❌ Error in polling: {e}")
        .6 + (pattern_factor * max_possible_generation) * 0.4)
            else:time.sleep(POLL_INTERVAL_MINUTES * 60)

# ----------------------------
# Flask
                blended_generation = curve_adjusted_generation
        
        forecast.append({
            'time': hour_data['time'],
            'hour': hour_of_day,
            'estimated_generation Web Routes
# ----------------------------
@app.route("/")
def home():
    # Data Extraction
    primary_battery = latest_data.get("primary_battery_min", 0)
    backup_voltage': max(0, blended_generation),
            'cloud_cover': cloud_cover,
            'solar_radiation': solar_radiation,
            'cloud_factor': cloud_factor
        })
    return forecast = latest_data.get("backup_battery_voltage", 0)
    backup_voltage_status =

def get_default_load_for_hour(hour):
    if 0 <= hour < 5 latest_data.get("backup_voltage_status", "Unknown") # Extracted correctly
    backup_active =: return 600
    if 5 <= hour < 8: return 1800
 latest_data.get("backup_active", False)
    generator_running = latest_data.get("    if 8 <= hour < 17: return 1200
    if 17 <=generator_running", False)
    total_load = latest_data.get("total_output_power", hour < 22: return 2800
    return 1000

def calculate_ 0)
    total_solar = latest_data.get("total_solar_input_W", moving_average_load(history_minutes=45):
    now = datetime.now(EAT)0)
    total_battery_discharge = latest_data.get("total_battery_discharge_W", 0)
    
    primary_kwh_real = (primary_battery / 100.
    cutoff = now - timedelta(minutes=history_minutes)
    recent_loads = [power for time0) * (PRIMARY_BATTERY_CAPACITY_WH / 1000.0)
    , power in load_history if time >= cutoff]
    return sum(recent_loads) / len(recent_loads) if recent_loads else 0

def generate_load_forecast(historical_pattern, moving_backup_percent_display = latest_data.get("backup_percent_calc", 0)
    backupavg_load=0):
    forecast = []
    now = datetime.now(EAT)
    _kwh_display = latest_data.get("backup_kwh_calc", 0)
    
    # 1. Appliance Safety Status
    if generator_running:
        app_status = "CR
    for hour_offset in range(FORECAST_HOURS):
        forecast_time = now + timedeltaITICAL: GENERATOR ON"
        app_sub = "Stop ALL non-essential loads immediately"
        app(hours=hour_offset)
        hour_of_day = forecast_time.hour
        base_load = 0
        found = False
        if historical_pattern:
            for pattern_hour, _,_color = "critical"
    elif backup_active:
        app_status = "❌ STOP HEAVY LOADS"
        app_sub = "Backup Battery is Active. Save power."
        app_color = actual_load in historical_pattern:
                if pattern_hour == hour_of_day:
                    base "critical"
    elif primary_battery < 45 and total_solar < total_load:
        _load = actual_load
                    found = True
                    break
        if not found: base_load =app_status = "⚠️ REDUCE LOADS"
        app_sub = "Primary Battery Low & Discharging get_default_load_for_hour(hour_of_day)
        
        is_spike =."
        app_color = "warning"
    elif total_solar > (total_load + 5 False
        if moving_avg_load > 0 and base_load > 0 and moving_avg_00):
        app_status = "✅ OVEN/KETTLE SAFE"
        app_subload > (base_load * 1.5): is_spike = True
        
        if moving_ = "Solar is covering consumption."
        app_color = "good"
    else:
        app_avg_load > 0:
            if hour_offset == 0: final_load = (moving_status = "ℹ️ MONITOR USAGE"
        app_sub = "System running normally."
        app_avg_load * 0.8) + (base_load * 0.2)
            elif hourcolor = "normal"

    # 2. Net Flow Visual
    net_watts = total_solar -_offset == 1: final_load = (moving_avg_load * 0.3) + ( total_load
    if net_watts > 100:
        flow_text = f"Chargingbase_load * 0.7) if is_spike else (moving_avg_load * 0. (+{net_watts:.0f}W)"
        flow_color = "#28a7455) + (base_load * 0.5)
            elif hour_offset == 2: final" # Green
        flow_icon = "⚡🔋"
    elif net_watts < -100_load = base_load if is_spike else (moving_avg_load * 0.2) +:
        flow_text = f"Draining ({net_watts:.0f}W)"
        flow (base_load * 0.8)
            else: final_load = base_load
        else: final_load = base_load
            
        forecast.append({'time': forecast_time, 'hour_color = "#dc3545" # Red
        flow_icon = "🔋🔻"
    else:
        flow_text = "Balanced"
        flow_color = "#17a2b8': hour_of_day, 'estimated_load': final_load})
    return forecast

# ----------------------------
# Cascade Simulation
#"
        flow_icon = "⚖️"

    # 3. Solar Badge
    solar_cond ----------------------------
def calculate_battery_cascade(solar_forecast_data, load_forecast_data, current = solar_conditions_cache
    if solar_cond and solar_cond['poor_conditions']:
        weather_badge = "☁️ Low_primary_percent, backup_active=False):
    if not solar_forecast_data or not load_forecast_data: return None
    
    p_wh_total = (current_primary_percent /  Solar Day"
        weather_class = "poor"
    else:
        weather_badge = "☀️100.0) * PRIMARY_BATTERY_CAPACITY_WH
    p_usable_daily_ High Solar Day"
        weather_class = "good"

    # Chart & Prediction Data
    battery_life_prediction = latest_data.get("battery_life_prediction")
    sim_times = ["Nowwh = max(0, p_wh_total - 12000) 
    
    "] + [d['time'].strftime('%H:%M') for d in latest_data.get("solar_b_wh_total = BACKUP_BATTERY_DEGRADED_WH * 0.9 
forecast", [])]
    trace_total_pct = []
    
    # 4 & 5.    b_usable_wh = max(0, b_wh_total - 4200)  Prediction Message & Gen Runtime
    pred_message = "Analyzing..."
    gen_hours = 0
    
    
    current_system_wh = p_usable_daily_wh + b_usable_wh
if battery_life_prediction:
        trace_total_pct = battery_life_prediction.get('trace    trace_total_pct = [(current_system_wh / TOTAL_SYSTEM_USABLE_WH) *_total_pct', [])
        if battery_life_prediction.get('generator_needed'):
            gen 100]
    
    limit = min(len(solar_forecast_data), len(load_hours = battery_life_prediction.get('genset_hours', 0)
            pred_message_forecast_data))
    generator_needed = False
    time_empty = None
    switchover_ = f"Gen Runtime: {gen_hours:.1f} Hours"
            pred_class = "criticaloccurred = False
    accumulated_genset_wh = 0
    
    for i in range"
        elif battery_life_prediction.get('switchover_occurred'):
            pred_message =(limit):
        solar = solar_forecast_data[i]['estimated_generation']
        load = load "Will switch to Backup"
            pred_class = "warning"
        else:
            pred_message_forecast_data[i]['estimated_load']
        time_str = solar_forecast_data[i = "Battery Sufficient"
            pred_class = "good"
    
    # 6. Load Speed]['time'].strftime("%I:%M %p")
        net_watts = load - solar
        energy_ometer (Scaled to Alert Levels)
    # Scale bar to 5000W so alerts look impactful
step_wh = net_watts * 1.0 
        
        if energy_step_wh >    visual_max = 5000
    load_pct = min(100, (total 0:
            if p_usable_daily_wh >= energy_step_wh:
                p__load / visual_max) * 100)
    
    # Alert Limits: 15usable_daily_wh -= energy_step_wh
            else:
                remaining_deficit = energy_step00 (Moderate), 2500 (High), 4500 (Critical)
    if_wh - p_usable_daily_wh
                p_usable_daily_wh = 0
                 total_load < 1500:
        load_color = "#28a745"switchover_occurred = True
                if b_usable_wh >= remaining_deficit:
                    b_ # Green
        load_msg = "Normal Usage"
    elif total_load < 2500usable_wh -= remaining_deficit
                else:
                    b_usable_wh = 0
                    generator:
        load_color = "#ffc107" # Yellow/Orange
        load_msg =_needed = True
                    accumulated_genset_wh += (remaining_deficit - b_usable_wh "Moderate Load"
    elif total_load < 4500:
        load_color = "#)
                    if time_empty is None: time_empty = time_str
        else:
            surplus = abs(energy_step_wh)
            space_p = 18000 - pfd7e14" # Orange
        load_msg = "High Load"
    else:
        load_color = "#dc3545" # Red
        load_msg = "CRITICAL LOAD"_usable_daily_wh
            if surplus <= space_p: p_usable_daily_wh += surplus

    html = f"""
<!DOCTYPE html>
<html>
<head>
    <title>Tulia
            else:
                p_usable_daily_wh = 18000
                surplus House - Solar Monitor</title>
    <script src="https://cdn.jsdelivr.net/npm/chart -= space_p
                space_b = 16800 - b_usable_wh
                if surplus <= space_b: b_usable_wh += surplus
                else: b_usable_wh =.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-annotation@2.1.0/dist/chartjs-plugin-annotation.min.js"></script 16800
        
        pct = ((p_usable_daily_wh + b_usable_wh) / TOTAL_SYSTEM_USABLE_WH) * 100
        trace_total_>
    <style>
        * {{ margin: 0; padding: 0; box-sizing:pct.append(pct)
        
    return {
        'trace_total_pct': trace_total border-box; font-family: 'Segoe UI', sans-serif; }}
        body {{ background: #f0f2f5; padding: 20px; }}
        .container {{ max-width:_pct,
        'generator_needed': generator_needed,
        'time_empty': time_empty,
        'switchover_occurred': switchover_occurred,
        'genset_hours': 1200px; margin: 0 auto; }}
        
        /* Top Status Bar */
 accumulated_genset_wh / 5000 
    }

def update_solar_pattern(        .status-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmaxcurrent_generation):
    now = datetime.now(EAT)
    hour = now.hour
    (250px, 1fr)); gap: 15px; margin-bottom: 25clean_generation = 0.0 if (hour < 6 or hour >= 19) else current_px; }}
        .status-card {{ padding: 20px; border-radius: 12generation
    solar_generation_pattern.append({'timestamp': now, 'hour': hour, 'generation': cleanpx; color: white; text-align: center; box-shadow: 0 4px 6px_generation, 'max_possible': TOTAL_SOLAR_CAPACITY_KW * 1000}) rgba(0,0,0,0.1); }}
        .status-card.critical {{ background:

def update_load_pattern(current_load):
    now = datetime.now(EAT)
 linear-gradient(135deg, #dc3545, #c82333);    load_demand_pattern.append({'timestamp': now, 'hour': now.hour, 'load': current }}
        .status-card.warning {{ background: linear-gradient(135deg, #fd7_load})

# ----------------------------
# Email function
# ----------------------------
def send_email(subjecte14, #e67e22); }}
        .status-card.good {{ background:, html_content, alert_type="general", send_via_email=True):
    global last_ linear-gradient(135deg, #28a745, #218838alert_time, alert_history
    cooldown_map = {
        "critical": 60,); }}
        .status-card.normal {{ background: linear-gradient(135deg, #1 "very_high_load": 30, "backup_active": 120, "high_7a2b8, #138496); }}
        
        .main-stat {{load": 60,
        "moderate_load": 120, "warning": 12 font-size: 1.6em; font-weight: bold; margin-bottom: 5px;0, "communication_lost": 60, "fault_alarm": 30,
        "high }}
        .sub-stat {{ font-size: 0.9em; opacity: 0.9_temperature": 60, "test": 0, "general": 120
    }
; }}
        
        /* Dashboard Cards */
        .card {{ background: white; padding: 25    cooldown_minutes = cooldown_map.get(alert_type, 120)
    
    if alert_type in last_alert_time and cooldown_minutes > 0:
        if datetime.px; border-radius: 15px; margin-bottom: 20px; box-shadow:now(EAT) - last_alert_time[alert_type] < timedelta(minutes=cooldown_ 0 2px 8px rgba(0,0,0,0.05); }}
        h2 {{ color: #444; margin-bottom: 15px; font-size: minutes):
            return False
    
    action_successful = False
    if send_via_email and1.3em; }}
        
        /* Load Speedometer */
        .load-bar-bg {{ all([RESEND_API_KEY, SENDER_EMAIL, RECIPIENT_EMAIL]):
        try background: #eee; height: 20px; border-radius: 10px; overflow: hidden:
            response = requests.post(
                "https://api.resend.com/emails",
; margin: 10px 0; }}
        .load-bar-fill {{ height: 1                headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/00%; transition: width 0.5s; }}
        
        /* Battery Visuals */
        json"},
                json={"from": SENDER_EMAIL, "to": [RECIPIENT_EMAIL],.batt-container {{ display: flex; gap: 20px; flex-wrap: wrap; }}
 "subject": subject, "html": html_content}
            )
            if response.status_code ==        .batt-box {{ flex: 1; border: 1px solid #eee; padding: 1 200: action_successful = True
        except Exception as e: print(f"✗ Error sending5px; border-radius: 10px; min-width: 300px; }}
 email: {e}")
    else:
        print(f"ℹ️ Alert logged to Dashboard: {subject        .batt-visual {{ height: 30px; background: #eee; border-radius: 1}")
        action_successful = True

    if action_successful:
        now = datetime.now(EAT)
        last_alert_time[alert_type] = now
        alert_history.append({"5px; overflow: hidden; position: relative; margin: 10px 0; }}
        .timestamp": now, "type": alert_type, "subject": subject})
        cutoff = now - timedelta(batt-fill {{ height: 100%; display: flex; align-items: center; justify-contenthours=12)
        alert_history[:] = [a for a in alert_history if a['timestamp: flex-end; padding-right: 10px; color: white; font-weight: bold;'] >= cutoff]
        return True
    return False

# ----------------------------
# Alert Logic
#  font-size: 0.8em; }}
        .batt-zone-bg {{ position: absolute;----------------------------
def check_and_send_alerts(inverter_data, solar_conditions, total_solar top:0; left:0; width:100%; height:100%; z-index:_input, total_battery_discharge, generator_running):
    inv1 = next((i for i in 1; }}
        .batt-mask {{ position: absolute; top:0; right:0; height inverter_data if i['SN'] == 'RKG3B0400T'), None)
    :100%; background: #eee; z-index: 2; transition: width 1s;inv2 = next((i for i in inverter_data if i['SN'] == 'KAM4N5W }}
        .batt-marker {{ position: absolute; top:0; height:100%; width:0AG'), None)
    inv3_backup = next((i for i in inverter_data if i[' 2px; background: rgba(255,255,255,0.7);SN'] == 'JNK1CDR0KQ'), None)
    if not all([inv1, inv2 z-index: 3; }}
        .batt-text {{ position: absolute; width:100, inv3_backup]): return
    
    primary_capacity = min(inv1['Capacity'], inv2%; text-align: center; line-height: 30px; font-weight: bold; color:['Capacity'])
    backup_voltage = inv3_backup['vBat']
    backup_active = inv white; text-shadow: 1px 1px 2px black; z-index: 4;3_backup['OutputPower'] > 50
    total_load = inv1['OutputPower'] + }}
        
        .metric-grid {{ display: grid; grid-template-columns: repeat(2, inv2['OutputPower'] + inv3_backup['OutputPower']
    
    for inv in inverter_ 1fr); gap: 10px; margin-top: 15px; }}
        .data:
        if inv.get('communication_lost', False): send_email(f"⚠️ Communication Lostmetric-box {{ background: #f8f9fa; padding: 10px; border-radius:: {inv['Label']}", "Check inverter.", "communication_lost")
        if inv.get('has 8px; text-align: center; }}
        .metric-val {{ font-size: 1_fault', False): send_email(f"🚨 FAULT ALARM: {inv['Label']}", ".2em; font-weight: bold; color: #333; }}
        .metric-lblInverter fault.", "fault_alarm")
        if inv.get('high_temperature', False): send_ {{ font-size: 0.8em; color: #666; }}
        
        .email(f"🌡️ HIGH TEMPERATURE: {inv['Label']}", f"Temp: {inv.get('weather-badge {{ display: inline-block; padding: 5px 12px; border-radius: 20px; font-size: 0.9em; font-weight: bold; margin-bottomtemperature',0)}", "high_temperature")
    
    if generator_running or backup_voltage < BACKUP_VOLTAGE_THRESHOLD:
        send_email("🚨 CRITICAL: Generator Running - Backup Battery Critical",: 10px; }}
        .weather-badge.good {{ background: #d4edda; "Action Required.", "critical")
        return
    
    if backup_active and primary_capacity < PRIMARY color: #155724; }}
        .weather-badge.poor {{ background: #f_BATTERY_THRESHOLD:
        send_email("⚠️ HIGH ALERT: Backup Inverter Active", "Reduce Load8d7da; color: #721c24; }}
    </style>
</head.", "backup_active")
        return
    
    if 40 < primary_capacity < 5>
<body>
    <div class="container">
        <div style="text-align:center; margin0:
        send_email("⚠️ WARNING: Primary Battery Low", "Reduce Load.", "warning", send_-bottom:30px;">
            <h1 style="color:#333;">TULIA HOUSEvia_email=backup_active)
    
    if total_battery_discharge >= 4500</h1>
            <p style="color:#666;">Solar Monitor • {latest_data.get('timestamp:
        send_email("🚨 URGENT: Very High Battery Discharge", "Critical Discharge.", "very_','Loading...')}</p>
        </div>

        <!-- 1. Top Status Grid -->
        <div classhigh_load", send_via_email=backup_active)
    elif 2500 <= total="status-grid">
            <!-- Appliance Safety -->
            <div class="status-card {app_color_battery_discharge < 3500:
        send_email("⚠️ WARNING: High Battery Discharge",}">
                <div class="main-stat">{app_status}</div>
                <div class="sub- "High Discharge.", "high_load", send_via_email=backup_active)
    elif 1stat">{app_sub}</div>
            </div>
            
            <!-- Prediction / Time Remaining -->
            <div500 <= total_battery_discharge < 2000 and primary_capacity < 50: class="status-card {pred_class}">
                <div class="main-stat">{pred_message}
        send_email("ℹ️ INFO: Moderate Battery Discharge - Low Battery", "Moderate Discharge.", "moderate_</div>
                <div class="sub-stat">Forecast based on current usage</div>
            </div>
            
            load", send_via_email=backup_active)

# ----------------------------
# Polling Loop
#<!-- Net Flow -->
            <div class="status-card normal" style="background: white; color: # ----------------------------
def poll_growatt():
    global latest_data, load_history, battery_history333; border: 2px solid {flow_color};">
                <div class="main-, weather_forecast, last_communication, solar_conditions_cache
    weather_forecast = get_weather_stat" style="color: {flow_color}">{flow_icon} {flow_text}</div>
                forecast()
    if weather_forecast: solar_conditions_cache = analyze_solar_conditions(weather_forecast<div class="sub-stat">Battery Flow</div>
            </div>
        </div>

        <!-- 2. Solar)
    last_weather_update = datetime.now(EAT)
    
    while True:
 & Load Context -->
        <div class="card">
            <div style="display:flex; justify-        try:
            now = datetime.now(EAT)
            cutoff_time = now - timedelta(content:space-between; align-items:center;">
                <h2>Current System Load</h2>
                <div classhours=12)
            alert_history[:] = [a for a in alert_history if a['timestamp="weather-badge {weather_class}">{weather_badge}</div>
            </div>
            
            <!-- Load'] >= cutoff_time]
            
            if now - last_weather_update > timedelta(minutes=3 Speedometer -->
            <div class="load-bar-bg">
                <div class="load-bar0):
                weather_forecast = get_weather_forecast()
                if weather_forecast: solar_conditions-fill" style="width: {load_pct}%; background: {load_color};"></div>
            _cache = analyze_solar_conditions(weather_forecast)
                last_weather_update = now
            </div>
            <div style="display:flex; justify-content:space-between; font-size:0
            total_output_power, total_battery_discharge_W, total_solar_input_W =.9em; color:#666;">
                <span>0W</span>
                <span style="font- 0, 0, 0
            inverter_data, primary_capacities = [], []
            weight:bold; color:{load_color}">{total_load:.0f}W • {load_msgbackup_data, generator_running = None, False
            
            for sn in SERIAL_NUMBERS:
                try:
                    response = requests.post(API_URL, data={"storage_sn": sn}, headers=headers, timeout=20)
                    response.raise_for_status()
                    data = response.json().get("data", {})}</span>
                <span>5,000W+ (Critical)</span>
            </div>
        </div>

        <!-- 3. Battery Status Gauges -->
        <div class="card">
            <h2>Battery Status</h2>
            <div class="batt-container">
                <!-- Primary -->
                <div class="batt-box">
                    last_communication[sn] = now
                    config = INVERTER_CONFIG.get(sn,
                    <div style="display:flex; justify-content:space-between; margin-bottom:5px {"label": sn, "type": "unknown", "display_order": 99})
                    
                    ;">
                        <strong>Primary Battery</strong>
                        <span>{primary_battery:.0f}%</span>
                    </div>
                    out_power = float(data.get("outPutPower") or 0)
                    capacity = float(<div class="batt-visual">
                        <div class="batt-zone-bg" style="background:data.get("capacity") or 0)
                    v_bat = float(data.get("vBat linear-gradient(to right, black 20%, #fd7e14 20%, #fd") or 0)
                    p_bat = float(data.get("pBat") or 0)7e14 40%, #28a745 40%);"></div>
                        <
                    ppv = float(data.get("ppv") or 0) + float(data.getdiv class="batt-mask" style="width: {100 - primary_battery}%"></div>
                        ("ppv2") or 0)
                    
                    temp = max(float(data.get("inv<div class="batt-marker" style="left: 20%"></div>
                        <div class="battTemperature") or 0), float(data.get("dcDcTemperature") or 0), float(data.-marker" style="left: 40%"></div>
                        <div class="batt-text">{primary_get("temperature") or 0))
                    
                    has_fault = int(data.get("errorCode")battery:.0f}%</div>
                    </div>
                    <div class="metric-grid">
                        <div class or 0) != 0 or int(data.get("faultCode") or 0) != 0="metric-box">
                            <div class="metric-val">{primary_kwh_real:.1f
                    
                    vac = float(data.get("vac") or 0)
                    pac_input =} kWh</div>
                            <div class="metric-lbl">Energy Stored</div>
                        </div>
                        <div float(data.get("pAcInPut") or 0)
                    
                    total_output_power class="metric-box">
                            <div class="metric-val">30 kWh</div>
                            <div += out_power
                    total_solar_input_W += ppv
                    if p_bat >  class="metric-lbl">Total Capacity</div>
                        </div>
                    </div>
                    <div style="font-size0: total_battery_discharge_W += p_bat
                    
                    inv_info = {
                        :0.8em; color:#666; margin-top:5px; text-align:center"SN": sn, "Label": config['label'], "Type": config['type'], "DisplayOrder": config;">
                        Green: Daily | Orange: Reserve | Black: Cutoff
                    </div>
                </div>

                <!--['display_order'],
                        "OutputPower": out_power, "Capacity": capacity, "vBat": Backup -->
                <div class="batt-box">
                    <div style="display:flex; justify- v_bat, "pBat": p_bat, "ppv": ppv,
                        "temperature":content:space-between; margin-bottom:5px;">
                        <strong>Backup Battery</strong>
                        <span>{backup temp, "high_temperature": temp >= INVERTER_TEMP_WARNING, "Status": data.get("_voltage:.1f}V</span>
                    </div>
                    <div class="batt-visual">
                        <statusText", "Unknown"),
                        "has_fault": has_fault, "fault_info": {"errorCodediv class="batt-zone-bg" style="background: linear-gradient(to right, black 20": int(data.get("errorCode") or 0), "faultCode": int(data.get("fault%, #28a745 20%);"></div>
                        <div class="batt-mask" style="width: {100 - backup_percent_display}%"></div>
                        <div class="batt-marker" style="left: 20%"></div>
                        <div class="batt-text">{backup_voltage:.1f}V</div>
                    </div>
                    <div class="metric-grid">
                        <div class="metric-box">
                            <div class="metric-val">~{backup_kwh_display:.1f} kWh</div>
                            <div class="metric-lbl">Est. Energy</div>
                        </div>
                        <div class="metric-box">
                            <div class="metric-val">{backup_voltage_status}</div>
                            <div class="metric-lbl">Health Status</div>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <!-- 4. Prediction Chart -->
        <div class="card">
            <h2>🔋 Total System Fuel Prediction</h2>
            <p style="color:#666; font-size:0.9em; margin-bottom:15px;">
                One line showing total energy (Primary + Backup). <br>
                <span style="color:#28a745">Green</span> = Normal. <span style="color:#fd7e14">Orange</span> = Backup Active. <span style="color:#dc3545">Red Line at 0%</span> = Generator Needed.
            </p>
            <div style="height:300px">
                <canvas id="cascadeChart"></canvas>
            </div>
        </div>

        <script>
            // Prediction Chart
            const ctx = document.getElementById('cascadeChart').getContext('2d');
            
            function getLineColor(ctx) {{
                const val = ctx.p0.parsed.y;
                return val < 48 ? '#fd7e14' : '#28a745'; 
            }}

            new Chart(ctx, {{
                Code") or 0)},
                        "communication_lost": False, "last_seen": now.strftime("%Y-%m-%d %H:%M:%S")
                    }
                    inverter_data.append(inv_info)
                    
                    if config['type'] == 'primary' and capacity > 0: primary_capacities.append(capacity)
                    elif config['type'] == 'backup':
                        backup_datatype: 'line',
                data: {{
                    labels: {json.dumps(sim_times)},
                    datasets: [{{
                         = inv_info
                        if vac > 100 or pac_input > 50: generator_running = True
                except:
                    if sn in last_communication and now - last_communication[sn]label: 'Total Fuel %',
                        data: {json.dumps(trace_total_pct)},
                         > timedelta(minutes=COMMUNICATION_TIMEOUT_MINUTES):
                        config = INVERTER_CONFIG.borderColor: 'gray',
                        segment: {{ borderColor: ctx => getLineColor(ctx) }},
                        borderget(sn, {})
                        inverter_data.append({"SN": sn, "Label": config.get('label', sn), "Type": config.get('type'), "DisplayOrder": config.get('display_order', 99), "communication_lost": True})
            
            inverter_data.sort(key=lambda x: x.get('DisplayOrder', 99))
            update_solar_pattern(total_solar_input_W)
            update_load_pattern(total_output_power)
            Width: 4,
                        tension: 0.3,
                        fill: true,
                        backgroundColor:
            load_history.append((now, total_output_power))
            load_history[:] = [(t, p) for t, p in load_history if t >= now - timedelta(hours=12)]
            battery_history.append((now, total_battery_discharge_W))
            battery_history[:] = [(t, p) for t, p in battery_history if t >= now - timedelta(hours=12)]
            
            solar_pattern = analyze_historical_solar_pattern()
            load_pattern = analyze_historical_load_pattern()
            
            solar_forecast = generate_solar_forecast(weather_ 'rgba(200, 200, 200, 0.1)'
                    forecast, solar_pattern)
            moving_avg_load = calculate_moving_average_load(45)
            load_forecast = generate_load_forecast(load_pattern, moving_avg_load)
            
            primary_battery_min = min(primary_capacities) if primary_capacities else 0
            backup_battery_voltage = backup_data['vBat'] if backup_data else 0
            backup_voltage_status, backup_voltage_color = get_backup_voltage_status(backup_battery_}}]
                }},
                options: {{
                    responsive: true,
                    maintainAspectRatio: false,
                    voltage)
            backup_active = backup_data['OutputPower'] > 50 if backup_data else False
            
            backup_percent_calc = max(0, min(100, (backup_scales: {{
                        y: {{ min: 0, max: 100, title: {{ displaybattery_voltage - 51.0) / 2.0 * 100))
            backup_kwh_calc = (backup_percent_calc / 100) * (BACKUP_BAT: true, text: 'Total Capacity (%)' }} }}
                    }},
                    plugins: {{
                        annotation:TERY_DEGRADED_WH / 1000)
            
            battery_life_prediction = calculate_battery_cascade(solar_forecast, load_forecast, primary_battery_min, backup_active)
            
            latest_data = {
                "timestamp": now.strftime("%Y-%m-%d % {{
                            annotations: {{
                                switchLine: {{
                                    type: 'line',
                                    yMin: 48, yMax: 48,
                                    borderColor: '#fd7e14', borderWidth: 2, borderDash: [5, 5],
                                    label: {{ content: 'H:%M:%S EAT"),
                "total_output_power": total_output_power,
                "total_battery_dischargeSwitch to Backup', display: true, position: 'start', backgroundColor: 'rgba(253, 126, 20, 0.8)', color: 'white' }}
                                }},
                                gen_W": total_battery_discharge_W,
                "total_solar_input_W": total_Line: {{
                                    type: 'line',
                                    yMin: 0, yMax: solar_input_W,
                "primary_battery_min": primary_battery_min,
                "0,
                                    borderColor: '#dc3545', borderWidth: 3,
                                    label:backup_battery_voltage": backup_battery_voltage,
                "backup_voltage_status": backup_voltage_status,
                "backup_voltage_color": backup_voltage_color,
                "backup_active": backup_active,
                "backup_percent_calc": backup_percent_calc,
                "backup_kwh_calc": backup_kwh_calc,
                "generator_running": generator_running,
                "inverters": inverter_data,
                "solar_forecast": solar_forecast,
                " {{ content: 'GENERATOR START', display: true, position: 'center', backgroundColor: '#dc354load_forecast": load_forecast,
                "battery_life_prediction": battery_life_prediction,
5', color: 'white' }}
                                }}
                            }}
                        }}
                    }}
                }}
            }});
        </script>
        
        <div style="text-align:center; margin-top:30px; font-size:0.8em; color:#999;">
            Data updates every {POLL_INTERVAL_MINUTES} minutes.
        </div>
    </div>
    <meta http-                "historical_pattern_count": len(solar_generation_pattern),
                "load_pattern_countequiv="refresh" content="300">
</body>
</html>
"""
    return render_template_string(html)

# ----------------------------
# Start background polling thread
# ----------------------------
Thread(target=": len(load_demand_pattern)
            }
            
            print(f"{latest_data['timestamp']} | Load={total_output_power:.0f}W | Solar={total_solar_input_W:.0f}W")
            check_and_send_alerts(inverter_data, solar_poll_growatt, daemon=True).start()

# ----------------------------
# Run Flask
# ----------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.conditions_cache, total_solar_input_W, total_battery_discharge_W, generator_running)0", port=int(os.getenv("PORT", 10000)))
