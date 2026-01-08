import os
import time
import requests
from datetime import datetime, timedelta
from threading import Thread
from flask import Flask

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

app = Flask(__name__)

# ----------------------------
# Email Function
# ----------------------------
def send_email(subject, html_content):
    global last_alert_time
    if not all([RESEND_API_KEY, SENDER_EMAIL, RECIPIENT_EMAIL]):
        print("✗ Error: Missing email credentials in env")
        return False
    
    # Rate limit
    if last_alert_time and datetime.utcnow() - last_alert_time < timedelta(minutes=ALERT_COOLDOWN_MINUTES):
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
            last_alert_time = datetime.utcnow()
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
    return datetime.utcnow() - last_alert_time > timedelta(minutes=ALERT_COOLDOWN_MINUTES)

# ----------------------------
# Growatt Polling
# ----------------------------
def poll_growatt():
    global high_load_start
    while True:
        try:
            total_output_power = 0
            battery_percent = None

            for sn in SERIAL_NUMBERS:
                response = requests.post(
                    API_URL, data={"storage_sn": sn}, headers=headers, timeout=20
                )
                response.raise_for_status()
                data = response.json().get("data", {})
                total_output_power += data.get("outPutPower", 0)

                if sn.startswith("KAM"):
                    battery_percent = data.get("soc") or data.get("capacity")

            print(f"{datetime.utcnow()} | Load={total_output_power}W | Battery={battery_percent}%")

            # --- Load Alert ---
            if total_output_power >= LOAD_THRESHOLD_WATTS:
                if high_load_start is None:
                    high_load_start = datetime.utcnow()
                elif datetime.utcnow() - high_load_start >= timedelta(hours=LOAD_DURATION_HOURS):
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
# Start Polling Thread
# ----------------------------
Thread(target=poll_growatt, daemon=True).start()

# ----------------------------
# Minimal Web Route (Required for Render Free)
# ----------------------------
@app.route("/")
def home():
    return "Growatt monitor running ✅"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
