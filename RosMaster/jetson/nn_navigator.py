"""
Neural Network Navigator for RosMaster X3.

Loads a trained navigation policy (TorchScript) and provides motor commands
based on LiDAR scan data. The NN replaces the rule-based frontier explorer
while still going through collision avoidance.

Usage:
    nav = NNNavigator("/home/jetson/RosMaster/models/nav_policy.pt")
    vx, vy, vz = nav.get_action(lidar_360_distances)

The model takes 36 LiDAR bins (10° each, normalized [0,1]) and outputs
3 continuous actions mapped to (vx, vy, vz) motor commands.
"""

import os
import math
import numpy as np
import logging

_log = logging.getLogger("nn_navigator")

# Must match training/robot_env.py
MAX_RANGE_MM = 6000
NUM_BINS = 36
VX_MIN, VX_MAX = -0.05, 0.15
VY_MIN, VY_MAX = -0.12, 0.12
VZ_MIN, VZ_MAX = -1.0, 1.0

# Default model path on Jetson
DEFAULT_MODEL_PATH = "/home/jetson/RosMaster/models/nav_policy.pt"


class NNNavigator:
    """Neural network navigation policy.

    Loads a TorchScript model and provides get_action() for motor commands.
    Falls back gracefully if PyTorch is not installed or model is missing.
    """

    def __init__(self, model_path=None):
        self.model = None
        self.available = False
        self.model_path = model_path or DEFAULT_MODEL_PATH

        if not os.path.exists(self.model_path):
            _log.info("NN model not found at %s — NN mode unavailable", self.model_path)
            return

        try:
            import torch
            self.torch = torch
            self.model = torch.jit.load(self.model_path, map_location='cpu')
            self.model.eval()
            self.available = True
            _log.info("NN navigation model loaded from %s", self.model_path)

            # Quick test
            dummy = torch.randn(1, NUM_BINS)
            with torch.no_grad():
                out = self.model(dummy)
            _log.info("Model test: input shape %s → output shape %s", dummy.shape, out.shape)

        except ImportError:
            _log.warning("PyTorch not installed — NN navigation unavailable")
        except Exception as e:
            _log.error("Failed to load NN model: %s", e)

    def get_action(self, lidar_scan_360):
        """Get motor commands from LiDAR scan.

        Args:
            lidar_scan_360: array of 360 distances in mm (one per degree)

        Returns:
            (vx, vy, vz) tuple of motor commands in m/s and rad/s.
            Returns (0, 0, 0) if model not available.
        """
        if not self.available or self.model is None:
            return 0.0, 0.0, 0.0

        try:
            # Downsample 360 → 36 bins (min distance per 10° bin)
            scan = np.array(lidar_scan_360, dtype=np.float32)
            if len(scan) != 360:
                # Resample to 360 if different
                indices = np.linspace(0, len(scan) - 1, 360).astype(int)
                scan = scan[indices]

            bins = scan.reshape(NUM_BINS, 10).min(axis=1)
            bins = np.clip(bins / MAX_RANGE_MM, 0.0, 1.0)

            # Inference
            with self.torch.no_grad():
                obs = self.torch.tensor(bins, dtype=self.torch.float32).unsqueeze(0)
                action = self.model(obs).numpy()[0]

            # Clip to [-1, 1] and map to real velocities
            action = np.clip(action, -1.0, 1.0)
            vx = action[0] * (VX_MAX - VX_MIN) / 2 + (VX_MAX + VX_MIN) / 2
            vy = action[1] * (VY_MAX - VY_MIN) / 2 + (VY_MAX + VY_MIN) / 2
            vz = action[2] * (VZ_MAX - VZ_MIN) / 2 + (VZ_MAX + VZ_MIN) / 2

            return float(vx), float(vy), float(vz)

        except Exception as e:
            _log.error("NN inference error: %s", e)
            return 0.0, 0.0, 0.0

    def reload(self, model_path=None):
        """Reload the model (e.g., after deploying a new version)."""
        path = model_path or self.model_path
        self.__init__(path)
