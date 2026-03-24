"""
xArm Controller -- self-contained servo control with gamepad input, IK, and Flask API.

Extracts all xArm-related logic from the XBee responder into a standalone class
that can run independently with or without hardware.

No imports from parent folders.  Only depends on:
  xarm.kinematics, xarm.local_gamepad, xarm.hardware, stdlib, Flask.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from flask import Blueprint, jsonify, request, send_file

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
GAMEPAD_DEADZONE = 0.10
VELOCITY_LOOP_HZ = 50
VELOCITY_LOOP_DT = 1.0 / VELOCITY_LOOP_HZ
GAMEPAD_SERVO_DURATION = 40  # ms per move command (smooth small steps)


class XArmController:
    """Self-contained xArm controller with gamepad, IK, and HTTP API.

    Parameters
    ----------
    config_dir : str or None
        Path to the xarm/ folder containing config.json, gamepads.json,
        simulation.html.  Defaults to the directory of *this* file.
    hardware : bool
        If True, attempt to import and connect xArm HID.
        If False, simulation only (no hardware dependency).
    """

    def __init__(self, config_dir: str | None = None, hardware: bool = True):
        self._config_dir = config_dir or os.path.dirname(os.path.abspath(__file__))

        # -- servo state --
        self._servo_positions: Dict[int, float] = {sid: 500.0 for sid in range(1, 7)}
        self._servo_velocity: Dict[int, float] = {sid: 0.0 for sid in range(1, 7)}
        self._servo_reverse: Dict[int, bool] = {sid: False for sid in range(1, 7)}
        self._servo_speed: Dict[int, float] = {sid: 150.0 for sid in range(1, 7)}
        self._servo_coupling: dict = {}
        self._servo_lock = threading.Lock()

        self._gripper_open = False

        # -- gamepad state --
        self._gamepad_arm_enabled = False  # True = send moves to physical arm
        self._gamepad_count = 0
        self._stick_map = {"lx": "0", "ly": "1", "rx": "2", "ry": "3"}
        self._axis_map: Dict[int, int] = {0: 2, 1: 3, 2: 6, 3: 4}
        self._latest_gamepad: Optional[dict] = None
        self._stick_left = [0.0, 0.0]
        self._stick_right = [0.0, 0.0]

        # -- velocity loop --
        self._running = False
        self._velocity_running = False
        self._home_target: Optional[Dict[int, float]] = None

        # -- IK / XYZ mode --
        self._ik_mode = False
        self._xyz_velocity = [0.0, 0.0, 0.0]
        self._current_xyz: Optional[dict] = None
        self._wrist_pitch_deg = -90.0
        self._kinematics = None
        self._init_kinematics()

        # -- hardware --
        self._hardware_enabled = hardware
        self._arm = None
        self._arm_connected = False
        self._arm_lock = threading.Lock()
        if hardware:
            self._init_hardware()

        # -- browser gamepad --
        self._browser_gamepad_enabled = False

        # -- local gamepad --
        self._local_gamepad = None
        self._local_gamepad_enabled = False
        self._local_gamepad_class = None
        self._list_gamepads_fn = None
        self._init_local_gamepad()

    # -----------------------------------------------------------------------
    # Initialisation helpers
    # -----------------------------------------------------------------------

    def _init_kinematics(self):
        """Load IK solver from xarm.kinematics."""
        try:
            from xarm.kinematics import XArmKinematics
            cfg = os.path.join(self._config_dir, "config.json")
            self._kinematics = XArmKinematics(cfg if os.path.exists(cfg) else None)
            if self._kinematics.is_configured():
                logger.info("IK solver loaded (L2=%.1f L3=%.1f)",
                            self._kinematics.L2, self._kinematics.L3)
                self._wrist_pitch_deg = self._kinematics.default_wrist_pitch
            else:
                logger.info("IK solver loaded but link lengths not set")
        except Exception as e:
            logger.warning("IK solver not available: %s", e)
            self._kinematics = None

    def _init_hardware(self):
        """Try to import xarm.hardware and create arm instance."""
        try:
            from xarm.hardware import XArm
            self._arm = XArm()
            logger.info("xArm hardware module loaded")
        except Exception as e:
            logger.info("xArm hardware not available: %s", e)
            self._arm = None

    def _init_local_gamepad(self):
        """Initialise local gamepad reader if available."""
        try:
            from xarm.local_gamepad import LocalGamepadReader, list_gamepads
            self._local_gamepad_class = LocalGamepadReader
            self._list_gamepads_fn = list_gamepads
            devices = list_gamepads()
            if devices:
                logger.info("Local gamepads found: %s",
                            [d["name"] for d in devices])
            else:
                logger.info("No local gamepads detected")
        except Exception as e:
            logger.info("Local gamepad not available: %s", e)

    # -----------------------------------------------------------------------
    # Gamepad profile loading
    # -----------------------------------------------------------------------

    def _load_gamepad_profile(self, device_name: str):
        """Load gamepad profile from gamepads.json based on device name."""
        try:
            cfg_path = os.path.join(self._config_dir, "gamepads.json")
            if not os.path.exists(cfg_path):
                logger.info("gamepads.json not found at %s", cfg_path)
                return
            with open(cfg_path) as f:
                cfg = json.load(f)
            gamepads = cfg.get("gamepads", {})
            matched = None
            dev_norm = " ".join(device_name.lower().split())
            dev_words = set(dev_norm.split())
            for key, profile in gamepads.items():
                pname = profile.get("name", key)
                key_norm = " ".join(key.lower().split())
                pname_norm = " ".join(pname.lower().split())
                key_words = {w for w in key_norm.split()
                             if len(w) > 2 and w.isalpha()}
                if key_words and key_words.issubset(dev_words):
                    matched = profile
                    logger.info("Gamepad profile matched: %s", key)
                    break
                if (key_norm in dev_norm or dev_norm in key_norm or
                        pname_norm in dev_norm or dev_norm in pname_norm):
                    matched = profile
                    logger.info("Gamepad profile matched: %s", key)
                    break
            if not matched:
                logger.info("No gamepad profile for '%s'", device_name)
                return
            # Apply stick map
            sm = matched.get("stick_map")
            if sm:
                self._stick_map = {k: str(v) for k, v in sm.items()}
                logger.info("Stick map set: %s", self._stick_map)
            # Apply servo map
            smap = matched.get("servo_map")
            if smap:
                self._axis_map = {int(k): int(v) for k, v in smap.items()}
                logger.info("Servo map set: %s", self._axis_map)
            # Apply coupling
            coup = matched.get("coupling")
            if coup:
                self._servo_coupling = coup
                logger.info("Coupling set: %s", coup)
            # Apply speeds
            speeds = matched.get("servo_speed")
            if speeds:
                for k, v in speeds.items():
                    self._servo_speed[int(k)] = float(v)
        except Exception as e:
            logger.warning("Failed to load gamepad profile: %s", e)

    # -----------------------------------------------------------------------
    # Gamepad -> servo mapping
    # -----------------------------------------------------------------------

    def _local_gamepad_callback(self, state: dict):
        """Called by LocalGamepadReader at 50Hz with gamepad state."""
        self._handle_gamepad(state)

    def _handle_gamepad(self, payload: dict):
        """Process incoming gamepad data (from local reader or external source)."""
        self._gamepad_count += 1
        axes = payload.get("axes", {})
        buttons = payload.get("buttons", {})
        hats = payload.get("hats", {})

        self._latest_gamepad = {"axes": axes, "buttons": buttons, "hats": hats}

        # Update stick positions
        sm = self._stick_map
        self._stick_left = [
            float(axes.get(sm["lx"], axes.get(str(sm["lx"]), 0.0))),
            float(axes.get(sm["ly"], axes.get(str(sm["ly"]), 0.0))),
        ]
        self._stick_right = [
            float(axes.get(sm["rx"], axes.get(str(sm["rx"]), 0.0))),
            float(axes.get(sm["ry"], axes.get(str(sm["ry"]), 0.0))),
        ]

        # Map to servo velocities
        if self._gamepad_arm_enabled or self._local_gamepad_enabled or self._browser_gamepad_enabled:
            self._gamepad_to_xarm(axes, buttons)

    def _gamepad_to_xarm(self, axes: dict, buttons: dict):
        """Store axis values as velocity targets. The velocity loop moves servos."""
        sm = self._stick_map

        with self._servo_lock:
            if self._ik_mode and self._kinematics and self._kinematics.is_configured():
                # --- XYZ mode: sticks -> Cartesian velocity ---
                rx_val = float(axes.get(sm["rx"], axes.get(str(sm["rx"]), 0.0)))
                ry_val = float(axes.get(sm["ry"], axes.get(str(sm["ry"]), 0.0)))
                lx_val = float(axes.get(sm["lx"], axes.get(str(sm["lx"]), 0.0)))
                ly_val = float(axes.get(sm["ly"], axes.get(str(sm["ly"]), 0.0)))

                if abs(rx_val) < GAMEPAD_DEADZONE:
                    rx_val = 0.0
                if abs(ry_val) < GAMEPAD_DEADZONE:
                    ry_val = 0.0
                if abs(lx_val) < GAMEPAD_DEADZONE:
                    lx_val = 0.0
                if abs(ly_val) < GAMEPAD_DEADZONE:
                    ly_val = 0.0

                # Left stick X -> X, Left stick Y -> Y, Right stick Y -> Z
                self._xyz_velocity = [lx_val, -ly_val, -ry_val]

                # Right stick X -> wrist rotation (servo 2, direct)
                self._servo_velocity[2] = rx_val if abs(rx_val) >= GAMEPAD_DEADZONE else 0.0

                # LB/RB -> wrist pitch adjustment
                lb = int(buttons.get(4, buttons.get("4", 0)))
                rb = int(buttons.get(5, buttons.get("5", 0)))
                if lb or rb:
                    self._wrist_pitch_deg += float(rb - lb) * 2.0
                    self._wrist_pitch_deg = max(-180, min(180, self._wrist_pitch_deg))

            else:
                # --- Servo-direct mode ---
                for axis_key, servo_id in self._axis_map.items():
                    val = float(axes.get(axis_key, axes.get(str(axis_key), 0.0)))
                    if abs(val) < GAMEPAD_DEADZONE:
                        val = 0.0
                    if self._servo_reverse.get(servo_id):
                        val = -val
                    self._servo_velocity[servo_id] = val

                # Apply servo coupling
                for axis_key_str, coupled in self._servo_coupling.items():
                    axis_key = int(axis_key_str) if axis_key_str.isdigit() else axis_key_str
                    val = float(axes.get(axis_key, axes.get(str(axis_key), 0.0)))
                    if abs(val) < GAMEPAD_DEADZONE:
                        val = 0.0
                    for entry in coupled:
                        sid = entry["servo"]
                        cval = -val if entry.get("reverse") else val
                        self._servo_velocity[sid] = cval

                # LB/RB -> Servo 2
                lb = int(buttons.get(4, buttons.get("4", 0)))
                rb = int(buttons.get(5, buttons.get("5", 0)))
                if lb or rb:
                    self._servo_velocity[2] = max(-1, min(1,
                        self._servo_velocity.get(2, 0.0) + float(rb - lb)))

            # Button A -> gripper toggle (both modes)
            btn_a = int(buttons.get(0, buttons.get("0", 0)))
            if btn_a:
                self._gripper_open = not self._gripper_open
                self._servo_positions[1] = 200.0 if self._gripper_open else 700.0
                with self._arm_lock:
                    if self._arm and self._arm.connected:
                        self._arm.move_servo(1, int(self._servo_positions[1]),
                                             GAMEPAD_SERVO_DURATION)

            # Button Y -> smooth home (both modes)
            btn_y = int(buttons.get(3, buttons.get("3", 0)))
            if btn_y:
                if self._ik_mode and self._kinematics:
                    hx, hy, hz = self._kinematics.home_xyz
                    result = self._kinematics.inverse_kinematics(
                        hx, hy, hz, self._wrist_pitch_deg)
                    if result:
                        self._home_target = dict(result)
                        self._home_target[1] = 500.0
                        self._home_target[2] = 500.0
                else:
                    self._home_target = {sid: 500.0 for sid in range(1, 7)}
                for sid in range(1, 7):
                    self._servo_velocity[sid] = 0.0
                self._xyz_velocity = [0.0, 0.0, 0.0]

    # -----------------------------------------------------------------------
    # 50 Hz servo velocity loop
    # -----------------------------------------------------------------------

    def _servo_velocity_loop(self):
        """Background loop: smoothly move servos based on velocity targets at 50Hz."""
        logger.info("Servo velocity loop started (%d Hz)", VELOCITY_LOOP_HZ)
        while self._velocity_running and self._running:
            sim_only = (self._local_gamepad_enabled or self._browser_gamepad_enabled) and not self._gamepad_arm_enabled
            if not self._gamepad_arm_enabled and not sim_only:
                time.sleep(0.1)
                continue

            moved = False
            with self._servo_lock:
                # -- Smooth home --
                if self._home_target is not None:
                    all_reached = True
                    for sid in range(1, 7):
                        target = self._home_target[sid]
                        pos = self._servo_positions[sid]
                        if abs(pos - target) < 1.0:
                            self._servo_positions[sid] = target
                            continue
                        all_reached = False
                        speed = self._servo_speed.get(sid, 150.0)
                        step = speed * VELOCITY_LOOP_DT
                        if pos < target:
                            self._servo_positions[sid] = min(target, pos + step)
                        else:
                            self._servo_positions[sid] = max(target, pos - step)
                        moved = True
                    if all_reached:
                        self._home_target = None
                        logger.info("Smooth home complete")

                elif self._ik_mode and self._kinematics and self._kinematics.is_configured():
                    # --- XYZ velocity control via IK ---
                    kin = self._kinematics
                    speed = kin.xyz_speed

                    dx = self._xyz_velocity[0] * speed * VELOCITY_LOOP_DT
                    dy = self._xyz_velocity[1] * speed * VELOCITY_LOOP_DT
                    dz = self._xyz_velocity[2] * speed * VELOCITY_LOOP_DT

                    if abs(dx) > 0.01 or abs(dy) > 0.01 or abs(dz) > 0.01:
                        # Initialise current XYZ from FK if needed
                        if self._current_xyz is None:
                            fk = kin.forward_kinematics(self._servo_positions)
                            if fk:
                                self._current_xyz = fk
                            else:
                                self._current_xyz = {"x": 0, "y": 0, "z": kin.L1}

                        nx = self._current_xyz["x"] + dx
                        ny = self._current_xyz["y"] + dy
                        nz = self._current_xyz["z"] + dz

                        wp = self._wrist_pitch_deg
                        result = kin.inverse_kinematics(nx, ny, nz, wp)
                        if result is None:
                            for try_wp in [0, -45, 45, -90, 90, -30, 30,
                                           -60, 60, -120, 120]:
                                result = kin.inverse_kinematics(nx, ny, nz, try_wp)
                                if result is not None:
                                    wp = try_wp
                                    break
                        if result is None:
                            nx, ny, nz = kin.clamp_to_workspace(
                                nx, ny, nz, self._wrist_pitch_deg)
                            result = kin.inverse_kinematics(
                                nx, ny, nz, self._wrist_pitch_deg)
                        if result is not None:
                            for sid, pos in result.items():
                                self._servo_positions[sid] = float(pos)
                            self._current_xyz = {"x": nx, "y": ny, "z": nz}
                            moved = True

                    # Servo 2 (wrist rotation) still direct velocity
                    vel2 = self._servo_velocity.get(2, 0.0)
                    if abs(vel2) >= GAMEPAD_DEADZONE:
                        spd2 = self._servo_speed.get(2, 150.0)
                        d2 = vel2 * spd2 * VELOCITY_LOOP_DT
                        old2 = self._servo_positions[2]
                        new2 = max(0, min(1000, old2 + d2))
                        if int(new2) != int(old2):
                            self._servo_positions[2] = new2
                            moved = True

                else:
                    # --- Normal servo-direct velocity control ---
                    for sid in range(1, 7):
                        vel = self._servo_velocity.get(sid, 0.0)
                        if abs(vel) < GAMEPAD_DEADZONE:
                            continue
                        speed = self._servo_speed.get(sid, 150.0)
                        delta = vel * speed * VELOCITY_LOOP_DT
                        old_pos = self._servo_positions[sid]
                        new_pos = max(0, min(1000, old_pos + delta))
                        if int(new_pos) != int(old_pos):
                            self._servo_positions[sid] = new_pos
                            moved = True

            if moved:
                with self._servo_lock:
                    moves = [(sid, int(self._servo_positions[sid]))
                             for sid in range(1, 7)]
                # Only send to physical arm if GP->Arm enabled
                if self._gamepad_arm_enabled:
                    with self._arm_lock:
                        if self._arm and self._arm.connected:
                            self._arm.move_servos(moves, GAMEPAD_SERVO_DURATION)

            time.sleep(VELOCITY_LOOP_DT)

    # -----------------------------------------------------------------------
    # IK mode
    # -----------------------------------------------------------------------

    def _enable_ik_mode(self):
        """Enable IK mode and initialise arm to home XYZ position."""
        self._ik_mode = True
        kin = self._kinematics
        hx, hy, hz = kin.home_xyz
        result = kin.inverse_kinematics(hx, hy, hz, kin.default_wrist_pitch)
        if result:
            for sid, pos in result.items():
                self._servo_positions[sid] = float(pos)
            self._current_xyz = {"x": hx, "y": hy, "z": hz}
        else:
            fk = kin.forward_kinematics(self._servo_positions)
            if fk:
                self._current_xyz = fk
        self._wrist_pitch_deg = kin.default_wrist_pitch
        self._xyz_velocity = [0.0, 0.0, 0.0]
        logger.info("XYZ (IK) mode ENABLED, pos=%s", self._current_xyz)

    # -----------------------------------------------------------------------
    # Hardware connection with exponential backoff
    # -----------------------------------------------------------------------

    def _try_xarm_connect(self):
        """Background thread: retry xArm connection with exponential backoff."""
        def _loop():
            delay = 2
            while self._running:
                with self._arm_lock:
                    if self._arm and self._arm.connected:
                        self._arm_connected = True
                        return
                if self._arm:
                    try:
                        ok = self._arm.connect()
                    except Exception as e:
                        logger.error("xArm connect error: %s", e)
                        ok = False
                    if ok:
                        logger.info("xArm connected")
                        with self._arm_lock:
                            self._arm_connected = True
                        # Read actual servo positions from hardware
                        try:
                            positions = self._arm.read_all_positions()
                            if positions:
                                with self._servo_lock:
                                    for sid, pos in positions.items():
                                        self._servo_positions[int(sid)] = float(pos)
                                logger.info("xArm positions read: %s",
                                            {k: int(v) for k, v in positions.items()})
                        except Exception as e:
                            logger.warning("Failed to read xArm positions: %s", e)
                        return
                logger.info("xArm connect failed, retry in %ds", delay)
                end = time.time() + delay
                while time.time() < end and self._running:
                    time.sleep(0.5)
                delay = min(delay * 1.5, 30)
        threading.Thread(target=_loop, daemon=True, name="xarm-connect").start()

    # -----------------------------------------------------------------------
    # Start / Stop
    # -----------------------------------------------------------------------

    def start(self):
        """Start background threads (velocity loop, hardware connection)."""
        if self._running:
            return
        self._running = True

        # Start hardware connection in background
        if self._hardware_enabled and self._arm:
            self._try_xarm_connect()

        # Start velocity loop
        self._velocity_running = True
        threading.Thread(target=self._servo_velocity_loop, daemon=True,
                         name="servo-velocity").start()
        logger.info("XArmController started (hardware=%s)", self._hardware_enabled)

    def stop(self):
        """Stop all background threads and disconnect hardware."""
        self._running = False
        self._velocity_running = False

        # Stop local gamepad
        if self._local_gamepad and self._local_gamepad.is_running:
            self._local_gamepad.stop()
        self._local_gamepad_enabled = False
        self._browser_gamepad_enabled = False

        # Disconnect hardware
        if self._arm:
            with self._arm_lock:
                try:
                    self._arm.disconnect()
                except Exception:
                    pass
                self._arm_connected = False
        logger.info("XArmController stopped")

    # -----------------------------------------------------------------------
    # Flask route registration
    # -----------------------------------------------------------------------

    def register_routes(self, app):
        """Register all xArm API routes on the Flask app."""
        bp = Blueprint("xarm_controller", __name__)

        # -- GET /api/xarm/status --
        @bp.route("/api/xarm/status")
        def xarm_status():
            with self._arm_lock:
                arm_connected = self._arm.connected if self._arm else False
            with self._servo_lock:
                servos = dict(self._servo_positions)
            return jsonify({
                "connected": arm_connected,
                "hardware_enabled": self._hardware_enabled,
                "gamepad_enabled": self._gamepad_arm_enabled,
                "servos": {str(k): int(v) for k, v in servos.items()},
                "reverse": {str(k): v for k, v in self._servo_reverse.items()},
                "speed": {str(k): v for k, v in self._servo_speed.items()},
                "ik_mode": self._ik_mode,
                "ik_configured": (self._kinematics.is_configured()
                                  if self._kinematics else False),
                "xyz": self._current_xyz,
                "wrist_pitch_deg": self._wrist_pitch_deg,
                "gamepad_count": self._gamepad_count,
                "local_gamepad_enabled": self._local_gamepad_enabled,
                "browser_gamepad_enabled": self._browser_gamepad_enabled,
            })

        # -- POST /api/xarm/move --
        @bp.route("/api/xarm/move", methods=["POST"])
        def xarm_move():
            data = request.json or {}
            sid = data.get("servo")
            pos = data.get("position")
            dur = data.get("duration", 200)
            if sid is None or pos is None:
                return jsonify({"error": "servo and position required"}), 400
            sid, pos, dur = int(sid), int(pos), int(dur)
            pos = max(0, min(1000, pos))
            with self._arm_lock:
                if self._arm and self._arm.connected:
                    self._arm.move_servo(sid, pos, dur)
                elif self._hardware_enabled:
                    return jsonify({"error": "xArm not connected"}), 503
            with self._servo_lock:
                self._servo_positions[sid] = float(pos)
            return jsonify({"ok": True, "servo": sid, "position": pos})

        # -- POST /api/xarm/toggle --
        @bp.route("/api/xarm/toggle", methods=["POST"])
        def xarm_toggle():
            data = request.json or {}
            self._gamepad_arm_enabled = bool(
                data.get("enabled", not self._gamepad_arm_enabled))
            return jsonify({"enabled": self._gamepad_arm_enabled})

        # -- POST /api/xarm/xyz-mode --
        @bp.route("/api/xarm/xyz-mode", methods=["POST"])
        def xarm_xyz_mode():
            data = request.json or {}
            enabled = data.get("enabled", not self._ik_mode)
            with self._servo_lock:
                if enabled and self._kinematics and self._kinematics.is_configured():
                    self._enable_ik_mode()
                else:
                    self._ik_mode = False
                    self._xyz_velocity = [0.0, 0.0, 0.0]
                    for sid in range(1, 7):
                        self._servo_velocity[sid] = 0.0
            return jsonify({
                "enabled": self._ik_mode,
                "xyz": self._current_xyz,
            })

        # -- POST /api/xarm/speed --
        @bp.route("/api/xarm/speed", methods=["POST"])
        def xarm_speed():
            data = request.json or {}
            sid = data.get("servo")
            speed = data.get("speed", 150)
            if sid is not None:
                self._servo_speed[int(sid)] = max(10, min(2000, float(speed)))
            return jsonify({"speed": {str(k): v
                                      for k, v in self._servo_speed.items()}})

        # -- POST /api/xarm/reverse --
        @bp.route("/api/xarm/reverse", methods=["POST"])
        def xarm_reverse():
            data = request.json or {}
            sid = data.get("servo")
            rev = data.get("reverse", False)
            if sid is not None:
                sid = int(sid)
                self._servo_reverse[sid] = bool(rev)
                if self._kinematics and sid in self._kinematics.calibration:
                    self._kinematics.calibration[sid]["direction"] = -1 if rev else 1
                    logger.info("Servo %d IK direction set to %d",
                                sid, self._kinematics.calibration[sid]["direction"])
            return jsonify({"reverse": {str(k): v
                                        for k, v in self._servo_reverse.items()}})

        # -- POST /api/xarm/read-positions --
        @bp.route("/api/xarm/read-positions", methods=["POST"])
        def xarm_read_positions():
            with self._arm_lock:
                if not self._arm or not self._arm.connected:
                    return jsonify({"error": "xArm not connected"}), 503
                try:
                    positions = self._arm.read_all_positions()
                    if positions:
                        with self._servo_lock:
                            for sid, pos in positions.items():
                                self._servo_positions[int(sid)] = float(pos)
                        return jsonify({"ok": True, "positions": {
                            str(k): int(v) for k, v in positions.items()}})
                    return jsonify({"error": "no data"}), 500
                except Exception as e:
                    return jsonify({"error": str(e)}), 500

        # -- POST /api/xarm/home --
        @bp.route("/api/xarm/home", methods=["POST"])
        def xarm_home():
            data = request.json or {}
            dur = data.get("duration", 1500)
            with self._arm_lock:
                if self._arm and self._arm.connected:
                    self._arm.home(int(dur))
            with self._servo_lock:
                for sid in range(1, 7):
                    self._servo_positions[sid] = 500.0
            return jsonify({"ok": True, "home": True})

        # -- GET/POST /api/xarm/ik-config --
        @bp.route("/api/xarm/ik-config", methods=["GET", "POST"])
        def xarm_ik_config():
            cfg_path = os.path.join(self._config_dir, "config.json")
            if request.method == "POST":
                data = request.json or {}
                try:
                    with open(cfg_path) as f:
                        cfg = json.load(f)
                except Exception:
                    cfg = {}
                if "link_lengths_mm" in data:
                    cfg["link_lengths_mm"] = data["link_lengths_mm"]
                with open(cfg_path, "w") as f:
                    json.dump(cfg, f, indent=4)
                if self._kinematics:
                    self._kinematics.load_config(cfg_path)
                return jsonify({"ok": True, **cfg})
            else:
                try:
                    with open(cfg_path) as f:
                        cfg = json.load(f)
                    return jsonify(cfg)
                except Exception:
                    return jsonify({})

        # -- POST /api/xarm/ik-goto --
        @bp.route("/api/xarm/ik-goto", methods=["POST"])
        def xarm_ik_goto():
            if not self._kinematics or not self._kinematics.is_configured():
                return jsonify({"error": "IK not configured"}), 503
            data = request.json or {}
            x = float(data.get("x", 0))
            y = float(data.get("y", 0))
            z = float(data.get("z", 0))
            wp = float(data.get("wrist_pitch_deg", self._wrist_pitch_deg))
            result = self._kinematics.inverse_kinematics(x, y, z, wp)
            if result is None:
                for try_wp in [0, -45, 45, -90, 90, -30, 30, -60, 60]:
                    result = self._kinematics.inverse_kinematics(x, y, z, try_wp)
                    if result is not None:
                        wp = try_wp
                        break
            if result is None:
                cx, cy, cz = self._kinematics.clamp_to_workspace(x, y, z, wp)
                result = self._kinematics.inverse_kinematics(cx, cy, cz, wp)
                if result is None:
                    return jsonify({"error": "unreachable"}), 400
                x, y, z = cx, cy, cz
            with self._servo_lock:
                for sid, pos in result.items():
                    self._servo_positions[sid] = float(pos)
                self._current_xyz = {"x": x, "y": y, "z": z}
            if self._gamepad_arm_enabled:
                moves = [(sid, int(pos)) for sid, pos in result.items()]
                with self._arm_lock:
                    if self._arm and self._arm.connected:
                        self._arm.move_servos(moves, 100)
            return jsonify({"ok": True, "x": x, "y": y, "z": z,
                            "servos": result})

        # -- GET /api/xarm/local-gamepad/list --
        @bp.route("/api/xarm/local-gamepad/list")
        def local_gamepad_list():
            if not self._list_gamepads_fn:
                return jsonify({"devices": [], "error": "not available"})
            devices = self._list_gamepads_fn()
            active = None
            if self._local_gamepad and self._local_gamepad.is_running:
                active = self._local_gamepad.device_path
            return jsonify({"devices": devices, "active": active})

        # -- POST /api/xarm/local-gamepad/start --
        @bp.route("/api/xarm/local-gamepad/start", methods=["POST"])
        def local_gamepad_start():
            if not self._local_gamepad_class:
                return jsonify({"error": "local gamepad not available"}), 503
            data = request.json or {}
            path = data.get("path", "/dev/input/js0")
            if self._local_gamepad and self._local_gamepad.is_running:
                self._local_gamepad.stop()
            self._local_gamepad = self._local_gamepad_class(
                callback=self._local_gamepad_callback, rate_hz=50)
            ok = self._local_gamepad.start(path)
            if ok:
                self._local_gamepad_enabled = True
                self._load_gamepad_profile(self._local_gamepad.device_name)
                # Ensure velocity loop is running
                if not self._velocity_running:
                    self._running = True
                    self._velocity_running = True
                    threading.Thread(target=self._servo_velocity_loop,
                                     daemon=True, name="servo-velocity").start()
                return jsonify({"ok": True, "path": path,
                                "name": self._local_gamepad.device_name})
            return jsonify({"error": f"Cannot open {path}"}), 400

        # -- POST /api/xarm/local-gamepad/stop --
        @bp.route("/api/xarm/local-gamepad/stop", methods=["POST"])
        def local_gamepad_stop():
            if self._local_gamepad and self._local_gamepad.is_running:
                self._local_gamepad.stop()
            self._local_gamepad_enabled = False
            return jsonify({"ok": True})

        # -- GET /api/xarm/local-gamepad/status --
        @bp.route("/api/xarm/local-gamepad/status")
        def local_gamepad_status():
            if not self._local_gamepad or not self._local_gamepad.is_running:
                return jsonify({"running": False, "path": None, "name": ""})
            return jsonify({
                "running": True,
                "path": self._local_gamepad.device_path,
                "name": self._local_gamepad.device_name,
                "state": self._local_gamepad.get_state(),
            })

        # -- POST /api/xarm/browser-gamepad/start --
        @bp.route("/api/xarm/browser-gamepad/start", methods=["POST"])
        def browser_gamepad_start():
            data = request.json or {}
            device_name = data.get("name", "")
            self._browser_gamepad_enabled = True
            if device_name:
                self._load_gamepad_profile(device_name)
            # Ensure velocity loop is running
            if not self._velocity_running:
                self._running = True
                self._velocity_running = True
                threading.Thread(target=self._servo_velocity_loop,
                                 daemon=True, name="servo-velocity").start()
            sm = self._stick_map
            return jsonify({"ok": True, "stick_map": sm})

        # -- POST /api/xarm/browser-gamepad/stop --
        @bp.route("/api/xarm/browser-gamepad/stop", methods=["POST"])
        def browser_gamepad_stop():
            self._browser_gamepad_enabled = False
            return jsonify({"ok": True})

        # -- POST /api/xarm/browser-gamepad/input --
        @bp.route("/api/xarm/browser-gamepad/input", methods=["POST"])
        def browser_gamepad_input():
            data = request.json or {}
            self._handle_gamepad(data)
            return jsonify({"ok": True})

        # -- GET /simulation --
        @bp.route("/simulation")
        def xarm_simulation():
            sim_path = os.path.join(self._config_dir, "simulation.html")
            if os.path.exists(sim_path):
                return send_file(sim_path)
            return "simulation.html not found", 404

        app.register_blueprint(bp)
