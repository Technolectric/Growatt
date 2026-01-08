import os
import time
import requests
from datetime import datetime, timedelta, timezone
from threading import Thread
from flask import Flask, render_template_string, request

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
LOAD_THRESHOLD_WATTS = int(os.getenv("LOAD_THRESHOLD_WATTS", 1000))
LOAD_DURATION_HOURS = int(os.getenv("LOAD_DURATION_HOURS", 2))
BATTERY_THRESHOLD_PERCENT = int(os.getenv("BATTERY_THRESHOLD_PERCENT", 40))
ALERT_COOLDOWN_MINUTES = int(os.getenv("ALERT_COOLDOWN_MINUTES", 60))
HISTORY_HOURS = 12  # 12-hour load history

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
high_load_start = None
last_alert_time = None
latest_data = {}
load_history = []  # store tuples: (timestamp, total_load)

# East African Timezone
EAT = timezone(timedelta(hours=3))

# ----------------------------
# Email function
# ----------------------------
def send_email(subject, html_content):
    global last_alert_time
    if not all([RESEND_API_KEY, SENDER_EMAIL, RECIPIENT_EMAIL]):
        print("✗ Error: Missing email credentials in env")
        return False
    
    # Rate limit
    if last_alert_time and datetime.now(EAT) - last_alert_time < timedelta(minutes=ALERT_COOLDOWN_MINUTES):
        print("⚠️ Alert cooldown active, skipping email")
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
            print(f"✓ Email sent: {subject}")
            last_alert_time = datetime.now(EAT)
            return True
        else:
            print(f"✗ Email failed {response.status_code}: {response.text}")
            return False
    except Exception as e:
        print(f"✗ Error sending email: {e}")
        return False

# ----------------------------
# Helper: Can send alert?
# ----------------------------
def can_send_alert():
    global last_alert_time
    if last_alert_time is None:
        return True
    return datetime.now(EAT) - last_alert_time > timedelta(minutes=ALERT_COOLDOWN_MINUTES)

# ----------------------------
# Growatt Polling Loop
# ----------------------------
def poll_growatt():
    global high_load_start, latest_data, load_history
    while True:
        try:
            total_output_power = 0
            battery_percent = None
            inverter_data = []

            now = datetime.now(EAT)

            for sn in SERIAL_NUMBERS:
                response = requests.post(
                    API_URL, data={"storage_sn": sn}, headers=headers, timeout=20
                )
                response.raise_for_status()
                data = response.json().get("data", {})
                out_power = data.get("outPutPower", 0)
                soc = data.get("soc") or data.get("capacity")
                total_output_power += out_power

                if sn.startswith("KAM"):
                    battery_percent = soc

                inverter_data.append({
                    "SN": sn,
                    "OutputPower": out_power,
                    "BatterySOC": soc
                })

            # Save latest readings
            latest_data = {
                "timestamp": now.strftime("%Y-%m-%d %H:%M:%S EAT"),
                "total_output_power": total_output_power,
                "battery_percent": battery_percent,
                "inverters": inverter_data
            }

            # Save to 12-hour history
            load_history.append((now, total_output_power))
            load_history = [(t, p) for t, p in load_history if t >= now - timedelta(hours=HISTORY_HOURS)]

            print(f"{latest_data['timestamp']} | Load={total_output_power}W | Battery={battery_percent}%")

            # --- Load Alert ---
            if total_output_power >= LOAD_THRESHOLD_WATTS:
                if high_load_start is None:
                    high_load_start = now
                elif now - high_load_start >= timedelta(hours=LOAD_DURATION_HOURS):
                    if can_send_alert():
                        send_email(
                            subject="⚠️ Growatt Alert: High Load",
                            html_content=f"<p>Total Load has been above {LOAD_THRESHOLD_WATTS}W for {LOAD_DURATION_HOURS} hours.<br>Current: {total_output_power}W</p>"
                        )
            else:
                high_load_start = None

            # --- Battery Alert ---
            if battery_percent is not None and battery_percent <= BATTERY_THRESHOLD_PERCENT:
                if can_send_alert():
                    send_email(
                        subject="⚠️ Growatt Alert: Low Battery",
                        html_content=f"<p>Battery percentage is low: {battery_percent}% (Threshold: {BATTERY_THRESHOLD_PERCENT}%)</p>"
                    )

        except Exception as e:
            print(f"❌ Error polling Growatt: {e}")

        time.sleep(POLL_INTERVAL_MINUTES * 60)

# ----------------------------
# Flask Web Routes
# ----------------------------
@app.route("/")
def home():
    # Colors for indicators
    load_color = "red" if latest_data.get("total_output_power", 0) >= LOAD_THRESHOLD_WATTS else "green"
    battery_color = "red" if latest_data.get("battery_percent", 100) <= BATTERY_THRESHOLD_PERCENT else "green"

    html = f"""
    <h2>Growatt Monitor</h2>
    <p>Last updated: {latest_data.get('timestamp', 'N/A')}</p>
    <p>Total Output Power: <span style="color:{load_color}">{latest_data.get('total_output_power', 'N/A')} W</span></p>
    <p>KAM Battery: <span style="color:{battery_color}">{latest_data.get('battery_percent', 'N/A')}%</span></p>

    <h3>Per Inverter Data</h3>
    <table border="1" cellpadding="5" cellspacing="0">
        <tr>
            <th>SN</th>
            <th>Output Power (W)</th>
            <th>Battery SOC (%)</th>
        </tr>
    """

    for inv in latest_data.get("inverters", []):
        inv_color = "red" if (inv['BatterySOC'] is not None and inv['BatterySOC'] <= BATTERY_THRESHOLD_PERCENT) else "green"
        html += f"""
        <tr>
            <td>{inv['SN']}</td>
            <td>{inv['OutputPower']}</td>
            <td style="color:{inv_color}">{inv['BatterySOC']}</td>
        </tr>
        """

    html += "</table><br>"

    # Load History Table
    html += """
    <h3>Load History (Last 12 hours)</h3>
    <table border="1" cellpadding="5" cellspacing="0">
        <tr>
            <th>Time (EAT)</th>
            <th>Total Load (W)</th>
        </tr>
    """
    for t, p in reversed(load_history):  # latest first
        html += f"""
        <tr>
            <td>{t.strftime('%Y-%m-%d %H:%M:%S')}</td>
            <td>{p}</td>
        </tr>
        """
    html += "</table><br>"

    # Manual test alert button
    html += """
    <form action="/test_alert" method="post">
        <button type="submit">Send Test Alert Email</button>
    </form>
    """

    return render_template_string(html)

@app.route("/test_alert", methods=["POST"])
def test_alert():
    send_email(
        subject="🔔 Growatt Test Alert",
        html_content="<p>This is a test alert from your Growatt monitor.</p>"
    )
    return "<p>Test alert sent! ✅ <a href='/'>Back</a></p>"

# ----------------------------
# Start background polling thread
# ----------------------------
Thread(target=poll_growatt, daemon=True).start()

# ----------------------------
# Run Flask
# ----------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
