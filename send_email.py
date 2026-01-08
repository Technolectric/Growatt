import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

def send_test_email():
    # Get credentials from environment variables
    sender_email = os.getenv('SENDER_EMAIL')
    sender_password = os.getenv('SENDER_PASSWORD')
    recipient_email = os.getenv('RECIPIENT_EMAIL')
    
    # Check if credentials are loaded
    if not all([sender_email, sender_password, recipient_email]):
        print("Error: Missing email credentials in .env file")
        return False
    
    # Create message
    message = MIMEMultipart()
    message['From'] = sender_email
    message['To'] = recipient_email
    message['Subject'] = 'Test Email from Python'
    
    # Email body
    body = """
    Hello!
    
    This is a test email sent from Python.
    If you're reading this, it worked! 🎉
    
    Best regards,
    Your Python Script
    """
    
    message.attach(MIMEText(body, 'plain'))
    
    try:
        # Connect to Gmail's SMTP server
        print("Connecting to email server...")
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()  # Enable security
        
        # Login
        print("Logging in...")
        server.login(sender_email, sender_password)
        
        # Send email
        print("Sending email...")
        server.send_message(message)
        
        print(f"✓ Email sent successfully to {recipient_email}!")
        
        # Close connection
        server.quit()
        return True
        
    except smtplib.SMTPAuthenticationError:
        print("✗ Authentication failed. Check your email and password/app password.")
        return False
    except smtplib.SMTPException as e:
        print(f"✗ SMTP error occurred: {e}")
        return False
    except Exception as e:
        print(f"✗ Error: {e}")
        return False

if __name__ == "__main__":
    print("=" * 50)
    print("Email Test Script")
    print("=" * 50)
    send_test_email()
