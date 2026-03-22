#!/usr/bin/env python3
"""
AJ Robotics - Christy Watchdog
Runs on Christy (always-on) to monitor all machines for reboots and send SMS alerts.
Independent of Dreamer's Flask server.

Usage:
    python3 christy_watchdog.py
    # or via systemd / crontab @reboot
"""

import os
import re
import smtplib
import subprocess
import time
import logging
from email.mime.text import MIMEText
from datetime import datetime

# ── Configuration ──

GMAIL_USER = "Dreamittogether@gmail.com"
GMAIL_APP_PASSWORD = "ybxgmceixhqbscas"
SMS_PHONE = "6616180571"
SMS_GATEWAY = f"{SMS_PHONE}@vtext.com"

CHECK_INTERVAL = 45      # seconds between checks
SSH_TIMEOUT = 8           # seconds for SSH commands
STARTUP_DELAY = 30        # seconds to wait before first check (let network settle)

# Machines to monitor: {name: (host, username)}
# Christy monitors everything except itself
MACHINES = {
    "Dreamer":  ("192.168.1.44", "Dream"),
    "CashCow":  ("192.168.1.91", "dongchul"),
    "R1":       ("192.168.1.82", "dream"),
}

LOG_DIR = os.path.expanduser("~/logs/watchdog")
os.makedirs(LOG_DIR, exist_ok=True)

# ── Logging ──

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "watchdog.log")),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("watchdog")

# ── State ──

last_uptime = {}  # {machine_name: uptime_seconds}


def send_sms(message):
    """Send SMS via Gmail SMTP -> Verizon email-to-SMS gateway."""
    try:
        msg = MIMEText(message)
        msg["From"] = GMAIL_USER
        msg["To"] = SMS_GATEWAY
        msg["Subject"] = ""

        with smtplib.SMTP("smtp.gmail.com", 587, timeout=10) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_USER, [SMS_GATEWAY], msg.as_string())

        log.info(f"SMS sent: {message}")
        return True
    except Exception as e:
        log.error(f"SMS failed: {e}")
        return False


def ssh_run(user, host, command, timeout=SSH_TIMEOUT):
    """Run a command on a remote machine via SSH."""
    cmd = [
        "ssh",
        "-o", "ConnectTimeout=3",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        f"{user}@{host}",
        command,
    ]
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        return proc.stdout.decode("utf-8", errors="replace").strip(), proc.returncode
    except subprocess.TimeoutExpired:
        return "", -1
    except Exception as e:
        log.error(f"SSH error ({user}@{host}): {e}")
        return "", -1


def get_uptime(host, user):
    """Get machine uptime in seconds."""
    out, rc = ssh_run(user, host, "cat /proc/uptime 2>/dev/null | awk '{print $1}'")
    if rc == 0 and out:
        try:
            return float(out)
        except ValueError:
            pass
    # For Windows machines, try PowerShell
    out, rc = ssh_run(
        user, host,
        'powershell -NoProfile -Command "(Get-Date) - (Get-CimInstance Win32_OperatingSystem).LastBootUpTime | Select-Object -ExpandProperty TotalSeconds"',
        timeout=10,
    )
    if rc == 0 and out:
        try:
            return float(out.strip())
        except ValueError:
            pass
    return None


def get_reboot_info(host, user):
    """Get detailed reboot info (boot time, local IP, external IP)."""
    # Try Linux first
    cmd = (
        "echo 'MARK_BOOT'; "
        "uptime -s 2>/dev/null || who -b 2>/dev/null | awk '{print $3, $4}' || echo 'N/A'; "
        "echo 'MARK_IP'; "
        "hostname -I 2>/dev/null | awk '{print $1}' || echo 'N/A'; "
        "echo 'MARK_EXT'; "
        "curl -s --max-time 3 https://api.ipify.org 2>/dev/null || "
        "curl -s --max-time 3 https://ifconfig.me 2>/dev/null || echo 'N/A'; "
        "echo 'MARK_END'"
    )
    out, rc = ssh_run(user, host, cmd, timeout=15)

    if not out or "MARK_BOOT" not in out:
        # Try Windows PowerShell
        cmd_win = (
            'powershell -NoProfile -Command "'
            "$boot = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToString('yyyy-MM-dd HH:mm:ss'); "
            "$ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.InterfaceAlias -notlike '*Loopback*' } | Select-Object -First 1).IPAddress; "
            "Write-Output \\\"MARK_BOOT\\\"; Write-Output $boot; "
            "Write-Output \\\"MARK_IP\\\"; Write-Output $ip; "
            "Write-Output \\\"MARK_EXT\\\"; "
            "try { Write-Output (Invoke-WebRequest -Uri 'https://api.ipify.org' -TimeoutSec 3).Content } catch { Write-Output 'N/A' }; "
            'Write-Output \\"MARK_END\\""'
        )
        out, rc = ssh_run(user, host, cmd_win, timeout=15)

    sections = re.split(r'MARK_(?:BOOT|IP|EXT|END)', out)
    sec = [s.strip() for s in sections]

    boot_time = sec[1] if len(sec) > 1 and sec[1] and sec[1] != "N/A" else "Unknown"
    local_ip = sec[2] if len(sec) > 2 and sec[2] and sec[2] != "N/A" else host
    ext_ip = sec[3] if len(sec) > 3 and sec[3] and sec[3] != "N/A" else "N/A"

    return boot_time, local_ip, ext_ip


def get_reboot_trigger(host, user):
    """Try to determine who/what triggered the reboot."""
    # Check shutdown log for clues
    cmd = (
        "last reboot 2>/dev/null | head -1; "
        "echo 'MARK_WHO'; "
        "grep -i 'shutdown\\|reboot' /var/log/auth.log 2>/dev/null | tail -3 || "
        "grep -i 'shutdown\\|reboot' /var/log/syslog 2>/dev/null | tail -3 || "
        "echo 'N/A'"
    )
    out, rc = ssh_run(user, host, cmd, timeout=8)
    if not out:
        return "Unknown"

    # Check for common patterns
    lower = out.lower()
    if "ajrobotics" in lower:
        return "Christy (ajrobotics)"
    elif "dongchul" in lower:
        return "CashCow user (dongchul)"
    elif "dream" in lower:
        return "Dreamer/R1 user (dream)"
    elif "systemd" in lower or "system" in lower:
        return "System (auto/scheduled)"
    elif "power" in lower:
        return "Power event"
    else:
        return "Unknown"


def check_machine(name, host, user):
    """Check a single machine for reboot."""
    uptime_sec = get_uptime(host, user)

    if uptime_sec is None:
        log.warning(f"{name} unreachable")
        return

    prev = last_uptime.get(name)
    last_uptime[name] = uptime_sec

    # First check — just record baseline
    if prev is None:
        log.info(f"{name} baseline uptime: {uptime_sec:.0f}s")
        return

    # Reboot detected: new uptime < previous
    if uptime_sec < prev:
        log.info(f"REBOOT DETECTED: {name} (was {prev:.0f}s, now {uptime_sec:.0f}s)")
        boot_time, local_ip, ext_ip = get_reboot_info(host, user)
        triggered_by = get_reboot_trigger(host, user)
        msg = (
            f"{name} REBOOTED\n"
            f"{boot_time}\n"
            f"L:{local_ip} W:{ext_ip}\n"
            f"By:{triggered_by}\n"
            f"[Christy]"
        )
        log.info(f"Sending SMS: {msg}")
        send_sms(msg)


def main():
    log.info("=" * 50)
    log.info("Christy Watchdog started")
    log.info(f"Monitoring: {', '.join(MACHINES.keys())}")
    log.info(f"Check interval: {CHECK_INTERVAL}s")
    log.info(f"SMS target: {SMS_PHONE}")
    log.info("=" * 50)

    # Wait for network to settle after boot
    log.info(f"Waiting {STARTUP_DELAY}s for network to settle...")
    time.sleep(STARTUP_DELAY)

    send_sms(f"Christy Watchdog started. Monitoring: {', '.join(MACHINES.keys())}")

    while True:
        for name, (host, user) in MACHINES.items():
            try:
                check_machine(name, host, user)
            except Exception as e:
                log.error(f"Error checking {name}: {e}")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
