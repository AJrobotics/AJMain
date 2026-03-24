"""
xArm1S Control Module (RPi)
Communicates with Hiwonder xArm1S via USB HID protocol.
VID: 0x0483, PID: 0x5750
"""

import logging
import time

import hid

VID = 0x0483
PID = 0x5750

CMD_SERVO_MOVE = 3
CMD_GET_BATTERY_VOLTAGE = 15
CMD_SERVO_OFF = 20
CMD_SERVO_POSITION_READ = 21

SERVO_COUNT = 6

log = logging.getLogger(__name__)


class XArm:
    def __init__(self):
        self.dev = None

    @property
    def connected(self) -> bool:
        return self.dev is not None

    def connect(self) -> bool:
        """Connect to the xArm1S over USB HID. Returns True on success."""
        try:
            self.dev = hid.device()
            self.dev.open(VID, PID)
            self.dev.set_nonblocking(True)
            try:
                info = self.dev.get_product_string()
            except Exception:
                info = "(unknown)"
            log.info("xArm connected: %s", info)
            return True
        except Exception as exc:
            log.warning("xArm connect failed: %s", exc)
            if self.dev:
                try:
                    self.dev.close()
                except Exception:
                    pass
            self.dev = None
            return False

    def disconnect(self):
        if self.dev:
            try:
                self.dev.close()
            except Exception:
                pass
            self.dev = None
            log.info("xArm disconnected")

    def _send(self, command, params=None):
        if self.dev is None:
            return
        if params is None:
            params = []
        length = len(params) + 2
        packet = [0x00, 0x55, 0x55, length, command] + params
        packet += [0x00] * (65 - len(packet))
        self.dev.write(packet)

    def _read(self, timeout_ms=100):
        if self.dev is None:
            return None
        time.sleep(timeout_ms / 1000.0)
        data = self.dev.read(64)
        if data and len(data) >= 4 and data[0] == 0x55 and data[1] == 0x55:
            return data
        return None

    def move_servo(self, servo_id, position, duration_ms=500):
        position = max(0, min(1000, int(position)))
        duration_ms = max(0, min(30000, int(duration_ms)))
        pos_lo, pos_hi = position & 0xFF, (position >> 8) & 0xFF
        dur_lo, dur_hi = duration_ms & 0xFF, (duration_ms >> 8) & 0xFF
        params = [1, dur_lo, dur_hi, servo_id, pos_lo, pos_hi]
        self._send(CMD_SERVO_MOVE, params)

    def move_servos(self, moves, duration_ms=500):
        """moves: list of (servo_id, position) tuples."""
        duration_ms = max(0, min(30000, int(duration_ms)))
        dur_lo, dur_hi = duration_ms & 0xFF, (duration_ms >> 8) & 0xFF
        params = [len(moves), dur_lo, dur_hi]
        for servo_id, position in moves:
            position = max(0, min(1000, int(position)))
            pos_lo, pos_hi = position & 0xFF, (position >> 8) & 0xFF
            params += [servo_id, pos_lo, pos_hi]
        self._send(CMD_SERVO_MOVE, params)

    def read_position(self, servo_id):
        self._send(CMD_SERVO_POSITION_READ, [1, servo_id])
        data = self._read(150)
        if data and len(data) >= 7 and data[2] == len(data) - 3:
            if data[3] == CMD_SERVO_POSITION_READ:
                return data[6] | (data[7] << 8)
        if data and len(data) >= 8:
            for i in range(len(data) - 4):
                if data[i] == 0x55 and data[i + 1] == 0x55:
                    cmd_idx = i + 3
                    if cmd_idx < len(data) and data[cmd_idx] == CMD_SERVO_POSITION_READ:
                        pos_idx = cmd_idx + 3
                        if pos_idx + 1 < len(data):
                            return data[pos_idx] | (data[pos_idx + 1] << 8)
        return None

    def read_all_positions(self):
        positions = {}
        for sid in range(1, SERVO_COUNT + 1):
            pos = self.read_position(sid)
            positions[sid] = pos
        return positions

    def servo_off(self, servo_id):
        self._send(CMD_SERVO_OFF, [1, servo_id])

    def all_servos_off(self):
        params = [SERVO_COUNT] + list(range(1, SERVO_COUNT + 1))
        self._send(CMD_SERVO_OFF, params)

    def get_battery_voltage(self):
        self._send(CMD_GET_BATTERY_VOLTAGE)
        data = self._read(150)
        if data and len(data) >= 6:
            for i in range(len(data) - 4):
                if data[i] == 0x55 and data[i + 1] == 0x55:
                    cmd_idx = i + 3
                    if cmd_idx < len(data) and data[cmd_idx] == CMD_GET_BATTERY_VOLTAGE:
                        v_idx = cmd_idx + 1
                        if v_idx + 1 < len(data):
                            return data[v_idx] | (data[v_idx + 1] << 8)
        return None

    def home(self, duration_ms=1500):
        moves = [(sid, 500) for sid in range(1, SERVO_COUNT + 1)]
        self.move_servos(moves, duration_ms)
