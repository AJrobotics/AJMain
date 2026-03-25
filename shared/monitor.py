"""
System monitoring utilities for local and remote machines.
"""

import socket
import time
import json
import os
import psutil
from collections import deque
from datetime import datetime
from threading import Thread, Lock

_HOSTNAME = socket.gethostname()

# Activity log (in-memory, last 200 entries)
_log_entries = deque(maxlen=200)
_log_lock = Lock()

# Machine status cache
_machine_status = {}
_status_lock = Lock()

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "configs", "hosts.json")


def log_event(message, level="info"):
    """Add an event to the activity log."""
    entry = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "message": message,
        "level": level,
    }
    with _log_lock:
        _log_entries.appendleft(entry)


def get_recent_logs(limit=50):
    """Return recent log entries."""
    with _log_lock:
        return list(_log_entries)[:limit]


def get_local_resources():
    """Get CPU, memory, disk, and network stats for localhost."""
    cpu = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    net = psutil.net_io_counters()

    return {
        "cpu_percent": cpu,
        "memory": {
            "percent": mem.percent,
            "used_gb": round(mem.used / (1024 ** 3), 1),
            "total_gb": round(mem.total / (1024 ** 3), 1),
        },
        "disk": {
            "percent": disk.percent,
            "used_gb": round(disk.used / (1024 ** 3), 1),
            "total_gb": round(disk.total / (1024 ** 3), 1),
        },
        "network": {
            "bytes_sent": net.bytes_sent,
            "bytes_recv": net.bytes_recv,
        },
    }


def ping_host(host, port=22, timeout=2):
    """Check if a host is reachable via TCP connect to SSH port."""
    if host == "localhost":
        return {"online": True, "latency_ms": 0}
    try:
        start = time.time()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((host, port))
        sock.close()
        latency = round((time.time() - start) * 1000, 1)
        return {"online": True, "latency_ms": latency}
    except (socket.timeout, socket.error, OSError):
        return {"online": False, "latency_ms": None}


def load_hosts():
    """Load machine definitions from hosts.json."""
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def _is_local(info):
    """Check if a machine entry refers to the machine we're running on."""
    hostname = info.get("hostname", "")
    return info.get("host") == "localhost" or hostname.lower() == _HOSTNAME.lower()


def check_all_machines():
    """Ping all machines and update status cache."""
    hosts = load_hosts()
    results = {}

    for category in ["computers", "raspberry_pis"]:
        for name, info in hosts.get(category, {}).items():
            host = info.get("host", "")
            if _is_local(info):
                status = {"online": True, "latency_ms": 0}
            else:
                status = ping_host(host)
            results[name] = {
                "name": name,
                "host": host,
                "role": info.get("role", ""),
                "type": info.get("type", ""),
                "online": status["online"],
                "latency_ms": status["latency_ms"],
            }

    for name, info in hosts.get("robots", {}).items():
        host = info.get("host", "")
        if host:
            status = ping_host(host)
            results[name] = {
                "name": name,
                "host": host,
                "role": "Robot",
                "type": "robot",
                "online": status["online"],
                "latency_ms": status["latency_ms"],
            }
        else:
            results[name] = {
                "name": name,
                "host": "TBD",
                "role": "Robot",
                "type": "robot",
                "online": False,
                "latency_ms": None,
                "status": info.get("status", "unknown"),
            }

    with _status_lock:
        _machine_status.update(results)

    return results


def get_machine_status():
    """Return cached machine status."""
    with _status_lock:
        return dict(_machine_status)


def start_background_monitor(interval=10):
    """Start background thread that periodically checks all machines."""
    def _monitor_loop():
        while True:
            try:
                results = check_all_machines()
                online = sum(1 for r in results.values() if r.get("online"))
                total = len(results)
                log_event(f"Ping sweep: {online}/{total} machines online")
            except Exception as e:
                log_event(f"Monitor error: {e}", level="error")
            time.sleep(interval)

    thread = Thread(target=_monitor_loop, daemon=True)
    thread.start()
    log_event("Background monitor started")
