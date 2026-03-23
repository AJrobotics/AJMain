"""
XBee Responder Module — R1 (and future robots).
Replaces rpi_xbee.py's Tkinter display with web-based agent dashboard.
Runs headless (no monitor required).

Handles:
  - Heartbeat: "All Good?" -> replies "R1!"
  - JSON commands: gamepad, chat, GPIO, xArm, sensors
  - Binary command packets (0x02 movement)
  - Gamepad oscilloscope data buffering for web display
  - xArm servo control (optional, graceful fallback)
  - GPIO control (optional, graceful fallback)
"""

import glob as _glob
import json
import logging
import os
import struct
import threading
import time
from datetime import datetime

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
XBEE_BAUD = 115200
XBEE_PORT_CANDIDATES = [
    "/dev/ttyUSB0", "/dev/ttyUSB1",
    "/dev/ttyACM0", "/dev/ttyACM1",
    "/dev/serial0",
]

HEARTBEAT_MSG = "All Good?"
DEVICE_REPLY = "R1!"
STATUS_INTERVAL = 30
PKT_STATUS = 0x01
SCOPE_MAX = 80
ALLOWED_PINS = {17, 27, 22}

# Gamepad -> xArm mapping
GAMEPAD_AXIS_MAP = {0: 2, 1: 3, 2: 6, 3: 4}
GAMEPAD_DEADZONE = 0.10
VELOCITY_LOOP_HZ = 50          # servo update rate
VELOCITY_LOOP_DT = 1.0 / VELOCITY_LOOP_HZ
GAMEPAD_SERVO_DURATION = 40    # ms per move command (smooth small steps)

# ACK intervals
ACK_INTERVAL_ACTIVE = 1
ACK_INTERVAL_IDLE = 5

# ---------------------------------------------------------------------------
# Optional hardware (graceful fallback)
# ---------------------------------------------------------------------------
_gpio_available = False
try:
    import RPi.GPIO as GPIO
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    _gpio_available = True
    logger.info("GPIO available")
except Exception:
    logger.info("GPIO not available (not on RPi or no RPi.GPIO)")

_xarm_class = None
try:
    import sys as _sys
    _goodson = "/home/dream/GoodSon"
    if _goodson not in _sys.path:
        _sys.path.insert(0, _goodson)
    from xarm import XArm as _XArm
    _xarm_class = _XArm
    logger.info("xArm library available")
except Exception:
    logger.info("xArm library not available")


def _detect_xbee_port():
    """Try candidate ports and /dev/ttyUSB* glob; return first that exists."""
    candidates = list(XBEE_PORT_CANDIDATES)
    for pattern in ("/dev/ttyUSB*", "/dev/ttyACM*"):
        for path in sorted(_glob.glob(pattern)):
            if path not in candidates:
                candidates.append(path)
    for port in candidates:
        if os.path.exists(port):
            return port
    return None


class XbeeResponderModule:
    name = "xbee_responder"

    def __init__(self):
        self._device = None
        self._running = False
        self._thread = None
        self._lock = threading.Lock()
        self._error_msg = ""
        self._xbee_port = None

        # XBee stats
        self._heartbeats_received = 0
        self._replies_sent = 0
        self._status_packets_sent = 0
        self._last_heartbeat_from = ""
        self._last_rssi = None
        self._last_time = ""
        self._history = []

        # Robot status (GPS, battery)
        self._latitude = 0.0
        self._longitude = 0.0
        self._battery_voltage = 0.0
        self._robot_id = 0
        self._status_flags = 0
        self._status_interval = STATUS_INTERVAL
        self._send_status = False

        # Gamepad oscilloscope
        self._scope_channels = {}
        self._scope_cursor = 0
        self._scope_started = False
        self._scope_channel_order = []
        self._gamepad_count = 0
        self._stick_left = [0.0, 0.0]
        self._stick_right = [0.0, 0.0]
        self._latest_gamepad = None  # latest {axes, buttons, hats}

        # Chat messages (separate display from history)
        self._chat_messages = []

        # GPIO
        self._gpio_states = {pin: 0 for pin in ALLOWED_PINS}
        if _gpio_available:
            for pin in ALLOWED_PINS:
                try:
                    GPIO.setup(pin, GPIO.OUT, initial=GPIO.LOW)
                except Exception:
                    pass

        # xArm
        self._arm = None
        self._arm_connected = False
        self._arm_lock = threading.Lock()
        self._gamepad_arm_enabled = False
        self._servo_positions = {sid: 500.0 for sid in range(1, 7)}
        self._servo_lock = threading.Lock()
        self._gripper_open = False

        # Stick axis mapping (updated by gamepad_config from sender)
        self._stick_map = {"lx": "0", "ly": "1", "rx": "2", "ry": "3"}

        # Servo reverse flags (toggled from dashboard UI)
        self._servo_reverse = {sid: False for sid in range(1, 7)}

        # Per-servo speed factor (units/sec at full stick deflection)
        self._servo_speed = {sid: 150.0 for sid in range(1, 7)}

        # Velocity control: latest axis values as velocity targets (-1 to +1)
        self._servo_velocity = {sid: 0.0 for sid in range(1, 7)}
        self._velocity_running = False

        # Smooth home: target positions (None = not homing)
        self._home_target = None

        # Servo coupling: axis -> [{servo, reverse}, ...]
        self._servo_coupling = {}

        # IK / XYZ mode
        self._ik_mode = False
        self._xyz_velocity = [0.0, 0.0, 0.0]   # normalised dx, dy, dz
        self._current_xyz = None                 # {x, y, z} in mm
        self._wrist_pitch_deg = -90.0            # default: gripper down
        self._kinematics = None
        self._init_kinematics()

        # Local gamepad reader
        self._local_gamepad = None
        self._local_gamepad_enabled = False
        self._init_local_gamepad()

        # Ack sender
        self._last_remote = None
        self._last_rx_time = 0.0
        self._ack_running = False

    def _init_kinematics(self):
        """Load IK config if available."""
        try:
            from agent_modules.xarm_kinematics import XArmKinematics
            cfg = os.path.join(os.path.dirname(__file__), "..",
                               "configs", "xarm_kinematics.json")
            self._kinematics = XArmKinematics(cfg)
            if self._kinematics.is_configured():
                logger.info("IK solver loaded (L2=%.1f L3=%.1f)",
                            self._kinematics.L2, self._kinematics.L3)
                self._wrist_pitch_deg = self._kinematics.default_wrist_pitch
            else:
                logger.info("IK solver loaded but link lengths not set")
        except Exception as e:
            logger.warning("IK solver not available: %s", e)
            self._kinematics = None

    def _init_local_gamepad(self):
        """Initialise local gamepad reader if available."""
        try:
            from agent_modules.local_gamepad import LocalGamepadReader, list_gamepads
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
            self._local_gamepad_class = None
            self._list_gamepads_fn = None

    def _load_gamepad_profile(self, device_name):
        """Load gamepad profile from gamepads.json based on device name."""
        try:
            cfg_path = os.path.join(os.path.dirname(__file__), "..",
                                    "configs", "gamepads.json")
            with open(cfg_path) as f:
                cfg = json.load(f)
            gamepads = cfg.get("gamepads", {})
            # Match by partial name (normalize whitespace)
            matched = None
            dev_norm = " ".join(device_name.lower().split())
            dev_words = set(dev_norm.split())
            for key, profile in gamepads.items():
                pname = profile.get("name", key)
                key_norm = " ".join(key.lower().split())
                pname_norm = " ".join(pname.lower().split())
                # Extract alpha-only words (skip vendor:product IDs)
                key_words = {w for w in key_norm.split()
                             if len(w) > 2 and w.isalpha()}
                # Match if all key words appear in device name
                if key_words and key_words.issubset(dev_words):
                    matched = profile
                    logger.info("Gamepad profile matched: %s", key)
                    break
                # Also try substring matches
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
            # Apply servo map (axis -> servo)
            smap = matched.get("servo_map")
            if smap:
                global GAMEPAD_AXIS_MAP
                GAMEPAD_AXIS_MAP = {int(k): int(v) for k, v in smap.items()}
                logger.info("Servo map set: %s", GAMEPAD_AXIS_MAP)
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

    def _local_gamepad_callback(self, state):
        """Called by LocalGamepadReader at 50Hz with gamepad state."""
        # Feed into the same pipeline as XBee gamepad data
        self._handle_gamepad(None, state)

    @property
    def is_running(self):
        return self._running and self._thread is not None and self._thread.is_alive()

    def _read_rssi(self):
        try:
            db = self._device.get_parameter("DB")
            return -int.from_bytes(db, "big")
        except Exception:
            return None

    # -------------------------------------------------------------------
    # Chat log helper
    # -------------------------------------------------------------------
    def _add_chat(self, text):
        ts = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            self._chat_messages.append({"time": ts, "text": text})
            if len(self._chat_messages) > 200:
                self._chat_messages = self._chat_messages[-200:]

    # -------------------------------------------------------------------
    # XBee receive callback
    # -------------------------------------------------------------------
    def _on_receive(self, xbee_message):
        try:
            addr = str(xbee_message.remote_device.get_64bit_addr())
            raw = xbee_message.data
            text = raw.decode(errors="replace").strip()
            rssi = self._read_rssi()
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            self._last_rssi = rssi

            event = {
                "time": now,
                "from": addr[-8:],
                "msg": text[:100],
                "rssi": rssi,
                "reply": None,
            }

            # Heartbeat
            if text == HEARTBEAT_MSG and self._device and self._device.is_open():
                self._device.send_data_broadcast(DEVICE_REPLY.encode())
                self._heartbeats_received += 1
                self._replies_sent += 1
                self._last_heartbeat_from = addr
                self._last_time = now
                event["reply"] = DEVICE_REPLY
                self._add_chat(f"[RX] Heartbeat: {HEARTBEAT_MSG}")
                self._add_chat(f"[TX] {DEVICE_REPLY}")
                logger.info("Heartbeat from ...%s -> replied %s (RSSI: %s)",
                            addr[-8:], DEVICE_REPLY, rssi)
            else:
                # Try JSON command
                try:
                    data = json.loads(text)
                    # Support compact gamepad format: {t:'gp', r:'R1', a:[...], b:int}
                    if data.get("t") == "gp":
                        axes_list = data.get("a", [])
                        bmask = data.get("b", 0)
                        axes = {str(i): v for i, v in enumerate(axes_list)}
                        buttons = {}
                        for i in range(16):
                            buttons[str(i)] = 1 if (bmask & (1 << i)) else 0
                        msg_type = "gamepad"
                        payload = {"axes": axes, "buttons": buttons}
                    else:
                        msg_type = data.get("type")
                        payload = data.get("payload", {})
                    short = json.dumps(payload, separators=(",", ":"))[:80]
                    self._add_chat(f"[RX {msg_type}] {short}")
                    self._dispatch_json(msg_type, payload,
                                        xbee_message.remote_device)
                except json.JSONDecodeError:
                    # Check binary command (0x02)
                    if len(raw) >= 1 and raw[0] == 0x02:
                        self._handle_binary_command(raw, addr)
                    else:
                        self._add_chat(f"[RX TEXT] {text}")
                        logger.info("RECV from ...%s: %s (RSSI: %s)",
                                    addr[-8:], text, rssi)

            with self._lock:
                self._history.append(event)
                if len(self._history) > 100:
                    self._history = self._history[-100:]

        except Exception as e:
            logger.error("XBee receive error: %s", e)

    # -------------------------------------------------------------------
    # JSON command dispatch
    # -------------------------------------------------------------------
    def _dispatch_json(self, msg_type, payload, remote_device):
        handlers = {
            "chat": self._handle_chat,
            "gamepad": self._handle_gamepad,
            "gpio_set": self._handle_gpio_set,
            "gpio_read": self._handle_gpio_read,
            "sensor_read": self._handle_sensor_read,
            "xarm_move": self._handle_xarm_move,
            "xarm_moves": self._handle_xarm_moves,
            "xarm_read": self._handle_xarm_read,
            "xarm_home": self._handle_xarm_home,
            "xarm_off": self._handle_xarm_off,
            "xarm_battery": self._handle_xarm_battery,
            "xarm_gamepad_toggle": self._handle_xarm_gamepad_toggle,
            "xarm_status": self._handle_xarm_status,
            "gamepad_config": self._handle_gamepad_config,
            "xyz_mode": self._handle_xyz_mode,
        }
        handler = handlers.get(msg_type)
        if handler:
            handler(remote_device, payload)
        else:
            logger.warning("Unknown message type: %s", msg_type)

    def _send_response(self, remote_device, msg):
        """Unicast JSON response back to sender."""
        if not self._device or not self._device.is_open():
            return
        payload = json.dumps(msg, separators=(",", ":"))
        if len(payload.encode()) > 256:
            logger.error("Response too large (%d bytes), dropping", len(payload.encode()))
            return
        try:
            self._device.send_data(remote_device, payload.encode())
        except Exception as e:
            logger.error("Send response failed: %s", e)

    # -------------------------------------------------------------------
    # Command handlers
    # -------------------------------------------------------------------
    def _handle_chat(self, remote, payload):
        text = payload.get("text", "")
        logger.info("Chat: %s", text)
        self._send_response(remote, {
            "type": "chat",
            "payload": {"text": f"[RPi] {text}"},
        })

    def _handle_gamepad(self, remote, payload):
        self._gamepad_count += 1
        self._last_remote = remote
        self._last_rx_time = time.time()

        axes = payload.get("axes", {})
        buttons = payload.get("buttons", {})
        hats = payload.get("hats", {})

        # Store latest gamepad data for client-side rendering
        self._latest_gamepad = {"axes": axes, "buttons": buttons, "hats": hats}

        # Update scope buffer
        self._push_scope(axes, buttons, hats)

        # Update stick positions using dynamic mapping
        sm = self._stick_map
        self._stick_left = [
            float(axes.get(sm["lx"], axes.get(str(sm["lx"]), 0.0))),
            float(axes.get(sm["ly"], axes.get(str(sm["ly"]), 0.0))),
        ]
        self._stick_right = [
            float(axes.get(sm["rx"], axes.get(str(sm["rx"]), 0.0))),
            float(axes.get(sm["ry"], axes.get(str(sm["ry"]), 0.0))),
        ]

        # Gamepad -> xArm (or simulation)
        if self._gamepad_arm_enabled or self._local_gamepad_enabled:
            self._gamepad_to_xarm(axes, buttons)

    def _push_scope(self, axes, buttons, hats):
        """Push one sample into the oscilloscope ring buffer."""
        with self._lock:
            for key, val in sorted(axes.items(), key=lambda x: str(x[0])):
                ch = f"A{key}"
                if ch not in self._scope_channels:
                    self._scope_channels[ch] = [None] * SCOPE_MAX
                    self._update_channel_order()
                self._scope_channels[ch][self._scope_cursor] = float(val)

            for key, val in sorted(hats.items(), key=lambda x: str(x[0])):
                chx, chy = f"H{key}x", f"H{key}y"
                for ch in (chx, chy):
                    if ch not in self._scope_channels:
                        self._scope_channels[ch] = [None] * SCOPE_MAX
                        self._update_channel_order()
                if isinstance(val, (list, tuple)):
                    self._scope_channels[chx][self._scope_cursor] = float(val[0])
                    self._scope_channels[chy][self._scope_cursor] = float(val[1])
                else:
                    self._scope_channels[chx][self._scope_cursor] = float(val)

            for key, val in sorted(buttons.items(), key=lambda x: str(x[0])):
                ch = f"B{key}"
                if ch not in self._scope_channels:
                    self._scope_channels[ch] = [None] * SCOPE_MAX
                    self._update_channel_order()
                self._scope_channels[ch][self._scope_cursor] = int(val)

            self._scope_cursor = (self._scope_cursor + 1) % SCOPE_MAX
            self._scope_started = True

    def _update_channel_order(self):
        def sort_key(k):
            prefix = 0 if k.startswith("A") else (1 if k.startswith("H") else 2)
            num = ""
            for c in k[1:]:
                if c.isdigit():
                    num += c
                else:
                    break
            return (prefix, int(num) if num else 0, k)
        self._scope_channel_order = sorted(self._scope_channels.keys(),
                                           key=sort_key)

    def _gamepad_to_xarm(self, axes, buttons):
        """Store axis values as velocity targets. The velocity loop moves servos."""
        sm = self._stick_map

        with self._servo_lock:
            if self._ik_mode and self._kinematics and self._kinematics.is_configured():
                # --- XYZ mode: sticks -> Cartesian velocity ---
                rx_val = float(axes.get(sm["rx"], axes.get(str(sm["rx"]), 0.0)))
                ry_val = float(axes.get(sm["ry"], axes.get(str(sm["ry"]), 0.0)))
                lx_val = float(axes.get(sm["lx"], axes.get(str(sm["lx"]), 0.0)))
                ly_val = float(axes.get(sm["ly"], axes.get(str(sm["ly"]), 0.0)))

                # Apply deadzone
                if abs(rx_val) < GAMEPAD_DEADZONE: rx_val = 0.0
                if abs(ry_val) < GAMEPAD_DEADZONE: ry_val = 0.0
                if abs(lx_val) < GAMEPAD_DEADZONE: lx_val = 0.0
                if abs(ly_val) < GAMEPAD_DEADZONE: ly_val = 0.0

                # Left stick X -> X (left/right)
                # Left stick Y -> Y (forward/back, along +Y axis)
                # Right stick Y -> Z (up/down)
                self._xyz_velocity = [lx_val, -ly_val, -ry_val]

                # Right stick X -> wrist rotation (servo 2, direct)
                if abs(rx_val) >= GAMEPAD_DEADZONE:
                    self._servo_velocity[2] = rx_val
                else:
                    self._servo_velocity[2] = 0.0

                # LB/RB -> wrist pitch adjustment
                lb = int(buttons.get(4, buttons.get("4", 0)))
                rb = int(buttons.get(5, buttons.get("5", 0)))
                if lb or rb:
                    self._wrist_pitch_deg += float(rb - lb) * 2.0
                    self._wrist_pitch_deg = max(-180, min(180, self._wrist_pitch_deg))

            else:
                # --- Servo-direct mode ---
                for axis_key, servo_id in GAMEPAD_AXIS_MAP.items():
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
                    # Home to configured XYZ position
                    hx, hy, hz = self._kinematics.home_xyz
                    result = self._kinematics.inverse_kinematics(
                        hx, hy, hz, self._wrist_pitch_deg)
                    if result:
                        self._home_target = dict(result)
                        self._home_target[1] = 500.0  # gripper center
                        self._home_target[2] = 500.0  # wrist rotation center
                else:
                    self._home_target = {sid: 500.0 for sid in range(1, 7)}
                for sid in range(1, 7):
                    self._servo_velocity[sid] = 0.0
                self._xyz_velocity = [0.0, 0.0, 0.0]

    def _servo_velocity_loop(self):
        """Background loop: smoothly move servos based on velocity targets at 50Hz."""
        logger.info("Servo velocity loop started (%d Hz)", VELOCITY_LOOP_HZ)
        while self._velocity_running and self._running:
            sim_only = self._local_gamepad_enabled and not self._gamepad_arm_enabled
            if not self._gamepad_arm_enabled and not sim_only:
                time.sleep(0.1)
                continue

            moved = False
            with self._servo_lock:
                # Smooth home: move toward target positions
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
                    speed = kin.xyz_speed  # mm/s at full stick

                    dx = self._xyz_velocity[0] * speed * VELOCITY_LOOP_DT
                    dy = self._xyz_velocity[1] * speed * VELOCITY_LOOP_DT
                    dz = self._xyz_velocity[2] * speed * VELOCITY_LOOP_DT

                    if abs(dx) > 0.01 or abs(dy) > 0.01 or abs(dz) > 0.01:
                        # initialise current XYZ from FK if needed
                        if self._current_xyz is None:
                            fk = kin.forward_kinematics(self._servo_positions)
                            if fk:
                                self._current_xyz = fk
                            else:
                                self._current_xyz = {"x": 0, "y": 0,
                                                     "z": kin.L1}

                        nx = self._current_xyz["x"] + dx
                        ny = self._current_xyz["y"] + dy
                        nz = self._current_xyz["z"] + dz

                        # Try IK with current wrist pitch, then try
                        # auto-adjusting pitch if unreachable
                        wp = self._wrist_pitch_deg
                        result = kin.inverse_kinematics(nx, ny, nz, wp)
                        if result is None:
                            # Try multiple wrist pitches to find reachable
                            for try_wp in [0, -45, 45, -90, 90, -30, 30,
                                           -60, 60, -120, 120]:
                                result = kin.inverse_kinematics(
                                    nx, ny, nz, try_wp)
                                if result is not None:
                                    wp = try_wp
                                    break
                        if result is None:
                            # Still unreachable, clamp position
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

    def _handle_gpio_set(self, remote, payload):
        pin = payload.get("pin")
        state = payload.get("state")
        if pin not in ALLOWED_PINS:
            return
        state = 1 if state else 0
        if _gpio_available:
            GPIO.output(pin, GPIO.HIGH if state else GPIO.LOW)
        self._gpio_states[pin] = state
        logger.info("GPIO %d -> %s", pin, "HIGH" if state else "LOW")
        self._send_response(remote, {
            "type": "gpio_state",
            "payload": {"pin": pin, "state": state},
        })

    def _handle_gpio_read(self, remote, payload):
        pin = payload.get("pin")
        if pin not in ALLOWED_PINS:
            return
        self._send_response(remote, {
            "type": "gpio_state",
            "payload": {"pin": pin, "state": self._gpio_states.get(pin, 0)},
        })

    def _handle_sensor_read(self, remote, _payload):
        self._send_response(remote, {
            "type": "sensor_data",
            "payload": {"temperature": 22.5, "humidity": 48.0},
        })

    def _handle_xarm_move(self, remote, payload):
        sid = payload.get("servo")
        pos = payload.get("position")
        dur = payload.get("duration", 500)
        if sid is None or pos is None:
            return
        with self._arm_lock:
            if not self._arm or not self._arm.connected:
                self._send_response(remote, {
                    "type": "xarm_error",
                    "payload": {"error": "not connected"},
                })
                return
            self._arm.move_servo(int(sid), int(pos), int(dur))
        with self._servo_lock:
            self._servo_positions[int(sid)] = float(pos)
        self._send_response(remote, {
            "type": "xarm_moved",
            "payload": {"servo": sid, "position": pos},
        })

    def _handle_xarm_moves(self, remote, payload):
        moves_raw = payload.get("moves", [])
        dur = payload.get("duration", 500)
        with self._arm_lock:
            if not self._arm or not self._arm.connected:
                self._send_response(remote, {
                    "type": "xarm_error",
                    "payload": {"error": "not connected"},
                })
                return
            moves = [(int(m[0]), int(m[1])) for m in moves_raw]
            self._arm.move_servos(moves, int(dur))
        with self._servo_lock:
            for sid, pos in moves:
                self._servo_positions[sid] = float(pos)
        self._send_response(remote, {
            "type": "xarm_moved",
            "payload": {"moves": moves_raw},
        })

    def _handle_xarm_read(self, remote, payload):
        sid = payload.get("servo")
        with self._arm_lock:
            if not self._arm or not self._arm.connected:
                self._send_response(remote, {
                    "type": "xarm_error",
                    "payload": {"error": "not connected"},
                })
                return
            if sid is not None:
                pos = self._arm.read_position(int(sid))
                self._send_response(remote, {
                    "type": "xarm_positions",
                    "payload": {"positions": {str(sid): pos}},
                })
            else:
                positions = self._arm.read_all_positions()
                self._send_response(remote, {
                    "type": "xarm_positions",
                    "payload": {
                        "positions": {str(k): v for k, v in positions.items()},
                    },
                })

    def _handle_xarm_home(self, remote, payload):
        dur = payload.get("duration", 1500)
        with self._arm_lock:
            if not self._arm or not self._arm.connected:
                self._send_response(remote, {
                    "type": "xarm_error",
                    "payload": {"error": "not connected"},
                })
                return
            self._arm.home(int(dur))
        with self._servo_lock:
            for sid in range(1, 7):
                self._servo_positions[sid] = 500.0
        self._send_response(remote, {
            "type": "xarm_moved",
            "payload": {"home": True},
        })

    def _handle_xarm_off(self, remote, payload):
        sid = payload.get("servo")
        with self._arm_lock:
            if not self._arm or not self._arm.connected:
                self._send_response(remote, {
                    "type": "xarm_error",
                    "payload": {"error": "not connected"},
                })
                return
            if sid is not None:
                self._arm.servo_off(int(sid))
            else:
                self._arm.all_servos_off()
        self._send_response(remote, {
            "type": "xarm_off",
            "payload": {"servo": sid or "all"},
        })

    def _handle_xarm_battery(self, remote, _payload):
        with self._arm_lock:
            if not self._arm or not self._arm.connected:
                self._send_response(remote, {
                    "type": "xarm_error",
                    "payload": {"error": "not connected"},
                })
                return
            mv = self._arm.get_battery_voltage()
        self._send_response(remote, {
            "type": "xarm_battery",
            "payload": {"millivolts": mv},
        })

    def _handle_xarm_gamepad_toggle(self, remote, payload):
        self._gamepad_arm_enabled = bool(
            payload.get("enabled", not self._gamepad_arm_enabled))
        logger.info("Gamepad->xArm: %s",
                     "ON" if self._gamepad_arm_enabled else "OFF")
        self._send_response(remote, {
            "type": "xarm_gamepad_state",
            "payload": {"enabled": self._gamepad_arm_enabled},
        })

    def _handle_xarm_status(self, remote, _payload):
        with self._arm_lock:
            connected = self._arm.connected if self._arm else False
        self._send_response(remote, {
            "type": "xarm_status",
            "payload": {
                "connected": connected,
                "gamepad_enabled": self._gamepad_arm_enabled,
            },
        })

    def _handle_gamepad_config(self, remote, payload):
        """Receive stick axis mapping and coupling from sender."""
        stick_map = payload.get("stick_map")
        if stick_map:
            self._stick_map = {k: str(v) for k, v in stick_map.items()}
            logger.info("Stick map updated: %s", self._stick_map)
        coupling = payload.get("coupling")
        if coupling:
            self._servo_coupling = coupling
            logger.info("Servo coupling updated: %s", self._servo_coupling)
        self._send_response(remote, {
            "type": "gamepad_config_ack",
            "payload": {"stick_map": self._stick_map,
                        "coupling": self._servo_coupling},
        })

    def _enable_ik_mode(self):
        """Enable IK mode and initialise arm to home XYZ position."""
        self._ik_mode = True
        kin = self._kinematics
        # Move to home XYZ (along +Y)
        hx, hy, hz = kin.home_xyz
        result = kin.inverse_kinematics(hx, hy, hz, kin.default_wrist_pitch)
        if result:
            for sid, pos in result.items():
                self._servo_positions[sid] = float(pos)
            self._current_xyz = {"x": hx, "y": hy, "z": hz}
        else:
            # Fallback: read current position
            fk = kin.forward_kinematics(self._servo_positions)
            if fk:
                self._current_xyz = fk
        self._wrist_pitch_deg = kin.default_wrist_pitch
        self._xyz_velocity = [0.0, 0.0, 0.0]
        logger.info("XYZ (IK) mode ENABLED, pos=%s", self._current_xyz)

    def _handle_xyz_mode(self, remote, payload):
        """Toggle XYZ (IK) mode from gamepad sender."""
        enabled = payload.get("enabled", not self._ik_mode)
        with self._servo_lock:
            if enabled and self._kinematics and self._kinematics.is_configured():
                self._enable_ik_mode()
            else:
                self._ik_mode = False
                self._xyz_velocity = [0.0, 0.0, 0.0]
                for sid in range(1, 7):
                    self._servo_velocity[sid] = 0.0
                logger.info("XYZ (IK) mode DISABLED")
        self._send_response(remote, {
            "type": "xyz_mode_ack",
            "payload": {"enabled": self._ik_mode},
        })

    # -------------------------------------------------------------------
    # Binary command (0x02)
    # -------------------------------------------------------------------
    def _handle_binary_command(self, data, addr):
        if len(data) < 10:
            return
        try:
            _, linear_x, angular_z, flags = struct.unpack('<BffB', data[:10])
            logger.info("CMD from ...%s: lin=%.2f ang=%.2f flags=0x%02X",
                        addr[-8:], linear_x, angular_z, flags)
            self._add_chat(
                f"[CMD] lin={linear_x:.2f} ang={angular_z:.2f} "
                f"flags=0x{flags:02X}")
        except struct.error as e:
            logger.error("Command parse error: %s", e)

    # -------------------------------------------------------------------
    # Status packet broadcast
    # -------------------------------------------------------------------
    def _send_status_packet(self):
        if not self._device or not self._device.is_open():
            return
        try:
            pkt = struct.pack('<BfffBB',
                              PKT_STATUS,
                              self._latitude,
                              self._longitude,
                              self._battery_voltage,
                              self._robot_id,
                              self._status_flags)
            self._device.send_data_broadcast(pkt)
            self._status_packets_sent += 1
        except Exception as e:
            logger.error("Status packet error: %s", e)

    # -------------------------------------------------------------------
    # Ack sender (periodic ack to PC while gamepad data flows)
    # -------------------------------------------------------------------
    def _ack_loop(self):
        last_sent = 0.0
        while self._ack_running and self._running:
            time.sleep(0.5)
            remote = self._last_remote
            if remote is None:
                continue
            now = time.time()
            idle = (now - self._last_rx_time) > ACK_INTERVAL_IDLE
            interval = ACK_INTERVAL_IDLE if idle else ACK_INTERVAL_ACTIVE
            if (now - last_sent) < interval:
                continue
            last_sent = now
            self._send_response(remote, {
                "type": "gamepad_ack",
                "payload": {"count": self._gamepad_count, "idle": idle},
            })

    # -------------------------------------------------------------------
    # Background thread
    # -------------------------------------------------------------------
    def _keep_alive(self):
        logger.info("XBee responder active — reply '%s' to '%s'",
                     DEVICE_REPLY, HEARTBEAT_MSG)
        status_timer = 0

        # Start xArm connection in background
        if _xarm_class:
            self._arm = _xarm_class()
            self._try_xarm_connect()

        # Start ack sender
        self._ack_running = True
        threading.Thread(target=self._ack_loop, daemon=True,
                         name="gamepad-ack").start()

        # Start servo velocity loop
        self._velocity_running = True
        threading.Thread(target=self._servo_velocity_loop, daemon=True,
                         name="servo-velocity").start()

        while self._running:
            time.sleep(1)
            if self._send_status:
                status_timer += 1
                if status_timer >= self._status_interval:
                    self._send_status_packet()
                    status_timer = 0
            # Periodically check xArm
            if self._arm:
                with self._arm_lock:
                    self._arm_connected = self._arm.connected

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
        threading.Thread(target=_loop, daemon=True,
                         name="xarm-connect").start()

    # -------------------------------------------------------------------
    # Start / Stop
    # -------------------------------------------------------------------
    def start(self):
        if self.is_running:
            return True
        self._error_msg = ""
        try:
            from digi.xbee.devices import XBeeDevice
        except ImportError:
            self._error_msg = "digi-xbee not installed"
            logger.error(self._error_msg)
            return False

        port = _detect_xbee_port()
        if not port:
            self._error_msg = "No XBee serial port found"
            logger.error(self._error_msg)
            return False

        try:
            self._xbee_port = port
            self._device = XBeeDevice(port, XBEE_BAUD)
            self._device.set_sync_ops_timeout(10)
            self._device.open()
            self._device.add_data_received_callback(self._on_receive)
            self._running = True
            self._thread = threading.Thread(
                target=self._keep_alive, daemon=True, name="xbee-responder")
            self._thread.start()
            logger.info("XBee responder started on %s @ %d", port, XBEE_BAUD)
            return True
        except Exception as e:
            self._error_msg = str(e)
            logger.error("XBee start failed: %s", e)
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
        self._ack_running = False
        self._velocity_running = False
        if self._device:
            try:
                if self._device.is_open():
                    self._device.close()
            except Exception:
                pass
            self._device = None
        if self._arm:
            with self._arm_lock:
                try:
                    self._arm.disconnect()
                except Exception:
                    pass
                self._arm_connected = False
        if _gpio_available:
            try:
                GPIO.cleanup()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        logger.info("XBee responder stopped")

    # -------------------------------------------------------------------
    # Flask routes
    # -------------------------------------------------------------------
    def register(self, app):
        bp = Blueprint("xbee_responder", __name__)

        @bp.route("/api/xbee/status")
        def xbee_status():
            with self._lock:
                return jsonify({
                    "running": self.is_running,
                    "port": self._xbee_port or "auto-detect",
                    "device_reply": DEVICE_REPLY,
                    "heartbeats_received": self._heartbeats_received,
                    "replies_sent": self._replies_sent,
                    "status_packets_sent": self._status_packets_sent,
                    "last_heartbeat_from": self._last_heartbeat_from,
                    "last_rssi": self._last_rssi,
                    "last_time": self._last_time,
                    "send_status_enabled": self._send_status,
                    "status_interval": self._status_interval,
                    "history": self._history[-20:],
                    "error": self._error_msg,
                    "xarm_connected": self._arm_connected,
                    "xarm_available": _xarm_class is not None,
                    "gpio_available": _gpio_available,
                    "gamepad_arm_enabled": self._gamepad_arm_enabled,
                    "gamepad_count": self._gamepad_count,
                })

        @bp.route("/api/xbee/start", methods=["POST"])
        def xbee_start():
            ok = self.start()
            return jsonify({"success": ok, "error": self._error_msg})

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

        @bp.route("/api/xbee/scope")
        def xbee_scope():
            """Return latest gamepad values for client-side scope rendering."""
            with self._lock:
                return jsonify({
                    "latest": self._latest_gamepad,
                    "sticks": {
                        "left": list(self._stick_left),
                        "right": list(self._stick_right),
                    },
                    "stick_map": self._stick_map,
                    "gamepad_count": self._gamepad_count,
                    "gamepad_arm_enabled": self._gamepad_arm_enabled,
                })

        @bp.route("/api/xbee/chat")
        def xbee_chat():
            """Return recent chat/message log."""
            limit = request.args.get("limit", 50, type=int)
            with self._lock:
                return jsonify(self._chat_messages[-limit:])

        @bp.route("/api/xbee/hardware")
        def xbee_hardware():
            """Return xArm and GPIO status."""
            with self._arm_lock:
                arm_connected = (self._arm.connected
                                 if self._arm else False)
            with self._servo_lock:
                servos = dict(self._servo_positions)
            # IK state
            ik_mode = self._ik_mode
            xyz = self._current_xyz
            wrist_pitch = self._wrist_pitch_deg
            ik_configured = (self._kinematics.is_configured()
                             if self._kinematics else False)

            return jsonify({
                "xarm": {
                    "available": _xarm_class is not None,
                    "connected": arm_connected,
                    "gamepad_enabled": self._gamepad_arm_enabled,
                    "servos": {str(k): int(v) for k, v in servos.items()},
                    "reverse": {str(k): v for k, v in self._servo_reverse.items()},
                    "speed": {str(k): v for k, v in self._servo_speed.items()},
                    "ik_mode": ik_mode,
                    "ik_configured": ik_configured,
                    "xyz": xyz,
                    "wrist_pitch_deg": wrist_pitch,
                },
                "gpio": {
                    "available": _gpio_available,
                    "states": {str(k): v
                               for k, v in self._gpio_states.items()},
                    "pins": sorted(ALLOWED_PINS),
                },
            })

        @bp.route("/api/xbee/gpio", methods=["POST"])
        def gpio_control():
            """Set GPIO pin from web dashboard."""
            data = request.json or {}
            pin = data.get("pin")
            state = data.get("state")
            if pin is None or state is None:
                return jsonify({"error": "pin and state required"}), 400
            pin = int(pin)
            if pin not in ALLOWED_PINS:
                return jsonify({"error": f"Pin {pin} not allowed"}), 400
            state = 1 if state else 0
            if _gpio_available:
                GPIO.output(pin, GPIO.HIGH if state else GPIO.LOW)
            self._gpio_states[pin] = state
            return jsonify({"pin": pin, "state": state})

        @bp.route("/api/xbee/xarm-move", methods=["POST"])
        def xarm_move_http():
            """Move a servo via HTTP (from dashboard UI)."""
            data = request.json or {}
            sid = data.get("servo")
            pos = data.get("position")
            dur = data.get("duration", 200)
            if sid is None or pos is None:
                return jsonify({"error": "servo and position required"}), 400
            sid, pos, dur = int(sid), int(pos), int(dur)
            pos = max(0, min(1000, pos))
            with self._arm_lock:
                if not self._arm or not self._arm.connected:
                    return jsonify({"error": "xArm not connected"}), 503
                self._arm.move_servo(sid, pos, dur)
            with self._servo_lock:
                self._servo_positions[sid] = float(pos)
            return jsonify({"ok": True, "servo": sid, "position": pos})

        @bp.route("/api/xbee/xarm-toggle", methods=["POST"])
        def xarm_gamepad_toggle():
            """Toggle gamepad->xArm control."""
            data = request.json or {}
            self._gamepad_arm_enabled = bool(
                data.get("enabled", not self._gamepad_arm_enabled))
            return jsonify({"enabled": self._gamepad_arm_enabled})

        @bp.route("/api/xbee/xyz-mode", methods=["POST"])
        def xyz_mode_toggle():
            """Toggle XYZ (IK) mode from dashboard."""
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

        @bp.route("/api/xbee/xarm-read-positions", methods=["POST"])
        def xarm_read_positions():
            """Read actual servo positions from xArm hardware."""
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

        @bp.route("/api/xbee/xarm-ik-goto", methods=["POST"])
        def xarm_ik_goto():
            """Move arm tip to target XYZ position via IK."""
            if not self._kinematics or not self._kinematics.is_configured():
                return jsonify({"error": "IK not configured"}), 503
            data = request.json or {}
            x = float(data.get("x", 0))
            y = float(data.get("y", 0))
            z = float(data.get("z", 0))
            wp = float(data.get("wrist_pitch_deg",
                                self._wrist_pitch_deg))
            result = self._kinematics.inverse_kinematics(x, y, z, wp)
            if result is None:
                # Try multiple wrist pitches
                for try_wp in [0, -45, 45, -90, 90, -30, 30, -60, 60]:
                    result = self._kinematics.inverse_kinematics(x, y, z, try_wp)
                    if result is not None:
                        wp = try_wp
                        break
            if result is None:
                # Try clamped position
                cx, cy, cz = self._kinematics.clamp_to_workspace(x, y, z, wp)
                result = self._kinematics.inverse_kinematics(cx, cy, cz, wp)
                if result is None:
                    return jsonify({"error": "unreachable"}), 400
                x, y, z = cx, cy, cz
            with self._servo_lock:
                for sid, pos in result.items():
                    self._servo_positions[sid] = float(pos)
                self._current_xyz = {"x": x, "y": y, "z": z}
            # Send to physical arm if enabled
            if self._gamepad_arm_enabled:
                moves = [(sid, int(pos)) for sid, pos in result.items()]
                with self._arm_lock:
                    if self._arm and self._arm.connected:
                        self._arm.move_servos(moves, 100)
            return jsonify({"ok": True, "x": x, "y": y, "z": z,
                            "servos": result})

        @bp.route("/api/xbee/xarm-speed", methods=["POST"])
        def xarm_speed():
            """Set speed factor for a servo (units/sec at full stick)."""
            data = request.json or {}
            sid = data.get("servo")
            speed = data.get("speed", 150)
            if sid is not None:
                self._servo_speed[int(sid)] = max(10, min(2000, float(speed)))
            return jsonify({"speed": {str(k): v for k, v in self._servo_speed.items()}})

        @bp.route("/api/xbee/xarm-reverse", methods=["POST"])
        def xarm_reverse():
            """Set reverse flag for a servo. Also updates IK calibration direction."""
            data = request.json or {}
            sid = data.get("servo")
            rev = data.get("reverse", False)
            if sid is not None:
                sid = int(sid)
                self._servo_reverse[sid] = bool(rev)
                # Update IK calibration direction for IK servos
                if self._kinematics and sid in self._kinematics.calibration:
                    self._kinematics.calibration[sid]["direction"] = -1 if rev else 1
                    logger.info("Servo %d IK direction set to %d",
                                sid, self._kinematics.calibration[sid]["direction"])
            return jsonify({"reverse": {str(k): v for k, v in self._servo_reverse.items()}})

        @bp.route("/api/robot/status", methods=["GET", "POST"])
        def robot_status():
            if request.method == "POST":
                data = request.json or {}
                if "latitude" in data:
                    self._latitude = float(data["latitude"])
                if "longitude" in data:
                    self._longitude = float(data["longitude"])
                if "battery_voltage" in data:
                    self._battery_voltage = float(data["battery_voltage"])
                if "robot_id" in data:
                    self._robot_id = int(data["robot_id"])
                if "status_flags" in data:
                    self._status_flags = int(data["status_flags"])
                if "send_status" in data:
                    self._send_status = bool(data["send_status"])
                if "status_interval" in data:
                    self._status_interval = max(5, int(data["status_interval"]))
                return jsonify({"success": True})
            return jsonify({
                "latitude": self._latitude,
                "longitude": self._longitude,
                "battery_voltage": self._battery_voltage,
                "robot_id": self._robot_id,
                "status_flags": self._status_flags,
                "send_status": self._send_status,
                "status_interval": self._status_interval,
            })

        @bp.route("/api/xbee/local-gamepad/list")
        def local_gamepad_list():
            """List available local joystick devices."""
            if not self._list_gamepads_fn:
                return jsonify({"devices": [], "error": "not available"})
            devices = self._list_gamepads_fn()
            active = None
            if self._local_gamepad and self._local_gamepad.is_running:
                active = self._local_gamepad.device_path
            return jsonify({"devices": devices, "active": active})

        @bp.route("/api/xbee/local-gamepad/start", methods=["POST"])
        def local_gamepad_start():
            """Start reading from a local joystick device."""
            if not self._local_gamepad_class:
                return jsonify({"error": "local gamepad not available"}), 503
            data = request.json or {}
            path = data.get("path", "/dev/input/js0")
            # Stop existing if running
            if self._local_gamepad and self._local_gamepad.is_running:
                self._local_gamepad.stop()
            self._local_gamepad = self._local_gamepad_class(
                callback=self._local_gamepad_callback, rate_hz=50)
            ok = self._local_gamepad.start(path)
            if ok:
                self._local_gamepad_enabled = True
                # Auto-load gamepad profile based on device name
                self._load_gamepad_profile(self._local_gamepad.device_name)
                # Ensure velocity loop is running for simulation
                if not self._velocity_running:
                    self._running = True
                    self._velocity_running = True
                    threading.Thread(target=self._servo_velocity_loop,
                                     daemon=True, name="servo-velocity").start()
                return jsonify({"ok": True, "path": path,
                                "name": self._local_gamepad.device_name})
            return jsonify({"error": f"Cannot open {path}"}), 400

        @bp.route("/api/xbee/local-gamepad/stop", methods=["POST"])
        def local_gamepad_stop():
            """Stop the local gamepad reader."""
            if self._local_gamepad and self._local_gamepad.is_running:
                self._local_gamepad.stop()
            self._local_gamepad_enabled = False
            return jsonify({"ok": True})

        @bp.route("/api/xbee/local-gamepad/status")
        def local_gamepad_status():
            """Get local gamepad status and current state."""
            if not self._local_gamepad or not self._local_gamepad.is_running:
                return jsonify({"running": False, "path": None, "name": ""})
            return jsonify({
                "running": True,
                "path": self._local_gamepad.device_path,
                "name": self._local_gamepad.device_name,
                "state": self._local_gamepad.get_state(),
            })

        @bp.route("/simulation")
        def xarm_simulation():
            """Serve xArm simulation page."""
            from flask import render_template
            return render_template("xarm_simulation.html")

        @bp.route("/api/xbee/ik-config", methods=["GET", "POST"])
        def ik_config():
            """Get or update IK configuration (link lengths)."""
            cfg_path = os.path.join(os.path.dirname(__file__), "..",
                                    "configs", "xarm_kinematics.json")
            if request.method == "POST":
                data = request.json or {}
                # Load existing config
                try:
                    with open(cfg_path) as f:
                        cfg = json.load(f)
                except Exception:
                    cfg = {}
                # Update link lengths
                if "link_lengths_mm" in data:
                    cfg["link_lengths_mm"] = data["link_lengths_mm"]
                # Save
                with open(cfg_path, "w") as f:
                    json.dump(cfg, f, indent=4)
                # Reload kinematics if available
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

        app.register_blueprint(bp)
