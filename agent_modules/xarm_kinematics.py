"""
Inverse / Forward Kinematics for HiWonder xArm 1S (6-DOF).

Joint layout
  Servo 1  Gripper        (not used by IK)
  Servo 2  Base yaw       (rotation around Z axis)
  Servo 3  Shoulder pitch
  Servo 4  Elbow pitch
  Servo 5  Wrist pitch
  Servo 6  Wrist roll     (not used by IK)

Coordinate frame
  Origin = base rotation axis at ground level
  X = forward,  Y = left,  Z = up

LX-15D servos: 240 deg range mapped to position 0-1000.
  position 0   -> 0 deg
  position 500 -> 120 deg  (center)
  position 1000 -> 240 deg

Angle convention (geometric angles, all in the vertical plane):
  theta2 = base yaw from +X axis  (radians)
  theta3 = shoulder angle from horizontal  (positive = up)
  theta4 = elbow angle relative to upper arm  (positive = fold up)
  theta5 = wrist angle relative to forearm  (positive = fold up)
  wrist_pitch = absolute tip angle from horizontal = theta3 + theta4 + theta5
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


class XArmKinematics:
    """Pure-math IK/FK solver -- no hardware dependency."""

    POS_PER_DEG = 1000.0 / 240.0   # ~4.1667
    DEG_PER_POS = 240.0 / 1000.0   # 0.24

    def __init__(self, config_path: str | Path | None = None):
        # link lengths (mm)
        self.L1 = 0.0   # base to shoulder (vertical)
        self.L2 = 0.0   # upper arm
        self.L3 = 0.0   # forearm
        self.L4 = 0.0   # wrist to tip

        # per-servo calibration
        self.calibration: Dict[int, dict] = {
            sid: {"zero_offset_deg": 120.0, "direction": 1,
                  "min_pos": 0, "max_pos": 1000}
            for sid in (2, 3, 4, 5)
        }

        self.home_xyz = (150.0, 0.0, 150.0)
        self.xyz_speed = 100.0
        self.default_wrist_pitch = -90.0   # deg
        self.max_reach_frac = 0.95

        if config_path is not None:
            self.load_config(config_path)

    # ------------------------------------------------------------------
    # config
    # ------------------------------------------------------------------

    def load_config(self, path: str | Path) -> None:
        p = Path(path)
        if not p.exists():
            logger.warning("IK config not found: %s", p)
            return
        with open(p) as f:
            cfg = json.load(f)

        ll = cfg.get("link_lengths_mm", {})
        self.L1 = float(ll.get("L1", self.L1))
        self.L2 = float(ll.get("L2", self.L2))
        self.L3 = float(ll.get("L3", self.L3))
        self.L4 = float(ll.get("L4", self.L4))

        for sid_str, cal in cfg.get("servo_calibration", {}).items():
            sid = int(sid_str)
            if sid in self.calibration:
                self.calibration[sid].update(cal)

        hxyz = cfg.get("home_xyz_mm", {})
        self.home_xyz = (
            float(hxyz.get("x", self.home_xyz[0])),
            float(hxyz.get("y", self.home_xyz[1])),
            float(hxyz.get("z", self.home_xyz[2])),
        )
        self.xyz_speed = float(cfg.get("xyz_speed_mm_per_sec", self.xyz_speed))
        self.default_wrist_pitch = float(
            cfg.get("default_wrist_pitch_deg", self.default_wrist_pitch))
        self.max_reach_frac = float(
            cfg.get("max_reach_fraction", self.max_reach_frac))

        logger.info("IK config loaded: L1=%.1f L2=%.1f L3=%.1f L4=%.1f mm",
                     self.L1, self.L2, self.L3, self.L4)

    def is_configured(self) -> bool:
        return self.L2 > 0 and self.L3 > 0

    # ------------------------------------------------------------------
    # angle <-> servo position
    # ------------------------------------------------------------------

    def servo_to_angle(self, servo_id: int, position: float) -> float:
        """Servo position (0-1000) -> geometric angle (radians)."""
        cal = self.calibration[servo_id]
        servo_deg = position * self.DEG_PER_POS
        geo_deg = (servo_deg - cal["zero_offset_deg"]) * cal["direction"]
        return math.radians(geo_deg)

    def angle_to_servo(self, servo_id: int, angle_rad: float) -> float:
        """Geometric angle (radians) -> servo position (0-1000), clamped."""
        cal = self.calibration[servo_id]
        geo_deg = math.degrees(angle_rad)
        servo_deg = geo_deg * cal["direction"] + cal["zero_offset_deg"]
        pos = servo_deg * self.POS_PER_DEG
        return _clamp(pos, float(cal["min_pos"]), float(cal["max_pos"]))

    # ------------------------------------------------------------------
    # forward kinematics
    # ------------------------------------------------------------------

    def forward_kinematics(self, servo_positions: Dict[int, float]) -> Optional[dict]:
        """Compute tip (x,y,z) from servo positions {2:p, 3:p, 4:p, 5:p}."""
        if not self.is_configured():
            return None

        base  = self.servo_to_angle(2, servo_positions.get(2, 500))
        theta3 = self.servo_to_angle(3, servo_positions.get(3, 500))
        theta4 = self.servo_to_angle(4, servo_positions.get(4, 500))
        theta5 = self.servo_to_angle(5, servo_positions.get(5, 500))

        # cumulative angles in the vertical plane
        a_shoulder = theta3                        # from horizontal
        a_elbow    = theta3 + theta4               # absolute elbow direction
        a_wrist    = theta3 + theta4 + theta5      # absolute wrist/tip direction

        # planar positions (r = horizontal, z = vertical, origin at shoulder)
        r_tip = (self.L2 * math.cos(a_shoulder)
                 + self.L3 * math.cos(a_elbow)
                 + self.L4 * math.cos(a_wrist))
        z_tip = (self.L2 * math.sin(a_shoulder)
                 + self.L3 * math.sin(a_elbow)
                 + self.L4 * math.sin(a_wrist))

        x = r_tip * math.cos(base)
        y = r_tip * math.sin(base)
        z = z_tip + self.L1

        return {
            "x": round(x, 2), "y": round(y, 2), "z": round(z, 2),
            "wrist_pitch_deg": round(math.degrees(a_wrist), 2),
        }

    # ------------------------------------------------------------------
    # inverse kinematics
    # ------------------------------------------------------------------

    def inverse_kinematics(
        self, x: float, y: float, z: float,
        wrist_pitch_deg: Optional[float] = None,
    ) -> Optional[Dict[int, float]]:
        """Compute servo positions {2:p, 3:p, 4:p, 5:p} for tip at (x,y,z).

        Returns None if unreachable.
        """
        if not self.is_configured():
            return None
        if wrist_pitch_deg is None:
            wrist_pitch_deg = self.default_wrist_pitch
        wp = math.radians(wrist_pitch_deg)

        # --- base ---
        r = math.hypot(x, y)
        base = math.atan2(y, x) if r >= 1.0 else 0.0

        # --- vertical plane ---
        z_eff = z - self.L1

        # wrist point = tip minus L4 along wrist direction
        wx = r     - self.L4 * math.cos(wp)
        wz = z_eff - self.L4 * math.sin(wp)
        dw = math.hypot(wx, wz)

        max_reach = (self.L2 + self.L3) * self.max_reach_frac
        min_reach = abs(self.L2 - self.L3) * 1.05
        if dw > max_reach or dw < min_reach:
            return None

        # --- two-link IK ---
        cos_q4 = (dw**2 - self.L2**2 - self.L3**2) / (2 * self.L2 * self.L3)
        cos_q4 = _clamp(cos_q4, -1.0, 1.0)
        # elbow-up: theta4 negative (folds toward ground)
        theta4 = -math.acos(cos_q4)

        # shoulder
        alpha = math.atan2(wz, wx)
        k1 = self.L2 + self.L3 * math.cos(theta4)
        k2 = self.L3 * math.sin(theta4)
        theta3 = alpha - math.atan2(k2, k1)

        # wrist = desired pitch - shoulder - elbow
        theta5 = wp - theta3 - theta4

        # --- convert to servo ---
        pos2 = self.angle_to_servo(2, base)
        pos3 = self.angle_to_servo(3, theta3)
        pos4 = self.angle_to_servo(4, theta4)
        pos5 = self.angle_to_servo(5, theta5)

        return {2: round(pos2, 1), 3: round(pos3, 1),
                4: round(pos4, 1), 5: round(pos5, 1)}

    # ------------------------------------------------------------------
    # workspace helpers
    # ------------------------------------------------------------------

    def is_reachable(self, x: float, y: float, z: float,
                     wrist_pitch_deg: Optional[float] = None) -> bool:
        return self.inverse_kinematics(x, y, z, wrist_pitch_deg) is not None

    def clamp_to_workspace(
        self, x: float, y: float, z: float,
        wrist_pitch_deg: Optional[float] = None,
    ) -> Tuple[float, float, float]:
        """Return nearest reachable point (radial clamp in vertical plane)."""
        if self.is_reachable(x, y, z, wrist_pitch_deg):
            return (x, y, z)

        if wrist_pitch_deg is None:
            wrist_pitch_deg = self.default_wrist_pitch
        wp = math.radians(wrist_pitch_deg)

        r = math.hypot(x, y)
        z_eff = z - self.L1
        wx = r     - self.L4 * math.cos(wp)
        wz = z_eff - self.L4 * math.sin(wp)
        dw = math.hypot(wx, wz)

        max_reach = (self.L2 + self.L3) * self.max_reach_frac
        min_reach = abs(self.L2 - self.L3) * 1.05

        if dw < 0.001:
            dw = 0.001
        if dw > max_reach:
            scale = max_reach / dw
        elif dw < min_reach:
            scale = min_reach / dw
        else:
            return (x, y, z)

        wx *= scale
        wz *= scale
        new_r = wx + self.L4 * math.cos(wp)
        new_z = wz + self.L4 * math.sin(wp) + self.L1

        if r > 0.001:
            new_x = x * (new_r / r)
            new_y = y * (new_r / r)
        else:
            new_x, new_y = new_r, 0.0

        return (round(new_x, 2), round(new_y, 2), round(new_z, 2))
