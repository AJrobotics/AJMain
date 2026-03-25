"""
2D SLAM Engine for RosMaster X3.
Occupancy grid mapping with ICP scan matching.
Uses LiDAR scans to build a map and track robot pose.
Fuses LiDAR + depth camera data: in the overlapping forward ±30° zone,
only marks cells as occupied when both sensors agree.
"""

import math
import time
import threading
import numpy as np
from scipy.spatial import KDTree

# Grid parameters
GRID_SIZE = 600           # pixels (600x600)
CELL_SIZE_MM = 50         # 50mm per cell = 30m x 30m area
MAP_SIZE_MM = GRID_SIZE * CELL_SIZE_MM

# Log-odds parameters
L_FREE = -0.4
L_OCC = 0.9
L_OCC_FUSED = 1.5        # higher confidence when both sensors agree
L_OCC_SINGLE = 0.3       # lower confidence when only LiDAR in overlap zone
L_MIN = -5.0
L_MAX = 4.0

# ICP parameters
ICP_MAX_ITER = 20
ICP_TOLERANCE = 0.5  # mm convergence threshold
ICP_MAX_DIST = 500   # mm max correspondence distance

# Sensor fusion parameters
DEPTH_FOV_HALF = 30.0     # depth camera half-FOV in degrees
FUSION_DIST_TOL = 300     # mm tolerance for matching LiDAR vs depth distance


class SLAMEngine:
    def __init__(self, ignore_angle=120):
        self.grid = np.zeros((GRID_SIZE, GRID_SIZE), dtype=np.float32)
        self.pose = np.array([MAP_SIZE_MM / 2, MAP_SIZE_MM / 2, 0.0])  # x, y, theta (mm, mm, rad)
        self.home_pose = self.pose.copy()
        self.pose_history = [self.pose.copy()]
        self.prev_points = None
        self.ignore_angle = ignore_angle  # degrees to ignore at rear
        self.lock = threading.Lock()
        self.scan_count = 0

    def _filter_scan(self, scan):
        """Filter scan: remove rear ignore zone, convert to cartesian."""
        half_ignore = self.ignore_angle / 2.0
        points = []
        for p in scan:
            angle_deg = p["angle"]
            dist_mm = p["dist"]
            if dist_mm < 50 or dist_mm > 12000:
                continue
            # Skip rear ignore zone
            angle_from_rear = abs(((angle_deg - 180) + 180) % 360 - 180)
            if angle_from_rear < half_ignore:
                continue
            rad = math.radians(angle_deg)
            x = dist_mm * math.cos(rad)
            y = dist_mm * math.sin(rad)
            points.append([x, y])
        return np.array(points) if points else np.zeros((0, 2))

    def _icp(self, source, target):
        """Simple ICP: find transform from source to target point cloud."""
        if len(source) < 10 or len(target) < 10:
            return np.eye(3)

        # Subsample for speed
        if len(source) > 300:
            idx = np.random.choice(len(source), 300, replace=False)
            source = source[idx]

        tree = KDTree(target)
        T = np.eye(3)

        for _ in range(ICP_MAX_ITER):
            # Find closest points
            dists, indices = tree.query(source)
            mask = dists < ICP_MAX_DIST
            if mask.sum() < 5:
                break

            src = source[mask]
            tgt = target[indices[mask]]

            # Compute centroids
            src_mean = src.mean(axis=0)
            tgt_mean = tgt.mean(axis=0)

            # Center points
            src_c = src - src_mean
            tgt_c = tgt - tgt_mean

            # SVD for rotation
            H = src_c.T @ tgt_c
            U, _, Vt = np.linalg.svd(H)
            R = Vt.T @ U.T
            if np.linalg.det(R) < 0:
                Vt[-1, :] *= -1
                R = Vt.T @ U.T

            t = tgt_mean - R @ src_mean

            # Apply transform
            source = (R @ source.T).T + t

            # Build homogeneous transform
            T_step = np.eye(3)
            T_step[:2, :2] = R
            T_step[:2, 2] = t
            T = T_step @ T

            # Check convergence
            if np.linalg.norm(t) < ICP_TOLERANCE:
                break

        return T

    def _world_points(self, local_points, pose):
        """Transform local points to world coordinates using pose."""
        x, y, theta = pose
        cos_t = math.cos(theta)
        sin_t = math.sin(theta)
        R = np.array([[cos_t, -sin_t], [sin_t, cos_t]])
        world = (R @ local_points.T).T + np.array([x, y])
        return world

    def _update_grid(self, world_points, pose):
        """Update occupancy grid with Bresenham ray casting."""
        ox = int(pose[0] / CELL_SIZE_MM)
        oy = int(pose[1] / CELL_SIZE_MM)

        for wp in world_points:
            ex = int(wp[0] / CELL_SIZE_MM)
            ey = int(wp[1] / CELL_SIZE_MM)

            # Bresenham line from robot to endpoint
            cells = self._bresenham(ox, oy, ex, ey)

            # Free cells along the ray
            for cx, cy in cells[:-1]:
                if 0 <= cx < GRID_SIZE and 0 <= cy < GRID_SIZE:
                    self.grid[cy, cx] = max(L_MIN, self.grid[cy, cx] + L_FREE)

            # Occupied cell at endpoint
            if 0 <= ex < GRID_SIZE and 0 <= ey < GRID_SIZE:
                self.grid[ey, ex] = min(L_MAX, self.grid[ey, ex] + L_OCC)

    def _update_grid_fused(self, scan, world_points, pose, depth_lookup):
        """Update occupancy grid with sensor fusion.

        For points in the depth camera's FOV (±30° forward):
        - Both sensors agree → high confidence occupied (L_OCC_FUSED)
        - Depth contradicts LiDAR → low confidence (L_OCC_SINGLE)
        - No depth data → normal LiDAR confidence (L_OCC)

        For points outside depth FOV: normal LiDAR-only update (L_OCC).
        Free space rays are always applied regardless of sensor fusion.
        """
        ox = int(pose[0] / CELL_SIZE_MM)
        oy = int(pose[1] / CELL_SIZE_MM)

        for i, wp in enumerate(world_points):
            ex = int(wp[0] / CELL_SIZE_MM)
            ey = int(wp[1] / CELL_SIZE_MM)

            # Free cells along the ray (always applied)
            cells = self._bresenham(ox, oy, ex, ey)
            for cx, cy in cells[:-1]:
                if 0 <= cx < GRID_SIZE and 0 <= cy < GRID_SIZE:
                    self.grid[cy, cx] = max(L_MIN, self.grid[cy, cx] + L_FREE)

            # Determine occupancy confidence based on sensor fusion
            if 0 <= ex < GRID_SIZE and 0 <= ey < GRID_SIZE:
                # Get original scan angle and distance for this point
                if i < len(scan):
                    angle_deg = scan[i]["angle"]
                    lidar_dist = scan[i]["dist"]
                else:
                    # Fallback: use normal confidence
                    self.grid[ey, ex] = min(L_MAX, self.grid[ey, ex] + L_OCC)
                    continue

                if self._is_in_depth_fov(angle_deg):
                    # In overlap zone: check depth confirmation
                    confirmed = self._check_depth_confirms(angle_deg, lidar_dist, depth_lookup)
                    if confirmed is True:
                        # Both sensors agree — high confidence
                        self.grid[ey, ex] = min(L_MAX, self.grid[ey, ex] + L_OCC_FUSED)
                    elif confirmed is False:
                        # Sensors disagree — low confidence (likely noise)
                        self.grid[ey, ex] = min(L_MAX, self.grid[ey, ex] + L_OCC_SINGLE)
                    else:
                        # No depth data at this angle — normal confidence
                        self.grid[ey, ex] = min(L_MAX, self.grid[ey, ex] + L_OCC)
                else:
                    # Outside depth FOV — LiDAR only, normal confidence
                    self.grid[ey, ex] = min(L_MAX, self.grid[ey, ex] + L_OCC)

    def _bresenham(self, x0, y0, x1, y1):
        """Bresenham's line algorithm."""
        cells = []
        dx = abs(x1 - x0)
        dy = abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx - dy

        while True:
            cells.append((x0, y0))
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 > -dy:
                err -= dy
                x0 += sx
            if e2 < dx:
                err += dx
                y0 += sy
        return cells

    def _build_depth_lookup(self, depth_line):
        """Build a lookup dict from depth line: angle_deg → distance_mm.

        Returns dict mapping integer angle to depth distance for fast lookup.
        """
        if not depth_line:
            return {}
        lookup = {}
        for angle_deg, dist_mm in depth_line:
            key = int(round(angle_deg))
            lookup[key] = dist_mm
        return lookup

    def _is_in_depth_fov(self, angle_deg):
        """Check if a LiDAR angle falls within depth camera FOV (±30° from forward)."""
        # Normalize angle to -180..180, where 0 = forward
        a = angle_deg % 360
        if a > 180:
            a -= 360
        return abs(a) <= DEPTH_FOV_HALF

    def _check_depth_confirms(self, angle_deg, lidar_dist, depth_lookup):
        """Check if depth camera confirms a LiDAR reading at this angle.

        Returns:
            True  — depth confirms (distance agrees within tolerance)
            False — depth contradicts (distance differs significantly)
            None  — depth has no data at this angle (no confirmation possible)
        """
        a = angle_deg % 360
        if a > 180:
            a -= 360
        key = int(round(a))

        # Check nearby angles too (±2°) for robustness
        for offset in [0, -1, 1, -2, 2]:
            depth_dist = depth_lookup.get(key + offset)
            if depth_dist is not None and depth_dist > 0:
                if abs(lidar_dist - depth_dist) < FUSION_DIST_TOL:
                    return True   # confirmed
                else:
                    return False  # contradicted
        return None  # no depth data at this angle

    def update(self, scan, imu_yaw=None, depth_line=None):
        """Process a new LiDAR scan, update pose and map.

        Args:
            scan: list of {angle, dist} from LiDAR
            imu_yaw: IMU yaw in radians (optional)
            depth_line: list of (angle_deg, dist_mm) from depth camera (optional)
                In the overlapping ±30° zone, only marks cells as occupied
                when both LiDAR and depth camera agree on distance.
        """
        local_points = self._filter_scan(scan)
        if len(local_points) < 20:
            return

        # Build depth lookup for sensor fusion
        depth_lookup = self._build_depth_lookup(depth_line)

        with self.lock:
            # Scan matching via ICP
            if self.prev_points is not None and len(self.prev_points) > 20:
                T = self._icp(local_points, self.prev_points)
                # Extract translation and rotation from transform
                dx = T[0, 2]
                dy = T[1, 2]
                dtheta = math.atan2(T[1, 0], T[0, 0])

                # Update pose
                cos_t = math.cos(self.pose[2])
                sin_t = math.sin(self.pose[2])
                self.pose[0] += cos_t * dx - sin_t * dy
                self.pose[1] += sin_t * dx + cos_t * dy
                self.pose[2] += dtheta

                # Use IMU yaw as correction if available
                if imu_yaw is not None:
                    # Blend IMU and ICP rotation (IMU is more reliable for rotation)
                    self.pose[2] = 0.7 * self.pose[2] + 0.3 * imu_yaw

            self.prev_points = local_points.copy()

            # Transform points to world and update grid with sensor fusion
            world_pts = self._world_points(local_points, self.pose)
            if depth_lookup:
                self._update_grid_fused(scan, world_pts, self.pose, depth_lookup)
            else:
                self._update_grid(world_pts, self.pose)

            # Store pose history (every 5th scan to save memory)
            self.scan_count += 1
            if self.scan_count % 5 == 0:
                self.pose_history.append(self.pose.copy())

    def get_map_image(self):
        """Return occupancy grid as clean uint8 image.

        Uses thresholds to show only high-confidence features:
        - Solid black walls: cells with log-odds > 2.0 (confirmed by multiple scans)
        - White free space: cells with log-odds < -1.5 (cleared by rays)
        - Gray unknown: everything else (not enough evidence)
        This eliminates noisy single-scan artifacts.
        """
        with self.lock:
            img = np.full((GRID_SIZE, GRID_SIZE), 128, dtype=np.uint8)  # gray = unknown

            # Free space: log-odds clearly negative
            free_mask = self.grid < -1.5
            img[free_mask] = 240  # near-white

            # Occupied: log-odds clearly positive (multiple confirmations)
            wall_mask = self.grid > 2.0
            img[wall_mask] = 0  # black = solid wall

            # Weak evidence of occupancy (1-2 scans): show as dark gray hint
            hint_mask = (self.grid > 0.8) & (self.grid <= 2.0)
            img[hint_mask] = 80  # dark gray hint

            return img

    def get_walls_image(self):
        """Return image showing only confirmed walls (white on black)."""
        with self.lock:
            img = np.zeros((GRID_SIZE, GRID_SIZE), dtype=np.uint8)
            # Cells with decent confidence (log-odds > 0.9 = at least 1 full scan)
            wall_mask = self.grid > 0.9
            img[wall_mask] = 255
            return img

    def get_pose(self):
        with self.lock:
            return self.pose.copy()

    def get_pose_history(self):
        with self.lock:
            return [p.copy() for p in self.pose_history]

    def get_home_pose(self):
        return self.home_pose.copy()

    def reset(self):
        with self.lock:
            self.grid[:] = 0
            self.pose = np.array([MAP_SIZE_MM / 2, MAP_SIZE_MM / 2, 0.0])
            self.home_pose = self.pose.copy()
            self.pose_history = [self.pose.copy()]
            self.prev_points = None
            self.scan_count = 0
