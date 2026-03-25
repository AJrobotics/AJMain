"""
Movement calibration for RosMaster X3.
Tests forward, backward, strafe left/right, and rotation
to verify motor commands produce expected movement.
Collision avoidance stays active during calibration.
"""

import time
import math
import threading

# Calibration speed (m/s for translation, rad/s for rotation)
CAL_SPEED = 0.15       # m/s
CAL_ROT_SPEED = 1.0    # rad/s

# Test directions
TESTS = ["forward", "backward", "left", "right", "rotate_left", "rotate_right"]


class CalibrationRunner:
    def __init__(self, bot=None, collision=None):
        """
        Args:
            bot: Rosmaster_Lib instance (has set_car_motion, get_imu_attitude_data, get_motor_encoder)
            collision: CollisionAvoidance instance (filter_motion used as safety layer)
        """
        self.bot = bot
        self.collision = collision
        self.state = "idle"  # idle, running, done, aborted
        self.current_test = None
        self.results = []
        self._abort_flag = False
        self._thread = None

    def _move_filtered(self, vx, vy, vz):
        """Send movement through collision avoidance filter."""
        if self.collision and self.collision.enabled:
            self.collision.update_sectors()
            vx, vy, vz = self.collision.filter_motion(vx, vy, vz)
        if self.bot:
            self.bot.set_car_motion(vx, vy, vz)
        return vx, vy, vz

    def _stop(self):
        if self.bot:
            self.bot.set_car_motion(0, 0, 0)

    def _get_imu_yaw(self):
        if self.bot:
            try:
                _, _, yaw = self.bot.get_imu_attitude_data()
                return yaw
            except Exception:
                pass
        return 0.0

    def _run_translation_test(self, direction, distance_mm, speed):
        """Run a single translation test."""
        # Direction vectors
        vectors = {
            "forward":  (speed, 0, 0),
            "backward": (-speed, 0, 0),
            "left":     (0, speed, 0),
            "right":    (0, -speed, 0),
        }
        vx, vy, vz = vectors[direction]
        duration = (distance_mm / 1000.0) / speed  # seconds

        start_yaw = self._get_imu_yaw()
        start_time = time.time()

        # Move for calculated duration
        elapsed = 0
        actual_duration = 0
        while elapsed < duration and not self._abort_flag:
            actual_vx, actual_vy, actual_vz = self._move_filtered(vx, vy, vz)
            # Check if collision avoidance blocked the movement
            blocked = (abs(actual_vx) < 0.01 and abs(actual_vy) < 0.01
                       and (abs(vx) > 0.01 or abs(vy) > 0.01))
            if blocked:
                self._stop()
                return {
                    "direction": direction,
                    "status": "blocked",
                    "commanded_mm": distance_mm,
                    "actual_duration": round(elapsed, 2),
                    "reason": "collision avoidance",
                }
            time.sleep(0.05)
            elapsed = time.time() - start_time

        self._stop()
        actual_duration = time.time() - start_time
        time.sleep(0.5)  # settling time

        end_yaw = self._get_imu_yaw()
        yaw_delta = end_yaw - start_yaw

        return {
            "direction": direction,
            "status": "aborted" if self._abort_flag else "done",
            "commanded_mm": distance_mm,
            "commanded_duration": round(duration, 2),
            "actual_duration": round(actual_duration, 2),
            "imu_yaw_delta": round(yaw_delta, 2),
        }

    def _run_rotation_test(self, direction, angle_deg, rot_speed):
        """Run a single rotation test."""
        sign = 1 if direction == "rotate_left" else -1
        vz = sign * rot_speed
        duration = math.radians(angle_deg) / rot_speed  # seconds

        start_yaw = self._get_imu_yaw()
        start_time = time.time()

        elapsed = 0
        while elapsed < duration and not self._abort_flag:
            self._move_filtered(0, 0, vz)
            time.sleep(0.05)
            elapsed = time.time() - start_time

        self._stop()
        time.sleep(0.5)

        end_yaw = self._get_imu_yaw()
        yaw_delta = end_yaw - start_yaw

        return {
            "direction": direction,
            "status": "aborted" if self._abort_flag else "done",
            "commanded_deg": angle_deg,
            "commanded_duration": round(duration, 2),
            "actual_duration": round(time.time() - start_time, 2),
            "imu_yaw_start": round(start_yaw, 2),
            "imu_yaw_end": round(end_yaw, 2),
            "imu_yaw_delta": round(yaw_delta, 2),
        }

    def run_test(self, direction, distance_mm=500, speed=CAL_SPEED):
        """Run a single calibration test in a background thread."""
        if self.state == "running":
            return {"error": "Calibration already running"}

        self._abort_flag = False
        self.state = "running"
        self.current_test = direction
        self.results = []

        def _run():
            try:
                if direction.startswith("rotate"):
                    result = self._run_rotation_test(direction, distance_mm, CAL_ROT_SPEED)
                else:
                    result = self._run_translation_test(direction, distance_mm, speed)
                self.results = [result]
            except Exception as e:
                self.results = [{"direction": direction, "status": "error", "error": str(e)}]
            finally:
                self._stop()
                self.current_test = None
                self.state = "done" if not self._abort_flag else "aborted"

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        return {"ok": True, "test": direction}

    def run_all(self, distance_mm=500, speed=CAL_SPEED):
        """Run all 6 calibration tests sequentially."""
        if self.state == "running":
            return {"error": "Calibration already running"}

        self._abort_flag = False
        self.state = "running"
        self.results = []

        def _run_all():
            for direction in TESTS:
                if self._abort_flag:
                    break
                self.current_test = direction
                try:
                    if direction.startswith("rotate"):
                        result = self._run_rotation_test(direction, distance_mm, CAL_ROT_SPEED)
                    else:
                        result = self._run_translation_test(direction, distance_mm, speed)
                    self.results.append(result)
                except Exception as e:
                    self.results.append({"direction": direction, "status": "error", "error": str(e)})
                time.sleep(1)  # pause between tests

            self._stop()
            self.current_test = None
            self.state = "done" if not self._abort_flag else "aborted"

        self._thread = threading.Thread(target=_run_all, daemon=True)
        self._thread.start()
        return {"ok": True}

    def abort(self):
        """Abort current calibration."""
        self._abort_flag = True
        self._stop()
        self.state = "aborted"
        return {"ok": True}

    def get_status(self):
        return {
            "state": self.state,
            "current_test": self.current_test,
            "results": self.results,
        }
