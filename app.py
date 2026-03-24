"""
AJ Robotics - Main Control Hub
Web-based GUI for robotics control, smart trading, and remote deployment.
"""

import json
import os
import sys

# Fix HOME for SSH key discovery on Windows (must be set before any SSH calls)
if os.name == "nt" and ("HOME" not in os.environ or "My Drive" in os.environ.get("HOME", "")):
    os.environ["HOME"] = os.environ.get("USERPROFILE", os.path.expanduser("~"))

import re
import smtplib
import socket
import subprocess
import threading
import time as _time
from email.mime.text import MIMEText

import paramiko

import requests as http_requests
from flask import Flask, render_template, jsonify, request, Response
from shared.monitor import (
    get_local_resources,
    check_all_machines,
    get_machine_status,
    get_recent_logs,
    log_event,
    start_background_monitor,
)
from shared.heartbeat_responder import get_responder

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "configs", "hosts.json")


def load_hosts():
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def _detect_local_machine():
    """Detect which machine in hosts.json we are running on, by matching hostname."""
    hostname = socket.gethostname()
    hosts = load_hosts()
    for name, info in hosts.get("computers", {}).items():
        if info.get("hostname", "").lower() == hostname.lower():
            return name
    # Fallback: match if hostname starts with machine name
    for name, info in hosts.get("computers", {}).items():
        if hostname.lower().startswith(name.lower()):
            return name
    return "Unknown"


LOCAL_MACHINE = _detect_local_machine()


def find_machine(name):
    """Find a machine by name across all categories in hosts.json."""
    hosts = load_hosts()
    for category in ["computers", "raspberry_pis", "robots"]:
        if name in hosts.get(category, {}):
            return hosts[category][name]
    return None

app = Flask(__name__, template_folder="gui/templates", static_folder="gui/static")
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0


# --- Pages ---

@app.route("/")
def index():
    # CashCow: show machine detail as home page (trading-focused)
    if LOCAL_MACHINE == "CashCow":
        info = find_machine("CashCow")
        return render_template(
            "machine_detail.html",
            machine_name="CashCow",
            machine_info=info or {},
            local_machine=LOCAL_MACHINE,
        )
    return render_template("index.html", local_machine=LOCAL_MACHINE)


@app.route("/machines")
def machines_panel():
    return render_template("machines.html")


@app.route("/robotics")
def robotics_panel():
    return render_template("robotics.html")


@app.route("/robotics/gamepad")
def gamepad_test():
    return render_template("gamepad_test.html")


@app.route("/robotics/gamepad/<machine>")
def gamepad_via_machine(machine):
    """Gamepad page that sends data via a specific machine's XBee."""
    info = find_machine(machine)
    if not info:
        return f"Machine '{machine}' not found", 404
    host = info.get("host", "")
    port = info.get("agent_port", 5000)
    if machine == "Gram":
        return render_template(
            "gamepad_gram.html",
            proxy_machine=machine,
            proxy_host=host,
            proxy_port=port,
        )
    return render_template(
        "gamepad_test.html",
        proxy_machine=machine,
        proxy_host=host,
        proxy_port=port,
    )


@app.route("/trader")
def trader_panel():
    return render_template("trader.html")


@app.route("/today")
def today_panel():
    return render_template("today.html")


@app.route("/politician")
def politician_panel():
    return render_template("politician.html")


# --- Today's Trades API (local log parsing + IB intraday) ---

_TRADER_LOG_DIR = os.path.expanduser("~/ib_smart_trader/logs")


def _parse_today_trades(trader_type: str, date_str: str) -> list:
    """Parse a trader log file and extract today's executed trades with strategy context."""
    if trader_type == "day":
        log_name = "day_trader_stdout.log"
    else:
        log_name = "trader_stdout.log"

    log_path = os.path.join(_TRADER_LOG_DIR, log_name)
    if not os.path.isfile(log_path) or os.path.getsize(log_path) == 0:
        return []

    try:
        with open(log_path, "r", errors="replace") as f:
            all_lines = f.readlines()
    except Exception:
        return []

    trades = []

    # Patterns for executed orders
    if trader_type == "day":
        order_re = re.compile(
            r"✅\s+(BUY|SELL)\s+주문\s+전송!\s+(\w+)\s+x(\d+)\s*\|\s*주문ID:\s*(\d+)"
        )
        alert_re = re.compile(
            r"🔔\s+\[ALERT\]\s+(BUY|SELL)\s+(\w+)\s+x(\d+)\s+@\s+\$([0-9.]+)"
        )
    else:
        order_re = re.compile(
            r"✅\s+주문\s+전송!\s+(BUY|SELL)\s+(\w+)\s+x(\d+)\s*\|\s*주문\s*ID:\s*(\d+)"
        )
        alert_re = None

    ensemble_re = re.compile(
        r"🎯\s+(\w+).*합의:\s+([+-]?[0-9.]+)\s*\|\s*BUY:(\d+)\s+SELL:(\d+)"
    )
    signal_re = re.compile(
        r"([\w_]+)\s+→\s+(BUY|SELL|HOLD)\s+\((\d+)%\)\s+(.*)"
    )
    # Timestamp: either "YYYY-MM-DD HH:MM:SS" or just "HH:MM:SS" at line start
    ts_full_re = re.compile(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})")
    ts_time_re = re.compile(r"^(\d{2}:\d{2}:\d{2})")

    for i, line in enumerate(all_lines):
        m_order = order_re.search(line)
        m_alert = alert_re.search(line) if alert_re else None

        if not m_order and not m_alert:
            continue

        if m_order:
            side, symbol, quantity, order_id = (
                m_order.group(1), m_order.group(2),
                int(m_order.group(3)), m_order.group(4),
            )
            mode = "AUTO"
        else:
            side, symbol, quantity = (
                m_alert.group(1), m_alert.group(2), int(m_alert.group(3)),
            )
            order_id, mode = None, "ALERT"

        # Extract timestamp
        trade_time = None
        for check_line in [line] + all_lines[max(0, i - 5):i]:
            # Try full datetime first
            tm = ts_full_re.search(check_line)
            if tm:
                if tm.group(1) == date_str:
                    trade_time = tm.group(2)
                break
            # Try time-only (HH:MM:SS at line start)
            tm2 = ts_time_re.search(check_line)
            if tm2:
                trade_time = tm2.group(1)
                break

        if trade_time is None:
            trade_time = "unknown"

        # Look backwards for strategy signals.
        # Find the ensemble line for this symbol, then collect signals below it.
        signals = []
        consensus_score = None
        ensemble_line_idx = None
        for j in range(i - 1, max(0, i - 20) - 1, -1):
            back_line = all_lines[j]
            em = ensemble_re.search(back_line)
            if em and em.group(1) == symbol:
                consensus_score = float(em.group(2))
                ensemble_line_idx = j
                break
            # Stop if we hit another ensemble line (different symbol)
            if em and em.group(1) != symbol:
                break

        if ensemble_line_idx is not None:
            # Collect strategy signals between ensemble line and order line
            for j in range(ensemble_line_idx + 1, i):
                sm = signal_re.search(all_lines[j])
                if sm:
                    signals.append({
                        "strategy": sm.group(1),
                        "signal": sm.group(2),
                        "confidence": int(sm.group(3)),
                        "reason": sm.group(4).strip(),
                    })

        trades.append({
            "time": trade_time,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "order_id": order_id,
            "mode": mode,
            "consensus_score": consensus_score,
            "signals": signals,
        })

    return trades


@app.route("/api/trader/today")
def api_trader_today():
    """Parse today's trades from both Day Trader and Smart Trader logs."""
    from datetime import datetime as _dtx
    today_str = _dtx.now().strftime("%Y-%m-%d")
    day_trades = _parse_today_trades("day", today_str)
    smart_trades = _parse_today_trades("smart", today_str)
    return jsonify({
        "day_trades": day_trades,
        "smart_trades": smart_trades,
        "date": today_str,
    })


@app.route("/api/trader/intraday/<symbol>")
def api_trader_intraday(symbol):
    """Fetch today's 5-min bars for a symbol from IB Gateway."""
    port = request.args.get("port", 7497, type=int)
    try:
        import asyncio
        try:
            asyncio.get_event_loop()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())

        from ib_insync import IB, Stock
        ib = IB()
        ib.connect("127.0.0.1", port, clientId=98, timeout=5)

        contract = Stock(symbol, "SMART", "USD")
        ib.qualifyContracts(contract)

        bars = ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr="1 D",
            barSizeSetting="5 mins",
            whatToShow="TRADES",
            useRTH=False,
            formatDate=1,
        )
        ib.disconnect()

        result = []
        for bar in bars:
            result.append({
                "time": bar.date.strftime("%Y-%m-%d %H:%M:%S")
                        if hasattr(bar.date, "strftime")
                        else str(bar.date),
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": int(bar.volume),
            })
        return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e), "bars": []}), 200


@app.route("/deploy")
def deploy_panel():
    return render_template("deploy.html")


@app.route("/vision")
def vision_panel():
    return render_template("vision.html")


@app.route("/training")
def training_panel():
    return render_template("training.html")


# --- API Endpoints ---

@app.route("/api/gamepads/config")
def api_gamepads_config():
    """Serve gamepads.json config for frontend gamepad pages."""
    config_path = os.path.join(os.path.dirname(__file__), "configs", "gamepads.json")
    try:
        with open(config_path, "r") as f:
            return jsonify(json.load(f))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/status")
def api_status():
    machines = get_machine_status()
    online = sum(1 for m in machines.values() if m.get("online"))
    total = len(machines)
    return jsonify({
        "status": "System Online",
        "machines_online": online,
        "machines_total": total,
    })


@app.route("/api/system/resources")
def api_system_resources():
    return jsonify(get_local_resources())


@app.route("/api/machines/status")
def api_machines_status():
    return jsonify(check_all_machines())


@app.route("/api/logs/recent")
def api_logs():
    limit = request.args.get("limit", 50, type=int)
    return jsonify(get_recent_logs(limit))


@app.route("/api/actions/ping-all", methods=["POST"])
def api_ping_all():
    results = check_all_machines()
    online = sum(1 for r in results.values() if r.get("online"))
    log_event(f"Manual ping sweep: {online}/{len(results)} machines online")
    return jsonify(results)


@app.route("/api/actions/ssh/<machine>", methods=["POST"])
def api_ssh(machine):
    """Open an SSH session in Windows Terminal for the given machine."""
    info = find_machine(machine)
    if not info:
        return jsonify({"error": f"Machine '{machine}' not found"}), 404

    host = info.get("host")
    user = info.get("username")
    if not host or not user or host in ("localhost", "TBD"):
        return jsonify({"error": f"Cannot SSH to '{machine}' (host={host})"}), 400

    ssh_cmd = f"ssh {user}@{host}"
    try:
        # Open in Windows Terminal with a named tab
        subprocess.Popen(
            ["wt", "--title", f"SSH {machine}", "cmd", "/k", ssh_cmd],
            shell=True,
        )
        log_event(f"SSH session opened: {machine} ({user}@{host})")
        return jsonify({"ok": True, "command": ssh_cmd})
    except Exception as e:
        log_event(f"SSH launch failed for {machine}: {e}", level="error")
        return jsonify({"error": str(e)}), 500


@app.route("/api/actions/vnc/<machine>", methods=["POST"])
def api_vnc(machine):
    """Open a VNC session for the given machine."""
    info = find_machine(machine)
    if not info:
        return jsonify({"error": f"Machine '{machine}' not found"}), 404

    host = info.get("host")
    if not host or host in ("localhost", "TBD"):
        return jsonify({"error": f"Cannot VNC to '{machine}' (host={host})"}), 400

    vnc_addr = f"{host}:5900"
    try:
        # Try common VNC viewers on Windows
        vnc_viewers = [
            r"C:\Program Files\RealVNC\VNC Viewer\vncviewer.exe",
            r"C:\Program Files\TightVNC\tvnviewer.exe",
            r"C:\Program Files (x86)\TightVNC\tvnviewer.exe",
        ]
        viewer = None
        for path in vnc_viewers:
            if os.path.exists(path):
                viewer = path
                break

        if viewer:
            subprocess.Popen([viewer, vnc_addr])
        else:
            # Fallback: open with default handler
            subprocess.Popen(["cmd", "/c", "start", f"vnc://{host}"], shell=True)

        log_event(f"VNC session opened: {machine} ({host})")
        return jsonify({"ok": True, "address": vnc_addr})
    except Exception as e:
        log_event(f"VNC launch failed for {machine}: {e}", level="error")
        return jsonify({"error": str(e)}), 500


# --- Machine Detail Pages ---

@app.route("/machine/<name>")
def machine_detail(name):
    info = find_machine(name)
    if not info:
        return f"Machine '{name}' not found", 404
    return render_template(
        "machine_detail.html",
        machine_name=name,
        machine_info=info,
        local_machine=LOCAL_MACHINE,
    )


def _get_local_machine_info(name, info):
    """Get system info for the local machine using psutil + platform."""
    import platform
    import psutil
    from datetime import datetime, timedelta

    try:
        boot_time = datetime.fromtimestamp(psutil.boot_time())
        uptime_delta = datetime.now() - boot_time
        hours, remainder = divmod(int(uptime_delta.total_seconds()), 3600)
        minutes, _ = divmod(remainder, 60)
        uptime_str = f"up {hours}h {minutes}m (since {boot_time.strftime('%Y-%m-%d %H:%M')})"

        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        disk = psutil.disk_usage("C:\\" if os.name == "nt" else "/")
        cpu_pct = psutil.cpu_percent(interval=0.3)
        cpu_count = psutil.cpu_count()

        # Top processes by CPU
        processes = []
        for p in sorted(psutil.process_iter(['pid', 'name', 'username', 'cpu_percent', 'memory_percent']),
                        key=lambda x: x.info.get('cpu_percent') or 0, reverse=True)[:14]:
            pi = p.info
            processes.append({
                "user": (pi.get("username") or "SYSTEM").split("\\")[-1],
                "pid": str(pi.get("pid", "")),
                "cpu": str(round(pi.get("cpu_percent") or 0, 1)),
                "mem": str(round(pi.get("memory_percent") or 0, 1)),
                "command": pi.get("name") or "?",
            })

        # Network interfaces
        net_lines = []
        for iface, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if addr.family.name == "AF_INET":
                    net_lines.append(f"{iface:<20} {addr.address}")
        network = "\n".join(net_lines) if net_lines else "N/A"

        # CPU info
        cpu_info = f"CPU: {cpu_pct}% ({cpu_count} cores) | {platform.processor()}"

        def fmt_gb(b):
            return f"{b / (1024**3):.1f}Gi"

        result = {
            "name": name,
            "host": "localhost",
            "user": info.get("username", ""),
            "online": True,
            "uptime": uptime_str,
            "cpu_info": cpu_info,
            "memory": {
                "total": fmt_gb(mem.total),
                "used": fmt_gb(mem.used),
                "free": fmt_gb(mem.available),
                "available": fmt_gb(mem.available),
                "swap_total": fmt_gb(swap.total),
                "swap_used": fmt_gb(swap.used),
            },
            "disk": {
                "size": fmt_gb(disk.total),
                "used": fmt_gb(disk.used),
                "avail": fmt_gb(disk.free),
                "use_pct": f"{disk.percent}%",
            },
            "processes": processes,
            "network": network,
            "services": f"{platform.system()} {platform.version()} | Python {platform.python_version()}",
            "last_logins": "Local machine (no SSH logins)",
            "temperature_c": None,
        }
        log_event(f"Queried local system info: {name}")
        return jsonify(result)

    except Exception as e:
        log_event(f"Error getting local info: {e}", level="error")
        return jsonify({"name": name, "host": "localhost", "online": True, "error": str(e)})


def _get_remote_windows_info(name, info):
    """Get system info from a remote Windows machine via SSH + PowerShell."""
    host = info.get("host")
    user = info.get("username")
    result = {"name": name, "host": host, "user": user}

    try:
        # PowerShell one-liner to gather system info as JSON
        ps_cmd = (
            "powershell -NoProfile -Command \""
            "$os = Get-CimInstance Win32_OperatingSystem; "
            "$cpu = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average; "
            "$cpuName = (Get-CimInstance Win32_Processor).Name; "
            "$cpuCount = (Get-CimInstance Win32_Processor).NumberOfLogicalProcessors; "
            "$disk = Get-CimInstance Win32_LogicalDisk -Filter \\\"DeviceID='C:'\\\"; "
            "$boot = $os.LastBootUpTime; "
            "$uptime = (Get-Date) - $boot; "
            "$procs = Get-Process | Sort-Object CPU -Descending | Select-Object -First 10 "
            "  | ForEach-Object { $_.Name + '|' + $_.Id + '|' + [math]::Round($_.CPU,1) + '|' "
            "  + [math]::Round($_.WorkingSet64/1MB,0) }; "
            "@{"
            "  uptime = '{0}d {1}h {2}m' -f $uptime.Days, $uptime.Hours, $uptime.Minutes; "
            "  boot = $boot.ToString('yyyy-MM-dd HH:mm'); "
            "  cpu_pct = $cpu; "
            "  cpu_name = $cpuName; "
            "  cpu_count = $cpuCount; "
            "  mem_total = [math]::Round($os.TotalVisibleMemorySize/1MB,1); "
            "  mem_free = [math]::Round($os.FreePhysicalMemory/1MB,1); "
            "  mem_used = [math]::Round(($os.TotalVisibleMemorySize - $os.FreePhysicalMemory)/1MB,1); "
            "  disk_total = [math]::Round($disk.Size/1GB,1); "
            "  disk_free = [math]::Round($disk.FreeSpace/1GB,1); "
            "  disk_used = [math]::Round(($disk.Size - $disk.FreeSpace)/1GB,1); "
            "  procs = $procs -join ';;' "
            "} | ConvertTo-Json\""
        )
        output, stderr, rc = _ssh_run(user, host, ps_cmd, timeout=15)
        output = output.strip()

        if rc != 0 or not output:
            result["online"] = False
            result["error"] = stderr.strip() or "SSH command failed"
            return jsonify(result)

        import json as _json
        data = _json.loads(output)

        mem_total = data.get("mem_total", 0)
        mem_used = data.get("mem_used", 0)
        disk_total = data.get("disk_total", 0)
        disk_used = data.get("disk_used", 0)
        disk_free = data.get("disk_free", 0)

        # Parse processes
        processes = []
        procs_raw = data.get("procs", "")
        if procs_raw:
            for entry in procs_raw.split(";;"):
                parts = entry.split("|")
                if len(parts) >= 4:
                    processes.append({
                        "user": user,
                        "pid": parts[1],
                        "cpu": parts[2],
                        "mem": str(round(float(parts[3]) / (mem_total * 1024) * 100, 1)) if mem_total else "0",
                        "command": parts[0],
                    })

        result.update({
            "online": True,
            "uptime": f"up {data.get('uptime', '')} (since {data.get('boot', '')})",
            "cpu_info": f"CPU: {data.get('cpu_pct', 0)}% ({data.get('cpu_count', '')} cores) | {data.get('cpu_name', '')}",
            "memory": {
                "total": f"{mem_total:.1f}Gi",
                "used": f"{mem_used:.1f}Gi",
                "free": f"{data.get('mem_free', 0):.1f}Gi",
                "available": f"{data.get('mem_free', 0):.1f}Gi",
                "swap_total": "0Gi",
                "swap_used": "0Gi",
            },
            "disk": {
                "size": f"{disk_total:.1f}Gi",
                "used": f"{disk_used:.1f}Gi",
                "avail": f"{disk_free:.1f}Gi",
                "use_pct": f"{round(disk_used / disk_total * 100) if disk_total else 0}%",
            },
            "processes": processes,
            "network": f"{host}",
            "services": f"Windows | {info.get('os', '')}",
            "last_logins": "Remote (SSH)",
            "temperature_c": None,
        })
        log_event(f"Queried remote Windows info: {name}")

    except Exception as e:
        result["online"] = False
        result["error"] = str(e)
        log_event(f"Error querying {name}: {e}", level="error")

    return jsonify(result)


@app.route("/api/machine/<name>/info")
def api_machine_info(name):
    """Get live system info from a remote machine via SSH."""
    info = find_machine(name)
    if not info:
        return jsonify({"error": f"Machine '{name}' not found"}), 404

    host = info.get("host")
    user = info.get("username")

    if host == "TBD":
        return jsonify({"error": f"Cannot query '{name}' (TBD)"}), 400

    # --- Local machine (Dreamer or Gram, whichever is running the app) ---
    if host == "localhost" or name == LOCAL_MACHINE:
        return _get_local_machine_info(name, info)

    # --- Remote Windows machine via SSH ---
    os_type = info.get("os", "")
    if "windows" in os_type.lower():
        return _get_remote_windows_info(name, info)

    result = {"name": name, "host": host, "user": user}

    try:
        # Single SSH call with all commands
        cmd = (
            "echo '===UPTIME==='; uptime; "
            "echo '===CPU==='; top -bn1 | head -3; "
            "echo '===MEMORY==='; free -h; "
            "echo '===DISK==='; df -h /; "
            "echo '===PROCESSES==='; ps aux --sort=-%cpu | head -15; "
            "echo '===NETWORK==='; ip -br addr 2>/dev/null || ifconfig 2>/dev/null; "
            "echo '===SERVICES==='; systemctl list-units --type=service --state=running --no-pager 2>/dev/null | head -20; "
            "echo '===LAST_LOGINS==='; last -5 2>/dev/null; "
            "echo '===TEMPERATURE==='; cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null || echo 'N/A'"
        )
        output, stderr, rc = _ssh_run(user, host, cmd, timeout=10)

        # Parse sections
        sections = {}
        current_section = None
        current_lines = []
        for line in output.splitlines():
            if line.startswith("===") and line.endswith("==="):
                if current_section:
                    sections[current_section] = "\n".join(current_lines)
                current_section = line.strip("=")
                current_lines = []
            else:
                current_lines.append(line)
        if current_section:
            sections[current_section] = "\n".join(current_lines)

        # Parse memory
        mem_lines = sections.get("MEMORY", "").splitlines()
        mem_info = {}
        for line in mem_lines:
            if line.startswith("Mem:"):
                parts = line.split()
                mem_info = {"total": parts[1], "used": parts[2], "free": parts[3], "available": parts[6] if len(parts) > 6 else "N/A"}
            elif line.startswith("Swap:"):
                parts = line.split()
                mem_info["swap_total"] = parts[1]
                mem_info["swap_used"] = parts[2]

        # Parse disk
        disk_lines = sections.get("DISK", "").splitlines()
        disk_info = {}
        for line in disk_lines:
            if line.startswith("/dev"):
                parts = line.split()
                disk_info = {"size": parts[1], "used": parts[2], "avail": parts[3], "use_pct": parts[4]}

        # Parse processes
        proc_lines = sections.get("PROCESSES", "").splitlines()
        processes = []
        for line in proc_lines[1:]:  # skip header
            parts = line.split(None, 10)
            if len(parts) >= 11:
                processes.append({
                    "user": parts[0], "pid": parts[1],
                    "cpu": parts[2], "mem": parts[3],
                    "command": parts[10]
                })

        # Parse temperature
        temp_raw = sections.get("TEMPERATURE", "").strip()
        temperature = None
        if temp_raw and temp_raw != "N/A":
            try:
                temperature = round(int(temp_raw.splitlines()[0]) / 1000, 1)
            except (ValueError, IndexError):
                temperature = None

        result.update({
            "online": True,
            "uptime": sections.get("UPTIME", "").strip(),
            "cpu_info": sections.get("CPU", "").strip(),
            "memory": mem_info,
            "disk": disk_info,
            "processes": processes,
            "network": sections.get("NETWORK", "").strip(),
            "services": sections.get("SERVICES", "").strip(),
            "last_logins": sections.get("LAST_LOGINS", "").strip(),
            "temperature_c": temperature,
        })
        log_event(f"Queried system info: {name}")

    except Exception as e:
        result["online"] = False
        result["error"] = str(e)
        log_event(f"Error querying {name}: {e}", level="error")

    return jsonify(result)


# --- Deploy API ---

@app.route("/api/deploy/christy", methods=["POST"])
def api_deploy_christy():
    """Manage XBee monitor on Christy (runs locally via subprocess, no SSH needed)."""
    data = request.json or {}
    action = data.get("action", "sync")
    interval = data.get("interval", 10)

    try:
        if action == "status":
            # Check xbee-monitor process locally (no SSH — Flask runs on Christy)
            proc = subprocess.run(
                ["ps", "aux"], capture_output=True, text=True, timeout=5
            )
            ps_lines = [l for l in proc.stdout.splitlines() if "xbee_monitor" in l and "grep" not in l]
            output = "\n".join(ps_lines) if ps_lines else "NOT_RUNNING"

            svc = subprocess.run(
                ["systemctl", "is-active", "xbee-monitor.service"],
                capture_output=True, text=True, timeout=5
            )
            return jsonify({
                "ok": True,
                "action": "status",
                "output": output,
                "service_status": svc.stdout.strip(),
            })

        elif action == "run":
            # Start/restart xbee-monitor service
            proc = subprocess.run(
                ["sudo", "systemctl", "restart", "xbee-monitor.service"],
                capture_output=True, text=True, timeout=15
            )
            log_event(f"XBee monitor started: rc={proc.returncode}")
            return jsonify({"ok": proc.returncode == 0, "action": "run",
                          "output": proc.stdout + proc.stderr})

        elif action == "stop":
            # Stop xbee-monitor service
            proc = subprocess.run(
                ["sudo", "systemctl", "stop", "xbee-monitor.service"],
                capture_output=True, text=True, timeout=10
            )
            log_event(f"XBee monitor stopped: rc={proc.returncode}")
            return jsonify({"ok": proc.returncode == 0, "action": "stop",
                          "output": proc.stdout + proc.stderr})

        elif action == "sync":
            # Deploy files via deploy_christy.py
            deploy_script = os.path.join(os.path.dirname(__file__), "deploy", "deploy_christy.py")
            proc = subprocess.run(
                [sys.executable, deploy_script], capture_output=True, text=True, timeout=60
            )
            log_event(f"Deploy to Christy: rc={proc.returncode}")
            return jsonify({
                "ok": proc.returncode == 0,
                "action": "sync",
                "output": proc.stdout + proc.stderr,
            })

        else:
            return jsonify({"error": f"Unknown action: {action}"}), 400

    except Exception as e:
        log_event(f"Deploy to Christy failed: {e}", level="error")
        return jsonify({"error": str(e)}), 500


@app.route("/api/deploy/christy/logs")
def api_christy_logs():
    """Fetch recent XBee logs from Christy (local file read — Flask runs on Christy)."""
    try:
        limit = request.args.get("limit", 30, type=int)
        log_path = os.path.expanduser("~/logs/xbee/xbee_events.log")
        if not os.path.exists(log_path):
            return jsonify({"logs": []})
        proc = subprocess.run(
            ["tail", f"-{limit}", log_path],
            capture_output=True, text=True, timeout=5
        )
        lines = proc.stdout.strip().splitlines() if proc.stdout.strip() else []
        return jsonify({"logs": lines})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/xbee/responders")
def api_xbee_responders():
    """Parse recent XBee heartbeat logs to find responding devices (local file read)."""
    try:
        # Read last 200 lines from local log file (Flask runs on Christy)
        log_path = os.path.expanduser("~/logs/xbee/xbee_events.log")
        if not os.path.exists(log_path):
            return jsonify({"responders": [], "log_time": ""})
        proc = subprocess.run(["tail", "-200", log_path],
                              capture_output=True, text=True, timeout=5)
        output = proc.stdout

        # Parse: find all HEARTBEAT REPLY and device reply lines
        # Format: "2026-03-20 12:31:20 | INFO  | HEARTBEAT REPLY #73 from 0013A20041BB8D5E RSSI:-45"
        # Format: "2026-03-20 12:31:20 | INFO  | Text from 0013A20041BB8D5E: R1!"
        # Legacy: "2026-03-20 12:31:20 | INFO  | Text from 0013A20041BB8D5E: Roger!"
        devices = {}  # mac -> {last_time, rssi, count, name}

        # Known MAC -> name mapping
        mac_names = {
            "0013A20041BB8D5E": "R1",
            "0013A20041741E51": "Dreamer",
        }
        # Add from robot config
        try:
            from robotics.config import ROBOTS, KNOWN_XBEE_MACS
            mac_names.update(KNOWN_XBEE_MACS)
            for rid, rinfo in ROBOTS.items():
                if rinfo.get("mac"):
                    mac_names[rinfo["mac"]] = rinfo["name"]
        except ImportError:
            pass

        for line in output.splitlines():
            # Match HEARTBEAT REPLY lines
            m = re.search(
                r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*HEARTBEAT REPLY #(\d+) from (\w+)\s*RSSI:([-\d]+)',
                line
            )
            if m:
                timestamp, count, mac, rssi = m.group(1), int(m.group(2)), m.group(3), int(m.group(4))
                if mac not in devices:
                    devices[mac] = {"mac": mac, "count": 0, "rssi": None, "last_time": ""}
                devices[mac]["last_time"] = timestamp
                devices[mac]["rssi"] = rssi
                devices[mac]["count"] = count
                devices[mac]["name"] = mac_names.get(mac, "Unknown (...)" + mac[-4:])
                continue

            # Match device reply lines (R1!, R4!, Roger!, etc.)
            m2 = re.search(
                r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*Text from (\w+): (Roger!|R\d+!)',
                line
            )
            if m2:
                timestamp, mac = m2.group(1), m2.group(2)
                if mac not in devices:
                    devices[mac] = {"mac": mac, "count": 0, "rssi": None, "last_time": ""}
                devices[mac]["last_time"] = timestamp
                devices[mac]["name"] = mac_names.get(mac, "Unknown (..." + mac[-4:] + ")")

        # Determine online/offline: if last reply was within 30 seconds
        now_str = ""
        # Get latest timestamp from logs to compare
        for d in devices.values():
            if d["last_time"] > now_str:
                now_str = d["last_time"]

        responders = []
        for mac, d in devices.items():
            # Consider online if replied in the recent batch (within ~30s of newest log entry)
            try:
                from datetime import datetime
                last_t = datetime.strptime(d["last_time"], "%Y-%m-%d %H:%M:%S")
                newest_t = datetime.strptime(now_str, "%Y-%m-%d %H:%M:%S")
                age_s = (newest_t - last_t).total_seconds()
                d["online"] = age_s < 30
            except Exception:
                d["online"] = False

            responders.append(d)

        # Sort: online first, then by name
        responders.sort(key=lambda x: (not x["online"], x.get("name", "")))

        return jsonify({"responders": responders, "log_time": now_str})

    except Exception as e:
        return jsonify({"error": str(e), "responders": []}), 200


@app.route("/api/xbee/log-file")
def api_xbee_log_file():
    """Fetch a specific XBee log file (local file read — Flask runs on Christy)."""
    # Allowed log files (prevent path traversal)
    allowed_files = {
        "xbee_events.log", "xbee_heartbeat.log", "xbee_errors.log",
        "xbee_events.jsonl", "monitor_stdout.log",
    }
    filename = request.args.get("file", "xbee_events.log")
    if filename not in allowed_files:
        return jsonify({"error": f"Invalid log file: {filename}"}), 400

    limit = request.args.get("limit", 100, type=int)
    limit = min(limit, 5000)  # cap at 5000 lines

    log_path = os.path.expanduser(f"~/logs/xbee/{filename}")

    try:
        file_info = {}
        if os.path.exists(log_path):
            stat = os.stat(log_path)
            from datetime import datetime
            file_info = {
                "size_bytes": stat.st_size,
                "size_human": _human_size(stat.st_size),
                "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            }
            proc = subprocess.run(["tail", f"-{limit}", log_path],
                                  capture_output=True, text=True, timeout=5)
            content = proc.stdout.strip()
        else:
            content = "File not found or empty"

        lines = content.splitlines()

        return jsonify({
            "file": filename,
            "lines": lines,
            "total_lines": len(lines),
            "limit": limit,
            "info": file_info,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 200


def _human_size(size_bytes):
    """Convert bytes to human-readable size."""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


@app.route("/api/machine/<name>/trader-status")
def api_trader_status(name):
    """Get Smart Trader status from a remote machine via SSH."""
    info = find_machine(name)
    if not info:
        return jsonify({"error": f"Machine '{name}' not found"}), 404

    host = info.get("host")
    user = info.get("username")
    if not host or not user or host in ("localhost", "TBD"):
        return jsonify({"error": f"Cannot query '{name}'"}), 400

    result = {"name": name, "host": host}

    try:
        # Single SSH call with all commands separated by markers
        base_dir = "/home/dongchul/ib_smart_trader/ib_smart_trader"
        log_dir = "/home/dongchul/ib_smart_trader/logs"
        # Try stdout log first (active), fall back to smart_trader.log
        log_cmd = (
            f"if [ -s {log_dir}/trader_stdout.log ]; then "
            f"tail -30 {log_dir}/trader_stdout.log; "
            f"else tail -30 {base_dir}/smart_trader.log 2>/dev/null || echo 'NO_LOGS'; fi"
        )
        cmd = (
            f"echo 'MARK_PROCESS'; "
            f"ps aux | grep 'run.py' | grep -v grep || echo 'NOT_RUNNING'; "
            f"echo 'MARK_LOG'; "
            f"{log_cmd}; "
            f"echo 'MARK_PICKS'; "
            f"cat {base_dir}/daily_picks.json 2>/dev/null || echo 'NO_PICKS'; "
            f"echo 'MARK_CONFIG'; "
            f"cat {base_dir}/config.json 2>/dev/null || echo 'NO_CONFIG'; "
            f"echo 'MARK_END'"
        )
        output, ssh_err, rc = _ssh_run(user, host, cmd, timeout=15)
        ssh_err = ssh_err.strip()

        # Parse by markers — split on marker lines
        import re
        sections_raw = re.split(r'MARK_(?:PROCESS|LOG|PICKS|CONFIG|END)', output)
        # sections_raw: ['', process, log, picks, config, '']
        sec = [s.strip() for s in sections_raw]

        proc_info = sec[1] if len(sec) > 1 else ""
        log_tail = sec[2] if len(sec) > 2 else ""
        picks_raw = sec[3] if len(sec) > 3 else ""
        config_raw = sec[4] if len(sec) > 4 else ""

        # Process status
        is_running = bool(proc_info) and "NOT_RUNNING" not in proc_info
        mode = "UNKNOWN"
        trader_type = "smart"  # "smart" or "day"
        pid = "--"
        if is_running:
            mode = "AUTO" if "--auto" in proc_info else "ALERT"
            if "--day" in proc_info:
                trader_type = "day"
            for line in proc_info.splitlines():
                parts = line.split()
                if len(parts) > 1 and parts[1].isdigit():
                    pid = parts[1]
                    break

        # Log lines
        log_lines = []
        if log_tail and log_tail != "NO_LOGS":
            log_lines = log_tail.splitlines()

        # Daily picks
        picks = None
        if picks_raw and picks_raw != "NO_PICKS":
            try:
                picks = json.loads(picks_raw)
            except (json.JSONDecodeError, ValueError):
                picks = None

        # Config
        trader_config = None
        if config_raw and config_raw != "NO_CONFIG":
            try:
                trader_config = json.loads(config_raw)
            except (json.JSONDecodeError, ValueError):
                trader_config = None

        # Market status (US Eastern Time)
        from datetime import datetime as _dt
        import pytz
        et = pytz.timezone("US/Eastern")
        now_et = _dt.now(et)
        is_weekday = now_et.weekday() < 5
        market_hour = now_et.hour + now_et.minute / 60.0
        is_market_open = is_weekday and 9.5 <= market_hour < 16.0  # 9:30 AM - 4:00 PM ET
        is_premarket = is_weekday and 4.0 <= market_hour < 9.5
        is_afterhours = is_weekday and 16.0 <= market_hour < 20.0

        if is_market_open:
            market_status = "open"
        elif is_premarket:
            market_status = "pre-market"
        elif is_afterhours:
            market_status = "after-hours"
        elif not is_weekday:
            market_status = "weekend"
        else:
            market_status = "closed"

        # Combined status label
        if is_running and is_market_open:
            status_label = "Trading"
        elif is_running and not is_market_open:
            status_label = f"Running ({market_status.title()})"
        elif not is_running and is_market_open:
            status_label = "Stopped (Market Open!)"
        else:
            status_label = f"Idle ({market_status.title()})"

        result.update({
            "running": is_running,
            "mode": mode,
            "trader_type": trader_type,
            "pid": pid,
            "log_lines": log_lines,
            "daily_picks": picks,
            "config": trader_config,
            "process_info": proc_info if is_running else None,
            "market_status": market_status,
            "market_open": is_market_open,
            "status_label": status_label,
            "market_time": now_et.strftime("%I:%M %p ET"),
        })

    except Exception as e:
        result["running"] = False
        result["error"] = str(e)

    return jsonify(result)


@app.route("/api/deploy/cashcow", methods=["POST"])
def api_deploy_cashcow():
    """Deploy Smart Trader code to CashCow."""
    data = request.json or {}
    action = data.get("action", "sync")

    deploy_script = os.path.join(os.path.dirname(__file__), "deploy", "deploy_cashcow.py")
    cmd = [sys.executable, deploy_script]

    if action == "run":
        cmd.append("--run")
        if data.get("auto"):
            cmd.append("--auto")
        if data.get("day"):
            cmd.append("--day")
        port = data.get("port", 7497)
        cmd.extend(["--port", str(port)])
    elif action == "stop":
        cmd.append("--stop")
    elif action == "status":
        cmd.append("--status")
    elif action == "logs":
        cmd.append("--logs")
    elif action == "sync":
        pass

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        log_event(f"Deploy to CashCow: action={action}, rc={proc.returncode}")
        return jsonify({
            "ok": proc.returncode == 0,
            "action": action,
            "output": proc.stdout + proc.stderr,
        })
    except subprocess.TimeoutExpired:
        log_event("Deploy to CashCow: timeout", level="error")
        return jsonify({"error": "Deploy timed out"}), 500
    except Exception as e:
        log_event(f"Deploy to CashCow failed: {e}", level="error")
        return jsonify({"error": str(e)}), 500


@app.route("/api/deploy/cashcow/logs")
def api_cashcow_logs():
    """Fetch recent Smart Trader logs from CashCow."""
    info = find_machine("CashCow")
    if not info:
        return jsonify({"error": "CashCow not found"}), 404

    host = info.get("host")
    user = info.get("username")
    try:
        limit = request.args.get("limit", 30, type=int)
        trader_type = request.args.get("type", "smart")
        if trader_type == "day":
            log_file = "day_trader_stdout.log"
        else:
            log_file = "trader_stdout.log"
        log_cmd = (
            f"if [ -s /home/dongchul/ib_smart_trader/logs/{log_file} ]; then "
            f"tail -{limit} /home/dongchul/ib_smart_trader/logs/{log_file}; "
            f"else tail -{limit} /home/dongchul/ib_smart_trader/ib_smart_trader/smart_trader.log 2>/dev/null || echo 'No logs'; fi"
        )
        output, stderr, rc = _ssh_run(user, host, log_cmd, timeout=10)
        return jsonify({"logs": output.strip().splitlines()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/cashcow/portfolio")
def api_cashcow_portfolio():
    """Proxy to CashCow agent's portfolio API (avoids CORS)."""
    try:
        r = http_requests.get("http://192.168.1.91:5000/api/trader/portfolio", timeout=10)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"connected": False, "error": str(e)}), 200


# --- IB Gateway Monitor ---

_ib_monitor = {
    "running": False,
    "gateway_connected": None,     # True/False/None
    "gateway_pid": None,
    "port_open": False,
    "last_check": None,
    "last_error": None,
    "sms_sent_today": False,       # prevent multiple SMS per day
    "sms_last_sent": None,
    "check_count": 0,
}
_ib_monitor_lock = threading.Lock()

SMS_PHONE = "6616180571"
SMS_GATEWAY = f"{SMS_PHONE}@vtext.com"

# Gmail SMTP settings — use App Password (not regular password)
# To create: Google Account → Security → 2-Step Verification → App passwords
GMAIL_USER = os.environ.get("GMAIL_USER", "")        # e.g. yourname@gmail.com
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")  # 16-char app password

# --- Reboot Detection ---
# Track last known uptime (seconds) per machine to detect reboots
_last_uptime = {}       # {machine_name: uptime_seconds}
_reboot_triggers = {}   # {machine_name: (triggered_by, timestamp)}
_reboot_lock = threading.Lock()
_reboot_monitor_started = False


def _check_ib_gateway():
    """Check if IB Gateway/TWS is running on CashCow via SSH (paramiko)."""
    info = find_machine("CashCow")
    if not info:
        return {"connected": False, "error": "CashCow not found"}

    host = info.get("host")
    user = info.get("username")

    try:
        cmd = (
            "echo 'MARK_GW'; "
            "ps aux | grep -E '(ibgateway|IBGateway|java.*jts|java.*ibgateway)' | grep -v grep || echo 'GW_NOT_RUNNING'; "
            "echo 'MARK_PORT'; "
            "ss -tlnp 2>/dev/null | grep ':7497' || netstat -tlnp 2>/dev/null | grep ':7497' || echo 'PORT_CLOSED'; "
            "echo 'MARK_TWS'; "
            "ps aux | grep -E '(Trader Workstation|tws)' | grep -v grep || echo 'TWS_NOT_RUNNING'; "
            "echo 'MARK_END'"
        )
        output, stderr, rc = _ssh_run(user, host, cmd, timeout=10)
        sections = re.split(r'MARK_(?:GW|PORT|TWS|END)', output)
        sec = [s.strip() for s in sections]

        gw_info = sec[1] if len(sec) > 1 else ""
        port_info = sec[2] if len(sec) > 2 else ""
        tws_info = sec[3] if len(sec) > 3 else ""

        gw_running = bool(gw_info) and "GW_NOT_RUNNING" not in gw_info
        tws_running = bool(tws_info) and "TWS_NOT_RUNNING" not in tws_info
        port_open = bool(port_info) and "PORT_CLOSED" not in port_info

        connected = gw_running or tws_running or port_open

        pid = None
        proc_line = gw_info if gw_running else tws_info if tws_running else ""
        if proc_line:
            for line in proc_line.splitlines():
                parts = line.split()
                if len(parts) > 1 and parts[1].isdigit():
                    pid = parts[1]
                    break

        return {
            "connected": connected,
            "gateway_running": gw_running,
            "tws_running": tws_running,
            "port_open": port_open,
            "pid": pid,
            "error": None,
        }
    except Exception as e:
        return {"connected": False, "error": str(e)}


def _send_sms_alert(message):
    """Send SMS via Verizon email-to-SMS gateway using Gmail SMTP."""
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        err = ("Gmail credentials not set. Set environment variables: "
               "GMAIL_USER=yourname@gmail.com GMAIL_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx")
        log_event(f"SMS alert failed: {err}", level="error")
        return False, err

    try:
        msg = MIMEText(message)
        msg["From"] = GMAIL_USER
        msg["To"] = SMS_GATEWAY
        msg["Subject"] = ""  # SMS doesn't use subject

        with smtplib.SMTP("smtp.gmail.com", 587, timeout=10) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_USER, [SMS_GATEWAY], msg.as_string())

        log_event(f"SMS alert sent to {SMS_PHONE}: {message}")
        return True, None
    except smtplib.SMTPAuthenticationError as e:
        err = f"Gmail login failed. Check GMAIL_USER and GMAIL_APP_PASSWORD. ({e})"
        log_event(f"SMS alert error: {err}", level="error")
        return False, err
    except Exception as e:
        err = str(e)
        log_event(f"SMS alert error: {err}", level="error")
        return False, err


def _ib_monitor_loop():
    """Background thread: check IB Gateway every 30s, alert before market open."""
    from datetime import datetime
    import pytz

    et = pytz.timezone("US/Eastern")

    while _ib_monitor["running"]:
        try:
            result = _check_ib_gateway()
            now_et = datetime.now(et)

            with _ib_monitor_lock:
                _ib_monitor["gateway_connected"] = result["connected"]
                _ib_monitor["gateway_pid"] = result.get("pid")
                _ib_monitor["port_open"] = result.get("port_open", False)
                _ib_monitor["last_check"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                _ib_monitor["last_error"] = result.get("error")
                _ib_monitor["check_count"] += 1

                # Reset SMS flag at midnight ET
                if now_et.hour == 0 and now_et.minute < 1:
                    _ib_monitor["sms_sent_today"] = False

                # Alert: 30 min before market open (9:00 AM ET) on weekdays
                is_weekday = now_et.weekday() < 5
                is_alert_window = now_et.hour == 9 and 0 <= now_et.minute <= 5
                if (is_weekday and is_alert_window and
                        not result["connected"] and
                        not _ib_monitor["sms_sent_today"]):
                    ok, _ = _send_sms_alert(
                        f"AJ Alert: IB Gateway NOT connected on CashCow! "
                        f"Market opens in 30min. Check now."
                    )
                    if ok:
                        _ib_monitor["sms_sent_today"] = True
                        _ib_monitor["sms_last_sent"] = _ib_monitor["last_check"]

        except Exception as e:
            with _ib_monitor_lock:
                _ib_monitor["last_error"] = str(e)

        # Wait 30 seconds
        for _ in range(30):
            if not _ib_monitor["running"]:
                break
            _time.sleep(1)


def _start_ib_monitor():
    """Start the IB Gateway monitor background thread."""
    if _ib_monitor["running"]:
        return
    _ib_monitor["running"] = True
    t = threading.Thread(target=_ib_monitor_loop, daemon=True, name="ib-gateway-monitor")
    t.start()
    log_event("IB Gateway monitor started (30s interval)")


_SSH_DIR = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")), ".ssh")

# Collect ALL available SSH private key paths at startup
_SSH_KEY_PATHS = []
for _kn in ["id_ed25519", "id_rsa"]:
    _kp = os.path.join(_SSH_DIR, _kn)
    if os.path.exists(_kp):
        _SSH_KEY_PATHS.append(_kp)


def _ssh_run(user, host, command, timeout=10):
    """Execute a remote command via paramiko (no subprocess/ssh.exe dependency).
    Tries all available SSH keys. Returns (stdout_str, stderr_str, returncode).
    """
    last_err = None
    for key_path in (_SSH_KEY_PATHS or [None]):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            connect_kwargs = {
                "hostname": host,
                "username": user,
                "timeout": min(timeout, 5),
                "banner_timeout": 5,
                "auth_timeout": 5,
                "allow_agent": False,
                "look_for_keys": False,
            }
            if key_path:
                connect_kwargs["key_filename"] = key_path
            client.connect(**connect_kwargs)
            stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
            rc = stdout.channel.recv_exit_status()
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            return out, err, rc
        except paramiko.AuthenticationException as e:
            last_err = e
            continue
        except Exception:
            raise
        finally:
            try:
                client.close()
            except Exception:
                pass
    raise last_err or paramiko.AuthenticationException("No valid SSH key found")


def _get_machine_uptime_quick(host, user):
    """Quick uptime check only via paramiko (fast, for regular polling)."""
    try:
        out, err, rc = _ssh_run(user, host, "cat /proc/uptime 2>/dev/null | awk '{print $1}'", timeout=8)
        val = out.strip()
        if rc != 0 or not val:
            log_event(f"Reboot monitor SSH fail ({user}@{host}): rc={rc} err={err[:100]}", level="warn")
            return None
        return float(val) if val else None
    except Exception as e:
        log_event(f"Reboot monitor SSH exception ({user}@{host}): {e}", level="warn")
        return None


def _get_machine_reboot_info(host, user):
    """Get detailed info after reboot detected (boot time, IPs) via paramiko."""
    try:
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
        output, _, _ = _ssh_run(user, host, cmd, timeout=15)
        sections = re.split(r'MARK_(?:BOOT|IP|EXT|END)', output)
        sec = [s.strip() for s in sections]

        boot_time = sec[1] if len(sec) > 1 and sec[1] != "N/A" else "Unknown"
        local_ip = sec[2] if len(sec) > 2 and sec[2] != "N/A" else host
        external_ip = sec[3] if len(sec) > 3 and sec[3] != "N/A" else "N/A"

        return boot_time, local_ip, external_ip
    except Exception:
        return "Unknown", host, "N/A"


def _reboot_monitor_loop():
    """Background thread: check all machines for reboots every 60s."""
    global _reboot_monitor_started
    # Machines to monitor: {name: (host, user)}
    monitors = {}
    try:
        hosts = load_hosts()
        for category in ["computers", "raspberry_pis"]:
            for name, info in hosts.get(category, {}).items():
                h = info.get("host", "")
                u = info.get("username", "")
                if h and u and h not in ("localhost", "TBD"):
                    # Skip if this is the local machine
                    hostname = info.get("hostname", "")
                    import socket as _sock
                    if hostname.lower() == _sock.gethostname().lower():
                        continue
                    monitors[name] = (h, u)
    except Exception as e:
        log_event(f"Reboot monitor: failed to load hosts: {e}", level="error")
        return

    log_event(f"Reboot monitor started — watching: {', '.join(monitors.keys())}")

    while True:
        for name, (host, user) in monitors.items():
            try:
                # Quick uptime check (fast SSH, no extra commands)
                uptime_sec = _get_machine_uptime_quick(host, user)

                if uptime_sec is None:
                    log_event(f"Reboot monitor: {name} unreachable (SSH failed)", level="warn")
                    continue

                with _reboot_lock:
                    prev_uptime = _last_uptime.get(name)
                    _last_uptime[name] = uptime_sec

                    # Reboot detected: new uptime < previous uptime
                    # Skip first check (prev_uptime is None = first time seeing this machine)
                    if prev_uptime is not None and uptime_sec < prev_uptime:
                        log_event(f"Reboot detected: {name} (was {prev_uptime:.0f}s, now {uptime_sec:.0f}s)")
                        # SMS is handled by Christy Watchdog — only log here

            except Exception as e:
                log_event(f"Reboot monitor error ({name}): {e}", level="error")

        # Check every 45 seconds
        for _ in range(45):
            _time.sleep(1)


def start_reboot_monitor():
    """Start the reboot detection background thread."""
    global _reboot_monitor_started
    if _reboot_monitor_started:
        return
    _reboot_monitor_started = True
    t = threading.Thread(target=_reboot_monitor_loop, daemon=True, name="reboot-monitor")
    t.start()


@app.route("/api/cashcow/ib-gateway-status")
def api_ib_gateway_status():
    """Get IB Gateway connection status."""
    # Start monitor if not running
    if not _ib_monitor["running"]:
        _start_ib_monitor()

    with _ib_monitor_lock:
        return jsonify({
            "connected": _ib_monitor["gateway_connected"],
            "pid": _ib_monitor["gateway_pid"],
            "port_open": _ib_monitor["port_open"],
            "last_check": _ib_monitor["last_check"],
            "last_error": _ib_monitor["last_error"],
            "sms_sent_today": _ib_monitor["sms_sent_today"],
            "sms_last_sent": _ib_monitor["sms_last_sent"],
            "check_count": _ib_monitor["check_count"],
            "monitor_running": _ib_monitor["running"],
        })


@app.route("/api/cashcow/ib-gateway-check", methods=["POST"])
def api_ib_gateway_check():
    """Force an immediate IB Gateway check."""
    result = _check_ib_gateway()
    with _ib_monitor_lock:
        _ib_monitor["gateway_connected"] = result["connected"]
        _ib_monitor["gateway_pid"] = result.get("pid")
        _ib_monitor["port_open"] = result.get("port_open", False)
        _ib_monitor["last_check"] = _time.strftime("%Y-%m-%d %H:%M:%S")
        _ib_monitor["last_error"] = result.get("error")
        _ib_monitor["check_count"] += 1
    return jsonify(result)


@app.route("/api/cashcow/sms-test", methods=["POST"])
def api_sms_test():
    """Send a test SMS to verify the alert system works."""
    try:
        data = request.json or {}
        message = data.get("message", "AJ Robotics test message")
        ok, error = _send_sms_alert(message)
        return jsonify({"ok": ok, "phone": SMS_PHONE, "message": message,
                        "error": error, "gmail_user": GMAIL_USER or "(not set)"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/cashcow/analysis", methods=["POST"])
def api_cashcow_analysis():
    """Run on-demand analysis on CashCow."""
    data = request.json or {}
    analysis_type = data.get("type", "news")

    info = find_machine("CashCow")
    if not info:
        return jsonify({"error": "CashCow not found"}), 404

    host = info.get("host")
    user = info.get("username")

    base_dir = "/home/dongchul/ib_smart_trader/ib_smart_trader"

    # Map analysis type to command
    commands = {
        "news": f"cd {base_dir} && python -c \""
                "from analysis.news_analyzer import NewsAnalyzer; "
                "na = NewsAnalyzer(); r = na.analyze(); "
                "import json; print(json.dumps(r, indent=2, default=str))\" 2>&1 || "
                f"echo 'News analysis module not available. Install with: pip install feedparser'",

        "technical": f"cd {base_dir} && python -c \""
                     "from analysis.technical import TechnicalAnalyzer; "
                     "ta = TechnicalAnalyzer(); r = ta.scan(); "
                     "import json; print(json.dumps(r, indent=2, default=str))\" 2>&1 || "
                     f"echo 'Technical analysis: checking daily_picks.json...'; "
                     f"cat {base_dir}/daily_picks.json 2>/dev/null || echo 'No data'",

        "sentiment": f"cd {base_dir} && python -c \""
                     "from analysis.sentiment import MarketSentiment; "
                     "ms = MarketSentiment(); r = ms.check(); "
                     "import json; print(json.dumps(r, indent=2, default=str))\" 2>&1 || "
                     "echo 'Fetching market indicators...'; "
                     "curl -s 'https://api.alternative.me/fng/?limit=1' 2>/dev/null || echo 'API unavailable'",

        "risk": f"cd {base_dir} && python -c \""
                "from analysis.risk import RiskAnalyzer; "
                "ra = RiskAnalyzer(); r = ra.check_portfolio(); "
                "import json; print(json.dumps(r, indent=2, default=str))\" 2>&1 || "
                f"echo 'Portfolio risk check requires active positions data.'",

        "earnings": f"cd {base_dir} && python -c \""
                    "from analysis.earnings import EarningsCalendar; "
                    "ec = EarningsCalendar(); r = ec.upcoming(); "
                    "import json; print(json.dumps(r, indent=2, default=str))\" 2>&1 || "
                    "echo 'Earnings calendar module not available.'",

        "screener": f"cd {base_dir} && python run.py --screener-only 2>&1 | tail -50 || "
                    "echo 'Screener not available'",
    }

    cmd = commands.get(analysis_type, commands["news"])

    try:
        output, stderr, rc = _ssh_run(user, host, cmd, timeout=60)
        output = output.strip()
        stderr = stderr.strip()

        # Try to parse as JSON
        result_data = None
        try:
            result_data = json.loads(output)
        except (json.JSONDecodeError, ValueError):
            pass

        return jsonify({
            "type": analysis_type,
            "output": output,
            "data": result_data,
            "error": stderr if rc != 0 else None,
            "timestamp": _time.strftime("%Y-%m-%d %H:%M:%S"),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- Heartbeat Responder ---

@app.route("/api/heartbeat/status")
def api_heartbeat_status():
    """Proxy heartbeat status from Dreamer's agent."""
    dreamer = find_machine("Dreamer")
    if not dreamer:
        return jsonify({"running": False, "error": "Dreamer not found"}), 404
    host = dreamer.get("host", "192.168.1.44")
    port = dreamer.get("agent_port", 5000)
    try:
        import urllib.request
        url = f"http://{host}:{port}/api/heartbeat/status"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            import json as _json
            data = _json.loads(resp.read().decode("utf-8"))
            return jsonify(data)
    except Exception as e:
        return jsonify({
            "running": False, "error": f"Dreamer XBee service unreachable: {e}",
            "port": "COM18", "baud": 115200, "reply": "R4!",
            "received_count": 0, "replied_count": 0,
            "last_from": "", "last_rssi": None, "last_rssi_bar": "",
            "last_time": "", "history": [],
        })


@app.route("/api/heartbeat/toggle", methods=["POST"])
def api_heartbeat_toggle():
    """Proxy heartbeat toggle to Dreamer's agent."""
    dreamer = find_machine("Dreamer")
    if not dreamer:
        return jsonify({"ok": False, "error": "Dreamer not found"}), 404
    host = dreamer.get("host", "192.168.1.44")
    port = dreamer.get("agent_port", 5000)
    try:
        import urllib.request
        import json as _json
        body = _json.dumps(request.json or {"action": "toggle"}).encode("utf-8")
        url = f"http://{host}:{port}/api/heartbeat/toggle"
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
            return jsonify(data)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Dreamer XBee service unreachable: {e}"})


@app.route("/api/xbee/send", methods=["POST"])
def api_xbee_send():
    """Buffer gamepad data for XBee transmission (non-blocking)."""
    responder = get_responder()
    if not responder.is_running:
        return jsonify({"ok": False, "error": "XBee not running. Turn on Heartbeat Responder first."}), 400

    data = request.json.get("data", "") if request.json else ""
    if not data:
        return jsonify({"ok": False, "error": "No data to send"}), 400

    # Buffer data — background thread handles actual XBee send
    responder.buffer_gamepad(data)
    return jsonify({"ok": True, "packets": responder.gamepad_status["packets_sent"]})


@app.route("/api/xbee/gamepad-control", methods=["POST"])
def api_xbee_gamepad_control():
    """Start/stop the gamepad sender thread."""
    responder = get_responder()
    if not responder.is_running:
        return jsonify({"ok": False, "error": "XBee not running"}), 400

    action = request.json.get("action", "start") if request.json else "start"
    interval = request.json.get("interval_ms", 200) if request.json else 200

    if action == "start":
        responder.start_gamepad_sender(interval / 1000.0)
    elif action == "stop":
        responder.stop_gamepad_sender()

    return jsonify({"ok": True, **responder.gamepad_status})


# --- Gamepad Proxy (Dreamer -> remote machine's agent) ---

def _proxy_to_agent(machine_name, path, method="GET", json_body=None):
    """Forward an API call to a remote machine's agent."""
    info = find_machine(machine_name)
    if not info:
        return jsonify({"ok": False, "error": f"Machine '{machine_name}' not found"}), 404
    host = info.get("host", "")
    port = info.get("agent_port", 5000)
    url = f"http://{host}:{port}{path}"
    try:
        if method == "POST":
            resp = http_requests.post(url, json=json_body, timeout=5)
        else:
            resp = http_requests.get(url, timeout=5)
        return jsonify(resp.json()), resp.status_code
    except Exception as e:
        return jsonify({"ok": False, "error": f"{machine_name} agent unreachable: {e}"}), 503


@app.route("/api/proxy/<machine>/heartbeat/status")
def api_proxy_heartbeat_status(machine):
    """Get heartbeat/XBee status from a remote machine's agent."""
    return _proxy_to_agent(machine, "/api/heartbeat/status")


@app.route("/api/proxy/<machine>/heartbeat/start", methods=["POST"])
def api_proxy_heartbeat_start(machine):
    """Start heartbeat responder on a remote machine's agent."""
    return _proxy_to_agent(machine, "/api/heartbeat/start", "POST", request.json)


@app.route("/api/proxy/<machine>/heartbeat/stop", methods=["POST"])
def api_proxy_heartbeat_stop(machine):
    """Stop heartbeat responder on a remote machine's agent."""
    return _proxy_to_agent(machine, "/api/heartbeat/stop", "POST", request.json)


@app.route("/api/proxy/<machine>/heartbeat/toggle", methods=["POST"])
def api_proxy_heartbeat_toggle(machine):
    """Toggle heartbeat responder on a remote machine's agent."""
    return _proxy_to_agent(machine, "/api/heartbeat/toggle", "POST", request.json)


@app.route("/api/proxy/<machine>/gamepad/buffer", methods=["POST"])
def api_proxy_gamepad_buffer(machine):
    """Buffer gamepad data on a remote machine's agent for XBee send."""
    return _proxy_to_agent(machine, "/api/gamepad/buffer", "POST", request.json)


@app.route("/api/proxy/<machine>/gamepad/start", methods=["POST"])
def api_proxy_gamepad_start(machine):
    """Start gamepad sender on a remote machine's agent."""
    return _proxy_to_agent(machine, "/api/gamepad/start", "POST", request.json)


@app.route("/api/proxy/<machine>/gamepad/stop", methods=["POST"])
def api_proxy_gamepad_stop(machine):
    """Stop gamepad sender on a remote machine's agent."""
    return _proxy_to_agent(machine, "/api/gamepad/stop", "POST", request.json)


_restart_results = {}
_restart_lock = threading.Lock()


@app.route("/api/agent/restart/<machine>", methods=["POST"])
def api_agent_restart(machine):
    """Restart a remote machine's agent via SSH (paramiko)."""
    info = find_machine(machine)
    if not info:
        return jsonify({"ok": False, "error": f"Machine '{machine}' not found"}), 404
    host = info.get("host", "")
    username = info.get("username", "")
    os_type = info.get("os", "").lower()
    is_rpi = info.get("type") == "raspberry_pi"

    def _do_restart():
        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(host, username=username, timeout=10)

            if "windows" in os_type:
                ajmain = info.get("ajmain_path", "C:\\Users\\Dream\\AJMain")
                ps1 = ajmain + "\\scripts\\start_agent_bg.ps1"
                ssh.exec_command("taskkill /F /IM python.exe")
                _time.sleep(2)
                create_cmd = f'schtasks /Create /TN "AJAgent_{machine}" /TR "powershell -ExecutionPolicy Bypass -File \\"{ps1}\\" -Machine {machine}" /SC ONCE /ST 00:00 /F'
                ssh.exec_command(create_cmd)
                _time.sleep(1)
                ssh.exec_command(f'schtasks /Run /TN "AJAgent_{machine}"')
                _time.sleep(3)
            elif is_rpi:
                ssh.exec_command("pkill -f 'agent.start_agent' || true")
                _time.sleep(2)
                ssh.exec_command(f"cd /home/{username}/AJMain && nohup ./venv/bin/python -m agent.start_agent --machine {machine} > /tmp/agent.log 2>&1 &")
                _time.sleep(3)
            else:
                ssh.exec_command("pkill -f 'agent.start_agent' || true")
                _time.sleep(2)
                ssh.exec_command(f"cd /home/{username}/AJMain && nohup python3 -m agent.start_agent --machine {machine} > /tmp/agent.log 2>&1 &")
                _time.sleep(3)
            ssh.close()

            # Verify agent is up
            port = info.get("agent_port", 5000)
            try:
                r = http_requests.get(f"http://{host}:{port}/api/health", timeout=5)
                ok = r.status_code == 200
            except Exception:
                ok = False

            with _restart_lock:
                _restart_results[machine] = {
                    "status": "success" if ok else "started",
                    "time": _time.strftime("%H:%M:%S"),
                    "verified": ok,
                }
        except Exception as e:
            with _restart_lock:
                _restart_results[machine] = {
                    "status": "failed",
                    "error": str(e),
                    "time": _time.strftime("%H:%M:%S"),
                }

    threading.Thread(target=_do_restart, daemon=True).start()
    return jsonify({"ok": True, "message": f"Restarting {machine} agent..."})


@app.route("/api/agent/restart/<machine>/status")
def api_agent_restart_status(machine):
    """Check restart status."""
    with _restart_lock:
        result = _restart_results.get(machine)
    if not result:
        return jsonify({"status": "none"})
    return jsonify(result)


@app.route("/api/proxy/<machine>/xbee/<path:subpath>", methods=["GET", "POST"])
def api_proxy_xbee(machine, subpath):
    """Catch-all proxy for any machine's /api/xbee/* endpoints (scope, hardware, status, etc.)."""
    method = request.method
    return _proxy_to_agent(
        machine, f"/api/xbee/{subpath}", method,
        request.json if method == "POST" else None,
    )


# --- Vision Capture Proxy (any machine -> R1's vision_capture module) ---

@app.route("/api/proxy/<machine>/vision/capture", methods=["POST"])
def api_proxy_vision_capture(machine):
    """Proxy capture request to a machine's vision_capture module."""
    return _proxy_to_agent(machine, "/api/vision/capture", "POST", request.json)


@app.route("/api/proxy/<machine>/vision/status")
def api_proxy_machine_vision_status(machine):
    """Proxy vision status from a machine's vision_capture module."""
    return _proxy_to_agent(machine, "/api/vision/status")


@app.route("/api/proxy/<machine>/vision/history")
def api_proxy_machine_vision_history(machine):
    """Proxy vision history from a machine's vision_capture module."""
    limit = request.args.get("limit", 20)
    return _proxy_to_agent(machine, f"/api/vision/history?limit={limit}")


@app.route("/api/proxy/<machine>/vision/image/<image_id>")
def api_proxy_machine_vision_image(machine, image_id):
    """Proxy vision image from a machine's vision_capture module."""
    info = find_machine(machine)
    if not info:
        return jsonify({"error": f"Machine '{machine}' not found"}), 404
    host = info.get("host", "")
    port = info.get("agent_port", 5000)
    try:
        r = http_requests.get(f"http://{host}:{port}/api/vision/image/{image_id}", timeout=10)
        return Response(r.content, mimetype="image/jpeg")
    except Exception as e:
        return jsonify({"error": str(e)}), 503


# --- Vision API Proxy (Dreamer -> Christy:5100) ---

VISION_SERVER_URL = "http://127.0.0.1:5100"  # via SSH tunnel to Christy


@app.route("/api/proxy/vision/status")
def api_vision_status():
    """Proxy to Christy's vision server status, enriched with robot reachability."""
    try:
        r = http_requests.get(f"{VISION_SERVER_URL}/api/vision/status", timeout=5)
        data = r.json()
    except Exception as e:
        data = {"server": "offline", "error": str(e), "robots": {}}

    # Enrich with network reachability for all configured robots
    from robotics.vision_config import CAMERA_ROBOTS
    if "robots" not in data:
        data["robots"] = {}
    for rid, rinfo in CAMERA_ROBOTS.items():
        host = rinfo.get("host", "TBD")
        if host == "TBD":
            continue
        # Ping check (1 packet, 1s timeout)
        try:
            ping_cmd = ["ping", "-c", "1", "-W", "1", host]
            if os.name == "nt":
                ping_cmd = ["ping", "-n", "1", "-w", "1000", host]
            ping_result = subprocess.run(ping_cmd, capture_output=True, timeout=3)
            reachable = ping_result.returncode == 0
        except Exception:
            reachable = False
        if rid not in data["robots"]:
            data["robots"][rid] = {"total_analyses": 0}
        data["robots"][rid]["reachable"] = reachable
        if reachable and not data["robots"][rid].get("last_seen"):
            data["robots"][rid]["last_seen"] = "network-online"

    return jsonify(data)


@app.route("/api/proxy/vision/history")
def api_vision_history():
    """Proxy to Christy's vision history."""
    robot_id = request.args.get("robot_id", "")
    limit = request.args.get("limit", 20, type=int)
    try:
        r = http_requests.get(f"{VISION_SERVER_URL}/api/vision/history",
                              params={"robot_id": robot_id, "limit": limit}, timeout=5)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"results": [], "error": str(e)})


@app.route("/api/proxy/vision/latest")
def api_vision_latest():
    """Proxy latest image + analysis from Christy."""
    robot_id = request.args.get("robot_id", "R1")
    try:
        r = http_requests.get(f"{VISION_SERVER_URL}/api/vision/latest",
                              params={"robot_id": robot_id}, timeout=5)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/proxy/vision/image/<image_id>")
def api_vision_image(image_id):
    """Proxy image from Christy's vision server."""
    try:
        full = request.args.get("full", "0")
        r = http_requests.get(f"{VISION_SERVER_URL}/api/vision/image/{image_id}",
                              params={"full": full}, timeout=10)
        return (r.content, r.status_code,
                {"Content-Type": r.headers.get("Content-Type", "image/jpeg")})
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/vision/capture", methods=["POST"])
def api_vision_capture():
    """Trigger capture on a robot via SSH, then send to Christy for analysis."""
    data = request.json or {}
    robot_id = data.get("robot_id", "R1")
    analyze = data.get("analyze", True)
    prompt = data.get("prompt", "")

    # Robot connection info
    from robotics.vision_config import CAMERA_ROBOTS
    robot = CAMERA_ROBOTS.get(robot_id)
    if not robot:
        return jsonify({"ok": False, "error": f"Unknown robot: {robot_id}"}), 400

    host = robot.get("host")
    user = robot.get("user")
    camera = robot.get("camera", "/dev/video0")

    if not host or host == "TBD":
        return jsonify({"ok": False, "error": f"Robot {robot_id} host not configured"}), 400

    try:
        # Step 1: Capture image on robot via paramiko (binary stdout)
        rotate = robot.get("rotate", 0)
        rotate_code = ""
        if rotate == 180:
            rotate_code = "f = cv2.rotate(f, cv2.ROTATE_180);"
        elif rotate == 90:
            rotate_code = "f = cv2.rotate(f, cv2.ROTATE_90_CLOCKWISE);"
        elif rotate == 270:
            rotate_code = "f = cv2.rotate(f, cv2.ROTATE_90_COUNTERCLOCKWISE);"

        capture_script = (
            "python3 -c \""
            "import cv2, sys;"
            "cap = cv2.VideoCapture(0);"
            "ret, f = cap.read();"
            "cap.release();"
            f"{rotate_code}"
            "_, jpg = cv2.imencode('.jpg', f, [cv2.IMWRITE_JPEG_QUALITY, 75]) if ret else (False, None);"
            "sys.stdout.buffer.write(jpg.tobytes()) if ret else sys.exit(1)"
            "\""
        )
        log_event(f"Vision: capturing from {robot_id} ({host})...")

        # Use paramiko directly for binary data (try all keys)
        last_err = None
        image_bytes = b""
        err_str = ""
        rc = -1
        for key_path in (_SSH_KEY_PATHS or [None]):
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            connect_kwargs = {
                "hostname": host, "username": user,
                "timeout": 5, "banner_timeout": 5, "auth_timeout": 5,
                "allow_agent": False, "look_for_keys": False,
            }
            if key_path:
                connect_kwargs["key_filename"] = key_path
            try:
                client.connect(**connect_kwargs)
                stdin, stdout, stderr = client.exec_command(capture_script, timeout=15)
                rc = stdout.channel.recv_exit_status()
                image_bytes = stdout.read()  # raw bytes, no decode
                err_str = stderr.read().decode("utf-8", errors="replace")
                break  # success
            except paramiko.AuthenticationException as e:
                last_err = e
                continue
            finally:
                try:
                    client.close()
                except Exception:
                    pass
        else:
            if last_err:
                return jsonify({"ok": False, "error": f"SSH auth failed: {last_err}"})

        if rc != 0:
            return jsonify({"ok": False, "error": f"Capture failed: {err_str[:200]}"})

        if len(image_bytes) < 100:
            return jsonify({"ok": False, "error": "Captured image too small"})

        log_event(f"Vision: captured {len(image_bytes)} bytes from {robot_id}")

        # Step 2: Send to Christy's vision server
        import io
        endpoint = "/api/vision/analyze" if analyze else "/api/vision/upload"
        files = {"image": ("capture.jpg", io.BytesIO(image_bytes), "image/jpeg")}
        form_data = {"robot_id": robot_id}
        if prompt:
            form_data["prompt"] = prompt

        resp = http_requests.post(
            f"{VISION_SERVER_URL}{endpoint}",
            files=files, data=form_data, timeout=60
        )

        if resp.status_code == 200:
            result = resp.json()
            log_event(f"Vision: {robot_id} analysis complete ({result.get('processing_time_ms', '?')}ms)")
            return jsonify(result)
        else:
            return jsonify({"ok": False, "error": f"Vision server error: {resp.text[:200]}"})

    except http_requests.exceptions.ConnectionError:
        return jsonify({"ok": False, "error": "Cannot connect to Vision Server on Christy. Deploy & Run first."})
    except Exception as e:
        log_event(f"Vision capture error: {e}", level="error")
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/deploy/vision", methods=["POST"])
def api_deploy_vision():
    """Deploy vision server to Christy."""
    data = request.json or {}
    action = data.get("action", "sync")

    deploy_script = os.path.join(os.path.dirname(__file__), "deploy", "deploy_vision.py")
    cmd = [sys.executable, deploy_script]

    if action == "run":
        cmd.append("--run")
    elif action == "stop":
        cmd.append("--stop")
    elif action == "status":
        cmd.append("--status")
    elif action == "sync":
        pass

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        log_event(f"Deploy vision: action={action}, rc={proc.returncode}")
        return jsonify({
            "ok": proc.returncode == 0,
            "action": action,
            "output": proc.stdout + proc.stderr,
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Deploy timed out"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- YOLO Training API Proxy (Christy -> Dreamer:5002) ---

TRAINING_SERVICE_URL = "http://192.168.1.44:5002"


@app.route("/api/proxy/training/<path:subpath>", methods=["GET", "POST", "DELETE"])
def api_training_proxy(subpath):
    """Proxy all training API calls to Dreamer's Training Service (port 5002)."""
    url = f"{TRAINING_SERVICE_URL}/api/training/{subpath}"
    try:
        if request.method == "GET":
            r = http_requests.get(url, params=request.args, timeout=10)
        elif request.method == "DELETE":
            r = http_requests.delete(url, timeout=10)
        else:
            # POST - forward JSON or form data
            if request.content_type and "multipart" in request.content_type:
                # File upload - forward files and form data
                files = {}
                for key, f in request.files.items():
                    files[key] = (f.filename, f.stream, f.content_type)
                r = http_requests.post(url, files=files, data=request.form, timeout=60)
            elif request.is_json and request.data:
                r = http_requests.post(url, json=request.json, timeout=30)
            else:
                # Empty body POST (e.g., delete actions)
                r = http_requests.post(url, timeout=30)

        # Return the response with matching content type
        return (r.content, r.status_code,
                {"Content-Type": r.headers.get("Content-Type", "application/json")})
    except http_requests.exceptions.ConnectionError:
        return jsonify({
            "ok": False,
            "error": "Cannot connect to Training Service on Dreamer (port 5002). Start the service first."
        }), 502
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


# --- Remote Reboot ---

@app.route("/api/machine/reboot", methods=["POST"])
def api_machine_reboot():
    """Reboot a remote machine via SSH (through Christy as gateway)."""
    data = request.json or {}
    target = data.get("machine", "")

    # Allowed targets
    allowed = {
        "CashCow":  ("192.168.1.91", "dongchul"),
        "R1":       ("192.168.1.82", "dream"),
        "Christy":  ("192.168.1.94", "ajrobotics"),
    }

    if target not in allowed:
        return jsonify({"ok": False, "error": f"Unknown machine: {target}"}), 400

    host, user = allowed[target]

    # Determine who triggered the reboot
    local_machine = _detect_local_machine() or "Dreamer"
    trigger_source = f"{local_machine} Web UI"

    try:
        # Record trigger info before rebooting
        with _reboot_lock:
            _reboot_triggers[target] = (trigger_source, _time.strftime("%H:%M:%S"))
        # Reboot via paramiko
        out, err, rc = _ssh_run(user, host, "sudo reboot", timeout=10)
        log_event(f"Reboot command sent to {target} ({user}@{host}) by {trigger_source}")
        return jsonify({
            "ok": True,
            "machine": target,
            "message": f"Reboot command sent to {target}",
            "triggered_by": trigger_source,
        })
    except Exception as e:
        log_event(f"Reboot failed for {target}: {e}", level="error")
        return jsonify({"ok": False, "error": str(e)}), 500


# --- Debug ---

@app.route("/api/debug/ssh-test")
def api_debug_ssh():
    """Debug: test SSH from Flask process using paramiko."""
    import time as _t
    results = {}
    results["env"] = {
        "HOME": os.environ.get("HOME", "(not set)"),
        "USERPROFILE": os.environ.get("USERPROFILE", "(not set)"),
        "ssh_dir": _SSH_DIR,
        "ssh_keys": _SSH_KEY_PATHS or ["(none found)"],
        "id_ed25519_exists": os.path.exists(os.path.join(_SSH_DIR, "id_ed25519")),
        "id_rsa_exists": os.path.exists(os.path.join(_SSH_DIR, "id_rsa")),
        "method": "paramiko",
    }
    for name, user, host in [("CashCow", "dongchul", "192.168.1.91"),
                               ("Christy", "ajrobotics", "192.168.1.94"),
                               ("R1", "dream", "192.168.1.82")]:
        start = _t.time()
        try:
            out, err, rc = _ssh_run(user, host, "echo OK", timeout=8)
            elapsed = round(_t.time() - start, 2)
            results[name] = {
                "stdout": out.strip(),
                "stderr": err.strip()[:200],
                "rc": rc,
                "time": elapsed,
            }
        except Exception as e:
            results[name] = {"error": str(e), "time": round(_t.time() - start, 2)}
    return jsonify(results)


# --- Camera / go2rtc APIs ---

CAMERAS_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "configs", "cameras.json")


def _load_cameras_config():
    try:
        with open(CAMERAS_CONFIG_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {}


@app.route("/api/cameras/config")
def cameras_config():
    """Return cameras config (without RTSP credentials)."""
    cfg = _load_cameras_config()
    # Strip sensitive fields from each camera
    safe_cameras = {}
    for cam_id, cam in cfg.get("cameras", {}).items():
        safe_cameras[cam_id] = {k: v for k, v in cam.items()
                                if k not in ("rtsp_url", "go2rtc_source")}
    safe = {
        "go2rtc": cfg.get("go2rtc", {}),
        "cameras": safe_cameras,
    }
    return jsonify(safe)


@app.route("/api/cameras/go2rtc/status")
def cameras_go2rtc_status():
    """Proxy go2rtc API /api/streams to check stream status."""
    cfg = _load_cameras_config()
    api_url = cfg.get("go2rtc", {}).get("api_url", "http://192.168.1.94:1984")
    try:
        resp = http_requests.get(f"{api_url}/api/streams", timeout=5)
        return jsonify({"online": True, "streams": resp.json()})
    except Exception as e:
        return jsonify({"online": False, "error": str(e)})


@app.route("/api/cameras/eufy/events")
def cameras_eufy_events():
    """Proxy eufy event monitor API for push notification events."""
    try:
        resp = http_requests.get("http://127.0.0.1:63340/api/events", timeout=5)
        return jsonify(resp.json())
    except Exception as e:
        return jsonify({"events": [], "error": str(e)})


@app.route("/api/cameras/eufy/status")
def cameras_eufy_status():
    """Proxy eufy event monitor status."""
    try:
        resp = http_requests.get("http://127.0.0.1:63340/api/status", timeout=5)
        return jsonify(resp.json())
    except Exception as e:
        return jsonify({"connected": False, "error": str(e)})


@app.route("/api/cameras/eufy/history")
def cameras_eufy_history():
    """Proxy eufy cloud event history (thumbnails + metadata)."""
    try:
        sn = request.args.get("sn", "")
        url = "http://127.0.0.1:63340/api/events/history"
        if sn:
            url += f"?sn={sn}"
        resp = http_requests.get(url, timeout=10)
        return jsonify(resp.json())
    except Exception as e:
        return jsonify({"events": [], "error": str(e)})


# --- Startup ---

if __name__ == "__main__":
    log_event("AJ Robotics Control Hub started")
    start_background_monitor(interval=30)
    start_reboot_monitor()
    app.run(host="0.0.0.0", port=5000, debug=False)
