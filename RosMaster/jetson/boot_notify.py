#!/usr/bin/env python3
"""
Send SMS notification on boot via Verizon email-to-SMS gateway.
Deployed to Jetson and triggered by systemd on startup.
"""

import smtplib
import socket
import subprocess
import time
from datetime import datetime
from email.mime.text import MIMEText

# --- Configuration ---
SMS_GATEWAY = "6616180571@vtext.com"
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
# Store credentials in /home/jetson/.rosmaster_smtp
CRED_FILE = "/home/jetson/.rosmaster_smtp"


def get_ip():
    """Get the local IP address."""
    for _ in range(30):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            if ip and not ip.startswith("127."):
                return ip
        except Exception:
            pass
        time.sleep(2)
    return "unknown"


def get_hostname():
    return socket.gethostname()


def load_credentials():
    """Load SMTP credentials from file. Format: email\\napp_password"""
    try:
        with open(CRED_FILE, "r") as f:
            lines = f.read().strip().split("\n")
            return lines[0].strip(), lines[1].strip()
    except (FileNotFoundError, IndexError):
        print(f"ERROR: Credentials file not found: {CRED_FILE}")
        print("Create it with:")
        print(f"  echo 'your_email@gmail.com' > {CRED_FILE}")
        print(f"  echo 'your_app_password' >> {CRED_FILE}")
        print(f"  chmod 600 {CRED_FILE}")
        return None, None


def send_sms(message):
    email, password = load_credentials()
    if not email:
        return False

    msg = MIMEText(message)
    msg["From"] = email
    msg["To"] = SMS_GATEWAY
    msg["Subject"] = ""  # Keep empty for cleaner SMS

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
            server.starttls()
            server.login(email, password)
            server.sendmail(email, SMS_GATEWAY, msg.as_string())
        print(f"SMS sent to {SMS_GATEWAY}")
        return True
    except Exception as e:
        print(f"Failed to send SMS: {e}")
        return False


def main():
    # Wait for network
    ip = get_ip()
    hostname = get_hostname()
    now = datetime.now().strftime("%m/%d %I:%M%p")

    message = f"RosMaster {hostname} booted\n{now}\nIP: {ip}"
    print(f"Sending: {message}")
    send_sms(message)


if __name__ == "__main__":
    main()
