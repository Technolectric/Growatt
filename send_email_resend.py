import os
import requests
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

def send_test_email():
    # Get credentials from environment variables
    resend_api_key = os.getenv('RESEND_API_KEY')
    sender_email = os.getenv('SENDER_EMAIL')
    recipient_email = os.getenv('RECIPIENT_EMAIL')
    
    # Check if credentials are loaded
    if not all([resend_api_key, sender_email, recipient_email]):
        print("✗ Error: Missing credentials in environment variables")
        print("Required: RESEND_API_KEY, SENDER_EMAIL, RECIPIENT_EMAIL")
        return False
    
    # Prepare email data
    email_data = {
        "from": sender_email,
        "to": [recipient_email],
        "subject": "Test Email from Python (Resend)",
        "html": """
        <html>
            <body>
                <h2>Hello! 👋</h2>
                <p>This is a test email sent from Python using Resend API.</p>
                <p>If you're reading this, it worked! 🎉</p>
                <br>
                <p><strong>Best regards,</strong><br>Your Python Script</p>
            </body>
        </html>
        """
    }
    
    try:
        print("=" * 50)
        print("Email Test Script (Resend API)")
        print("=" * 50)
        print("Sending email via Resend...")
        
        # Send email using Resend API
        response = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {resend_api_key}",
                "Content-Type": "application/json"
            },
            json=email_data
        )
        
        if response.status_code == 200:
            print(f"✓ Email sent successfully!")
            print(f"  To: {recipient_email}")
            print(f"  From: {sender_email}")
            result = response.json()
            print(f"  Email ID: {result.get('id', 'N/A')}")
            return True
        else:
            print(f"✗ Error: {response.status_code}")
            print(f"  Message: {response.text}")
            return False
        
    except Exception as e:
        print(f"✗ Error sending email: {e}")
        return False

if __name__ == "__main__":
    send_test_email()
