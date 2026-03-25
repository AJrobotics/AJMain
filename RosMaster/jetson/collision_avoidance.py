"""
Collision Avoidance for RosMaster X3.
Uses RPLidar S2 (360°) and Orbbec Astra depth camera to prevent collisions.
Intercepts motor commands and scales speed based on proximity to obstacles.
"""

import math
import numpy as np

# Safety zone thresholds (mm)
STOP_DIST = 200       # Emergency stop
SLOW_DIST = 500       # 30% speed
CAUTION_DIST = 800    # 70% speed

# Sector definitions: 8 sectors of 45° each
# Sector 0 = front (337.5° to 22.5°), clockwise
NUM_SECTORS = 8
SECTOR_SIZE = 360.0 / NUM_SECTORS  # 45°

# Depth camera FOV (~60°), mapped to front 3 sub-zones
DEPTH_FOV_DEG = 60
DEPTH_WIDTH = 640
DEPTH_HEIGHT = 480

# Default rear ignore zone: 140° centered at 180° (backward)
# Covers 110°-250° to exclude cables/devices mounted at rear
DEFAULT_IGNORE_ANGLE = 140


class CollisionAvoidance:
    def __init__(self, lidar_reader=None, depth_reader=None):
        self.lidar = lidar_reader
        self.depth = depth_reader
        self.enabled = True
        self.ignore_angle = DEFAULT_IGNORE_ANGLE  # degrees to ignore at rear
        self._sector_distances = [9999] * NUM_SECTORS  # mm per sector

    def _angle_to_sector(self, angle_deg):
        """Convert angle (0=front, clockwise) to sector index."""
        adjusted = (angle_deg + SECTOR_SIZE / 2) % 360
        return int(adjusted / SECTOR_SIZE) % NUM_SECTORS

    def update_sectors(self):
        """Recompute sector distances from latest sensor data."""
        sectors = [9999.0] * NUM_SECTORS

        # --- LiDAR data (360° coverage, with rear ignore zone) ---
        if self.lidar:
            scan = self.lidar.get_scan()
            half_ignore = self.ignore_angle / 2.0
            for point in scan:
                angle = point["angle"]
                dist = point["dist"]
                if dist < 50:  # ignore very close readings (robot body/noise)
                    continue
                # Skip points in the rear ignore zone (centered at 180°)
                angle_from_rear = abs(((angle - 180) + 180) % 360 - 180)
                if angle_from_rear < half_ignore:
                    continue
                sector = self._angle_to_sector(angle)
                sectors[sector] = min(sectors[sector], dist)

        # --- Depth camera (forward FOV) ---
        if self.depth and self.depth.connected:
            try:
                self._fuse_depth(sectors)
            except Exception:
                pass

        self._sector_distances = sectors

    def _fuse_depth(self, sectors):
        """Fuse depth camera data into front sectors.

        Only uses rows 15%-35% from top (wall-level band) to avoid
        floor readings and robot body parts in lower rows.
        """
        if not hasattr(self.depth, '_get_raw_depth'):
            return

        depth_frame = self.depth._get_raw_depth()
        if depth_frame is None:
            return

        h, w = depth_frame.shape

        # Only use wall-level rows (15%-35% from top)
        row_start = int(h * 0.15)
        row_end = int(h * 0.35)
        wall_band = depth_frame[row_start:row_end, :]

        # Split into 3 zones: left-front, center-front, right-front
        third = w // 3
        zones = [
            wall_band[:, :third],         # left side of image = right-front (mirrored)
            wall_band[:, third:2*third],   # center
            wall_band[:, 2*third:],        # right side of image = left-front (mirrored)
        ]

        # Map to sectors: right-front=7, front=0, left-front=1
        zone_sectors = [7, 0, 1]

        for zone_data, sector_idx in zip(zones, zone_sectors):
            valid = zone_data[(zone_data > 300) & (zone_data < 8000)]  # ignore noise and robot body
            if len(valid) > 100:  # need substantial data
                min_depth = float(np.percentile(valid, 10))  # 10th percentile
                sectors[sector_idx] = min(sectors[sector_idx], min_depth)

    @property
    def min_dist(self):
        """Minimum distance across all active sectors."""
        return min(self._sector_distances)

    def filter_motion(self, vx, vy, vz):
        """Filter a motion command for safety.

        HIGHEST PRIORITY: collision avoidance always overrides any movement.

        Args:
            vx: forward/backward speed (m/s), positive=forward
            vy: left/right speed (m/s), positive=left
            vz: rotation speed (rad/s), positive=left

        Returns:
            (safe_vx, safe_vy, safe_vz) with speed scaled down near obstacles.
            Rotation (vz) is always allowed.
        """
        if not self.enabled:
            return vx, vy, vz

        self.update_sectors()

        # No translational movement — nothing to filter
        speed = math.sqrt(vx * vx + vy * vy)
        if speed < 0.001:
            return vx, vy, vz

        # Determine movement direction in degrees (0=front, clockwise)
        # vx=forward, vy=left → angle: atan2(-vy, vx) for clockwise convention
        move_angle_deg = math.degrees(math.atan2(-vy, vx)) % 360

        # Check wide arc: target sector + 2 neighbors on each side (5 sectors = 225°)
        target = self._angle_to_sector(move_angle_deg)
        neighbors = [
            (target - 2) % NUM_SECTORS,
            (target - 1) % NUM_SECTORS,
            target,
            (target + 1) % NUM_SECTORS,
            (target + 2) % NUM_SECTORS,
        ]

        # Find minimum distance in the wide movement arc
        min_dist = min(self._sector_distances[s] for s in neighbors)

        # Scale speed based on distance
        if min_dist < STOP_DIST:
            scale = 0.0
        elif min_dist < SLOW_DIST:
            scale = 0.3
        elif min_dist < CAUTION_DIST:
            scale = 0.7
        else:
            scale = 1.0

        return vx * scale, vy * scale, vz

    def get_sector_distances(self):
        """Return current sector distances for UI display."""
        return list(self._sector_distances)

    def get_status(self):
        """Return collision avoidance status for UI."""
        sectors = self._sector_distances
        min_dist = min(sectors)

        if min_dist < STOP_DIST:
            level = "STOP"
        elif min_dist < SLOW_DIST:
            level = "SLOW"
        elif min_dist < CAUTION_DIST:
            level = "CAUTION"
        else:
            level = "CLEAR"

        return {
            "enabled": self.enabled,
            "level": level,
            "min_dist": round(min_dist),
            "sectors": [round(d) for d in sectors],
            "ignore_angle": self.ignore_angle,
            "thresholds": {
                "stop": STOP_DIST,
                "slow": SLOW_DIST,
                "caution": CAUTION_DIST,
            },
        }
