"""
Orbbec Astra depth camera reader for RosMaster X3.
Uses OpenNI2 via primesense library for depth frames.
Provides depth data as a downscaled JPEG heatmap for web streaming.
"""

import time
import threading
import base64
import numpy as np
import cv2

OPENNI2_PATH = "/home/jetson/yahboomcar_ros2_ws/software/library_ws/install/astra_camera/include/openni2/openni2_redist/arm64"


class DepthReader:
    # Orbbec Astra horizontal FOV in degrees
    HFOV = 60.0

    def __init__(self):
        self.running = False
        self.connected = False
        self.depth_jpeg = None  # base64-encoded JPEG of depth heatmap
        self.depth_stats = {}
        self.floor_jpeg = None  # base64 JPEG of floor detection view
        self.floor_stats = {}   # floor detection stats
        self.depth_line = []    # horizontal line: list of (angle_deg, distance_mm)
        self._raw_depth = None  # raw uint16 depth frame for collision avoidance
        self.angle_offset = 0.0  # degrees to add to each depth angle for LiDAR alignment
        self.lock = threading.Lock()
        self._thread = None
        self.floor_baseline = None  # learned floor distance per column

    def start(self):
        self.running = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def _read_loop(self):
        try:
            from primesense import openni2

            openni2.initialize(OPENNI2_PATH)
            dev = openni2.Device.open_any()
            info = dev.get_device_info()
            print(f"Depth camera: {info.name.decode()} by {info.vendor.decode()}")

            depth_stream = dev.create_depth_stream()
            depth_stream.start()
            self.connected = True
            print("Depth stream started")

            while self.running:
                frame = depth_stream.read_frame()
                data = np.frombuffer(frame.get_buffer_as_uint16(), dtype=np.uint16)
                data = data.reshape((frame.height, frame.width))

                # Stats
                valid = data[data > 0]
                stats = {
                    "min": int(valid.min()) if len(valid) > 0 else 0,
                    "max": int(valid.max()) if len(valid) > 0 else 0,
                    "mean": int(valid.mean()) if len(valid) > 0 else 0,
                    "width": frame.width,
                    "height": frame.height,
                }

                # Create heatmap: normalize to 0-255, apply colormap
                normalized = np.zeros_like(data, dtype=np.uint8)
                mask = data > 0
                if mask.any():
                    d_min, d_max = valid.min(), min(valid.max(), 5000)
                    clipped = np.clip(data, d_min, d_max).astype(np.float32)
                    clipped[~mask] = 0
                    normalized[mask] = (255 * (1.0 - (clipped[mask] - d_min) / max(d_max - d_min, 1))).astype(np.uint8)

                colored = cv2.applyColorMap(normalized, cv2.COLORMAP_JET)
                colored[~mask] = [0, 0, 0]  # Black for no-data

                # Downscale for web
                small = cv2.resize(colored, (240, 180))
                _, jpeg = cv2.imencode('.jpg', small, [cv2.IMWRITE_JPEG_QUALITY, 70])
                b64 = base64.b64encode(jpeg.tobytes()).decode('ascii')

                # Floor detection: bottom third of frame
                floor_b64, floor_st = self._process_floor(data)

                # Extract horizontal line from middle row for LiDAR overlay
                depth_line = self._extract_depth_line(data)

                with self.lock:
                    self.depth_jpeg = b64
                    self._raw_depth = data.copy()
                    self.depth_stats = stats
                    self.depth_line = depth_line
                    if floor_b64:
                        self.floor_jpeg = floor_b64
                        self.floor_stats = floor_st

                time.sleep(0.1)  # ~10 FPS

            depth_stream.stop()
            dev.close()
            openni2.unload()

        except Exception as e:
            print(f"Depth camera error: {e}")
            self.connected = False

    def _process_floor(self, data):
        """Analyze bottom third of depth frame to detect low obstacles.

        Strategy:
        - Use bottom 1/3 of the frame (rows 320-480) which sees the floor nearby
        - Learn a baseline floor distance per column over time
        - Objects significantly closer than floor baseline = obstacles
        - Color: green = floor, red = obstacle, black = no data
        """
        h, w = data.shape
        bottom = data[h * 2 // 3:, :]  # bottom third
        valid_mask = bottom > 50  # ignore noise

        # Build or update floor baseline (median per column, smoothed)
        if self.floor_baseline is None:
            col_medians = np.zeros(w, dtype=np.float32)
            for c in range(w):
                col = bottom[:, c]
                v = col[col > 50]
                col_medians[c] = np.median(v) if len(v) > 5 else 0
            self.floor_baseline = col_medians
        else:
            # Slowly update baseline (IIR filter)
            for c in range(w):
                col = bottom[:, c]
                v = col[col > 50]
                if len(v) > 5:
                    med = np.median(v)
                    if self.floor_baseline[c] == 0:
                        self.floor_baseline[c] = med
                    else:
                        self.floor_baseline[c] = 0.95 * self.floor_baseline[c] + 0.05 * med

        # Detect obstacles: pixels significantly closer than baseline
        obstacle_thresh = 0.7  # object must be < 70% of floor distance
        floor_img = np.zeros((bottom.shape[0], w, 3), dtype=np.uint8)

        obstacle_count = 0
        min_obstacle_dist = 99999

        for c in range(w):
            baseline = self.floor_baseline[c]
            if baseline < 100:
                continue
            col = bottom[:, c]
            for r in range(len(col)):
                if col[r] < 50:
                    continue  # no data
                if col[r] < baseline * obstacle_thresh:
                    # Obstacle — closer than expected floor
                    floor_img[r, c] = [0, 0, 255]  # red
                    obstacle_count += 1
                    min_obstacle_dist = min(min_obstacle_dist, int(col[r]))
                elif col[r] > 50:
                    # Normal floor
                    floor_img[r, c] = [0, 80, 0]  # dark green

        # Resize for display
        display = cv2.resize(floor_img, (240, 80))
        _, jpeg = cv2.imencode('.jpg', display, [cv2.IMWRITE_JPEG_QUALITY, 70])
        b64 = base64.b64encode(jpeg.tobytes()).decode('ascii')

        stats = {
            "obstacles": obstacle_count,
            "min_dist": min_obstacle_dist if min_obstacle_dist < 99999 else 0,
            "has_obstacle": obstacle_count > 50,
        }

        return b64, stats

    def _extract_depth_line(self, data):
        """Extract horizontal distance line from middle row of depth frame.

        Returns list of (angle_deg, distance_mm) pairs.
        Angle 0 = straight ahead, negative = left, positive = right.
        The depth camera faces forward, so angle range is ±HFOV/2.
        """
        h, w = data.shape
        mid_row = data[h // 2, :]

        half_fov = self.HFOV / 2.0
        line = []
        # Sample every 4th pixel for efficiency (160 points across 640px)
        for col in range(0, w, 4):
            d = int(mid_row[col])
            if d < 100 or d > 8000:
                continue
            # Map pixel column to angle: col 0 = +30° (right), col 639 = -30° (left)
            # Depth image is mirrored relative to LiDAR, so negate
            angle = -(col - w / 2.0) / (w / 2.0) * half_fov + self.angle_offset
            line.append((round(angle, 1), d))

        return line

    def get_frame(self):
        """Returns (base64_jpeg, stats_dict) or (None, {})."""
        with self.lock:
            return self.depth_jpeg, self.depth_stats.copy()

    def get_depth_line(self):
        """Returns list of (angle_deg, distance_mm) for LiDAR overlay."""
        with self.lock:
            return list(self.depth_line)

    def set_angle_offset(self, offset_deg):
        """Set angle offset in degrees for aligning depth line with LiDAR data."""
        self.angle_offset = float(offset_deg)

    def get_floor(self):
        """Returns (floor_jpeg_b64, floor_stats) for floor detection view."""
        with self.lock:
            return self.floor_jpeg, self.floor_stats.copy()

    def _get_raw_depth(self):
        """Returns raw uint16 depth frame (640x480) for collision avoidance."""
        with self.lock:
            return self._raw_depth.copy() if self._raw_depth is not None else None

    def stop(self):
        self.running = False
        if self._thread:
            self._thread.join(timeout=3)
