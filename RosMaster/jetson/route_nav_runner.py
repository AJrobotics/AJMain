"""Route navigation runner using behavior cloning model.

Takes camera (RGB) + LiDAR as input, outputs motor commands.
The model was trained from recorded routes using optical flow + IMU.

Usage:
    runner = RouteNavRunner()
    vx, vy, vz = runner.get_action(rgb_frame_b64, lidar_scan)
"""

import os
import base64
import numpy as np
import logging

_log = logging.getLogger("route_nav_runner")

DEFAULT_MODEL_PATH = "/home/jetson/RosMaster/models/route_nav_policy.pt"
MAX_RANGE_MM = 6000


class RouteNavRunner:
    """Behavior cloning navigation: camera + LiDAR -> motor commands."""

    def __init__(self, model_path=None):
        self.model = None
        self.available = False
        self.model_path = model_path or DEFAULT_MODEL_PATH

        if not os.path.exists(self.model_path):
            _log.info("Route nav model not found: %s", self.model_path)
            return

        try:
            import torch
            self.torch = torch
            self.model = torch.jit.load(self.model_path, map_location='cpu')
            self.model.eval()
            self.available = True
            _log.info("Route nav model loaded: %s", self.model_path)
        except ImportError:
            _log.warning("PyTorch not installed — route nav unavailable")
        except Exception as e:
            _log.error("Route nav model load error: %s", e)

    def get_action(self, rgb_b64, lidar_scan):
        """Get motor command from camera + LiDAR.

        Args:
            rgb_b64: base64-encoded JPEG from camera
            lidar_scan: list of {"angle": deg, "dist": mm} dicts

        Returns:
            (vx, vy, vz) motor command tuple
        """
        if not self.available:
            return 0, 0, 0

        try:
            import cv2
            torch = self.torch

            # Decode and preprocess RGB
            rgb_bytes = base64.b64decode(rgb_b64)
            rgb_arr = np.frombuffer(rgb_bytes, dtype=np.uint8)
            img = cv2.imdecode(rgb_arr, cv2.IMREAD_GRAYSCALE)
            if img is None:
                return 0, 0, 0
            img = cv2.resize(img, (80, 60)).astype(np.float32) / 255.0
            img_t = torch.tensor(img).unsqueeze(0).unsqueeze(0)  # (1, 1, 60, 80)

            # Downsample LiDAR to 36 bins
            distances = np.full(360, MAX_RANGE_MM, dtype=np.float32)
            if lidar_scan:
                for pt in lidar_scan:
                    angle = pt.get("angle", 0) if isinstance(pt, dict) else pt[0]
                    dist = pt.get("dist", 0) if isinstance(pt, dict) else pt[1]
                    idx = int(round(angle)) % 360
                    if 0 < dist < distances[idx]:
                        distances[idx] = dist
            bins = distances.reshape(36, 10).min(axis=1)
            lidar = np.clip(bins / MAX_RANGE_MM, 0, 1).astype(np.float32)
            lidar_t = torch.tensor(lidar).unsqueeze(0)  # (1, 36)

            # Inference
            with torch.no_grad():
                pred = self.model(img_t, lidar_t).numpy()[0]

            # Map [-1, 1] to real velocities
            # Note: model often outputs negative vx (optical flow convention)
            # Take absolute value for forward speed, use sign only for rotation
            raw_vx = float(pred[0])
            raw_vy = float(pred[1])
            raw_vz = float(pred[2])

            # Forward speed: use magnitude of vx prediction (always go forward)
            vx = abs(raw_vx) * 0.08 + 0.02   # [0.02, 0.10] slower, safer
            vy = raw_vy * 0.08                 # [-0.08, 0.08] strafe
            vz = raw_vz * 1.0                  # [-1.0, 1.0] rotation

            return vx, vy, vz

        except Exception as e:
            _log.error("Route nav inference error: %s", e)
            return 0, 0, 0
