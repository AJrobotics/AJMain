"""
RPLidar S2 data reader for RosMaster X3.
Uses pyrplidar library for proper protocol handling.
"""

import math
import time
import threading


LIDAR_PORT = "/dev/rplidar"
LIDAR_BAUD = 1000000
MOTOR_PWM = 800


class LidarReader:
    def __init__(self, port=LIDAR_PORT, baud=LIDAR_BAUD):
        self.port = port
        self.baud = baud
        self.running = False
        self.scan_data = []
        self.has_new_scan = False
        self.lock = threading.Lock()
        self.connected = False
        self.simulated = False
        self.scan_mode = "standard"  # "standard" or "express"
        self._mode_change = False     # flag to trigger mode switch
        self._thread = None

    def set_scan_mode(self, mode):
        """Switch scan mode. Requires restart of reader thread."""
        if mode not in ("standard", "express"):
            return
        if mode != self.scan_mode:
            self.scan_mode = mode
            self._mode_change = True
            print(f"LiDAR scan mode changed to: {mode}")

    def start(self):
        self.running = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def _read_loop(self):
        try:
            from pyrplidar import PyRPlidar

            while self.running:
                lidar = PyRPlidar()
                lidar.connect(port=self.port, baudrate=self.baud, timeout=3)

                info = lidar.get_info()
                print(f"RPLidar connected: {info}")
                self.connected = True
                self.simulated = False
                self._mode_change = False

                lidar.set_motor_pwm(MOTOR_PWM)
                time.sleep(1)

                # Start scan based on current mode
                if self.scan_mode == "express":
                    print("LiDAR: Express scan mode (2x points)")
                    scan_generator = lidar.start_scan_express(0)
                else:
                    print("LiDAR: Standard scan mode")
                    scan_generator = lidar.start_scan()

                angle_map = {}
                prev_angle = -1

                for measurement in scan_generator():
                    if not self.running or self._mode_change:
                        break

                    angle = measurement.angle
                    distance = measurement.distance
                    quality = measurement.quality

                    # Detect revolution: angle wraps backward by > 300°
                    if prev_angle > 0 and angle < prev_angle - 300:
                        if len(angle_map) > 200:
                            scan = sorted(angle_map.values(), key=lambda p: p["angle"])
                            with self.lock:
                                self.scan_data = scan
                                self.has_new_scan = True
                        angle_map = {}

                    prev_angle = angle

                    if distance > 0 and (quality > 0 or self.scan_mode == "express"):
                        key = int(angle) % 360
                        angle_map[key] = {
                            "angle": round(angle, 1),
                            "dist": round(distance),
                        }

                lidar.stop()
                lidar.set_motor_pwm(0)
                lidar.disconnect()

                if self._mode_change:
                    print(f"LiDAR: switching to {self.scan_mode} mode...")
                    self._mode_change = False
                    time.sleep(1)
                    continue  # reconnect with new mode
                else:
                    break  # normal exit

        except Exception as e:
            print(f"RPLidar error: {e}. Using simulated data.")
            self.connected = False
            self.simulated = True
            self._simulated_loop()

    def _simulated_loop(self):
        import random
        while self.running:
            points = []
            t = time.time()
            for i in range(360):
                dist = 2000
                if -30 < i < 30:
                    dist = 1500 + 100 * math.sin(t * 2 + i * 0.1)
                elif 60 < i < 120:
                    dist = 2000 + 50 * math.sin(t * 3 + i * 0.1)
                elif 150 < i < 210:
                    dist = 3000 + 100 * math.sin(t + i * 0.05)
                elif 240 < i < 300:
                    dist = 1800 + 80 * math.sin(t * 1.5 + i * 0.1)
                else:
                    dist = 2500 + 300 * math.sin(i * math.pi / 60 + t)
                dist += random.gauss(0, 20)
                dist = max(25, min(8000, dist))
                points.append({"angle": round(i, 1), "dist": round(dist)})
            with self.lock:
                self.scan_data = points
            time.sleep(0.1)

    def get_scan(self):
        with self.lock:
            return self.scan_data.copy()

    def stop(self):
        self.running = False
        if self._thread:
            self._thread.join(timeout=3)
