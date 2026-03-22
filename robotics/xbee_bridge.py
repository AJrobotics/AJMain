"""XBee DigiMesh Bridge — broadcast send/receive, packet parsing, heartbeat.

This module handles all XBee communication:
  - Heartbeat: broadcasts "All Good?" every N seconds, tracks "Roger!" replies
  - Status packets (0x01): GPS + battery from robots
  - YOLO packets (0x03): detection results from robots
  - Command packets (0x02): movement commands to robots
"""

from __future__ import annotations

import logging
import struct
import threading
import time
from typing import Callable

from digi.xbee.devices import XBeeDevice
from digi.xbee.models.message import XBeeMessage

from robotics import config
from robotics.models import RobotState

logger = logging.getLogger(__name__)


class XBeeBridge:
    """Bridge between XBee device and robot states.

    - Receive: callback-driven packet parsing, updates RobotState
    - Send: broadcast command packets
    - Heartbeat: periodic "All Good?" with reply tracking
    """

    def __init__(
        self,
        port: str = config.XBEE_PORT,
        baud: int = config.XBEE_BAUD,
        heartbeat_interval: int = config.HEARTBEAT_INTERVAL,
    ):
        self.port = port
        self.baud = baud
        self._device: XBeeDevice | None = None
        self._running = False

        # robot_id -> RobotState
        self.robots: dict[int, RobotState] = {}
        self._init_robots()

        # Heartbeat state
        self.heartbeat_interval = heartbeat_interval
        self.heartbeat_sent: int = 0
        self.heartbeat_recv: int = 0
        self.last_reply_from: str = ""
        self.last_reply_time: float = 0.0
        self._heartbeat_thread: threading.Thread | None = None

        # RSSI (signal strength)
        self.rssi_dbm: int = 0

        # External callbacks
        self._on_update: Callable[[RobotState], None] | None = None
        self._on_heartbeat: Callable[[dict], None] | None = None
        self._on_message: Callable[[dict], None] | None = None

    def _init_robots(self) -> None:
        """Create initial RobotState objects from config."""
        for rid, info in config.ROBOTS.items():
            self.robots[rid] = RobotState(
                robot_id=rid,
                name=info["name"],
                mac=info.get("mac"),
            )

    def set_on_update(self, callback: Callable[[RobotState], None]) -> None:
        """Register callback for robot state updates."""
        self._on_update = callback

    def set_on_heartbeat(self, callback: Callable[[dict], None]) -> None:
        """Register callback for heartbeat events."""
        self._on_heartbeat = callback

    def set_on_message(self, callback: Callable[[dict], None]) -> None:
        """Register callback for all received messages (for logging)."""
        self._on_message = callback

    # -- Connection --

    def open(self) -> None:
        """Open XBee device, register receive callback, start heartbeat."""
        self._device = XBeeDevice(self.port, self.baud)
        self._device.set_sync_ops_timeout(10)
        self._device.open()
        self._device.add_data_received_callback(self._on_receive)
        self._running = True
        logger.info("XBee opened on %s @ %d", self.port, self.baud)

        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True
        )
        self._heartbeat_thread.start()

    def close(self) -> None:
        """Close XBee device."""
        self._running = False
        if self._device and self._device.is_open():
            self._device.close()
            logger.info("XBee closed")

    def is_open(self) -> bool:
        return self._device is not None and self._device.is_open()

    # -- RSSI --

    def read_rssi(self) -> int:
        """Read RSSI of last received packet (DB AT command)."""
        if not self.is_open():
            return 0
        try:
            db_value = self._device.get_parameter("DB")
            self.rssi_dbm = -int.from_bytes(db_value, "big")
            return self.rssi_dbm
        except Exception as e:
            logger.debug("RSSI read failed: %s", e)
            return self.rssi_dbm

    @property
    def rssi_bars(self) -> str:
        """Convert RSSI to visual bar string."""
        rssi = self.rssi_dbm
        if rssi == 0:
            return "No signal"
        a = abs(rssi)
        if a <= 50:
            return f"[=====] {rssi} dBm"
        elif a <= 65:
            return f"[==== ] {rssi} dBm"
        elif a <= 75:
            return f"[===  ] {rssi} dBm"
        elif a <= 85:
            return f"[==   ] {rssi} dBm"
        elif a <= 95:
            return f"[=    ] {rssi} dBm"
        else:
            return f"[     ] {rssi} dBm"

    # -- Receive --

    def _on_receive(self, xbee_message: XBeeMessage) -> None:
        """XBee receive callback (called from library thread)."""
        data = xbee_message.data
        addr = str(xbee_message.remote_device.get_64bit_addr())

        self.read_rssi()

        if len(data) < 1:
            return

        pkt_type = data[0]

        # Text message detection (ASCII printable range)
        if pkt_type > 0x20:
            self._handle_text(data, addr)
            return

        try:
            if pkt_type == config.PKT_STATUS:
                self._handle_status(data, addr)
            elif pkt_type == config.PKT_YOLO:
                self._handle_yolo(data, addr)
            else:
                logger.warning("Unknown packet type 0x%02X from %s", pkt_type, addr)
        except struct.error as e:
            logger.error("Packet parse error from %s: %s", addr, e)

    def _handle_status(self, data: bytes, addr: str) -> None:
        """Process 0x01 status packet."""
        if len(data) < 15:
            logger.warning("Status packet too short: %d bytes", len(data))
            return

        robot_id = data[13]
        robot = self._get_or_create_robot(robot_id, addr)
        robot.update_from_status_packet(data)

        msg = {
            "type": "status",
            "robot_id": robot_id,
            "robot_name": robot.name,
            "mac": addr,
            "lat": robot.latitude,
            "lon": robot.longitude,
            "battery_v": robot.battery_voltage,
            "status": robot.status_text,
            "rssi": self.rssi_dbm,
        }
        logger.info("Status from %s: lat=%.4f lon=%.4f batt=%.1fV",
                     robot.name, robot.latitude, robot.longitude, robot.battery_voltage)
        self._notify_message(msg)
        self._notify(robot)

    def _handle_yolo(self, data: bytes, addr: str) -> None:
        """Process 0x03 YOLO packet."""
        if len(data) < 8:
            logger.warning("YOLO packet too short: %d bytes", len(data))
            return

        robot = self._find_robot_by_mac(addr)
        if robot is None:
            logger.warning("YOLO from unknown MAC %s", addr)
            return

        robot.update_from_yolo_packet(data[1:])

        msg = {
            "type": "yolo",
            "robot_id": robot.robot_id,
            "robot_name": robot.name,
            "mac": addr,
            "class": robot.last_yolo.class_name,
            "confidence": robot.last_yolo.confidence,
            "rssi": self.rssi_dbm,
        }
        logger.info("YOLO from %s: %s (%.2f)",
                     robot.name, robot.last_yolo.class_name, robot.last_yolo.confidence)
        self._notify_message(msg)
        self._notify(robot)

    def _handle_text(self, data: bytes, addr: str) -> None:
        """Process text messages (heartbeat replies, etc.)."""
        text = data.decode(errors="replace").strip()
        logger.info("Text from %s: %s", addr, text)

        msg = {
            "type": "text",
            "mac": addr,
            "text": text,
            "rssi": self.rssi_dbm,
        }

        if text in config.VALID_REPLIES:
            self.heartbeat_recv += 1
            self.last_reply_from = addr
            self.last_reply_time = time.time()
            msg["type"] = "heartbeat_reply"
            msg["recv_count"] = self.heartbeat_recv
            msg["reply_text"] = text
            logger.info("Heartbeat reply #%d from %s: '%s' (RSSI: %d dBm)",
                        self.heartbeat_recv, addr, text, self.rssi_dbm)
            self._notify_heartbeat(msg)

        self._notify_message(msg)

    def _get_or_create_robot(self, robot_id: int, mac: str) -> RobotState:
        """Find robot by ID or create new one."""
        if robot_id not in self.robots:
            self.robots[robot_id] = RobotState(
                robot_id=robot_id,
                name=f"Robot-{robot_id}",
                mac=mac,
            )
        robot = self.robots[robot_id]
        if robot.mac is None:
            robot.mac = mac
        return robot

    def _find_robot_by_mac(self, mac: str) -> RobotState | None:
        for robot in self.robots.values():
            if robot.mac == mac:
                return robot
        return None

    # -- Heartbeat --

    def _heartbeat_loop(self) -> None:
        """Heartbeat sender thread — broadcasts 'All Good?' periodically."""
        while self._running and self.is_open():
            try:
                self._device.send_data_broadcast(config.HEARTBEAT_MSG.encode())
                self.heartbeat_sent += 1
                logger.debug("Heartbeat #%d sent", self.heartbeat_sent)

                msg = {
                    "type": "heartbeat_sent",
                    "sent_count": self.heartbeat_sent,
                    "recv_count": self.heartbeat_recv,
                }
                self._notify_heartbeat(msg)
                self._notify_message(msg)
            except Exception as e:
                logger.error("Heartbeat send error: %s", e)
            time.sleep(self.heartbeat_interval)

    # -- Send --

    def send_command(
        self,
        linear_x: float = 0.0,
        angular_z: float = 0.0,
        cmd_flags: int = 0,
    ) -> None:
        """Broadcast 0x02 command packet to all robots."""
        if not self.is_open():
            logger.warning("Cannot send: XBee not open")
            return

        pkt = struct.pack('<BffB',
                          config.PKT_COMMAND,
                          linear_x,
                          angular_z,
                          cmd_flags)
        self._device.send_data_broadcast(pkt)
        logger.debug("Command sent: lin=%.2f ang=%.2f flags=0x%02X",
                      linear_x, angular_z, cmd_flags)

    def send_stop(self) -> None:
        self.send_command(cmd_flags=config.CMD_STOP)

    def send_estop(self) -> None:
        self.send_command(cmd_flags=config.CMD_ESTOP)

    # -- Callbacks --

    def _notify(self, robot: RobotState) -> None:
        if self._on_update:
            try:
                self._on_update(robot)
            except Exception:
                logger.exception("Error in on_update callback")

    def _notify_heartbeat(self, data: dict) -> None:
        if self._on_heartbeat:
            try:
                self._on_heartbeat(data)
            except Exception:
                logger.exception("Error in on_heartbeat callback")

    def _notify_message(self, data: dict) -> None:
        if self._on_message:
            try:
                self._on_message(data)
            except Exception:
                logger.exception("Error in on_message callback")

    # -- Status --

    def get_status(self) -> dict:
        """Get current bridge status summary."""
        return {
            "xbee_open": self.is_open(),
            "port": self.port,
            "heartbeat_sent": self.heartbeat_sent,
            "heartbeat_recv": self.heartbeat_recv,
            "heartbeat_ratio": (
                f"{self.heartbeat_recv}/{self.heartbeat_sent}"
                if self.heartbeat_sent > 0 else "0/0"
            ),
            "rssi": self.rssi_dbm,
            "rssi_bars": self.rssi_bars,
            "last_reply_from": self.last_reply_from,
            "last_reply_time": self.last_reply_time,
            "robots": {
                rid: r.to_dict() for rid, r in self.robots.items()
            },
        }
