"""
Trader Module — CashCow only.
Manages IB Smart Trader locally (no SSH needed).
Provides status, log tail, daily picks, start/stop via REST API.
"""

import json
import os
import subprocess
import logging

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

# Paths on CashCow
TRADER_BASE = os.path.expanduser("~/ib_smart_trader/ib_smart_trader")
TRADER_LOG_DIR = os.path.expanduser("~/ib_smart_trader/logs")
TRADER_RUN_SCRIPT = os.path.join(TRADER_BASE, "run.py")
DAILY_PICKS_PATH = os.path.join(TRADER_BASE, "daily_picks.json")
CONFIG_PATH = os.path.join(TRADER_BASE, "config.json")


class TraderModule:
    name = "trader"

    def _is_running(self) -> dict:
        """Check if Smart Trader process is running."""
        try:
            proc = subprocess.run(
                ["pgrep", "-af", "run.py"],
                capture_output=True, text=True, timeout=5
            )
            lines = [l for l in proc.stdout.strip().splitlines()
                     if "run.py" in l and "pgrep" not in l]
            if not lines:
                return {"running": False, "mode": "STOPPED", "pid": None}

            line = lines[0]
            parts = line.split()
            pid = parts[0] if parts else None
            mode = "AUTO" if "--auto" in line else "ALERT"
            return {"running": True, "mode": mode, "pid": pid}
        except Exception as e:
            logger.error("Process check failed: %s", e)
            return {"running": False, "mode": "ERROR", "pid": None, "error": str(e)}

    def _read_json(self, path: str) -> dict | None:
        try:
            with open(path, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def _tail_log(self, lines: int = 30) -> list[str]:
        """Read last N lines from the trader log."""
        # Try stdout log first, then smart_trader.log
        for name in ("trader_stdout.log", "smart_trader.log"):
            path = os.path.join(TRADER_LOG_DIR, name)
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                try:
                    with open(path, "r", errors="replace") as f:
                        all_lines = f.readlines()
                        return [l.rstrip() for l in all_lines[-lines:]]
                except Exception:
                    continue
        # Fallback: log in base dir
        path = os.path.join(TRADER_BASE, "smart_trader.log")
        if os.path.isfile(path):
            try:
                with open(path, "r", errors="replace") as f:
                    all_lines = f.readlines()
                    return [l.rstrip() for l in all_lines[-lines:]]
            except Exception:
                pass
        return []

    def register(self, app):
        bp = Blueprint("trader", __name__)

        @bp.route("/api/trader/status")
        def trader_status():
            proc = self._is_running()
            log_lines = self._tail_log(30)
            picks = self._read_json(DAILY_PICKS_PATH)
            config = self._read_json(CONFIG_PATH)
            return jsonify({
                **proc,
                "log_lines": log_lines,
                "daily_picks": picks,
                "config": config,
            })

        @bp.route("/api/trader/start", methods=["POST"])
        def trader_start():
            data = request.json or {}
            auto = data.get("auto", True)
            port = data.get("port", 7497)

            if self._is_running()["running"]:
                return jsonify({"error": "Already running"}), 409

            cmd = ["python", TRADER_RUN_SCRIPT]
            if auto:
                cmd.append("--auto")
            cmd.extend(["--port", str(port)])

            try:
                subprocess.Popen(
                    cmd,
                    stdout=open(os.path.join(TRADER_LOG_DIR, "trader_stdout.log"), "a"),
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                return jsonify({"success": True, "message": "Trader started"})
            except Exception as e:
                return jsonify({"error": str(e)}), 500

        @bp.route("/api/trader/stop", methods=["POST"])
        def trader_stop():
            try:
                subprocess.run(["pkill", "-f", "run.py"], timeout=5)
                return jsonify({"success": True, "message": "Trader stopped"})
            except Exception as e:
                return jsonify({"error": str(e)}), 500

        @bp.route("/api/trader/logs")
        def trader_logs():
            lines = request.args.get("lines", 50, type=int)
            return jsonify({"log_lines": self._tail_log(lines)})

        app.register_blueprint(bp)
