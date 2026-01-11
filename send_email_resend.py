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
# ----------------------------
API_URL = "https://openapi.growatt.com/v1/device/storage/storage_last_data"
TOKEN = os.getenv("API_TOKEN")
SERIAL_NUMBERS = os.getenv("SERIAL_NUMBERS", "").split(",")
POLL_INTERVAL_MINUTES = int(os.getenv("POLL_INTERVAL_MINUTES", 5))

# ----------------------------
# Inverter Configuration
# ----------------------------
INVERTER_CONFIG = {
    "RKG3B0400T": {"label": "Inverter 1", "type": "primary", "datalog": "DDD0B021CC", "display_order": 1},
    "KAM4N5W0AG": {"label": "Inverter 2", "type": "primary", "datalog": "DDD0B02121", "display_order": 2},
    "JNK1CDR0KQ": {"label": "Inverter 3 (Backup)", "type": "backup", "datalog": "DDD0B0221H", "display_order": 3}
}

# Thresholds & Battery Specs
PRIMARY_BATTERY_THRESHOLD = 40
BACKUP_VOLTAGE_THRESHOLD = 51.2
TOTAL_SOLAR_CAPACITY_KW = 10
PRIMARY_INVERTER_CAPACITY_W = 10000
BACKUP_INVERTER_CAPACITY_W = 5000

BACKUP_VOLTAGE_GOOD = 53.0
BACKUP_VOLTAGE_MEDIUM = 52.3
BACKUP_VOLTAGE_LOW = 52.0

INVERTER_TEMP_WARNING = 60
INVERTER_TEMP_CRITICAL = 70
COMMUNICATION_TIMEOUT_MINUTES = 10

# Battery Specs (LiFePO4)
PRIMARY_BATTERY_CAPACITY_WH = 30000 
PRIMARY_DAILY_MIN_PCT = 40 
BACKUP_BATTERY_DEGRADED_WH = 21000   
BACKUP_CUTOFF_PCT = 20
TOTAL_SYSTEM_USABLE_WH = 34800 

# ----------------------------
# Location & Email
# ----------------------------
LATITUDE = -1.85238
LONGITUDE = 36.77683
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

# Pool Pump / High Load Tracking
pool_pump_start_time = None
pool_pump_last_alert = None

# Historical Data & Forecast Containers
solar_forecast = []
solar_generation_pattern = deque(maxlen=5000)
load_demand_pattern = deque(maxlen=5000)
SOLAR_EFFICIENCY_FACTOR = 0.85
FORECAST_HOURS = 12
EAT = timezone(timedelta(hours=3))

# ----------------------------
# Weather Functions
# ----------------------------
def get_weather_from_openmeteo():
    try:
        url = f"https://api.open-meteo.com/v1/forecast?latitude={LATITUDE}&longitude={LONGITUDE}&hourly=cloud_cover,shortwave_radiation&timezone=Africa/Nairobi&forecast_days=2"
        response = requests.get(url, timeout=10)
        return {'times': response.json()['hourly']['time'], 'cloud_cover': response.json()['hourly']['cloud_cover'], 'solar_radiation': response.json()['hourly']['shortwave_radiation'], 'source': 'Open-Meteo'}
    except: return None

def get_weather_from_weatherapi():
    try:
        WEATHERAPI_KEY = os.getenv("WEATHERAPI_KEY") 
        url = f"http://api.weatherapi.com/v1/forecast.json?key={WEATHERAPI_KEY}&q={LATITUDE},{LONGITUDE}&days=2"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            times, cloud, solar = [], [], []
            for day in data.get('forecast', {}).get('forecastday', []):
                for hour in day.get('hour', []):
                    times.append(hour['time'])
                    cloud.append(hour['cloud'])
                    solar.append(hour.get('uv', 0) * 120) 
            if times: return {'times': times, 'cloud_cover': cloud, 'solar_radiation': solar, 'source': 'WeatherAPI'}
    except: pass
    return None
        
def get_weather_from_7timer():
    try:
        url = f"http://www.7timer.info/bin/api.pl?lon={LONGITUDE}&lat={LATITUDE}&product=civil&output=json"
        response = requests.get(url, timeout=15)
        data = response.json()
        times, cloud, solar = [], [], []
        base = datetime.now(EAT)
        for item in data.get('dataseries', [])[:48]:
            t = base + timedelta(hours=item.get('timepoint', 0))
            times.append(t.strftime('%Y-%m-%dT%H:%M'))
            c_pct = min((item.get('cloudcover', 5) * 12), 100)
            cloud.append(c_pct)
            solar.append(max(800 * (1 - c_pct/100), 0))
        if times: return {'times': times, 'cloud_cover': cloud, 'solar_radiation': solar, 'source': '7Timer'}
    except: pass
    return None

def get_fallback_weather():
    times, clouds, rads = [], [], []
    now = datetime.now(EAT).replace(minute=0, second=0, microsecond=0)
    for i in range(48):
        t = now + timedelta(hours=i)
        times.append(t.isoformat())
        clouds.append(20)
        h = t.hour
        rads.append(max(0, 1000 - (abs(12 - h) * 150)) if 6 <= h <= 18 else 0)
    return {'times': times, 'cloud_cover': clouds, 'solar_radiation': rads, 'source': 'Synthetic (Offline)'}

def get_weather_forecast():
    global weather_source
    print("🌤️ Fetching weather forecast...")
    for src, func in [("Open-Meteo", get_weather_from_openmeteo), ("WeatherAPI", get_weather_from_weatherapi), ("7Timer", get_weather_from_7timer)]:
        f = func()
        if f and len(f.get('times', [])) > 0:
            weather_source = f['source']
            return f
    weather_source = "Synthetic (Offline)"
    return get_fallback_weather()

def analyze_solar_conditions(forecast):
    if not forecast: return None
    try:
        now = datetime.now(EAT)
        h = now.hour
        is_night = h < 6 or h >= 18
        if is_night:
            start = (now + timedelta(days=1)).replace(hour=6, minute=0)
            end = (now + timedelta(days=1)).replace(hour=18, minute=0)
            label = "Tomorrow's Daylight"
        else:
            start = now
            end = now.replace(hour=18, minute=0)
            label = "Today's Remaining Daylight"
        
        c_sum, s_sum, count = 0, 0, 0
        for i, t_str in enumerate(forecast['times']):
            try:
                ft = datetime.fromisoformat(t_str.replace('Z', '')) if 'T' in t_str else datetime.strptime(t_str, '%Y-%m-%d %H:%M')
                ft = ft.replace(tzinfo=EAT) if ft.tzinfo is None else ft.astimezone(EAT)
                if start <= ft <= end and 6 <= ft.hour <= 18:
                    c_sum += forecast['cloud_cover'][i]
                    s_sum += forecast['solar_radiation'][i]
                    count += 1
            except: continue
        
        if count > 0:
            return {
                'avg_cloud_cover': c_sum/count,
                'avg_solar_radiation': s_sum/count,
                'poor_conditions': (c_sum/count) > 70 or (s_sum/count) < 200,
                'analysis_period': label,
                'is_nighttime': is_night
            }
    except: pass
    return None

# ----------------------------
# Helper & Forecasting
# ----------------------------
def get_backup_voltage_status(voltage):
    if voltage >= BACKUP_VOLTAGE_GOOD: return "Good", "green"
    elif voltage >= BACKUP_VOLTAGE_MEDIUM: return "Medium", "orange"
    else: return "Low", "red"

def check_generator_running(backup_data):
    if not backup_data: return False
    return float(backup_data.get('vac', 0) or 0) > 100 or float(backup_data.get('pAcInPut', 0) or 0) > 50

def analyze_historical_solar_pattern():
    if len(solar_generation_pattern) < 3: return None
    pattern, hour_map = [], {}
    for d in solar_generation_pattern:
        h = d['hour']
        if h not in hour_map: hour_map[h] = []
        hour_map[h].append(d['generation'] / d.get('max_possible', TOTAL_SOLAR_CAPACITY_KW * 1000))
    for h, v in hour_map.items(): pattern.append((h, np.mean(v)))
    return pattern

def analyze_historical_load_pattern():
    if len(load_demand_pattern) < 3: return None
    pattern, hour_map = [], {}
    for d in load_demand_pattern:
        h = d['hour']
        if h not in hour_map: hour_map[h] = []
        hour_map[h].append(d['load'])
    for h, v in hour_map.items(): pattern.append((h, 0, np.mean(v)))
    return pattern

def get_hourly_weather_forecast(weather_data, num_hours=12):
    hourly = []
    now = datetime.now(EAT)
    if not weather_data: return hourly
    w_times = []
    for i, t_str in enumerate(weather_data['times']):
        try:
            ft = datetime.fromisoformat(t_str.replace('Z', '')) if 'T' in t_str else datetime.strptime(t_str, '%Y-%m-%d %H:%M')
            ft = ft.replace(tzinfo=EAT) if ft.tzinfo is None else ft.astimezone(EAT)
            w_times.append({'time': ft, 'cloud': weather_data['cloud_cover'][i], 'solar': weather_data['solar_radiation'][i]})
        except: continue
    w_times.sort(key=lambda x: x['time'])
    for i in range(num_hours):
        ft = now + timedelta(hours=i)
        closest = min(w_times, key=lambda x: abs(x['time'] - ft))
        hourly.append({'time': ft, 'hour': ft.hour, 'cloud_cover': closest['cloud'], 'solar_radiation': closest['solar']})
    return hourly

def apply_solar_curve(gen, hour):
    if hour < 6 or hour >= 19: return 0.0
    curve = np.sin(((hour - 6) / 13.0) * np.pi) ** 2
    return gen * curve * (0.7 if hour <= 7 or hour >= 18 else 1.0)

def generate_solar_forecast(weather_data, pattern):
    forecast = []
    hourly = get_hourly_weather_forecast(weather_data, FORECAST_HOURS)
    max_gen = TOTAL_SOLAR_CAPACITY_KW * 1000
    for d in hourly:
        h = d['hour']
        if h < 6 or h >= 19:
            est = 0.0
        else:
            theo = (d['solar_radiation'] / 1000) * max_gen * SOLAR_EFFICIENCY_FACTOR
            curved = apply_solar_curve(theo, h)
            if pattern:
                p_val = next((v for ph, v in pattern if ph == h), 0)
                est = (curved * 0.6 + (p_val * max_gen) * 0.4)
            else: est = curved
        forecast.append({'time': d['time'], 'hour': h, 'estimated_generation': max(0, est)})
    return forecast

def calculate_moving_average_load(mins=45):
    cutoff = datetime.now(EAT) - timedelta(minutes=mins)
    recent = [p for t, p in load_history if t >= cutoff]
    return sum(recent) / len(recent) if recent else 0

def generate_load_forecast(pattern, current_avg=0):
    forecast = []
    now = datetime.now(EAT)
    for i in range(FORECAST_HOURS):
        ft = now + timedelta(hours=i)
        h = ft.hour
        base = 1000
        # Incorporate Historical Load Profile Data
        if pattern:
            match = next((l for ph, _, l in pattern if ph == h), None)
            if match is not None: base = match
        else:
            if 0 <= h < 5: base = 600
            elif 5 <= h < 8: base = 1800
            elif 8 <= h < 17: base = 1200
            elif 17 <= h < 22: base = 2800
        
        is_spike = current_avg > (base * 1.5)
        if current_avg > 0:
            if i == 0: val = (current_avg * 0.8) + (base * 0.2)
            elif i == 1: val = (current_avg * 0.3) + (base * 0.7) if is_spike else (current_avg * 0.5) + (base * 0.5)
            elif i == 2: val = base if is_spike else (current_avg * 0.2) + (base * 0.8)
            else: val = base
        else: val = base
        forecast.append({'time': ft, 'hour': h, 'estimated_load': val})
    return forecast

def calculate_battery_cascade(solar, load, p_pct, b_active=False):
    if not solar or not load: return None
    
    p_daily_wh = max(0, ((p_pct/100)*30000) - 12000)
    b_wh = max(0, (21000 * 0.9) - 4200)
    
    trace = [((p_daily_wh + b_wh) / 34800) * 100]
    gen_needed, empty_time, switch_occurred = False, None, False
    acc_gen_wh = 0
    
    for i in range(min(len(solar), len(load))):
        net = load[i]['estimated_load'] - solar[i]['estimated_generation']
        step = net * 1.0
        
        if step > 0:
            if p_daily_wh >= step: p_daily_wh -= step
            else:
                rem = step - p_daily_wh
                p_daily_wh = 0
                switch_occurred = True
                if b_wh >= rem: b_wh -= rem
                else:
                    b_wh = 0
                    gen_needed = True
                    acc_gen_wh += (rem - b_wh)
                    if not empty_time: empty_time = solar[i]['time'].strftime("%I:%M %p")
        else:
            surplus = abs(step)
            space_p = 18000 - p_daily_wh
            if surplus <= space_p: p_daily_wh += surplus
            else:
                p_daily_wh = 18000
                surplus -= space_p
                if surplus <= (16800 - b_wh): b_wh += surplus
                else: b_wh = 16800
        
        trace.append(((p_daily_wh + b_wh) / 34800) * 100)
    
    return {'trace_total_pct': trace, 'generator_needed': gen_needed, 'time_empty': empty_time, 'switchover_occurred': switch_occurred, 'genset_hours': acc_gen_wh/5000}

# ----------------------------
# Updates & Email
# ----------------------------
def update_patterns(solar, load):
    now = datetime.now(EAT)
    h = now.hour
    clean_s = 0.0 if (h < 6 or h >= 19) else solar
    solar_generation_pattern.append({'timestamp': now, 'hour': h, 'generation': clean_s, 'max_possible': 10000})
    load_demand_pattern.append({'timestamp': now, 'hour': h, 'load': load})

def send_email(subject, html, alert_type="general", send_via_email=True):
    global last_alert_time, alert_history
    cooldown = 120
    if "critical" in alert_type: cooldown = 60
    elif "very_high" in alert_type: cooldown = 30
    
    if alert_type in last_alert_time and (datetime.now(EAT) - last_alert_time[alert_type]) < timedelta(minutes=cooldown):
        return False
        
    success = False
    if send_via_email and all([RESEND_API_KEY, SENDER_EMAIL, RECIPIENT_EMAIL]):
        try:
            r = requests.post("https://api.resend.com/emails", headers={"Authorization": f"Bearer {RESEND_API_KEY}"}, json={"from": SENDER_EMAIL, "to": [RECIPIENT_EMAIL], "subject": subject, "html": html})
            if r.status_code == 200: success = True
        except: pass
    else: success = True # Dashboard only
    
    if success:
        now = datetime.now(EAT)
        last_alert_time[alert_type] = now
        alert_history.append({"timestamp": now, "type": alert_type, "subject": subject})
        alert_history[:] = [a for a in alert_history if a['timestamp'] >= (now - timedelta(hours=12))]
        return True
    return False

def check_alerts(inv_data, solar, total_solar, bat_discharge, gen_run):
    inv1 = next((i for i in inv_data if i['SN'] == 'RKG3B0400T'), None)
    inv2 = next((i for i in inv_data if i['SN'] == 'KAM4N5W0AG'), None)
    inv3 = next((i for i in inv_data if i['SN'] == 'JNK1CDR0KQ'), None)
    if not all([inv1, inv2, inv3]): return
    
    p_cap = min(inv1['Capacity'], inv2['Capacity'])
    b_active = inv3['OutputPower'] > 50
    b_volt = inv3['vBat']
    
    for inv in inv_data:
        if inv.get('communication_lost'): send_email(f"⚠️ Comm Lost: {inv['Label']}", "Check inverter", "communication_lost")
        if inv.get('has_fault'): send_email(f"🚨 FAULT: {inv['Label']}", "Fault code", "fault_alarm")
        if inv.get('high_temperature'): send_email(f"🌡️ High Temp: {inv['Label']}", f"Temp: {inv['temperature']}", "high_temperature")
        
    if gen_run or b_volt < 51.2:
        send_email("🚨 CRITICAL: Generator Running", "Backup critical", "critical")
        return
    if b_active and p_cap < 40:
        send_email("⚠️ HIGH ALERT: Backup Active", "Reduce Load", "backup_active")
        return
    if 40 < p_cap < 50:
        send_email("⚠️ Primary Low", "Reduce Load", "warning", send_via_email=b_active)
    
    if bat_discharge >= 4500: send_email("🚨 URGENT: High Discharge", "Critical", "very_high_load", send_via_email=b_active)
    elif 2500 <= bat_discharge < 3500: send_email("⚠️ High Discharge", "Warning", "high_load", send_via_email=b_active)
    elif 1500 <= bat_discharge < 2000 and p_cap < 50: send_email("ℹ️ Moderate Discharge", "Info", "moderate_load", send_via_email=b_active)

# ----------------------------
# Polling Loop
# ----------------------------
def poll_growatt():
    global latest_data, load_history, battery_history, weather_forecast, last_communication, solar_conditions_cache
    global pool_pump_start_time, pool_pump_last_alert

    weather_forecast = get_weather_forecast()
    if weather_forecast: solar_conditions_cache = analyze_solar_conditions(weather_forecast)
    last_wx = datetime.now(EAT)
    
    while True:
        try:
            now = datetime.now(EAT)
            alert_history[:] = [a for a in alert_history if a['timestamp'] >= (now - timedelta(hours=12))]
            
            if (now - last_wx) > timedelta(minutes=30):
                weather_forecast = get_weather_forecast()
                if weather_forecast: solar_conditions_cache = analyze_solar_conditions(weather_forecast)
                last_wx = now
                
            tot_out, tot_bat, tot_sol = 0, 0, 0
            inv_data, p_caps = [], []
            b_data, gen_on = None, False
            
            for sn in SERIAL_NUMBERS:
                try:
                    r = requests.post(API_URL, data={"storage_sn": sn}, headers=headers, timeout=20)
                    r.raise_for_status()
                    d = r.json().get("data", {})
                    last_communication[sn] = now
                    cfg = INVERTER_CONFIG.get(sn, {"label": sn, "type": "unknown", "display_order": 99})
                    
                    op = float(d.get("outPutPower") or 0)
                    cap = float(d.get("capacity") or 0)
                    vb = float(d.get("vBat") or 0)
                    pb = float(d.get("pBat") or 0)
                    sol = float(d.get("ppv") or 0) + float(d.get("ppv2") or 0)
                    tmp = max(float(d.get("invTemperature") or 0), float(d.get("dcDcTemperature") or 0), float(d.get("temperature") or 0))
                    flt = int(d.get("errorCode") or 0) != 0
                    
                    tot_out += op
                    tot_sol += sol
                    if pb > 0: tot_bat += pb
                    
                    info = {
                        "SN": sn, "Label": cfg['label'], "Type": cfg['type'], "DisplayOrder": cfg['display_order'],
                        "OutputPower": op, "Capacity": cap, "vBat": vb, "pBat": pb, "ppv": sol, "temperature": tmp,
                        "high_temperature": tmp >= 60, "Status": d.get("statusText", "Unknown"), "has_fault": flt,
                        "last_seen": now.strftime("%Y-%m-%d %H:%M:%S"), "communication_lost": False
                    }
                    inv_data.append(info)
                    
                    if cfg['type'] == 'primary' and cap > 0: p_caps.append(cap)
                    elif cfg['type'] == 'backup':
                        b_data = info
                        if float(d.get("vac") or 0) > 100 or float(d.get("pAcInPut") or 0) > 50: gen_on = True
                except:
                    if sn in last_communication and (now - last_communication[sn]) > timedelta(minutes=10):
                        cfg = INVERTER_CONFIG.get(sn, {})
                        inv_data.append({"SN": sn, "Label": cfg.get('label', sn), "Type": cfg.get('type'), "DisplayOrder": 99, "communication_lost": True})
            
            inv_data.sort(key=lambda x: x.get('DisplayOrder', 99))
            update_patterns(tot_sol, tot_out)
            
            # EXPANDED HISTORY: Keep 14 days of data in memory
            load_history.append((now, tot_out))
            load_history[:] = [(t, p) for t, p in load_history if t >= (now - timedelta(days=14))]
            battery_history.append((now, tot_bat))
            battery_history[:] = [(t, p) for t, p in battery_history if t >= (now - timedelta(days=14))]
            
            # Use Historical Patterns for Forecasting
            s_pat = analyze_historical_solar_pattern()
            l_pat = analyze_historical_load_pattern()
            s_cast = generate_solar_forecast(weather_forecast, s_pat)
            avg_load = calculate_moving_average_load(45)
            l_cast = generate_load_forecast(l_pat, avg_load)
            
            p_min = min(p_caps) if p_caps else 0
            b_volts = b_data['vBat'] if b_data else 0
            b_act = b_data['OutputPower'] > 50 if b_data else False
            b_pct = max(0, min(100, (b_volts - 51.0) / 2.0 * 100))
            b_kwh = (b_pct / 100) * 21.0
            
            pred = calculate_battery_cascade(s_cast, l_cast, p_min, b_act)

            # --- POOL PUMP / CONTINUOUS LOAD ALERT ---
            if now.hour >= 16:
                if tot_bat > 1100:
                    if pool_pump_start_time is None:
                        pool_pump_start_time = now
                    
                    duration = now - pool_pump_start_time
                    if duration > timedelta(hours=3) and now.hour >= 18:
                        if pool_pump_last_alert is None or (now - pool_pump_last_alert) > timedelta(hours=1):
                            send_email(
                                "⚠️ HIGH LOAD ALERT: Pool Pumps?", 
                                f"Battery discharge has been over 1.1kW for {duration.seconds//3600} hours. Did you leave the pool pumps on?", 
                                "high_load_continuous"
                            )
                            pool_pump_last_alert = now
                else:
                    pool_pump_start_time = None
            else:
                pool_pump_start_time = None
            
            latest_data = {
                "timestamp": now.strftime("%Y-%m-%d %H:%M:%S EAT"),
                "total_output_power": tot_out,
                "total_battery_discharge_W": tot_bat,
                "total_solar_input_W": tot_sol,
                "primary_battery_min": p_min,
                "backup_battery_voltage": b_volts,
                "backup_voltage_status": get_backup_voltage_status(b_volts)[0],
                "backup_active": b_act,
                "backup_percent_calc": b_pct,
                "backup_kwh_calc": b_kwh,
                "generator_running": gen_on,
                "inverters": inv_data,
                "solar_forecast": s_cast,
                "battery_life_prediction": pred
            }
            
            print(f"{latest_data['timestamp']} | Load={tot_out:.0f}W | Solar={tot_sol:.0f}W")
            check_alerts(inv_data, solar_conditions_cache, tot_sol, tot_bat, gen_on)
        except Exception as e: print(f"Error in polling: {e}")
        time.sleep(POLL_INTERVAL_MINUTES * 60)

# ----------------------------
# Web Interface
# ----------------------------
@app.route("/")
def home():
    # 1. Prepare Data
    p_bat = latest_data.get("primary_battery_min", 0)
    b_volt = latest_data.get("backup_battery_voltage", 0)
    b_stat = latest_data.get("backup_voltage_status", "Unknown")
    b_active = latest_data.get("backup_active", False)
    gen_on = latest_data.get("generator_running", False)
    tot_load = latest_data.get("total_output_power", 0)
    tot_sol = latest_data.get("total_solar_input_W", 0)
    tot_dis = latest_data.get("total_battery_discharge_W", 0)
    
    p_kwh = (p_bat / 100.0) * 30.0
    b_pct = latest_data.get("backup_percent_calc", 0)
    b_kwh = latest_data.get("backup_kwh_calc", 0)
    
    # Weather & Surplus Calculations
    sol_cond = solar_conditions_cache
    weather_bad = sol_cond and sol_cond['poor_conditions']
    surplus_power = tot_sol - tot_load

    # Calculate Flow Speeds
    def get_anim_speed(watts):
        if watts < 100: return "1000s" # Stopped
        if watts < 1000: return "3s"
        if watts < 3000: return "1.5s"
        return "0.8s"

    # Determine Flow States for the HUB Layout
    flow_s_h = min(tot_sol, tot_load) if tot_sol > 0 else 0
    flow_s_b = max(0, surplus_power)
    flow_b_h = tot_dis
    flow_g_h = tot_load if gen_on else 0

    anim_s_h = get_anim_speed(tot_sol)
    anim_g_h = get_anim_speed(flow_g_h)
    anim_load = get_anim_speed(tot_load)
    
    # Battery Flow: Down = Charge, Up = Discharge
    bat_anim_class = "stopped"
    bat_anim_speed = "1000s"
    if flow_s_b > 100:
        bat_anim_class = "flow-down"
        bat_anim_speed = get_anim_speed(flow_s_b)
        bat_dot_color = "#28a745" # Green charging
    elif flow_b_h > 100:
        bat_anim_class = "flow-up"
        bat_anim_speed = get_anim_speed(flow_b_h)
        bat_dot_color = "#dc3545" # Red discharging
    else:
        bat_dot_color = "#ccc"

    # 2. Status Logic (Oven Safe / Warnings)
    if gen_on:
        app_st, app_sub, app_col = "CRITICAL: GENERATOR ON", "Stop all non-essential loads", "critical"
    elif b_active:
        app_st, app_sub, app_col = "❌ STOP HEAVY LOADS", "Backup Active. Save power.", "critical"
    elif p_bat < 45 and tot_sol < tot_load:
        app_st, app_sub, app_col = "⚠️ REDUCE LOADS", "Primary Low & Discharging", "warning"
    
    # NEW: Battery is full (95%), ignore bad forecast
    elif p_bat > 95:
        app_st, app_sub, app_col = "🔋 BATTERY FULL", "System charged & ready", "good"

    # NEW: Solar is decent and covering most of load (approx balanced)
    elif tot_sol > 2000 and (tot_sol > tot_load * 0.9):
        app_st, app_sub, app_col = "✅ SOLAR POWERING", "Solar supporting loads", "good"

    # Existing Oven Safe logic (Strict Surplus)
    elif (p_bat > 75 and surplus_power > 3000):
        app_st, app_sub, app_col = "✅ OVEN/KETTLE SAFE", f"Surplus {surplus_power/1000:.1f}kW", "good"
    
    # Relaxed "Cook Now" - allow if battery is high, even if surplus is slightly negative
    elif weather_bad and p_bat > 80:
        app_st, app_sub, app_col = "⚡ COOK NOW", "Bad forecast ahead. Use power now.", "good"
    
    # Conserve - Only if battery is dropping and weather is bad
    elif weather_bad and p_bat < 70:
        app_st, app_sub, app_col = "☁️ CONSERVE POWER", "Low Solar Forecast Expected", "warning"
        
    elif surplus_power > 100:
        app_st, app_sub, app_col = "🔋 CHARGING", f"System recovering (+{surplus_power:.0f}W)", "normal"
    else:
        app_st, app_sub, app_col = "ℹ️ MONITOR USAGE", "System running normally", "normal"
        
    w_bdg, w_cls = ("☁️ Low Solar Forecast", "poor") if weather_bad else ("☀️ High Solar Forecast", "good")

    # 3. Smart Schedule Logic
    schedule_html = ""
    forecast_data = latest_data.get('solar_forecast', [])
    
    # Override logic: If status is "COOK NOW", "OVEN SAFE", "BATTERY FULL", or "SOLAR POWERING"
    # ignore bad forecast warnings in the Smart Schedule
    safe_statuses = ["COOK NOW", "OVEN", "BATTERY FULL", "SOLAR POWERING"]
    is_safe_now = any(s in app_st for s in safe_statuses)

    if is_safe_now:
         schedule_html += '<li style="margin-bottom:10px; color:#28a745"><strong>⚡ Current Window:</strong><br>Status is Good. Safe to use heavy loads now.</li>'
         
    elif forecast_data:
        best_start, best_end, current_run = None, None, 0
        best_max_gen = 0
        for d in forecast_data:
            gen = d['estimated_generation']
            if gen > 2000:
                if current_run == 0: temp_start = d['time']
                current_run += 1
                if gen > best_max_gen: best_max_gen = gen
            else:
                if current_run > 0:
                    if best_start is None or current_run > (best_end.hour - best_start.hour):
                        best_start = temp_start
                        best_end = d['time']
                    current_run = 0
        
        schedule_html += '<ul style="padding-left:20px; margin:0;">'
        if best_start:
            s_str = best_start.strftime("%I %p").lstrip("0")
            e_str = best_end.strftime("%I %p").lstrip("0")
            schedule_html += f'<li style="margin-bottom:10px"><strong>🚿 Best Washing Time:</strong><br>{s_str} - {e_str} today.</li>'
        else:
            schedule_html += '<li style="margin-bottom:10px; color:#dc3545"><strong>⚠️ No High Solar Window:</strong><br>Avoid heavy loads today.</li>'
        
        next_3_gen = sum([d['estimated_generation'] for d in forecast_data[:3]]) / 3
        if next_3_gen < 500 and 8 <= datetime.now(EAT).hour <= 16:
            schedule_html += '<li style="margin-bottom:10px; color:#fd7e14"><strong>☁️ Cloud Warning:</strong><br>Low solar for next 3 hours.</li>'
        schedule_html += '</ul>'
    else:
        schedule_html += "<ul><li>Initializing...</li></ul>"
    
    # 4. Chart Data (Fixing Empty Graph by Pre-filling if empty)
    if not load_history:
        # Pre-fill with current single point so chart isn't empty on restart
        times = [datetime.now(EAT).strftime('%d %b %H:%M')]
        l_vals = [tot_load]
        b_vals = [tot_dis]
    else:
        # Dynamic downsampling
        total_points = len(load_history)
        step = max(1, total_points // 150)
        
        times = [t.strftime('%d %b %H:%M') for i, (t, p) in enumerate(load_history) if i % step == 0]
        l_vals = [p for i, (t, p) in enumerate(load_history) if i % step == 0]
        b_vals = [p for i, (t, p) in enumerate(battery_history) if i % step == 0]
    
    pred = latest_data.get("battery_life_prediction")
    sim_t = ["Now"] + [d['time'].strftime('%H:%M') for d in latest_data.get("solar_forecast", [])]
    trace_pct = []
    
    if pred:
        trace_pct = pred.get('trace_total_pct', [])
            
    # Load Speedometer Scale
    vis_max = 5000
    l_pct = min(100, (tot_load / vis_max) * 100)
    if tot_load < 1500: l_col, l_msg = "#28a745", "Normal"
    elif tot_load < 2500: l_col, l_msg = "#ffc107", "Moderate"
    elif tot_load < 4500: l_col, l_msg = "#fd7e14", "High"
    else: l_col, l_msg = "#dc3545", "CRITICAL"

    # 5. Generate HTML
    html = f"""
<!DOCTYPE html>
<html>
<head>
    <title>Tulia House</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-annotation@2.1.0/dist/chartjs-plugin-annotation.min.js"></script>
    <style>
        body {{ font-family: 'Segoe UI', sans-serif; background: linear-gradient(rgba(0,0,0,0.5), rgba(0,0,0,0.5)), url('https://images.unsplash.com/photo-1582268611958-ebfd161ef9cf?w=1600&q=80') center/cover fixed; margin:0; padding:20px; color:#333; }}
        .container {{ max-width: 1200px; margin: 0 auto; }}
        .card {{ background: rgba(255,255,255,0.95); padding: 20px; border-radius: 15px; margin-bottom: 20px; box-shadow: 0 4px 15px rgba(0,0,0,0.2); backdrop-filter: blur(5px); }}
        .header {{ text-align: center; color: white; text-shadow: 2px 2px 4px rgba(0,0,0,0.7); margin-bottom: 25px; }}
        
        /* Power Flow Animation CSS - LAYERED FIX (No Gaps) */
        .flow-container {{ 
            position: relative; 
            height: 220px; 
            width: 100%;
            max-width: 500px;
            margin: 0 auto 20px auto;
        }}

        /* Icons - Higher Z-Index to sit ON TOP of lines */
        .f-icon {{ font-size: 2em; background: white; padding: 10px; border-radius: 50%; box-shadow: 0 4px 10px rgba(0,0,0,0.1); width: 45px; height: 45px; display:flex; align-items:center; justify-content:center; position:relative; z-index:2; }}
        .hub-box {{ width: 50px; height: 50px; background: white; color: #333; border-radius: 10px; display: flex; align-items: center; justify-content: center; font-weight: bold; font-size: 1.5em; box-shadow: 0 4px 15px rgba(0,0,0,0.3); z-index:2; border: 2px solid #333; }}
        
        /* Positioning Icons */
        .pos-hub {{ position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); }}
        .pos-sun {{ position: absolute; top: 50%; left: 0px; transform: translateY(-50%); display:flex; flex-direction:column; align-items:center; }}
        .pos-house {{ position: absolute; top: 50%; right: 0px; transform: translateY(-50%); display:flex; flex-direction:column; align-items:center; }}
        .pos-gen {{ position: absolute; top: 0px; left: 50%; transform: translateX(-50%); }}
        .pos-bat {{ position: absolute; bottom: 0px; left: 50%; transform: translateX(-50%); display:flex; flex-direction:column; align-items:center; }}

        /* Connecting Lines - Lower Z-Index to go UNDER icons. Full span to ensure connection */
        .line-h-left {{ position: absolute; top: 50%; left: 0; right: 50%; height: 6px; background: #ddd; transform: translateY(-50%); z-index: 1; }}
        .line-h-right {{ position: absolute; top: 50%; left: 50%; right: 0; height: 6px; background: #ddd; transform: translateY(-50%); z-index: 1; }}
        .line-v-top {{ position: absolute; top: 0; left: 50%; bottom: 50%; width: 6px; background: #ddd; transform: translateX(-50%); z-index: 1; }}
        .line-v-bottom {{ position: absolute; top: 50%; left: 50%; bottom: 0; width: 6px; background: #ddd; transform: translateX(-50%); z-index: 1; }}
        
        .f-dot {{ position: absolute; width: 10px; height: 10px; background: #28a745; border-radius: 50%; opacity: 0; }}
        
        /* Animations */
        @keyframes flowRight {{ 0% {{ left: 0%; opacity:1; }} 100% {{ left: 100%; opacity:0; }} }}
        @keyframes flowDown {{ 0% {{ top: 0%; opacity:1; }} 100% {{ top: 100%; opacity:0; }} }}
        @keyframes flowUp {{ 0% {{ top: 100%; opacity:1; }} 100% {{ top: 0%; opacity:0; }} }}
        
        .flow-right {{ animation: flowRight infinite linear; }}
        .flow-down {{ animation: flowDown infinite linear; }}
        .flow-up {{ animation: flowUp infinite linear; }}
        
        /* Status Cards */
        .status-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 15px; margin-bottom: 20px; }}
        .st-card {{ padding: 20px; border-radius: 12px; color: white; text-align: center; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }}
        .st-card.critical {{ background: linear-gradient(135deg, #dc3545, #c82333); }}
        .st-card.warning {{ background: linear-gradient(135deg, #fd7e14, #e67e22); }}
        .st-card.good {{ background: linear-gradient(135deg, #28a745, #218838); }}
        .st-card.normal {{ background: linear-gradient(135deg, #17a2b8, #138496); }}
        .st-val {{ font-size: 1.4em; font-weight: bold; margin-bottom: 5px; }}
        
        .load-bg {{ background: #eee; height: 25px; border-radius: 15px; overflow: hidden; margin: 15px 0; box-shadow: inset 0 2px 5px rgba(0,0,0,0.1); }}
        .load-fill {{ height: 100%; transition: width 0.5s; }}
        
        .batt-row {{ display: flex; gap: 20px; flex-wrap: wrap; }}
        .batt-col {{ flex: 1; min-width: 300px; border: 1px solid #ddd; padding: 15px; border-radius: 10px; background: #fff; }}
        .b-vis {{ height: 35px; background: #eee; border-radius: 18px; margin: 15px 0; position: relative; overflow: hidden; box-shadow: inset 0 2px 5px rgba(0,0,0,0.2); }}
        .b-fill {{ height: 100%; transition: width 1s; position: absolute; top:0; right:0; background: #eee; z-index: 2; border-left: 2px solid #555; }}
        .b-bg {{ position: absolute; width:100%; height:100%; top:0; left:0; z-index: 1; }}
        .b-mark {{ position: absolute; width:2px; height:100%; background: rgba(255,255,255,0.8); z-index: 3; top:0; }}
        .b-txt {{ position: absolute; width:100%; text-align: center; line-height: 35px; font-weight: bold; color: white; text-shadow: 1px 1px 2px black; z-index: 4; top:0; }}
        
        .inv-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 15px; margin-top: 10px; }}
        .inv-card {{ background: #f8f9fa; padding: 15px; border-radius: 10px; border-left: 5px solid #6c757d; font-size: 0.9em; }}
        .inv-row {{ display: flex; justify-content: space-between; padding: 5px 0; border-bottom: 1px solid #eee; }}
        
        .alert-row {{ display: grid; grid-template-columns: 100px 1fr; gap: 10px; padding: 10px; border-bottom: 1px solid #eee; font-size: 0.9em; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>TULIA HOUSE</h1>
            <p>Solar Monitor • {latest_data.get('timestamp','Loading...')}</p>
        </div>

        <!-- NEW: Animated HUB Diagram (Absolute Layout + Layering) -->
        <div class="card" style="background: rgba(255,255,255,0.9);">
            <div class="flow-container">
                
                <!-- Lines (Z-Index 1: Underlay) -->
                <div class="line-v-top">
                    <div class="f-dot flow-down" style="animation-duration:{anim_g_h}; background:#dc3545; display:{'block' if gen_on else 'none'}"></div>
                </div>
                <div class="line-h-left">
                     <div class="f-dot flow-right" style="animation-duration:{anim_s_h}; display:{'block' if tot_sol > 10 else 'none'}"></div>
                </div>
                <div class="line-h-right">
                    <div class="f-dot flow-right" style="animation-duration:{anim_load}; background:#007bff"></div>
                </div>
                <div class="line-v-bottom">
                     <div class="f-dot {bat_anim_class}" style="animation-duration:{bat_anim_speed}; background:{bat_dot_color}; display:{'none' if 'stopped' in bat_anim_class else 'block'}"></div>
                </div>

                <!-- Icons (Z-Index 2: Overlay) -->
                <!-- Generator -->
                <div class="pos-gen">
                    <div class="f-icon" style="border: 2px solid {'#dc3545' if gen_on else '#ccc'}">🔌</div>
                </div>

                <!-- Sun -->
                <div class="pos-sun">
                    <div class="f-icon" style="color:#fd7e14">☀️</div>
                    <div style="font-weight:bold; font-size:0.8em; margin-top:5px;">{tot_sol:.0f}W</div>
                </div>
                
                <!-- Hub -->
                <div class="pos-hub">
                    <div class="hub-box">⚡</div>
                </div>

                <!-- House -->
                <div class="pos-house">
                    <div class="f-icon">🏠</div>
                    <div style="font-weight:bold; font-size:0.8em; margin-top:5px;">{tot_load:.0f}W</div>
                </div>

                <!-- Battery -->
                <div class="pos-bat">
                    <div class="f-icon" style="color:{bat_dot_color}">🔋</div>
                    <div style="font-weight:bold; font-size:0.8em; margin-top:5px;">{p_bat:.0f}%</div>
                </div>
                
            </div>
        </div>

        <div class="status-grid">
            <div class="st-card {app_col}">
                <div class="st-val">{app_st}</div>
                <div>{app_sub}</div>
            </div>
            
            <!-- Smart Schedule Card -->
            <div class="st-card normal" style="background:#fff; color:#333; text-align:left; border-left: 5px solid #17a2b8;">
                <div style="font-weight:bold; font-size:1.1em; margin-bottom:5px; border-bottom:1px solid #eee; padding-bottom:5px;">📅 Smart Schedule (Next 12h)</div>
                {schedule_html}
            </div>
        </div>

        <div class="card">
            <div style="display:flex; justify-content:space-between; align-items:center;">
                <h2 style="margin:0">Current Load</h2>
                <span style="background:{'#d4edda' if 'High' in w_bdg else '#f8d7da'}; padding:5px 10px; border-radius:15px; font-size:0.9em; color:{'#155724' if 'High' in w_bdg else '#721c24'}">{w_bdg}</span>
            </div>
            <div class="load-bg">
                <div class="load-fill" style="width: {l_pct}%; background: {l_col};"></div>
            </div>
            <div style="display:flex; justify-content:space-between; color:#666; font-size:0.9em;">
                <span>0W</span>
                <span style="font-weight:bold; color:{l_col}">{tot_load:.0f}W ({l_msg})</span>
                <span>5000W+</span>
            </div>
        </div>

        <div class="card">
            <h2 style="margin-top:0">Battery Status</h2>
            <div class="batt-row">
                <!-- Primary -->
                <div class="batt-col">
                    <div style="display:flex; justify-content:space-between;"><strong>Primary</strong> <span>{p_bat:.0f}%</span></div>
                    <div class="b-vis">
                        <div class="b-bg" style="background: linear-gradient(to right, black 20%, #fd7e14 20%, #fd7e14 40%, #28a745 40%);"></div>
                        <div class="b-fill" style="width: {100 - p_bat}%"></div>
                        <div class="b-mark" style="left:20%"></div><div class="b-mark" style="left:40%"></div>
                        <div class="b-txt">{p_bat:.0f}%</div>
                    </div>
                    <div style="text-align:center; font-size:0.9em;"><strong>{p_kwh:.1f} kWh</strong> / 30 kWh</div>
                </div>
                <!-- Backup -->
                <div class="batt-col">
                    <div style="display:flex; justify-content:space-between;"><strong>Backup</strong> <span>{b_volt:.1f}V</span></div>
                    <div class="b-vis">
                        <div class="b-bg" style="background: linear-gradient(to right, black 20%, #28a745 20%);"></div>
                        <div class="b-fill" style="width: {100 - b_pct}%"></div>
                        <div class="b-mark" style="left:20%"></div>
                        <div class="b-txt">{b_volt:.1f}V</div>
                    </div>
                    <div style="text-align:center; font-size:0.9em;"><strong>~{b_kwh:.1f} kWh</strong> ({b_stat})</div>
                </div>
            </div>
        </div>

        <div class="card">
            <h2 style="margin-top:0">Total Fuel Prediction (Using Historical Load)</h2>
            <div style="height:300px"><canvas id="predChart"></canvas></div>
        </div>

        <div class="card">
            <h2 style="margin-top:0">Power History (14 Days)</h2>
            <div style="height:250px"><canvas id="histChart"></canvas></div>
        </div>

        <div class="card">
            <h2 style="margin-top:0">System Details</h2>
            <div class="inv-grid">
"""
    for inv in latest_data.get('inverters', []):
        bg = "#fff3cd" if inv.get('communication_lost') else "#f8f9fa"
        bd = "#ffc107" if inv.get('communication_lost') else "#6c757d"
        html += f"""
                <div class="inv-card" style="background:{bg}; border-left-color:{bd}">
                    <div style="font-weight:bold; margin-bottom:5px;">{inv.get('Label')}</div>
                    <div class="inv-row"><span>Power</span> <strong>{inv.get('OutputPower',0):.0f}W</strong></div>
                    <div class="inv-row"><span>Solar</span> <strong>{inv.get('ppv',0):.0f}W</strong></div>
                    <div class="inv-row"><span>Bat Volts</span> <strong>{inv.get('vBat',0):.1f}V</strong></div>
                    <div class="inv-row"><span>Temp</span> <strong>{inv.get('temperature',0):.1f}°C</strong></div>
                    <div style="margin-top:5px; font-size:0.8em; color:#666">{inv.get('Status')}</div>
                </div>
        """
    html += """
            </div>
        </div>

        <div class="card">
            <h2 style="margin-top:0">Recent Alerts</h2>
            <div>
"""
    if alert_history:
        for a in reversed(alert_history):
            clr = "#dc3545" if "critical" in a['type'] else "#ffc107"
            html += f'<div class="alert-row" style="border-left:3px solid {clr}"><span style="color:#666">{a["timestamp"].strftime("%H:%M")}</span><strong>{a["subject"]}</strong></div>'
    else:
        html += '<div style="text-align:center; padding:15px; color:#999;">No recent alerts</div>'
    
    html += f"""
            </div>
        </div>
    </div>

    <script>
        const pCtx = document.getElementById('predChart').getContext('2d');
        new Chart(pCtx, {{
            type: 'line',
            data: {{
                labels: {json.dumps(sim_t)},
                datasets: [{{
                    label: 'Total Fuel %',
                    data: {json.dumps(trace_pct)},
                    borderColor: 'gray',
                    segment: {{ borderColor: ctx => ctx.p0.parsed.y < 48 ? '#fd7e14' : '#28a745' }},
                    borderWidth: 3, tension: 0.3, fill: true, backgroundColor: 'rgba(200,200,200,0.1)'
                }}]
            }},
            options: {{
                responsive: true, maintainAspectRatio: false,
                scales: {{ y: {{ min: 0, max: 100, title: {{ display: true, text: '%' }} }} }},
                plugins: {{
                    annotation: {{
                        annotations: {{
                            sw: {{ type:'line', yMin:48, yMax:48, borderColor:'#fd7e14', borderWidth:2, borderDash:[5,5], label:{{content:'Backup Start', display:true, position:'start', backgroundColor:'rgba(253,126,20,0.8)'}} }},
                            gn: {{ type:'line', yMin:0, yMax:0, borderColor:'#dc3545', borderWidth:2, label:{{content:'Generator', display:true, backgroundColor:'#dc3545'}} }}
                        }}
                    }}
                }}
            }}
        }});

        const hCtx = document.getElementById('histChart').getContext('2d');
        new Chart(hCtx, {{
            type: 'line',
            data: {{
                labels: {json.dumps(times)},
                datasets: [
                    {{ label: 'Load', data: {json.dumps(l_vals)}, borderColor: '#007bff', backgroundColor: 'rgba(0,123,255,0.1)', fill: true, tension: 0.3, pointRadius: 1 }},
                    {{ label: 'Discharge', data: {json.dumps(b_vals)}, borderColor: '#dc3545', backgroundColor: 'rgba(220,53,69,0.1)', fill: true, tension: 0.3, pointRadius: 1 }}
                ]
            }},
            options: {{ responsive: true, maintainAspectRatio: false }}
        }});
    </script>
</body>
</html>
"""
    return render_template_string(html)

if __name__ == "__main__":
    Thread(target=poll_growatt, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
