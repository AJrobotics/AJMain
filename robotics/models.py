"""Robot state data models."""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field


@dataclass
class YoloDetection:
    """YOLO detection result (packet type 0x03)."""

    class_id: int = 0        # 0=weed, 1=crop
    confidence: float = 0.0
    bbox_x: int = 0
    bbox_y: int = 0
    timestamp: float = field(default_factory=time.time)

    CLASS_NAMES = {0: "weed", 1: "crop"}

    @property
    def class_name(self) -> str:
        return self.CLASS_NAMES.get(self.class_id, f"unknown({self.class_id})")

    @classmethod
    def from_bytes(cls, data: bytes) -> YoloDetection:
        """Unpack 0x03 payload (excluding pkt_type byte)."""
        class_id, confidence, bbox_x, bbox_y = struct.unpack('<Bf HH', data)
        return cls(
            class_id=class_id,
            confidence=confidence,
            bbox_x=bbox_x,
            bbox_y=bbox_y,
        )


@dataclass
class RobotState:
    """Full state of a single robot."""

    robot_id: int = 0
    name: str = ""
    mac: str | None = None

    # GPS
    latitude: float = 0.0
    longitude: float = 0.0

    # Battery
    battery_voltage: float = 0.0

    # Status flags (bit0=moving, bit1=yolo_active, bit2=error)
    status_flags: int = 0

    # Latest YOLO detection
    last_yolo: YoloDetection | None = None

    # Timestamp
    last_seen: float = 0.0

    @property
    def is_moving(self) -> bool:
        return bool(self.status_flags & 0x01)

    @property
    def is_yolo_active(self) -> bool:
        return bool(self.status_flags & 0x02)

    @property
    def has_error(self) -> bool:
        return bool(self.status_flags & 0x04)

    @property
    def is_online(self) -> bool:
        """Online if last seen within 30 seconds."""
        if self.last_seen == 0.0:
            return False
        return (time.time() - self.last_seen) < 30.0

    @property
    def battery_percent(self) -> int:
        """Battery voltage -> percent (3-cell LiPo: 9.0V ~ 12.6V)."""
        min_v, max_v = 9.0, 12.6
        pct = (self.battery_voltage - min_v) / (max_v - min_v) * 100
        return max(0, min(100, int(pct)))

    @property
    def status_text(self) -> str:
        if not self.is_online:
            return "OFFLINE"
        if self.has_error:
            return "ERROR"
        if self.is_moving:
            return "MOVING"
        return "ONLINE"

    def update_from_status_packet(self, data: bytes) -> None:
        """Update from 0x01 status packet (full packet including pkt_type)."""
        _, lat, lon, batt, robot_id, flags = struct.unpack('<BfffBB', data[:15])
        self.latitude = lat
        self.longitude = lon
        self.battery_voltage = batt
        self.robot_id = robot_id
        self.status_flags = flags
        self.last_seen = time.time()

    def update_from_yolo_packet(self, data: bytes) -> None:
        """Update from 0x03 YOLO payload (excluding pkt_type byte)."""
        self.last_yolo = YoloDetection.from_bytes(data)
        self.last_seen = time.time()

    def to_dict(self) -> dict:
        """Serialize to dictionary for logging/JSON."""
        return {
            "robot_id": self.robot_id,
            "name": self.name,
            "mac": self.mac,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "battery_voltage": round(self.battery_voltage, 2),
            "battery_percent": self.battery_percent,
            "status": self.status_text,
            "is_moving": self.is_moving,
            "is_yolo_active": self.is_yolo_active,
            "has_error": self.has_error,
            "last_yolo": {
                "class": self.last_yolo.class_name,
                "confidence": round(self.last_yolo.confidence, 3),
            } if self.last_yolo else None,
            "last_seen": self.last_seen,
        }
