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
latest_data = {
    "timestamp": "Initializing...",
    "total_output_power": 0,
    "total_battery_discharge_W": 0,
    "total_solar_input_W": 0,
    "primary_battery_min": 0,
    "backup_battery_voltage": 0,
    "backup_voltage_status": "Unknown",
    "backup_active": False,
    "backup_percent_calc": 0,
    "backup_kwh_calc": 0,
    "generator_running": False,
    "inverters": [],
    "solar_forecast": [],
    "load_forecast": [],
    "battery_life_prediction": None,
    "weather_source": "Initializing..."
}
load_history = []
battery_history = []
weather_forecast = {}
weather_source = "Initializing..."
solar_conditions_cache = None
alert_history = []
last_communication = {}

pool_pump_start_time = None
pool_pump_last_alert = None

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

# Helper Functions
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
    def _num(val):
        """Safe number conversion"""
        try:
            return float(val) if val is not None else 0
        except (ValueError, TypeError):
            return 0
    
    # Extract data safely
    p_bat = _num(latest_data.get("primary_battery_min", 0))
    b_volt = _num(latest_data.get("backup_battery_voltage", 0))
    b_stat = latest_data.get("backup_voltage_status", "Unknown")
    b_active = latest_data.get("backup_active", False)
    gen_on = latest_data.get("generator_running", False)
    tot_load = _num(latest_data.get("total_output_power", 0))
    tot_sol = _num(latest_data.get("total_solar_input_W", 0))
    tot_dis = _num(latest_data.get("total_battery_discharge_W", 0))
    
    p_kwh = (p_bat / 100.0) * 30.0
    b_pct = _num(latest_data.get("backup_percent_calc", 0))
    b_kwh = _num(latest_data.get("backup_kwh_calc", 0))
    
    sol_cond = solar_conditions_cache
    weather_bad = sol_cond and sol_cond['poor_conditions']
    surplus_power = tot_sol - tot_load

    # Status determination
    if gen_on:
        app_st, app_sub, app_col = "⚠️ GENERATOR RUNNING", "Stop all heavy loads immediately", "critical"
        status_icon = "🚨"
    elif b_active:
        app_st, app_sub, app_col = "⚠️ BACKUP ACTIVE", "Primary depleted - conserve power", "critical"
        status_icon = "⚠️"
    elif p_bat < 45 and tot_sol < tot_load:
        app_st, app_sub, app_col = "⚠️ REDUCE LOADS", "Battery low & discharging", "warning"
        status_icon = "⚠️"
    elif p_bat > 95:
        app_st, app_sub, app_col = "✅ BATTERY FULL", "System fully charged", "good"
        status_icon = "🔋"
    elif tot_sol > 2000 and (tot_sol > tot_load * 0.9):
        app_st, app_sub, app_col = "✅ SOLAR POWERING", "Solar covering loads", "good"
        status_icon = "☀️"
    elif (p_bat > 75 and surplus_power > 3000):
        app_st, app_sub, app_col = "✅ HIGH SURPLUS", f"Heavy loads safe", "good"
        status_icon = "⚡"
    elif weather_bad and p_bat > 80:
        app_st, app_sub, app_col = "⚡ USE POWER NOW", "Poor forecast - cook now", "good"
        status_icon = "⚡"
    elif weather_bad and p_bat < 70:
        app_st, app_sub, app_col = "☁️ CONSERVE POWER", "Low solar expected", "warning"
        status_icon = "☁️"
    elif surplus_power > 100:
        app_st, app_sub, app_col = "🔋 CHARGING", f"System recovering", "normal"
        status_icon = "🔋"
    else:
        app_st, app_sub, app_col = "ℹ️ NORMAL", "System running", "normal"
        status_icon = "ℹ️"
    
    # Chart data
    if not load_history:
        times = [datetime.now(EAT).strftime('%d %b %H:%M')]
        l_vals = [tot_load]
        b_vals = [tot_dis]
    else:
        total_points = len(load_history)
        step = max(1, total_points // 150)
        times = [t.strftime('%d %b %H:%M') for i, (t, p) in enumerate(load_history) if i % step == 0]
        l_vals = [p for i, (t, p) in enumerate(load_history) if i % step == 0]
        b_vals = [p for i, (t, p) in enumerate(battery_history) if i % step == 0]
    
    pred = latest_data.get("battery_life_prediction")
    sim_t = ["Now"] + [d['time'].strftime('%H:%M') for d in latest_data.get("solar_forecast", [])]
    trace_pct = pred.get('trace_total_pct', []) if pred else []
    
    s_forecast = latest_data.get("solar_forecast", [])
    l_forecast = latest_data.get("load_forecast", [])
    
    if s_forecast and l_forecast:
        forecast_times = [d['time'].strftime('%H:%M') for d in s_forecast[:12]]
        forecast_solar = [d['estimated_generation'] for d in s_forecast[:12]]
        forecast_load = [d['estimated_load'] for d in l_forecast[:12]]
    else:
        now = datetime.now(EAT)
        forecast_times = []
        forecast_solar = []
        forecast_load = []
        for i in range(12):
            hour = (now.hour + i) % 24
            forecast_times.append((now + timedelta(hours=i)).strftime('%H:%M'))
            if 6 <= hour <= 18:
                forecast_solar.append(3000 - abs(12 - hour) * 200)
            else:
                forecast_solar.append(0)
            forecast_load.append(1200)

    # Power flow states
    solar_active = tot_sol > 100
    battery_charging = surplus_power > 100
    battery_discharging = tot_dis > 100
    
    # Inverter temperature
    inverter_temps = [inv.get('temperature', 0) for inv in latest_data.get('inverters', [])]
    inverter_temp = f"{(sum(inverter_temps) / len(inverter_temps)):.0f}" if inverter_temps else "0"
    
    # Trends
    load_trend_icon = "↑" if tot_load > 2000 else "→" if tot_load > 1000 else "↓"
    load_trend_text = "High" if tot_load > 2000 else "Moderate" if tot_load > 1000 else "Low"
    load_trend_class = "trend-up" if tot_load > 2000 else "trend-down" if tot_load < 1000 else ""
    
    solar_trend_icon = "☀️" if tot_sol > 5000 else "⛅" if tot_sol > 2000 else "☁️"
    solar_trend_text = "Excellent" if tot_sol > 5000 else "Good" if tot_sol > 2000 else "Low"
    solar_trend_class = "trend-up" if tot_sol > 2000 else "trend-down"
    
    primary_color = "text-success" if p_bat > 60 else "text-warning" if p_bat > 40 else "text-danger"
    backup_color = "text-success" if b_volt > 52.3 else "text-warning" if b_volt > 51.5 else "text-danger"
    
    primary_battery_class = "" if p_bat > 60 else "warning" if p_bat > 40 else "critical"
    backup_battery_class = "" if b_pct > 60 else "warning" if b_pct > 40 else "critical"
    
    alerts = [{"time": a['timestamp'].strftime("%H:%M"), "subject": a['subject'], "type": a['type']} 
              for a in reversed(alert_history[-10:])]
    
    # Smart Recommendations
    recommendation_items = []
    
    safe_statuses = ["USE POWER NOW", "HIGH SURPLUS", "BATTERY FULL", "SOLAR POWERING"]
    is_safe_now = any(s in app_st for s in safe_statuses)
    
    if gen_on:
        recommendation_items.append({
            'icon': '🚨',
            'title': 'NO HEAVY LOADS',
            'description': 'Generator running - turn off all non-essential appliances',
            'appliances': [
                {'name': 'Oven', 'status': '❌', 'class': 'danger'},
                {'name': 'Kettle', 'status': '❌', 'class': 'danger'},
                {'name': 'Washing Machine', 'status': '❌', 'class': 'danger'},
                {'name': 'Pool Pumps', 'status': '❌', 'class': 'danger'}
            ],
            'class': 'critical'
        })
    elif b_active:
        recommendation_items.append({
            'icon': '⚠️',
            'title': 'MINIMIZE LOADS',
            'description': 'Backup battery active - essential loads only',
            'appliances': [
                {'name': 'Oven', 'status': '❌', 'class': 'danger'},
                {'name': 'Kettle', 'status': '⚠️', 'class': 'warning'},
                {'name': 'Washing Machine', 'status': '❌', 'class': 'danger'},
                {'name': 'Pool Pumps', 'status': '❌', 'class': 'danger'}
            ],
            'class': 'warning'
        })
    elif is_safe_now:
        recommendation_items.append({
            'icon': '✅',
            'title': 'SAFE TO USE HEAVY LOADS',
            'description': f'Battery: {p_bat:.0f}% | Solar: {tot_sol:.0f}W | Surplus: {surplus_power:.0f}W',
            'appliances': [
                {'name': 'Oven', 'status': '✅', 'class': 'good'},
                {'name': 'Kettle', 'status': '✅', 'class': 'good'},
                {'name': 'Washing Machine', 'status': '✅', 'class': 'good'},
                {'name': 'Pool Pumps', 'status': '✅', 'class': 'good'}
            ],
            'class': 'good'
        })
    elif p_bat < 50 and tot_sol < tot_load:
        recommendation_items.append({
            'icon': '⚠️',
            'title': 'CONSERVE POWER',
            'description': f'Battery low ({p_bat:.0f}%) and not charging well',
            'appliances': [
                {'name': 'Oven', 'status': '❌', 'class': 'danger'},
                {'name': 'Kettle', 'status': '⚠️', 'class': 'warning'},
                {'name': 'Washing Machine', 'status': '❌', 'class': 'danger'},
                {'name': 'Pool Pumps', 'status': '❌', 'class': 'danger'}
            ],
            'class': 'warning'
        })
    else:
        recommendation_items.append({
            'icon': 'ℹ️',
            'title': 'MONITOR USAGE',
            'description': 'Check schedule below for optimal times',
            'appliances': [
                {'name': 'Oven', 'status': '⚠️', 'class': 'warning'},
                {'name': 'Kettle', 'status': '✅', 'class': 'good'},
                {'name': 'Washing Machine', 'status': '⚠️', 'class': 'warning'},
                {'name': 'Pool Pumps', 'status': 'ℹ️', 'class': 'info'}
            ],
            'class': 'normal'
        })
    
    # Schedule items
    schedule_items = []
    
    if s_forecast:
        best_start, best_end, current_run = None, None, 0
        temp_start = None
        for d in s_forecast:
            gen = d['estimated_generation']
            if gen > 2000:
                if current_run == 0: 
                    temp_start = d['time']
                current_run += 1
            else:
                if current_run > 0:
                    if best_start is None or current_run > ((best_end.hour if best_end else 0) - (best_start.hour if best_start else 0)):
                        best_start = temp_start
                        best_end = d['time']
                    current_run = 0
        
        if best_start and best_end:
            schedule_items.append({
                'icon': '🚿',
                'title': 'Best Time for Heavy Loads',
                'time': f"{best_start.strftime('%I:%M %p').lstrip('0')} - {best_end.strftime('%I:%M %p').lstrip('0')}",
                'class': 'good'
            })
        else:
            schedule_items.append({
                'icon': '☁️',
                'title': 'No High Solar Window',
                'time': 'Avoid heavy loads today',
                'class': 'warning'
            })
        
        # Cloud warnings
        next_3_gen = sum([d['estimated_generation'] for d in s_forecast[:3]]) / 3 if len(s_forecast) >= 3 else 0
        current_hour = datetime.now(EAT).hour
        if next_3_gen < 500 and 8 <= current_hour <= 16:
            schedule_items.append({
                'icon': '☁️',
                'title': 'Cloud Warning',
                'time': 'Low solar next 3 hours',
                'class': 'warning'
            })

    html_template = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Tulia House Solar</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;700;800&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-annotation@3.0.1/dist/chartjs-plugin-annotation.min.js"></script>
    <style>
        :root {
            --bg: #0d1117;
            --surface: #161b22;
            --surface-2: #21262d;
            --border: rgba(48, 54, 61, 0.8);
            --text: #e6edf3;
            --text-muted: #8b949e;
            --primary: #3fb950;
            --warning: #f0883e;
            --danger: #f85149;
            --info: #58a6ff;
        }
        
        * { margin: 0; padding: 0; box-sizing: border-box; }
        
        body {
            font-family: 'Inter', -apple-system, sans-serif;
            background: var(--bg);
            color: var(--text);
            line-height: 1.5;
            -webkit-font-smoothing: antialiased;
        }
        
        .container {
            max-width: 1400px;
            margin: 0 auto;
            padding: 1rem;
        }
        
        @media (min-width: 768px) {
            .container { padding: 2rem; }
        }
        
        /* Header */
        header {
            text-align: center;
            padding: 1.5rem 0;
            margin-bottom: 2rem;
        }
        
        h1 {
            font-size: clamp(2rem, 5vw, 3rem);
            font-weight: 800;
            color: var(--primary);
            margin-bottom: 0.5rem;
        }
        
        .subtitle {
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.85rem;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.1em;
        }
        
        /* Status Hero */
        .status-hero {
            background: linear-gradient(135deg, var(--surface) 0%, var(--surface-2) 100%);
            border: 2px solid var(--border);
            border-radius: 1rem;
            padding: 2rem;
            text-align: center;
            margin-bottom: 2rem;
            position: relative;
            overflow: hidden;
        }
        
        .status-hero.critical { border-color: var(--danger); background: linear-gradient(135deg, rgba(248,81,73,0.1), var(--surface-2)); }
        .status-hero.warning { border-color: var(--warning); background: linear-gradient(135deg, rgba(240,136,62,0.1), var(--surface-2)); }
        .status-hero.good { border-color: var(--primary); background: linear-gradient(135deg, rgba(63,185,80,0.1), var(--surface-2)); }
        
        .status-icon {
            font-size: 3rem;
            margin-bottom: 0.5rem;
            display: inline-block;
            animation: pulse 2s ease-in-out infinite;
        }
        
        @keyframes pulse {
            0%, 100% { transform: scale(1); opacity: 1; }
            50% { transform: scale(1.1); opacity: 0.8; }
        }
        
        .status-title {
            font-size: clamp(1.5rem, 4vw, 2.5rem);
            font-weight: 800;
            margin-bottom: 0.5rem;
        }
        
        .status-hero.critical .status-title { color: var(--danger); }
        .status-hero.warning .status-title { color: var(--warning); }
        .status-hero.good .status-title { color: var(--primary); }
        .status-hero.normal .status-title { color: var(--info); }
        
        .status-subtitle {
            font-size: 1rem;
            color: var(--text-muted);
        }
        
        /* Grid */
        .grid { display: grid; gap: 1rem; margin-bottom: 2rem; }
        .grid-2 { grid-template-columns: 1fr; }
        .grid-3 { grid-template-columns: 1fr; }
        .grid-4 { grid-template-columns: repeat(2, 1fr); }
        
        @media (min-width: 768px) {
            .grid-2 { grid-template-columns: repeat(2, 1fr); }
            .grid-3 { grid-template-columns: repeat(3, 1fr); }
            .grid-4 { grid-template-columns: repeat(4, 1fr); }
        }
        
        /* Card */
        .card {
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 1rem;
            padding: 1.5rem;
            transition: all 0.3s ease;
        }
        
        .card:hover {
            border-color: var(--primary);
            box-shadow: 0 4px 12px rgba(63, 185, 80, 0.1);
        }
        
        h2 {
            font-size: 1.25rem;
            font-weight: 700;
            margin-bottom: 1.5rem;
            color: var(--text);
        }
        
        h3 {
            font-size: 1rem;
            font-weight: 600;
            margin-bottom: 1rem;
            color: var(--text);
        }
        
        /* Metric Card */
        .metric-label {
            font-size: 0.75rem;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-bottom: 0.5rem;
            font-weight: 600;
        }
        
        .metric-value {
            font-size: clamp(1.75rem, 4vw, 2.5rem);
            font-weight: 800;
            font-family: 'JetBrains Mono', monospace;
            line-height: 1;
            margin-bottom: 0.5rem;
        }
        
        .metric-unit {
            font-size: 1rem;
            font-weight: 400;
            color: var(--text-muted);
        }
        
        .metric-trend {
            font-size: 0.85rem;
            color: var(--text-muted);
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }
        
        .text-success { color: var(--primary); }
        .text-warning { color: var(--warning); }
        .text-danger { color: var(--danger); }
        .text-info { color: var(--info); }
        
        /* Power Flow */
        .power-flow {
            position: relative;
            width: 100%;
            max-width: 600px;
            aspect-ratio: 1.5;
            margin: 0 auto;
        }
        
        .flow-svg {
            position: absolute;
            width: 100%;
            height: 100%;
            top: 0;
            left: 0;
            pointer-events: none;
        }
        
        .flow-node {
            position: absolute;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            background: var(--surface-2);
            border: 2px solid var(--border);
            border-radius: 50%;
            transition: all 0.3s ease;
            z-index: 10;
        }
        
        .flow-node.active {
            border-color: var(--primary);
            box-shadow: 0 0 20px rgba(63, 185, 80, 0.3);
        }
        
        .flow-node.inverter {
            width: 20%;
            padding-bottom: 20%;
            background: linear-gradient(135deg, var(--surface), var(--surface-2));
            border-width: 3px;
            border-color: var(--info);
        }
        
        .flow-node:not(.inverter) {
            width: 15%;
            padding-bottom: 15%;
        }
        
        .flow-node-content {
            position: absolute;
            width: 100%;
            height: 100%;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
        }
        
        .flow-icon {
            font-size: clamp(1rem, 3vw, 1.5rem);
            margin-bottom: 0.25rem;
        }
        
        .flow-label {
            font-size: clamp(0.5rem, 1.5vw, 0.65rem);
            font-weight: 600;
            color: var(--text-muted);
            text-transform: uppercase;
        }
        
        .flow-value {
            font-size: clamp(0.55rem, 1.5vw, 0.75rem);
            font-family: 'JetBrains Mono', monospace;
            font-weight: 600;
            color: var(--primary);
            white-space: nowrap;
        }
        
        /* Position nodes */
        .flow-node.solar { top: 50%; left: 5%; transform: translateY(-50%); }
        .flow-node.inverter { top: 50%; left: 50%; transform: translate(-50%, -50%); }
        .flow-node.load { top: 50%; right: 5%; transform: translateY(-50%); }
        .flow-node.battery { bottom: 5%; left: 50%; transform: translateX(-50%); }
        .flow-node.generator { top: 5%; left: 50%; transform: translateX(-50%); }
        
        /* Battery Stack */
        .battery-stack {
            position: relative;
            height: 300px;
            display: flex;
            align-items: flex-end;
            justify-content: center;
            gap: 1rem;
        }
        
        .battery-card {
            width: 100%;
            max-width: 280px;
            border: 2px solid var(--border);
            border-radius: 1rem;
            overflow: hidden;
            transition: all 0.5s cubic-bezier(0.4, 0, 0.2, 1);
            background: var(--surface-2);
        }
        
        .battery-card.primary {
            height: 100%;
            z-index: 2;
        }
        
        .battery-card.backup {
            height: 60%;
            z-index: 1;
            opacity: 0.7;
        }
        
        .battery-card.backup.active {
            height: 100%;
            z-index: 3;
            opacity: 1;
            border-color: var(--warning);
            box-shadow: 0 0 30px rgba(240, 136, 62, 0.3);
        }
        
        .battery-card.primary.inactive {
            height: 60%;
            z-index: 1;
            opacity: 0.7;
        }
        
        .battery-header {
            padding: 1rem;
            border-bottom: 1px solid var(--border);
            text-align: center;
        }
        
        .battery-header h3 {
            margin: 0;
            font-size: 0.9rem;
            color: var(--text);
        }
        
        .battery-header .capacity {
            font-size: 0.75rem;
            color: var(--text-muted);
        }
        
        .battery-visual {
            position: relative;
            height: 200px;
            background: var(--surface);
            overflow: hidden;
        }
        
        .battery-fill {
            position: absolute;
            bottom: 0;
            left: 0;
            right: 0;
            background: linear-gradient(to top, var(--primary), #58ff80);
            transition: height 1s ease;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        
        .battery-fill.warning {
            background: linear-gradient(to top, var(--warning), #ffa040);
        }
        
        .battery-fill.critical {
            background: linear-gradient(to top, var(--danger), #ff6060);
        }
        
        .battery-percentage {
            font-size: 2rem;
            font-weight: 800;
            font-family: 'JetBrains Mono', monospace;
            color: white;
            text-shadow: 0 2px 8px rgba(0, 0, 0, 0.5);
        }
        
        .battery-stats {
            padding: 1rem;
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 0.5rem;
            font-size: 0.85rem;
        }
        
        .battery-stat {
            display: flex;
            justify-content: space-between;
        }
        
        .battery-stat-label {
            color: var(--text-muted);
        }
        
        .battery-stat-value {
            font-family: 'JetBrains Mono', monospace;
            font-weight: 600;
        }
        
        /* Recommendation Card */
        .rec-card {
            border-left: 4px solid var(--border);
        }
        
        .rec-card.critical { border-left-color: var(--danger); background: rgba(248,81,73,0.05); }
        .rec-card.warning { border-left-color: var(--warning); background: rgba(240,136,62,0.05); }
        .rec-card.good { border-left-color: var(--primary); background: rgba(63,185,80,0.05); }
        
        .rec-header {
            display: flex;
            align-items: center;
            gap: 1rem;
            margin-bottom: 1.5rem;
        }
        
        .rec-icon {
            font-size: 2.5rem;
        }
        
        .rec-title {
            font-size: 1.25rem;
            font-weight: 700;
            margin: 0;
        }
        
        .rec-description {
            font-size: 0.9rem;
            color: var(--text-muted);
            margin-top: 0.25rem;
        }
        
        .appliances-grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 0.75rem;
            margin-top: 1rem;
        }
        
        .appliance-item {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 0.75rem;
            background: var(--surface-2);
            border-radius: 0.5rem;
            border: 1px solid var(--border);
        }
        
        .appliance-name {
            font-weight: 600;
            font-size: 0.9rem;
        }
        
        .appliance-status {
            font-size: 1.25rem;
        }
        
        /* Schedule */
        .schedule-item {
            display: flex;
            align-items: center;
            gap: 1rem;
            padding: 1rem;
            background: var(--surface-2);
            border-radius: 0.75rem;
            margin-bottom: 0.75rem;
            border: 1px solid var(--border);
        }
        
        .schedule-item.good { border-left: 3px solid var(--primary); }
        .schedule-item.warning { border-left: 3px solid var(--warning); }
        
        .schedule-icon {
            font-size: 1.5rem;
        }
        
        .schedule-content {
            flex: 1;
        }
        
        .schedule-title {
            font-weight: 600;
            margin-bottom: 0.25rem;
        }
        
        .schedule-time {
            font-size: 0.85rem;
            color: var(--text-muted);
        }
        
        /* Chart */
        .chart-container {
            position: relative;
            height: 300px;
            margin: 1rem 0;
        }
        
        /* Inverter Cards */
        .inverter-card {
            background: var(--surface-2);
            border: 1px solid var(--border);
            border-radius: 0.75rem;
            padding: 1rem;
        }
        
        .inverter-card.fault {
            border-color: var(--danger);
            background: rgba(248,81,73,0.05);
        }
        
        .inverter-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 0.75rem;
        }
        
        .inverter-name {
            font-weight: 700;
        }
        
        .inverter-status {
            font-size: 0.7rem;
            padding: 0.25rem 0.5rem;
            border-radius: 0.25rem;
            background: var(--surface);
            text-transform: uppercase;
            font-weight: 600;
        }
        
        .inverter-metrics {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 0.5rem;
        }
        
        .inverter-metric {
            display: flex;
            justify-content: space-between;
            padding: 0.5rem 0;
            border-bottom: 1px solid var(--border);
            font-size: 0.85rem;
        }
        
        .inverter-metric-label {
            color: var(--text-muted);
        }
        
        .inverter-metric-value {
            font-family: 'JetBrains Mono', monospace;
            font-weight: 600;
        }
        
        /* Alert */
        .alert-item {
            display: flex;
            align-items: center;
            gap: 1rem;
            padding: 0.75rem;
            background: var(--surface-2);
            border-radius: 0.5rem;
            margin-bottom: 0.5rem;
            border-left: 3px solid var(--border);
        }
        
        .alert-item.critical { border-left-color: var(--danger); }
        .alert-item.warning { border-left-color: var(--warning); }
        
        .alert-time {
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.75rem;
            color: var(--text-muted);
            min-width: 50px;
        }
        
        .alert-message {
            flex: 1;
            font-size: 0.9rem;
        }
        
        /* Animations */
        @keyframes fadeInUp {
            from { opacity: 0; transform: translateY(20px); }
            to { opacity: 1; transform: translateY(0); }
        }
        
        .card { animation: fadeInUp 0.5s ease; }
        .card:nth-child(1) { animation-delay: 0.1s; }
        .card:nth-child(2) { animation-delay: 0.2s; }
        .card:nth-child(3) { animation-delay: 0.3s; }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>TULIA HOUSE</h1>
            <div class="subtitle">Solar Energy System • {{ timestamp }}</div>
        </header>
        
        <!-- Status Hero -->
        <div class="status-hero {{ app_col }}">
            <div class="status-icon">{{ status_icon }}</div>
            <div class="status-title">{{ app_st }}</div>
            <div class="status-subtitle">{{ app_sub }}</div>
        </div>
        
        <!-- Key Metrics -->
        <div class="grid grid-4">
            <div class="card">
                <div class="metric-label">Load Demand</div>
                <div class="metric-value text-info">{{ '%0.f'|format(tot_load) }}<span class="metric-unit">W</span></div>
                <div class="metric-trend">{{ load_trend_icon }} {{ load_trend_text }}</div>
            </div>
            
            <div class="card">
                <div class="metric-label">Solar Generation</div>
                <div class="metric-value text-success">{{ '%0.f'|format(tot_sol) }}<span class="metric-unit">W</span></div>
                <div class="metric-trend">{{ solar_trend_icon }} {{ solar_trend_text }}</div>
            </div>
            
            <div class="card">
                <div class="metric-label">Primary Battery</div>
                <div class="metric-value {{ primary_color }}">{{ '%0.f'|format(p_bat) }}<span class="metric-unit">%</span></div>
                <div style="margin-top: 0.5rem; color: var(--text-muted); font-size: 0.85rem;">{{ '%0.1f'|format(p_kwh) }} kWh</div>
            </div>
            
            <div class="card">
                <div class="metric-label">Backup Battery</div>
                <div class="metric-value {{ backup_color }}">{{ '%0.1f'|format(b_volt) }}<span class="metric-unit">V</span></div>
                <div style="margin-top: 0.5rem; color: var(--text-muted); font-size: 0.85rem;">{{ '%0.1f'|format(b_kwh) }} kWh</div>
            </div>
        </div>
        
        <!-- Power Flow -->
        <div class="card">
            <h2>Real-time Energy Flow</h2>
            <div class="power-flow">
                <svg class="flow-svg" viewBox="0 0 100 100" preserveAspectRatio="none">
                    <!-- Solar to Inverter -->
                    <line x1="12.5" y1="50" x2="37.5" y2="50" 
                          stroke="{{ 'var(--primary)' if solar_active else 'var(--border)' }}" 
                          stroke-width="0.5" vector-effect="non-scaling-stroke"/>
                    {% if solar_active %}
                    <circle r="1" fill="var(--primary)">
                        <animateMotion dur="2s" repeatCount="indefinite" path="M12.5,50 L37.5,50" />
                    </circle>
                    {% endif %}
                    
                    <!-- Inverter to Load -->
                    <line x1="62.5" y1="50" x2="87.5" y2="50" 
                          stroke="{{ 'var(--info)' if tot_load > 0 else 'var(--border)' }}" 
                          stroke-width="0.5" vector-effect="non-scaling-stroke"/>
                    {% if tot_load > 0 %}
                    <circle r="1" fill="var(--info)">
                        <animateMotion dur="1.5s" repeatCount="indefinite" path="M62.5,50 L87.5,50" />
                    </circle>
                    {% endif %}
                    
                    <!-- Battery to Inverter -->
                    <line x1="50" y1="60" x2="50" y2="87.5" 
                          stroke="{{ 'var(--primary)' if battery_charging else ('var(--danger)' if battery_discharging else 'var(--border)') }}" 
                          stroke-width="0.5" vector-effect="non-scaling-stroke"/>
                    {% if battery_charging %}
                    <circle r="1" fill="var(--primary)">
                        <animateMotion dur="2s" repeatCount="indefinite" path="M50,60 L50,87.5" />
                    </circle>
                    {% elif battery_discharging %}
                    <circle r="1" fill="var(--danger)">
                        <animateMotion dur="2s" repeatCount="indefinite" path="M50,87.5 L50,60" />
                    </circle>
                    {% endif %}
                    
                    <!-- Generator to Inverter -->
                    <line x1="50" y1="12.5" x2="50" y2="40" 
                          stroke="{{ 'var(--danger)' if gen_on else 'var(--border)' }}" 
                          stroke-width="0.5" vector-effect="non-scaling-stroke"/>
                    {% if gen_on %}
                    <circle r="1" fill="var(--danger)">
                        <animateMotion dur="1.5s" repeatCount="indefinite" path="M50,12.5 L50,40" />
                    </circle>
                    {% endif %}
                </svg>
                
                <!-- Nodes -->
                <div class="flow-node solar {{ 'active' if solar_active else '' }}">
                    <div class="flow-node-content">
                        <div class="flow-icon">☀️</div>
                        <div class="flow-label">Solar</div>
                        <div class="flow-value">{{ '%0.f'|format(tot_sol) }}W</div>
                    </div>
                </div>
                
                <div class="flow-node inverter active">
                    <div class="flow-node-content">
                        <div class="flow-icon">⚡</div>
                        <div class="flow-label">Inverter</div>
                        <div class="flow-value">{{ inverter_temp }}°C</div>
                    </div>
                </div>
                
                <div class="flow-node load active">
                    <div class="flow-node-content">
                        <div class="flow-icon">🏠</div>
                        <div class="flow-label">Load</div>
                        <div class="flow-value">{{ '%0.f'|format(tot_load) }}W</div>
                    </div>
                </div>
                
                <div class="flow-node battery {{ 'active' if battery_charging or battery_discharging else '' }}">
                    <div class="flow-node-content">
                        <div class="flow-icon">🔋</div>
                        <div class="flow-label">Battery</div>
                        <div class="flow-value">{{ '%0.f'|format(p_bat) }}%</div>
                    </div>
                </div>
                
                <div class="flow-node generator {{ 'active' if gen_on else '' }}">
                    <div class="flow-node-content">
                        <div class="flow-icon">{{ '⚠️' if gen_on else '🔌' }}</div>
                        <div class="flow-label">Generator</div>
                        <div class="flow-value">{{ 'ON' if gen_on else 'OFF' }}</div>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- Recommendations and Schedule -->
        <div class="grid grid-2">
            {% for rec in recommendation_items %}
            <div class="card rec-card {{ rec.class }}">
                <div class="rec-header">
                    <div class="rec-icon">{{ rec.icon }}</div>
                    <div>
                        <h3 class="rec-title">{{ rec.title }}</h3>
                        <div class="rec-description">{{ rec.description }}</div>
                    </div>
                </div>
                
                <div class="appliances-grid">
                    {% for appliance in rec.appliances %}
                    <div class="appliance-item">
                        <span class="appliance-name">{{ appliance.name }}</span>
                        <span class="appliance-status">{{ appliance.status }}</span>
                    </div>
                    {% endfor %}
                </div>
            </div>
            {% endfor %}
            
            <div class="card">
                <h3>📅 Optimal Usage Schedule</h3>
                {% for item in schedule_items %}
                <div class="schedule-item {{ item.class }}">
                    <div class="schedule-icon">{{ item.icon }}</div>
                    <div class="schedule-content">
                        <div class="schedule-title">{{ item.title }}</div>
                        <div class="schedule-time">{{ item.time }}</div>
                    </div>
                </div>
                {% endfor %}
                {% if not schedule_items %}
                <div style="text-align: center; padding: 2rem; color: var(--text-muted);">
                    Calculating optimal schedule...
                </div>
                {% endif %}
            </div>
        </div>
        
        <!-- Battery Status with Stacking -->
        <div class="card">
            <h2>Battery Status</h2>
            <div class="battery-stack">
                <div class="battery-card primary {{ 'inactive' if b_active else '' }}">
                    <div class="battery-header">
                        <h3>Primary Battery</h3>
                        <div class="capacity">30 kWh Capacity</div>
                    </div>
                    <div class="battery-visual">
                        <div class="battery-fill {{ primary_battery_class }}" style="height: {{ p_bat }}%;">
                            <div class="battery-percentage">{{ '%0.f'|format(p_bat) }}%</div>
                        </div>
                    </div>
                    <div class="battery-stats">
                        <div class="battery-stat">
                            <span class="battery-stat-label">Energy:</span>
                            <span class="battery-stat-value">{{ '%0.1f'|format(p_kwh) }} kWh</span>
                        </div>
                        <div class="battery-stat">
                            <span class="battery-stat-label">Status:</span>
                            <span class="battery-stat-value {{ primary_color }}">{{ 'Active' if not b_active else 'Standby' }}</span>
                        </div>
                    </div>
                </div>
                
                <div class="battery-card backup {{ 'active' if b_active else '' }}">
                    <div class="battery-header">
                        <h3>Backup Battery</h3>
                        <div class="capacity">21 kWh Capacity</div>
                    </div>
                    <div class="battery-visual">
                        <div class="battery-fill {{ backup_battery_class }}" style="height: {{ b_pct }}%;">
                            <div class="battery-percentage">{{ '%0.f'|format(b_pct) }}%</div>
                        </div>
                    </div>
                    <div class="battery-stats">
                        <div class="battery-stat">
                            <span class="battery-stat-label">Voltage:</span>
                            <span class="battery-stat-value">{{ '%0.1f'|format(b_volt) }}V</span>
                        </div>
                        <div class="battery-stat">
                            <span class="battery-stat-label">Status:</span>
                            <span class="battery-stat-value {{ backup_color }}">{{ 'Active' if b_active else 'Standby' }}</span>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- Forecast -->
        <div class="card">
            <h2>12-Hour Forecast</h2>
            <div class="chart-container">
                <canvas id="forecastChart"></canvas>
            </div>
        </div>
        
        <!-- Battery Prediction -->
        <div class="card">
            <h2>System Capacity Prediction</h2>
            <div class="chart-container">
                <canvas id="predictionChart"></canvas>
            </div>
        </div>
        
        <!-- History -->
        <div class="card">
            <h2>14-Day Power History</h2>
            <div class="chart-container">
                <canvas id="historyChart"></canvas>
            </div>
        </div>
        
        <!-- Inverter Status -->
        <div class="card">
            <h2>Inverter Status</h2>
            <div class="grid grid-3">
                {% for inv in latest_data.get('inverters', []) %}
                <div class="inverter-card {{ 'fault' if inv.has_fault or inv.high_temperature or inv.communication_lost else '' }}">
                    <div class="inverter-header">
                        <div class="inverter-name">{{ inv.Label }}</div>
                        <div class="inverter-status">{{ inv.Status }}</div>
                    </div>
                    <div class="inverter-metrics">
                        <div class="inverter-metric">
                            <span class="inverter-metric-label">Power</span>
                            <span class="inverter-metric-value">{{ '%0.f'|format(inv.OutputPower) }}W</span>
                        </div>
                        <div class="inverter-metric">
                            <span class="inverter-metric-label">Solar</span>
                            <span class="inverter-metric-value">{{ '%0.f'|format(inv.ppv) }}W</span>
                        </div>
                        <div class="inverter-metric">
                            <span class="inverter-metric-label">Battery</span>
                            <span class="inverter-metric-value">{{ '%0.1f'|format(inv.vBat) }}V</span>
                        </div>
                        <div class="inverter-metric">
                            <span class="inverter-metric-label">Temp</span>
                            <span class="inverter-metric-value {{ 'text-danger' if inv.high_temperature else '' }}">{{ '%0.1f'|format(inv.temperature) }}°C</span>
                        </div>
                    </div>
                    {% if inv.communication_lost %}
                    <div style="margin-top: 0.75rem; padding: 0.5rem; background: rgba(248, 81, 73, 0.1); border-radius: 0.5rem; text-align: center; font-size: 0.85rem; color: var(--danger);">
                        ⚠️ Communication Lost
                    </div>
                    {% endif %}
                </div>
                {% endfor %}
            </div>
        </div>
        
        <!-- Alerts -->
        <div class="card">
            <h2>Recent Alerts (Last 12 Hours)</h2>
            {% if alerts %}
                {% for alert in alerts %}
                <div class="alert-item {{ alert.type }}">
                    <div class="alert-time">{{ alert.time }}</div>
                    <div class="alert-message">{{ alert.subject }}</div>
                </div>
                {% endfor %}
            {% else %}
                <div style="text-align: center; padding: 2rem; color: var(--text-muted);">
                    No recent alerts - system operating normally
                </div>
            {% endif %}
        </div>
    </div>
    
    <script>
        // Chart.js config
        Chart.defaults.color = '#8b949e';
        Chart.defaults.borderColor = 'rgba(48, 54, 61, 0.3)';
        
        // Forecast Chart
        const forecastCtx = document.getElementById('forecastChart').getContext('2d');
        new Chart(forecastCtx, {
            type: 'line',
            data: {
                labels: {{ forecast_times|tojson }},
                datasets: [
                    {
                        label: 'Solar',
                        data: {{ forecast_solar|tojson }},
                        borderColor: '#3fb950',
                        backgroundColor: 'rgba(63, 185, 80, 0.1)',
                        fill: true,
                        tension: 0.4,
                        borderWidth: 2
                    },
                    {
                        label: 'Load',
                        data: {{ forecast_load|tojson }},
                        borderColor: '#58a6ff',
                        backgroundColor: 'rgba(88, 166, 255, 0.1)',
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
                    legend: { display: true, position: 'top' },
                    tooltip: {
                        mode: 'index',
                        intersect: false,
                        backgroundColor: 'rgba(22, 27, 34, 0.95)',
                        borderColor: 'rgba(48, 54, 61, 0.8)',
                        borderWidth: 1
                    }
                },
                scales: {
                    y: {
                        beginAtZero: true,
                        grid: { color: 'rgba(48, 54, 61, 0.3)' },
                        ticks: { callback: v => v + 'W' }
                    },
                    x: { grid: { display: false } }
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
                    label: 'Total Capacity',
                    data: {{ trace_pct|tojson }},
                    borderColor: '#58a6ff',
                    segment: {
                        borderColor: ctx => ctx.p0.parsed.y < 48 ? '#f0883e' : '#3fb950'
                    },
                    backgroundColor: 'rgba(88, 166, 255, 0.1)',
                    fill: true,
                    tension: 0.4,
                    borderWidth: 3
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: true },
                    annotation: {
                        annotations: {
                            backup: {
                                type: 'line',
                                yMin: 48,
                                yMax: 48,
                                borderColor: '#f0883e',
                                borderWidth: 2,
                                borderDash: [5, 5],
                                label: {
                                    content: 'Backup Activation (48%)',
                                    display: true,
                                    position: 'start',
                                    backgroundColor: 'rgba(240, 136, 62, 0.8)',
                                    color: '#000'
                                }
                            }
                        }
                    }
                },
                scales: {
                    y: {
                        min: 0,
                        max: 100,
                        grid: { color: 'rgba(48, 54, 61, 0.3)' },
                        ticks: { callback: v => v + '%' }
                    },
                    x: { grid: { display: false } }
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
                        borderColor: '#58a6ff',
                        backgroundColor: 'rgba(88, 166, 255, 0.1)',
                        fill: true,
                        tension: 0.4,
                        borderWidth: 2,
                        pointRadius: 0
                    },
                    {
                        label: 'Discharge',
                        data: {{ b_vals|tojson }},
                        borderColor: '#f85149',
                        backgroundColor: 'rgba(248, 81, 73, 0.1)',
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
                plugins: { legend: { display: true } },
                scales: {
                    y: {
                        beginAtZero: true,
                        grid: { color: 'rgba(48, 54, 61, 0.3)' },
                        ticks: { callback: v => v + 'W' }
                    },
                    x: {
                        grid: { display: false },
                        ticks: { maxRotation: 45, minRotation: 45 }
                    }
                }
            }
        });
        
        // Auto-refresh every 30 seconds
        setInterval(() => {
            fetch('/api/data')
                .then(res => res.json())
                .then(data => console.log('Refreshed:', data.timestamp))
                .catch(err => console.error('Refresh error:', err));
        }, 30000);
    </script>
</body>
</html>
    """
    
    from flask import render_template_string
    return render_template_string(
        html_template,
        timestamp=latest_data.get('timestamp', 'Initializing...'),
        status_icon=status_icon,
        app_st=app_st,
        app_sub=app_sub,
        app_col=app_col,
        tot_load=tot_load,
        tot_sol=tot_sol,
        p_bat=p_bat,
        b_volt=b_volt,
        p_kwh=p_kwh,
        b_kwh=b_kwh,
        b_pct=b_pct,
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
        solar_active=solar_active,
        battery_charging=battery_charging,
        battery_discharging=battery_discharging,
        gen_on=gen_on,
        b_active=b_active,
        inverter_temp=inverter_temp,
        recommendation_items=recommendation_items,
        schedule_items=schedule_items,
        forecast_times=forecast_times,
        forecast_solar=forecast_solar,
        forecast_load=forecast_load,
        sim_t=sim_t,
        trace_pct=trace_pct,
        times=times,
        l_vals=l_vals,
        b_vals=b_vals,
        latest_data=latest_data,
        alerts=alerts
    )

if __name__ == "__main__":
    Thread(target=poll_growatt, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
