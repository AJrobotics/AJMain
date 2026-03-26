"""
Collision Avoidance for RosMaster X3.
Uses pre-computed sector distances from LiDAR and depth camera processes.
Intercepts motor commands and scales speed based on proximity to obstacles.

HIGHEST PRIORITY: No code path may bypass collision avoidance.
"""

import math

# Safety zone thresholds (mm)
STOP_DIST = 100       # Emergency stop
SLOW_DIST = 200       # 30% speed
CAUTION_DIST = 300    # 70% speed

# Sector definitions: 8 sectors of 45° each
# Sector 0 = front (337.5° to 22.5°), clockwise
NUM_SECTORS = 8
SECTOR_SIZE = 360.0 / NUM_SECTORS  # 45°


class CollisionAvoidance:
    def __init__(self, lidar_reader=None, depth_reader=None):
        self.lidar = lidar_reader
        self.depth = depth_reader
        self.enabled = True
        self.ignore_angle = 140  # for status reporting only (actual ignore is in LiDAR process)
        self._sector_distances = [9999] * NUM_SECTORS
        # Configurable thresholds (can be changed at runtime via API)
        self.stop_dist = STOP_DIST
        self.slow_dist = SLOW_DIST
        self.caution_dist = CAUTION_DIST

    def update_sectors(self):
        """Recompute sector distances from shared memory.

        LiDAR process pre-computes 8 sector distances (with rear ignore zone).
        Depth process pre-computes 3 front sector distances.
        We just read and fuse them here — no raw data processing.
        """
        # Read LiDAR sector distances from shared memory
        if self.lidar:
            sectors = self.lidar.get_lidar_sectors()  # 8 floats
        else:
            sectors = [9999.0] * NUM_SECTORS

        # Fuse depth front sectors from shared memory
        if self.depth and self.depth.connected:
            try:
                depth_sectors = self.depth.get_depth_sectors()  # [right-front, front, left-front]
                sectors[7] = min(sectors[7], depth_sectors[0])  # right-front
                sectors[0] = min(sectors[0], depth_sectors[1])  # front
                sectors[1] = min(sectors[1], depth_sectors[2])  # left-front
            except Exception:
                pass

        self._sector_distances = sectors

    def _angle_to_sector(self, angle_deg):
        """Convert angle (0=front, clockwise) to sector index."""
        adjusted = (angle_deg + SECTOR_SIZE / 2) % 360
        return int(adjusted / SECTOR_SIZE) % NUM_SECTORS

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

        # HARD SAFETY: if ANY non-ignored sector is below STOP distance,
        # block ALL translational motion regardless of movement direction.
        global_min = min(self._sector_distances)
        if global_min < self.stop_dist:
            return 0, 0, vz  # only rotation allowed

        # Directional check: scale speed based on obstacles in movement direction
        move_angle_deg = math.degrees(math.atan2(-vy, vx)) % 360
        target = self._angle_to_sector(move_angle_deg)
        neighbors = [
            (target - 2) % NUM_SECTORS,
            (target - 1) % NUM_SECTORS,
            target,
            (target + 1) % NUM_SECTORS,
            (target + 2) % NUM_SECTORS,
        ]
        dir_min = min(self._sector_distances[s] for s in neighbors)

        # Scale speed: use the WORSE of global and directional minimums
        check_min = min(global_min, dir_min)
        if check_min < self.stop_dist:
            scale = 0.0
        elif check_min < self.slow_dist:
            scale = 0.3
        elif check_min < self.caution_dist:
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

        if min_dist < self.stop_dist:
            level = "STOP"
        elif min_dist < self.slow_dist:
            level = "SLOW"
        elif min_dist < self.caution_dist:
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
                "stop": self.stop_dist,
                "slow": self.slow_dist,
                "caution": self.caution_dist,
            },
        }
