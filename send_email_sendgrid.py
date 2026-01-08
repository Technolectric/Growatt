import os
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

def send_test_email():
    # Get credentials from environment variables
    sendgrid_api_key = os.getenv('SENDGRID_API_KEY')
    sender_email = os.getenv('SENDER_EMAIL')
    recipient_email = os.getenv('RECIPIENT_EMAIL')
    
    # Check if credentials are loaded
    if not all([sendgrid_api_key, sender_email, recipient_email]):
        print("✗ Error: Missing credentials in environment variables")
        print("Required: SENDGRID_API_KEY, SENDER_EMAIL, RECIPIENT_EMAIL")
        return False
    
    # Create email message
    message = Mail(
        from_email=sender_email,
        to_emails=recipient_email,
        subject='Test Email from Python (SendGrid)',
        html_content='''
        <html>
            <body>
                <h2>Hello! 👋</h2>
                <p>This is a test email sent from Python using SendGrid API.</p>
                <p>If you're reading this, it worked! 🎉</p>
                <br>
                <p><strong>Best regards,</strong><br>Your Python Script</p>
            </body>
        </html>
        '''
    )
    
    try:
        # Send email using SendGrid API
        print("=" * 50)
        print("Email Test Script (SendGrid API)")
        print("=" * 50)
        print("Sending email via SendGrid...")
        
        sg = SendGridAPIClient(sendgrid_api_key)
        response = sg.send(message)
        
        print(f"✓ Email sent successfully!")
        print(f"  Status code: {response.status_code}")
        print(f"  To: {recipient_email}")
        print(f"  From: {sender_email}")
        
        return True
        
    except Exception as e:
        print(f"✗ Error sending email: {e}")
        return False

if __name__ == "__main__":
    send_test_email()
