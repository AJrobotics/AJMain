"""Heartbeat Responder — runs on Dreamer (Windows PC).

Listens for XBee "All Good?" broadcasts from Christy and replies with
this device's call sign ("R4!") via XBee broadcast.
Also reads RSSI signal strength.

Based on: MainTower/heartbeat_listener.py

Protocol:
  - XBee DigiMesh 900HP @ 115200 baud
  - Christy broadcasts: "All Good?"
  - Dreamer replies:    "R4!"
  - RSSI read via AT command "DB"

Toggle on/off from the web dashboard at /machine/Dreamer.
"""

import logging
import threading
import time
from datetime import datetime

logger = logging.getLogger(__name__)

XBEE_PORT = "COM18"
XBEE_BAUD = 115200
HEARTBEAT_MSG = "All Good?"
HEARTBEAT_REPLY = "R4!"  # Dreamer's call sign


def _detect_xbee_port_windows():
    """Scan Windows COM ports for FTDI/USB-serial devices (XBee adapters)."""
    try:
        import serial.tools.list_ports
        for info in serial.tools.list_ports.comports():
            if info.vid == 0x0403:  # FTDI vendor ID
                logger.info("Auto-detected XBee on %s (%s)", info.device, info.description)
                return info.device
    except Exception as e:
        logger.debug("COM port scan failed: %s", e)
    return None


def _rssi_bar(dbm):
    """Visual RSSI bar from dBm value."""
    if dbm is None:
        return "?"
    a = abs(dbm)
    if a <= 50:   return "█████"
    elif a <= 65: return "████░"
    elif a <= 75: return "███░░"
    elif a <= 85: return "██░░░"
    elif a <= 95: return "█░░░░"
    else:         return "░░░░░"


class HeartbeatResponder:
    """XBee heartbeat responder — listens for 'All Good?' and replies with device call sign."""

    def __init__(self, port: str = XBEE_PORT, baud: int = XBEE_BAUD, reply: str = HEARTBEAT_REPLY):
        self.port = port
        self.baud = baud
        self.reply = reply
        self._running = False
        self._thread: threading.Thread | None = None
        self._device = None

        # Stats
        self.received_count = 0
        self.replied_count = 0
        self.last_from: str = ""
        self.last_rssi: int | None = None
        self.last_time: str = ""
        self._history: list[dict] = []  # last N events
        self._error_msg: str = ""

        # Gamepad ACK tracking (from remote robots)
        self._last_gp_ack: dict | None = None  # latest gamepad_ack received

    @property
    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    def _read_rssi(self) -> int | None:
        """Read last received RSSI from XBee (AT command DB)."""
        try:
            db = self._device.get_parameter("DB")
            return -int.from_bytes(db, "big")
        except Exception:
            return None

    def _on_receive(self, xbee_message):
        """Callback: handle incoming XBee message."""
        try:
            addr = str(xbee_message.remote_device.get_64bit_addr())
            data = xbee_message.data.decode(errors="replace")
            rssi = self._read_rssi()
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ts = datetime.now().strftime("%H:%M:%S")

            self.received_count += 1
            self.last_from = addr
            self.last_rssi = rssi
            self.last_time = now

            event = {
                "time": now,
                "from": addr[-8:],  # last 8 chars of MAC
                "msg": data.strip(),
                "rssi": rssi,
                "rssi_bar": _rssi_bar(rssi),
            }

            # Auto-reply to "All Good?"
            if data.strip() == HEARTBEAT_MSG and self._device and self._device.is_open():
                self._device.send_data_broadcast(self.reply.encode())
                self.replied_count += 1
                event["reply"] = self.reply
                logger.info(
                    "[%s] RECV from ...%s: %s | RSSI: %s dBm %s -> REPLY: %s",
                    ts, addr[-8:], data.strip(), rssi, _rssi_bar(rssi), self.reply
                )
            else:
                event["reply"] = None
                # Check for gamepad_ack from robots
                try:
                    import json as _json
                    msg = _json.loads(data.strip())
                    if msg.get("type") == "gamepad_ack":
                        p = msg.get("payload", {})
                        self._last_gp_ack = {
                            "time": now,
                            "from": addr[-8:],
                            "rssi": rssi,
                            "count": p.get("count", 0),
                            "idle": p.get("idle", False),
                        }
                except (ValueError, AttributeError):
                    pass
                logger.info(
                    "[%s] RECV from ...%s: %s | RSSI: %s dBm %s",
                    ts, addr[-8:], data.strip(), rssi, _rssi_bar(rssi)
                )

            self._history.append(event)
            if len(self._history) > 100:
                self._history = self._history[-100:]

        except Exception as e:
            logger.error("Error handling XBee message: %s", e)

    def start(self) -> bool:
        """Start the XBee heartbeat responder."""
        if self.is_running:
            logger.info("Heartbeat responder already running")
            return True

        self._error_msg = ""

        try:
            from digi.xbee.devices import XBeeDevice
        except ImportError:
            self._error_msg = "digi-xbee package not installed. Run: pip install digi-xbee"
            logger.error(self._error_msg)
            return False

        # Try configured port first; fall back to auto-detection on Windows
        port = self.port
        try:
            try:
                self._device = XBeeDevice(port, self.baud)
                self._device.set_sync_ops_timeout(3)
                self._device.open()
            except Exception as e:
                logger.warning("XBee open failed on %s: %s — trying auto-detect", port, e)
                detected = _detect_xbee_port_windows()
                if detected and detected != port:
                    port = detected
                    self._device = XBeeDevice(port, self.baud)
                    self._device.set_sync_ops_timeout(3)
                    self._device.open()
                else:
                    raise

            self.port = port
            self._device.add_data_received_callback(self._on_receive)
            self._running = True

            # Keep-alive thread (XBee callbacks are on their own thread)
            self._thread = threading.Thread(
                target=self._keep_alive, daemon=True, name="heartbeat-responder"
            )
            self._thread.start()

            logger.info("XBee heartbeat responder started on %s @ %d baud", self.port, self.baud)
            return True

        except Exception as e:
            self._error_msg = str(e)
            logger.error("Failed to start XBee heartbeat responder: %s", e)
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
        """Stop the XBee heartbeat responder."""
        self._running = False
        if self._device:
            try:
                if self._device.is_open():
                    self._device.close()
                    logger.info("XBee closed on %s", self.port)
            except Exception as e:
                logger.error("Error closing XBee: %s", e)
            self._device = None
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        logger.info("Heartbeat responder stopped")

    def send_data(self, data: str) -> bool:
        """Send data via XBee broadcast (shared connection)."""
        if not self.is_running or not self._device:
            return False
        try:
            if self._device.is_open():
                self._device.send_data_broadcast(data.encode())
                return True
        except Exception as e:
            logger.error("XBee send failed: %s", e)
        return False

    def send_data_to(self, data: str, addr64: str) -> bool:
        """Send data via XBee async unicast to a specific 64-bit address.
        Non-blocking: does not wait for ACK from remote device.
        """
        if not self.is_running or not self._device:
            return False
        try:
            from digi.xbee.devices import RemoteXBeeDevice, XBee64BitAddress
            if self._device.is_open():
                remote = RemoteXBeeDevice(self._device, XBee64BitAddress.from_hex_string(addr64))
                self._device.send_data_async(remote, data.encode())
                return True
        except Exception as e:
            logger.error("XBee async send failed: %s", e)
        return False

    # --- Gamepad buffered sending ---
    _gamepad_buffer: dict | None = None
    _gamepad_thread: threading.Thread | None = None
    _gamepad_sending: bool = False
    _gamepad_interval: float = 0.5  # seconds (2 Hz)
    _gamepad_packets_sent: int = 0
    _gamepad_target_addr: str | None = None  # 64-bit XBee addr for unicast

    def start_gamepad_sender(self, interval: float = 0.5, target_addr: str = None):
        """Start background thread that sends buffered gamepad data at fixed interval."""
        if self._gamepad_sending:
            self._gamepad_interval = interval
            self._gamepad_target_addr = target_addr
            return
        self._gamepad_sending = True
        self._gamepad_interval = interval
        self._gamepad_target_addr = target_addr
        self._gamepad_packets_sent = 0
        self._gamepad_thread = threading.Thread(
            target=self._gamepad_send_loop, daemon=True, name="gamepad-sender"
        )
        self._gamepad_thread.start()
        logger.info("Gamepad sender started (interval=%.0fms, unicast=%s)",
                     interval * 1000, target_addr or "broadcast")

    def stop_gamepad_sender(self):
        """Stop the gamepad sender thread."""
        self._gamepad_sending = False
        self._gamepad_buffer = None
        if self._gamepad_thread:
            self._gamepad_thread.join(timeout=2)
            self._gamepad_thread = None
        logger.info("Gamepad sender stopped (sent %d packets)", self._gamepad_packets_sent)

    def buffer_gamepad(self, data: str):
        """Buffer gamepad data for the sender thread to pick up."""
        self._gamepad_buffer = data

    def _gamepad_send_loop(self):
        """Background loop: send latest buffered gamepad data at fixed interval."""
        while self._gamepad_sending and self._running:
            buf = self._gamepad_buffer
            if buf is not None:
                self._gamepad_buffer = None  # consume immediately
                addr = self._gamepad_target_addr
                if addr:
                    ok = self.send_data_to(buf, addr)
                else:
                    ok = self.send_data(buf)
                if ok:
                    self._gamepad_packets_sent += 1
            time.sleep(self._gamepad_interval)

    @property
    def gamepad_status(self) -> dict:
        return {
            "sending": self._gamepad_sending,
            "interval_ms": int(self._gamepad_interval * 1000),
            "packets_sent": self._gamepad_packets_sent,
            "last_ack": self._last_gp_ack,
        }

    def get_status(self) -> dict:
        """Return current responder status."""
        return {
            "running": self.is_running,
            "port": self.port,
            "baud": self.baud,
            "reply": self.reply,
            "received_count": self.received_count,
            "replied_count": self.replied_count,
            "last_from": self.last_from,
            "last_rssi": self.last_rssi,
            "last_rssi_bar": _rssi_bar(self.last_rssi),
            "last_time": self.last_time,
            "history": self._history[-20:],  # last 20 events
            "error": self._error_msg,
            "gamepad": self.gamepad_status,
        }

    def _keep_alive(self):
        """Keep the responder thread alive while XBee callbacks handle messages."""
        logger.info("XBee listener active on %s — auto-reply '%s' to '%s'",
                     self.port, self.reply, HEARTBEAT_MSG)
        while self._running:
            time.sleep(1)
        logger.info("Heartbeat responder keep-alive ended")


# Singleton instance
_responder = HeartbeatResponder()


def get_responder() -> HeartbeatResponder:
    """Get the singleton heartbeat responder instance."""
    return _responder
