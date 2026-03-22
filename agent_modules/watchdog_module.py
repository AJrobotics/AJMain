"""
Watchdog Module — Christy only.
Periodically checks all machines' /api/health, CashCow trader status (during market hours),
and sends email alerts when issues are detected.
"""

import json
import logging
import os
import smtplib
import time
from datetime import datetime
from email.mime.text import MIMEText
from threading import Thread, Lock

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

# Alert email config (set via environment or watchdog_config.json)
WATCHDOG_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "configs", "watchdog_config.json"
)


def _load_watchdog_config() -> dict:
    defaults = {
        "check_interval": 60,
        "alert_email_to": "",
        "alert_email_from": "",
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "smtp_user": "",
        "smtp_pass": "",
        "offline_threshold": 3,
        "disk_warn_percent": 90,
        "memory_warn_percent": 90,
    }
    try:
        with open(WATCHDOG_CONFIG_PATH, "r") as f:
            cfg = json.load(f)
            defaults.update(cfg)
    except FileNotFoundError:
        pass
    return defaults


def _is_market_hours() -> bool:
    """Check if US stock market is currently open (ET rough check)."""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/New_York"))
    except ImportError:
        import time as _t
        # Rough UTC-5 offset
        now = datetime.utcnow()
        from datetime import timedelta
        now = now - timedelta(hours=5)
    # Mon-Fri, 9:30 - 16:00 ET
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return 570 <= t <= 960  # 9:30=570, 16:00=960


class WatchdogModule:
    name = "watchdog"

    def __init__(self):
        self._results: dict = {}
        self._lock = Lock()
        self._fail_counts: dict[str, int] = {}
        self._alerts_sent: list[dict] = []
        self._running = False
        self._thread = None

    def _check_machine(self, name: str, host: str, port: int) -> dict:
        from shared.agent_client import AgentClient
        client = AgentClient(host, port)
        healthy = client.health(timeout=5)
        result = {"name": name, "host": host, "online": healthy, "checked_at": datetime.now().isoformat()}

        if healthy:
            # Get system info for resource warnings
            data, err = client.get("/api/system/info", timeout=5)
            if data:
                result["cpu_percent"] = data.get("cpu_percent")
                result["memory_percent"] = data.get("memory", {}).get("percent")
                result["disk_percent"] = data.get("disk", {}).get("percent")
        return result

    def _check_trader(self, host: str, port: int) -> dict | None:
        """Check CashCow trader status during market hours."""
        if not _is_market_hours():
            return None
        from shared.agent_client import AgentClient
        client = AgentClient(host, port)
        data, err = client.get("/api/trader/status", timeout=5)
        if err:
            return {"running": False, "error": err}
        return data

    def _send_alert(self, subject: str, body: str):
        config = _load_watchdog_config()
        to = config.get("alert_email_to")
        frm = config.get("alert_email_from")
        if not to or not frm:
            logger.warning("Email alert not configured — skipping: %s", subject)
            return

        msg = MIMEText(body)
        msg["Subject"] = f"[AJ Watchdog] {subject}"
        msg["From"] = frm
        msg["To"] = to

        try:
            with smtplib.SMTP(config["smtp_host"], config["smtp_port"]) as srv:
                srv.starttls()
                srv.login(config["smtp_user"], config["smtp_pass"])
                srv.send_message(msg)
            logger.info("Alert email sent: %s", subject)
            self._alerts_sent.append({
                "time": datetime.now().isoformat(),
                "subject": subject,
            })
            if len(self._alerts_sent) > 100:
                self._alerts_sent = self._alerts_sent[-100:]
        except Exception as e:
            logger.error("Failed to send alert email: %s", e)

    def _run_check_cycle(self):
        from agent.base_agent import load_hosts
        config = _load_watchdog_config()
        hosts = load_hosts()
        threshold = config.get("offline_threshold", 3)
        disk_warn = config.get("disk_warn_percent", 90)
        mem_warn = config.get("memory_warn_percent", 90)

        results = {}
        for category in ("computers", "raspberry_pis"):
            for name, info in hosts.get(category, {}).items():
                host = info.get("host", "")
                port = info.get("agent_port", 5000)
                if not host or host == "TBD":
                    continue
                r = self._check_machine(name, host, port)
                results[name] = r

                # Track offline failures
                if not r["online"]:
                    self._fail_counts[name] = self._fail_counts.get(name, 0) + 1
                    if self._fail_counts[name] == threshold:
                        self._send_alert(
                            f"{name} OFFLINE",
                            f"{name} ({host}) has been offline for {threshold} consecutive checks."
                        )
                else:
                    self._fail_counts[name] = 0

                # Resource warnings
                if r.get("disk_percent", 0) > disk_warn:
                    self._send_alert(
                        f"{name} disk usage {r['disk_percent']}%",
                        f"Disk usage on {name} is at {r['disk_percent']}% (threshold: {disk_warn}%)"
                    )
                if r.get("memory_percent", 0) > mem_warn:
                    self._send_alert(
                        f"{name} memory usage {r['memory_percent']}%",
                        f"Memory usage on {name} is at {r['memory_percent']}% (threshold: {mem_warn}%)"
                    )

        # Check CashCow trader
        cashcow_info = hosts.get("computers", {}).get("CashCow", {})
        if cashcow_info:
            trader = self._check_trader(
                cashcow_info.get("host", ""),
                cashcow_info.get("agent_port", 5000)
            )
            if trader and not trader.get("running"):
                self._send_alert(
                    "CashCow Trader DOWN (market hours)",
                    f"Smart Trader is not running on CashCow during market hours.\n"
                    f"Error: {trader.get('error', 'Process not found')}"
                )
            if trader:
                results["CashCow_trader"] = trader

        with self._lock:
            self._results = results

    def _watchdog_loop(self):
        config = _load_watchdog_config()
        interval = config.get("check_interval", 60)
        while self._running:
            try:
                self._run_check_cycle()
            except Exception as e:
                logger.error("Watchdog cycle error: %s", e)
            time.sleep(interval)

    def register(self, app):
        bp = Blueprint("watchdog", __name__)

        @bp.route("/api/watchdog/status")
        def wd_status():
            with self._lock:
                return jsonify({
                    "running": self._running,
                    "results": self._results,
                    "fail_counts": dict(self._fail_counts),
                    "recent_alerts": self._alerts_sent[-10:],
                })

        @bp.route("/api/watchdog/start", methods=["POST"])
        def wd_start():
            if self._running:
                return jsonify({"message": "Already running"})
            self._running = True
            self._thread = Thread(target=self._watchdog_loop, daemon=True, name="watchdog")
            self._thread.start()
            return jsonify({"message": "Watchdog started"})

        @bp.route("/api/watchdog/stop", methods=["POST"])
        def wd_stop():
            self._running = False
            return jsonify({"message": "Watchdog stopped"})

        app.register_blueprint(bp)

        # Auto-start watchdog
        self._running = True
        self._thread = Thread(target=self._watchdog_loop, daemon=True, name="watchdog")
        self._thread.start()
        logger.info("Watchdog auto-started")
