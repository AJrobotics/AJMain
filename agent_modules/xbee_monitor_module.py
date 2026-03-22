"""
XBee Monitor Module — Christy only.
Coordinates XBee communication with all robots.
Sends periodic heartbeats ("All Good?"), logs responses, tracks robot status.
"""

import logging
import threading
import time
from datetime import datetime

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

XBEE_PORT = "/dev/ttyUSB0"  # Christy's XBee serial port
XBEE_BAUD = 115200
HEARTBEAT_MSG = "All Good?"
DEFAULT_HEARTBEAT_INTERVAL = 10  # seconds


class XbeeMonitorModule:
    name = "xbee_monitor"

    def __init__(self):
        self._device = None
        self._running = False
        self._thread = None
        self._responses: list[dict] = []
        self._total_responses = 0
        self._robot_status: dict[str, dict] = {}  # mac -> last status
        self._lock = threading.Lock()
        self._error_msg = ""
        self._heartbeats_sent = 0
        self._heartbeat_interval = DEFAULT_HEARTBEAT_INTERVAL

    @property
    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    def _on_receive(self, xbee_message):
        try:
            addr = str(xbee_message.remote_device.get_64bit_addr())
            data = xbee_message.data.decode(errors="replace").strip()
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # Read RSSI
            rssi = None
            try:
                db = self._device.get_parameter("DB")
                rssi = -int.from_bytes(db, "big")
            except Exception:
                pass

            entry = {
                "time": now,
                "from": addr,
                "from_short": addr[-8:],
                "data": data,
                "rssi": rssi,
            }

            with self._lock:
                self._responses.append(entry)
                self._total_responses += 1
                if len(self._responses) > 500:
                    self._responses = self._responses[-500:]

                # Update robot status
                self._robot_status[addr] = {
                    "last_seen": now,
                    "last_data": data,
                    "rssi": rssi,
                    "online": True,
                }

            logger.info("XBee RECV from ...%s: %s (RSSI: %s)", addr[-8:], data, rssi)

        except Exception as e:
            logger.error("XBee receive error: %s", e)

    def _heartbeat_loop(self):
        """Send periodic heartbeat broadcasts and mark stale robots offline."""
        while self._running:
            try:
                if self._device and self._device.is_open():
                    self._device.send_data_broadcast(HEARTBEAT_MSG.encode())
                    self._heartbeats_sent += 1
                    logger.info("XBee heartbeat sent (#%d)", self._heartbeats_sent)

                # Mark robots offline if no response in 5 minutes
                cutoff = time.time() - 300
                with self._lock:
                    for addr, status in self._robot_status.items():
                        try:
                            last = datetime.strptime(status["last_seen"], "%Y-%m-%d %H:%M:%S")
                            if last.timestamp() < cutoff:
                                status["online"] = False
                        except (ValueError, KeyError):
                            pass

            except Exception as e:
                logger.error("Heartbeat loop error: %s", e)

            time.sleep(self._heartbeat_interval)

    def start(self, interval: int | None = None) -> bool:
        if self.is_running:
            return True
        if interval is not None:
            self._heartbeat_interval = max(1, interval)
        self._error_msg = ""
        try:
            from digi.xbee.devices import XBeeDevice
        except ImportError:
            self._error_msg = "digi-xbee not installed"
            logger.error(self._error_msg)
            return False

        try:
            self._device = XBeeDevice(XBEE_PORT, XBEE_BAUD)
            self._device.set_sync_ops_timeout(10)
            self._device.open()
            self._device.add_data_received_callback(self._on_receive)
            self._running = True
            self._thread = threading.Thread(
                target=self._heartbeat_loop, daemon=True, name="xbee-monitor"
            )
            self._thread.start()
            logger.info("XBee monitor started on %s", XBEE_PORT)
            return True
        except Exception as e:
            self._error_msg = str(e)
            logger.error("XBee monitor start failed: %s", e)
            self._running = False
            if self._device:
                try:
                    if self._device.is_open():
                        self._device.close()
                except Exception:
                    pass
                self._device = None
            return False

    def stop(self):
        self._running = False
        if self._device:
            try:
                if self._device.is_open():
                    self._device.close()
            except Exception:
                pass
            self._device = None
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None

    def register(self, app):
        bp = Blueprint("xbee_monitor", __name__)

        @bp.route("/api/xbee/status")
        def xbee_status():
            with self._lock:
                return jsonify({
                    "running": self.is_running,
                    "port": XBEE_PORT,
                    "heartbeat_interval": self._heartbeat_interval,
                    "heartbeats_sent": self._heartbeats_sent,
                    "total_responses": self._total_responses,
                    "robots": dict(self._robot_status),
                    "recent_messages": self._responses[-20:],
                    "error": self._error_msg,
                })

        @bp.route("/api/xbee/start", methods=["POST"])
        def xbee_start():
            data = request.json or {}
            interval = data.get("interval")
            if interval is not None:
                interval = int(interval)
            ok = self.start(interval=interval)
            return jsonify({"success": ok, "error": self._error_msg})

        @bp.route("/api/xbee/interval", methods=["POST"])
        def xbee_interval():
            data = request.json or {}
            interval = data.get("interval")
            if interval is None:
                return jsonify({"error": "No interval provided"}), 400
            self._heartbeat_interval = max(1, int(interval))
            logger.info("Heartbeat interval changed to %d seconds", self._heartbeat_interval)
            return jsonify({"success": True, "interval": self._heartbeat_interval})

        @bp.route("/api/xbee/stop", methods=["POST"])
        def xbee_stop():
            self.stop()
            return jsonify({"success": True})

        @bp.route("/api/xbee/send", methods=["POST"])
        def xbee_send():
            data = request.json or {}
            msg = data.get("message", "")
            if not msg:
                return jsonify({"error": "No message"}), 400
            if not self.is_running or not self._device:
                return jsonify({"error": "XBee not running"}), 503
            try:
                self._device.send_data_broadcast(msg.encode())
                return jsonify({"success": True})
            except Exception as e:
                return jsonify({"error": str(e)}), 500

        @bp.route("/api/xbee/robots")
        def xbee_robots():
            with self._lock:
                return jsonify({"robots": dict(self._robot_status)})

        app.register_blueprint(bp)

        # Auto-start XBee monitor
        try:
            ok = self.start()
            if ok:
                logger.info("XBee monitor auto-started on %s", XBEE_PORT)
            else:
                logger.warning("XBee monitor auto-start failed: %s", self._error_msg)
        except Exception as e:
            logger.error("XBee monitor auto-start error: %s", e)
