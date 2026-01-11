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
    else: success = True
    
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
            
            load_history.append((now, tot_out))
            load_history[:] = [(t, p) for t, p in load_history if t >= (now - timedelta(days=14))]
            battery_history.append((now, tot_bat))
            battery_history[:] = [(t, p) for t, p in battery_history if t >= (now - timedelta(days=14))]
            
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
                "load_forecast": l_cast,
                "battery_life_prediction": pred,
                "weather_source": weather_source
            }
            
            print(f"{latest_data['timestamp']} | Load={tot_out:.0f}W | Solar={tot_sol:.0f}W")
            check_alerts(inv_data, solar_conditions_cache, tot_sol, tot_bat, gen_on)
        except Exception as e: print(f"Error in polling: {e}")
        time.sleep(POLL_INTERVAL_MINUTES * 60)

# ----------------------------
# API Endpoints
# ----------------------------
@app.route("/api/data")
def api_data():
    """Real-time data endpoint for AJAX updates"""
    p_bat = latest_data.get("primary_battery_min", 0)
    b_volt = latest_data.get("backup_battery_voltage", 0)
    tot_load = latest_data.get("total_output_power", 0)
    tot_sol = latest_data.get("total_solar_input_W", 0)
    tot_dis = latest_data.get("total_battery_discharge_W", 0)
    
    return jsonify({
        "timestamp": latest_data.get('timestamp'),
        "load": tot_load,
        "solar": tot_sol,
        "discharge": tot_dis,
        "primary_battery": p_bat,
        "backup_voltage": b_volt,
        "generator_running": latest_data.get("generator_running", False),
        "backup_active": latest_data.get("backup_active", False),
        "inverters": latest_data.get("inverters", []),
        "alerts": [{"time": a['timestamp'].strftime("%H:%M"), "subject": a['subject'], "type": a['type']} for a in alert_history[-10:]]
    })

# ----------------------------
# Web Interface
# ----------------------------
@app.route("/")
def home():
    # Prepare all data
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
    
    sol_cond = solar_conditions_cache
    weather_bad = sol_cond and sol_cond['poor_conditions']
    surplus_power = tot_sol - tot_load

    # Status determination
    if gen_on:
        app_st, app_sub, app_col = "CRITICAL: GENERATOR ON", "Stop all non-essential loads immediately", "critical"
    elif b_active:
        app_st, app_sub, app_col = "BACKUP ACTIVE", "Primary depleted - minimize loads", "critical"
    elif p_bat < 45 and tot_sol < tot_load:
        app_st, app_sub, app_col = "REDUCE LOADS", "Primary battery low & discharging", "warning"
    elif p_bat > 95:
        app_st, app_sub, app_col = "BATTERY FULL", "System fully charged", "good"
    elif tot_sol > 2000 and (tot_sol > tot_load * 0.9):
        app_st, app_sub, app_col = "SOLAR POWERING", "Solar covering most loads", "good"
    elif (p_bat > 75 and surplus_power > 3000):
        app_st, app_sub, app_col = "HIGH SURPLUS", f"Safe to use heavy appliances", "good"
    elif weather_bad and p_bat > 80:
        app_st, app_sub, app_col = "USE POWER NOW", "Poor forecast ahead - cook while you can", "good"
    elif weather_bad and p_bat < 70:
        app_st, app_sub, app_col = "CONSERVE POWER", "Low solar forecast expected", "warning"
    elif surplus_power > 100:
        app_st, app_sub, app_col = "CHARGING", f"Battery recovering", "normal"
    else:
        app_st, app_sub, app_col = "NORMAL OPERATION", "System running within parameters", "normal"
    
    # Chart data
    if not load_history:
        times = [datetime.now(EAT).strftime('%d %b %H:%M')]
        l_vals = [tot_load]
        b_vals = [tot_dis]
        s_vals = [tot_sol]
    else:
        total_points = len(load_history)
        step = max(1, total_points // 150)
        
        times = [t.strftime('%d %b %H:%M') for i, (t, p) in enumerate(load_history) if i % step == 0]
        l_vals = [p for i, (t, p) in enumerate(load_history) if i % step == 0]
        b_vals = [p for i, (t, p) in enumerate(battery_history) if i % step == 0]
        
        # Add solar data if available
        s_vals = [0] * len(times)  # Placeholder for now
    
    pred = latest_data.get("battery_life_prediction")
    sim_t = ["Now"] + [d['time'].strftime('%H:%M') for d in latest_data.get("solar_forecast", [])]
    trace_pct = pred.get('trace_total_pct', []) if pred else []
    
    # Solar forecast for next 12h
    s_forecast = latest_data.get("solar_forecast", [])
    l_forecast = latest_data.get("load_forecast", [])
    
    forecast_times = [d['time'].strftime('%H:%M') for d in s_forecast]
    forecast_solar = [d['estimated_generation'] for d in s_forecast]
    forecast_load = [d['estimated_load'] for d in l_forecast]

    html_template = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Tulia House Solar Monitor</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=Plus+Jakarta+Sans:wght@300;400;600;700;800&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-annotation@3.0.1/dist/chartjs-plugin-annotation.min.js"></script>
    <style>
        :root {
            --bg-primary: #0a0e1a;
            --bg-secondary: #141827;
            --bg-card: #1a1f35;
            --accent-primary: #00ff88;
            --accent-secondary: #00ccff;
            --accent-warning: #ffaa00;
            --accent-critical: #ff3366;
            --text-primary: #ffffff;
            --text-secondary: #a0aec0;
            --border-color: rgba(255, 255, 255, 0.1);
            --glow: 0 0 20px rgba(0, 255, 136, 0.3);
        }
        
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: 'Plus Jakarta Sans', -apple-system, sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            line-height: 1.6;
            overflow-x: hidden;
        }
        
        /* Animated gradient background */
        body::before {
            content: '';
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: 
                radial-gradient(circle at 20% 50%, rgba(0, 255, 136, 0.1) 0%, transparent 50%),
                radial-gradient(circle at 80% 80%, rgba(0, 204, 255, 0.1) 0%, transparent 50%);
            z-index: -1;
            animation: gradientShift 15s ease infinite;
        }
        
        @keyframes gradientShift {
            0%, 100% { opacity: 0.5; }
            50% { opacity: 0.8; }
        }
        
        .container {
            max-width: 1600px;
            margin: 0 auto;
            padding: 2rem;
        }
        
        /* Header */
        header {
            text-align: center;
            margin-bottom: 3rem;
            animation: fadeInDown 0.6s ease;
        }
        
        h1 {
            font-size: 3.5rem;
            font-weight: 800;
            background: linear-gradient(135deg, var(--accent-primary) 0%, var(--accent-secondary) 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
            margin-bottom: 0.5rem;
            letter-spacing: -0.03em;
            text-shadow: var(--glow);
        }
        
        .subtitle {
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.9rem;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.15em;
        }
        
        /* Card system */
        .card {
            background: var(--bg-card);
            border-radius: 16px;
            border: 1px solid var(--border-color);
            padding: 2rem;
            margin-bottom: 2rem;
            backdrop-filter: blur(10px);
            transition: all 0.3s ease;
            animation: fadeInUp 0.6s ease;
            animation-fill-mode: both;
        }
        
        .card:hover {
            border-color: rgba(0, 255, 136, 0.3);
            box-shadow: 0 8px 32px rgba(0, 255, 136, 0.1);
            transform: translateY(-2px);
        }
        
        /* Grid layouts */
        .grid-2 {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 1.5rem;
        }
        
        .grid-3 {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 1.5rem;
        }
        
        .grid-4 {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 1rem;
        }
        
        /* Status hero */
        .status-hero {
            background: linear-gradient(135deg, var(--bg-secondary) 0%, var(--bg-card) 100%);
            border-radius: 24px;
            padding: 3rem;
            text-align: center;
            border: 2px solid var(--border-color);
            position: relative;
            overflow: hidden;
            animation: fadeInUp 0.6s ease 0.1s both;
        }
        
        .status-hero::before {
            content: '';
            position: absolute;
            top: -50%;
            left: -50%;
            width: 200%;
            height: 200%;
            background: radial-gradient(circle, rgba(0, 255, 136, 0.1) 0%, transparent 70%);
            animation: pulse 4s ease-in-out infinite;
        }
        
        @keyframes pulse {
            0%, 100% { transform: scale(1); opacity: 0.5; }
            50% { transform: scale(1.1); opacity: 0.8; }
        }
        
        .status-hero.critical::before {
            background: radial-gradient(circle, rgba(255, 51, 102, 0.2) 0%, transparent 70%);
        }
        
        .status-hero.warning::before {
            background: radial-gradient(circle, rgba(255, 170, 0, 0.2) 0%, transparent 70%);
        }
        
        .status-title {
            font-size: 2.5rem;
            font-weight: 800;
            margin-bottom: 0.5rem;
            position: relative;
            z-index: 1;
        }
        
        .status-hero.critical .status-title { color: var(--accent-critical); }
        .status-hero.warning .status-title { color: var(--accent-warning); }
        .status-hero.good .status-title { color: var(--accent-primary); }
        .status-hero.normal .status-title { color: var(--accent-secondary); }
        
        .status-subtitle {
            font-size: 1.1rem;
            color: var(--text-secondary);
            position: relative;
            z-index: 1;
        }
        
        /* Metric cards */
        .metric-card {
            background: var(--bg-secondary);
            border-radius: 12px;
            padding: 1.5rem;
            border: 1px solid var(--border-color);
            transition: all 0.3s ease;
        }
        
        .metric-card:hover {
            transform: translateY(-4px);
            box-shadow: 0 8px 24px rgba(0, 0, 0, 0.3);
        }
        
        .metric-label {
            font-size: 0.85rem;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.1em;
            margin-bottom: 0.5rem;
            font-weight: 600;
        }
        
        .metric-value {
            font-size: 2.5rem;
            font-weight: 800;
            font-family: 'JetBrains Mono', monospace;
            line-height: 1;
            margin-bottom: 0.25rem;
        }
        
        .metric-unit {
            font-size: 1rem;
            color: var(--text-secondary);
            font-weight: 400;
        }
        
        .metric-trend {
            font-size: 0.9rem;
            margin-top: 0.5rem;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }
        
        .trend-up { color: var(--accent-primary); }
        .trend-down { color: var(--accent-critical); }
        
        /* Power flow visualization */
        .power-flow {
            position: relative;
            height: 300px;
            display: flex;
            align-items: center;
            justify-content: center;
            margin: 2rem 0;
        }
        
        .flow-node {
            position: absolute;
            width: 80px;
            height: 80px;
            border-radius: 50%;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            background: var(--bg-secondary);
            border: 2px solid var(--border-color);
            transition: all 0.3s ease;
            z-index: 2;
        }
        
        .flow-node:hover {
            transform: scale(1.1);
            box-shadow: 0 0 30px rgba(0, 255, 136, 0.4);
        }
        
        .flow-node.active {
            border-color: var(--accent-primary);
            box-shadow: 0 0 20px rgba(0, 255, 136, 0.4);
        }
        
        .flow-icon {
            font-size: 2rem;
        }
        
        .flow-value {
            font-size: 0.75rem;
            font-family: 'JetBrains Mono', monospace;
            font-weight: 600;
            margin-top: 0.25rem;
        }
        
        .flow-node.solar { top: 50%; left: 10%; transform: translate(-50%, -50%); }
        .flow-node.battery { bottom: 10%; left: 50%; transform: translateX(-50%); }
        .flow-node.load { top: 50%; right: 10%; transform: translate(50%, -50%); }
        .flow-node.grid { top: 10%; left: 50%; transform: translateX(-50%); }
        
        .flow-line {
            position: absolute;
            background: var(--border-color);
            z-index: 1;
            transition: all 0.3s ease;
        }
        
        .flow-line.active {
            background: var(--accent-primary);
            box-shadow: 0 0 10px rgba(0, 255, 136, 0.5);
        }
        
        .flow-line.horizontal {
            height: 3px;
            top: 50%;
            left: calc(10% + 40px);
            right: calc(10% + 40px);
        }
        
        .flow-line.vertical {
            width: 3px;
            left: 50%;
            top: calc(10% + 40px);
            bottom: calc(10% + 40px);
        }
        
        /* Battery visualization */
        .battery-container {
            position: relative;
            height: 200px;
            background: var(--bg-secondary);
            border-radius: 12px;
            overflow: hidden;
            border: 2px solid var(--border-color);
        }
        
        .battery-fill {
            position: absolute;
            bottom: 0;
            left: 0;
            right: 0;
            background: linear-gradient(to top, var(--accent-primary), var(--accent-secondary));
            transition: height 1s ease;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        
        .battery-fill.warning {
            background: linear-gradient(to top, var(--accent-warning), #ffcc00);
        }
        
        .battery-fill.critical {
            background: linear-gradient(to top, var(--accent-critical), #ff6688);
        }
        
        .battery-percentage {
            font-size: 2rem;
            font-weight: 800;
            font-family: 'JetBrains Mono', monospace;
            color: white;
            text-shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
        }
        
        /* Chart containers */
        .chart-container {
            position: relative;
            height: 300px;
            margin: 1rem 0;
        }
        
        /* Inverter cards */
        .inverter-card {
            background: var(--bg-secondary);
            border-radius: 12px;
            padding: 1.5rem;
            border-left: 4px solid var(--accent-secondary);
        }
        
        .inverter-card.fault {
            border-left-color: var(--accent-critical);
            background: rgba(255, 51, 102, 0.1);
        }
        
        .inverter-card.warning {
            border-left-color: var(--accent-warning);
            background: rgba(255, 170, 0, 0.1);
        }
        
        .inverter-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 1rem;
        }
        
        .inverter-name {
            font-weight: 700;
            font-size: 1.1rem;
        }
        
        .inverter-status {
            font-size: 0.75rem;
            padding: 0.25rem 0.75rem;
            border-radius: 12px;
            background: var(--bg-card);
            text-transform: uppercase;
            font-weight: 600;
        }
        
        .inverter-metrics {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 1rem;
        }
        
        .inverter-metric {
            display: flex;
            justify-content: space-between;
            padding: 0.5rem 0;
            border-bottom: 1px solid var(--border-color);
        }
        
        .inverter-metric-label {
            color: var(--text-secondary);
            font-size: 0.9rem;
        }
        
        .inverter-metric-value {
            font-weight: 700;
            font-family: 'JetBrains Mono', monospace;
        }
        
        /* Alerts */
        .alert-item {
            display: flex;
            align-items: center;
            gap: 1rem;
            padding: 1rem;
            background: var(--bg-secondary);
            border-radius: 8px;
            margin-bottom: 0.5rem;
            border-left: 4px solid var(--accent-secondary);
        }
        
        .alert-item.critical {
            border-left-color: var(--accent-critical);
            background: rgba(255, 51, 102, 0.1);
        }
        
        .alert-item.warning {
            border-left-color: var(--accent-warning);
            background: rgba(255, 170, 0, 0.1);
        }
        
        .alert-time {
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.85rem;
            color: var(--text-secondary);
            min-width: 60px;
        }
        
        .alert-message {
            flex: 1;
            font-weight: 600;
        }
        
        /* Animations */
        @keyframes fadeInUp {
            from {
                opacity: 0;
                transform: translateY(30px);
            }
            to {
                opacity: 1;
                transform: translateY(0);
            }
        }
        
        @keyframes fadeInDown {
            from {
                opacity: 0;
                transform: translateY(-30px);
            }
            to {
                opacity: 1;
                transform: translateY(0);
            }
        }
        
        /* Stagger animation delays */
        .card:nth-child(1) { animation-delay: 0.1s; }
        .card:nth-child(2) { animation-delay: 0.2s; }
        .card:nth-child(3) { animation-delay: 0.3s; }
        .card:nth-child(4) { animation-delay: 0.4s; }
        .card:nth-child(5) { animation-delay: 0.5s; }
        
        /* Responsive */
        @media (max-width: 768px) {
            h1 { font-size: 2.5rem; }
            .status-title { font-size: 1.8rem; }
            .metric-value { font-size: 2rem; }
            .container { padding: 1rem; }
        }
        
        /* Loading state */
        .loading {
            display: inline-block;
            width: 20px;
            height: 20px;
            border: 3px solid var(--border-color);
            border-top-color: var(--accent-primary);
            border-radius: 50%;
            animation: spin 1s linear infinite;
        }
        
        @keyframes spin {
            to { transform: rotate(360deg); }
        }
        
        /* Utility classes */
        .text-success { color: var(--accent-primary); }
        .text-warning { color: var(--accent-warning); }
        .text-danger { color: var(--accent-critical); }
        .text-info { color: var(--accent-secondary); }
        
        h2 {
            font-size: 1.5rem;
            font-weight: 700;
            margin-bottom: 1.5rem;
            color: var(--text-primary);
        }
        
        h3 {
            font-size: 1.2rem;
            font-weight: 600;
            margin-bottom: 1rem;
            color: var(--text-primary);
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>TULIA HOUSE</h1>
            <div class="subtitle">Solar Energy Management System</div>
            <div class="subtitle" style="margin-top: 0.5rem; opacity: 0.6;">{{ timestamp }}</div>
        </header>
        
        <!-- Status Hero -->
        <div class="status-hero {{ status_class }}">
            <div class="status-title">{{ status_title }}</div>
            <div class="status-subtitle">{{ status_subtitle }}</div>
        </div>
        
        <!-- Key Metrics Grid -->
        <div class="grid-4" style="margin-top: 2rem;">
            <div class="metric-card">
                <div class="metric-label">Load Demand</div>
                <div class="metric-value text-info">{{ load_value }}<span class="metric-unit">W</span></div>
                <div class="metric-trend {{ load_trend_class }}">
                    <span>{{ load_trend_icon }}</span>
                    <span>{{ load_trend_text }}</span>
                </div>
            </div>
            
            <div class="metric-card">
                <div class="metric-label">Solar Generation</div>
                <div class="metric-value text-success">{{ solar_value }}<span class="metric-unit">W</span></div>
                <div class="metric-trend {{ solar_trend_class }}">
                    <span>{{ solar_trend_icon }}</span>
                    <span>{{ solar_trend_text }}</span>
                </div>
            </div>
            
            <div class="metric-card">
                <div class="metric-label">Primary Battery</div>
                <div class="metric-value {{ primary_color }}">{{ primary_pct }}<span class="metric-unit">%</span></div>
                <div style="margin-top: 0.5rem; color: var(--text-secondary); font-size: 0.9rem;">{{ primary_kwh }} kWh</div>
            </div>
            
            <div class="metric-card">
                <div class="metric-label">Backup Battery</div>
                <div class="metric-value {{ backup_color }}">{{ backup_volt }}<span class="metric-unit">V</span></div>
                <div style="margin-top: 0.5rem; color: var(--text-secondary); font-size: 0.9rem;">{{ backup_kwh }} kWh</div>
            </div>
        </div>
        
        <!-- Power Flow Visualization -->
        <div class="card">
            <h2>Real-time Energy Flow</h2>
            <div class="power-flow">
                <div class="flow-line horizontal {{ 'active' if solar_active else '' }}"></div>
                <div class="flow-line vertical {{ 'active' if battery_active else '' }}"></div>
                
                <div class="flow-node solar {{ 'active' if solar_active else '' }}">
                    <div class="flow-icon">☀️</div>
                    <div class="flow-value">{{ solar_value }}W</div>
                </div>
                
                <div class="flow-node battery {{ 'active' if battery_active else '' }}">
                    <div class="flow-icon">🔋</div>
                    <div class="flow-value">{{ primary_pct }}%</div>
                </div>
                
                <div class="flow-node load active">
                    <div class="flow-icon">🏠</div>
                    <div class="flow-value">{{ load_value }}W</div>
                </div>
                
                <div class="flow-node grid {{ 'active' if generator_on else '' }}">
                    <div class="flow-icon">{{ '🔌' if generator_on else '⚡' }}</div>
                    <div class="flow-value">{{ 'GEN' if generator_on else 'GRID' }}</div>
                </div>
            </div>
        </div>
        
        <!-- Battery Status -->
        <div class="grid-2">
            <div class="card">
                <h2>Primary Battery (30 kWh)</h2>
                <div class="battery-container">
                    <div class="battery-fill {{ primary_battery_class }}" style="height: {{ primary_pct }}%;">
                        <div class="battery-percentage">{{ primary_pct }}%</div>
                    </div>
                </div>
            </div>
            
            <div class="card">
                <h2>Backup Battery (21 kWh)</h2>
                <div class="battery-container">
                    <div class="battery-fill {{ backup_battery_class }}" style="height: {{ backup_pct }}%;">
                        <div class="battery-percentage">{{ backup_pct }}%</div>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- Forecast Chart -->
        <div class="card">
            <h2>12-Hour Forecast</h2>
            <div class="chart-container">
                <canvas id="forecastChart"></canvas>
            </div>
        </div>
        
        <!-- Battery Life Prediction -->
        <div class="card">
            <h2>Total System Capacity Prediction</h2>
            <div class="chart-container">
                <canvas id="predictionChart"></canvas>
            </div>
        </div>
        
        <!-- Historical Data -->
        <div class="card">
            <h2>14-Day Power History</h2>
            <div class="chart-container">
                <canvas id="historyChart"></canvas>
            </div>
        </div>
        
        <!-- Inverter Status -->
        <div class="card">
            <h2>Inverter Status</h2>
            <div class="grid-3">
                {% for inv in inverters %}
                <div class="inverter-card {{ 'fault' if inv.has_fault else ('warning' if inv.high_temperature or inv.communication_lost else '') }}">
                    <div class="inverter-header">
                        <div class="inverter-name">{{ inv.Label }}</div>
                        <div class="inverter-status">{{ inv.Status }}</div>
                    </div>
                    <div class="inverter-metrics">
                        <div class="inverter-metric">
                            <span class="inverter-metric-label">Power</span>
                            <span class="inverter-metric-value">{{ inv.OutputPower|round(0)|int }}W</span>
                        </div>
                        <div class="inverter-metric">
                            <span class="inverter-metric-label">Solar</span>
                            <span class="inverter-metric-value">{{ inv.ppv|round(0)|int }}W</span>
                        </div>
                        <div class="inverter-metric">
                            <span class="inverter-metric-label">Battery</span>
                            <span class="inverter-metric-value">{{ inv.vBat|round(1) }}V</span>
                        </div>
                        <div class="inverter-metric">
                            <span class="inverter-metric-label">Temp</span>
                            <span class="inverter-metric-value {{ 'text-danger' if inv.high_temperature else '' }}">{{ inv.temperature|round(1) }}°C</span>
                        </div>
                    </div>
                    {% if inv.communication_lost %}
                    <div style="margin-top: 1rem; padding: 0.5rem; background: rgba(255, 51, 102, 0.2); border-radius: 6px; text-align: center; font-size: 0.85rem; color: var(--accent-critical);">
                        ⚠️ Communication Lost
                    </div>
                    {% endif %}
                </div>
                {% endfor %}
            </div>
        </div>
        
        <!-- Recent Alerts -->
        <div class="card">
            <h2>Recent Alerts (Last 12 Hours)</h2>
            <div id="alertsContainer">
                {% if alerts %}
                    {% for alert in alerts %}
                    <div class="alert-item {{ alert.type }}">
                        <div class="alert-time">{{ alert.time }}</div>
                        <div class="alert-message">{{ alert.subject }}</div>
                    </div>
                    {% endfor %}
                {% else %}
                    <div style="text-align: center; padding: 2rem; color: var(--text-secondary);">
                        No recent alerts - system operating normally
                    </div>
                {% endif %}
            </div>
        </div>
    </div>
    
    <script>
        // Chart.js default config
        Chart.defaults.color = '#a0aec0';
        Chart.defaults.borderColor = 'rgba(255, 255, 255, 0.1)';
        
        // Forecast Chart
        const forecastCtx = document.getElementById('forecastChart').getContext('2d');
        new Chart(forecastCtx, {
            type: 'line',
            data: {
                labels: {{ forecast_times|tojson }},
                datasets: [
                    {
                        label: 'Predicted Solar',
                        data: {{ forecast_solar|tojson }},
                        borderColor: '#00ff88',
                        backgroundColor: 'rgba(0, 255, 136, 0.1)',
                        fill: true,
                        tension: 0.4,
                        borderWidth: 2
                    },
                    {
                        label: 'Predicted Load',
                        data: {{ forecast_load|tojson }},
                        borderColor: '#00ccff',
                        backgroundColor: 'rgba(0, 204, 255, 0.1)',
                        fill: true,
                        tension: 0.4,
                        borderWidth: 2
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        display: true,
                        position: 'top',
                        labels: {
                            usePointStyle: true,
                            padding: 15
                        }
                    },
                    tooltip: {
                        mode: 'index',
                        intersect: false,
                        backgroundColor: 'rgba(26, 31, 53, 0.95)',
                        borderColor: 'rgba(255, 255, 255, 0.1)',
                        borderWidth: 1
                    }
                },
                scales: {
                    y: {
                        beginAtZero: true,
                        grid: {
                            color: 'rgba(255, 255, 255, 0.05)'
                        },
                        ticks: {
                            callback: function(value) {
                                return value + 'W';
                            }
                        }
                    },
                    x: {
                        grid: {
                            display: false
                        }
                    }
                },
                interaction: {
                    mode: 'nearest',
                    axis: 'x',
                    intersect: false
                }
            }
        });
        
        // Prediction Chart
        const predictionCtx = document.getElementById('predictionChart').getContext('2d');
        new Chart(predictionCtx, {
            type: 'line',
            data: {
                labels: {{ sim_t|tojson }},
                datasets: [{
                    label: 'Total System Capacity',
                    data: {{ trace_pct|tojson }},
                    borderColor: '#00ccff',
                    segment: {
                        borderColor: ctx => ctx.p0.parsed.y < 48 ? '#ffaa00' : '#00ff88'
                    },
                    backgroundColor: 'rgba(0, 204, 255, 0.1)',
                    fill: true,
                    tension: 0.4,
                    borderWidth: 3
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        display: true,
                        position: 'top'
                    },
                    annotation: {
                        annotations: {
                            backup: {
                                type: 'line',
                                yMin: 48,
                                yMax: 48,
                                borderColor: '#ffaa00',
                                borderWidth: 2,
                                borderDash: [5, 5],
                                label: {
                                    content: 'Backup Activation (48%)',
                                    display: true,
                                    position: 'start',
                                    backgroundColor: 'rgba(255, 170, 0, 0.8)',
                                    color: '#000'
                                }
                            },
                            critical: {
                                type: 'line',
                                yMin: 0,
                                yMax: 0,
                                borderColor: '#ff3366',
                                borderWidth: 2,
                                label: {
                                    content: 'Generator Required (0%)',
                                    display: true,
                                    backgroundColor: 'rgba(255, 51, 102, 0.8)',
                                    color: '#fff'
                                }
                            }
                        }
                    }
                },
                scales: {
                    y: {
                        min: 0,
                        max: 100,
                        grid: {
                            color: 'rgba(255, 255, 255, 0.05)'
                        },
                        ticks: {
                            callback: function(value) {
                                return value + '%';
                            }
                        }
                    },
                    x: {
                        grid: {
                            display: false
                        }
                    }
                }
            }
        });
        
        // History Chart
        const historyCtx = document.getElementById('historyChart').getContext('2d');
        new Chart(historyCtx, {
            type: 'line',
            data: {
                labels: {{ times|tojson }},
                datasets: [
                    {
                        label: 'Load',
                        data: {{ l_vals|tojson }},
                        borderColor: '#00ccff',
                        backgroundColor: 'rgba(0, 204, 255, 0.1)',
                        fill: true,
                        tension: 0.4,
                        borderWidth: 2,
                        pointRadius: 0
                    },
                    {
                        label: 'Battery Discharge',
                        data: {{ b_vals|tojson }},
                        borderColor: '#ff3366',
                        backgroundColor: 'rgba(255, 51, 102, 0.1)',
                        fill: true,
                        tension: 0.4,
                        borderWidth: 2,
                        pointRadius: 0
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        display: true,
                        position: 'top'
                    }
                },
                scales: {
                    y: {
                        beginAtZero: true,
                        grid: {
                            color: 'rgba(255, 255, 255, 0.05)'
                        },
                        ticks: {
                            callback: function(value) {
                                return value + 'W';
                            }
                        }
                    },
                    x: {
                        grid: {
                            display: false
                        },
                        ticks: {
                            maxRotation: 45,
                            minRotation: 45
                        }
                    }
                }
            }
        });
        
        // Auto-refresh data every 30 seconds
        setInterval(() => {
            fetch('/api/data')
                .then(res => res.json())
                .then(data => {
                    // Update values without full page reload
                    console.log('Data refreshed:', data.timestamp);
                })
                .catch(err => console.error('Refresh error:', err));
        }, 30000);
    </script>
</body>
</html>
    """
    
    # Determine trends
    load_trend_icon = "↑" if tot_load > 2000 else "→" if tot_load > 1000 else "↓"
    load_trend_text = "High" if tot_load > 2000 else "Moderate" if tot_load > 1000 else "Low"
    load_trend_class = "trend-up" if tot_load > 2000 else "trend-down" if tot_load < 1000 else ""
    
    solar_trend_icon = "☀️" if tot_sol > 5000 else "⛅" if tot_sol > 2000 else "☁️"
    solar_trend_text = "Excellent" if tot_sol > 5000 else "Good" if tot_sol > 2000 else "Low"
    solar_trend_class = "trend-up" if tot_sol > 2000 else "trend-down"
    
    # Battery classes
    primary_color = "text-success" if p_bat > 60 else "text-warning" if p_bat > 40 else "text-danger"
    backup_color = "text-success" if b_volt > 52.3 else "text-warning" if b_volt > 51.5 else "text-danger"
    
    primary_battery_class = "" if p_bat > 60 else "warning" if p_bat > 40 else "critical"
    backup_battery_class = "" if b_pct > 60 else "warning" if b_pct > 40 else "critical"
    
    # Power flow states
    solar_active = tot_sol > 100
    battery_active = tot_dis > 100 or surplus_power > 100
    
    # Prepare alerts
    alerts = [{"time": a['timestamp'].strftime("%H:%M"), "subject": a['subject'], "type": a['type']} 
              for a in reversed(alert_history[-10:])]
    
    from flask import render_template_string
    return render_template_string(
        html_template,
        timestamp=latest_data.get('timestamp', 'Initializing...'),
        status_title=app_st,
        status_subtitle=app_sub,
        status_class=app_col,
        load_value=f"{tot_load:.0f}",
        solar_value=f"{tot_sol:.0f}",
        primary_pct=f"{p_bat:.0f}",
        backup_volt=f"{b_volt:.1f}",
        primary_kwh=f"{p_kwh:.1f}",
        backup_kwh=f"{b_kwh:.1f}",
        load_trend_icon=load_trend_icon,
        load_trend_text=load_trend_text,
        load_trend_class=load_trend_class,
        solar_trend_icon=solar_trend_icon,
        solar_trend_text=solar_trend_text,
        solar_trend_class=solar_trend_class,
        primary_color=primary_color,
        backup_color=backup_color,
        primary_battery_class=primary_battery_class,
        backup_battery_class=backup_battery_class,
        backup_pct=f"{b_pct:.0f}",
        solar_active=solar_active,
        battery_active=battery_active,
        generator_on=gen_on,
        forecast_times=forecast_times,
        forecast_solar=forecast_solar,
        forecast_load=forecast_load,
        sim_t=sim_t,
        trace_pct=trace_pct,
        times=times,
        l_vals=l_vals,
        b_vals=b_vals,
        inverters=latest_data.get('inverters', []),
        alerts=alerts
    )

if __name__ == "__main__":
    Thread(target=poll_growatt, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
