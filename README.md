# Email Sender Script

A simple Python script to send test emails with protected credentials.

## Setup Instructions

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Create .env File
Copy `.env.example` to `.env`:
```bash
copy .env.example .env
```

Then edit `.env` and add your real credentials:
```
SENDER_EMAIL=your_email@gmail.com
SENDER_PASSWORD=your_app_password_here
RECIPIENT_EMAIL=recipient@example.com
```

### 3. Get Gmail App Password (Important!)

If using Gmail, you CANNOT use your regular password. You need an **App Password**:

1. Go to your Google Account: https://myaccount.google.com/
2. Click **Security** (left sidebar)
3. Enable **2-Step Verification** (if not already enabled)
4. Search for "App passwords" or go to: https://myaccount.google.com/apppasswords
5. Generate a new app password for "Mail"
6. Copy the 16-character password (no spaces)
7. Use THIS password in your `.env` file as `SENDER_PASSWORD`

### 4. Run the Script
```bash
python send_email.py
```

## For Other Email Providers

If not using Gmail, change the SMTP settings in `send_email.py`:

- **Outlook/Hotmail:** `smtp.office365.com`, port `587`
- **Yahoo:** `smtp.mail.yahoo.com`, port `587`
- **Custom:** Check your email provider's SMTP settings

## Security Notes

- ✓ Never commit `.env` to GitHub (it's in `.gitignore`)
- ✓ Use app passwords, not your main email password
- ✓ Keep your `.env` file private

## Deploying to Railway

When deploying, don't upload `.env`. Instead:
1. Go to Railway dashboard
2. Select your project
3. Go to **Variables** tab
4. Add each variable manually:
   - `SENDER_EMAIL`
   - `SENDER_PASSWORD`
   - `RECIPIENT_EMAIL`
