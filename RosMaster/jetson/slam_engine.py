"""
2D SLAM Engine for RosMaster X3.
Occupancy grid mapping with ICP + correlative scan matching.
Uses LiDAR scans to build a map and track robot pose.
Fuses LiDAR + depth camera data: in the overlapping forward ±30° zone,
only marks cells as occupied when both sensors agree.
Features: map persistence (save/load), wall line extraction, simple loop closure.
"""

import math
import os
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

# Correlative Scan Matcher (CSM) parameters
CSM_XY_RANGE = 200        # mm search window for translation
CSM_XY_STEP = 10          # mm step size for translation search
CSM_THETA_RANGE = 15      # degrees search window for rotation
CSM_THETA_STEP = 1.0      # degree step size for rotation search

# Loop closure parameters
LOOP_CLOSURE_DIST = 300   # mm — trigger when near a previous pose
LOOP_CLOSURE_MIN_SCANS = 200  # minimum scans between visit and revisit (~40 seconds)
LOOP_CLOSURE_SCAN_STORE = 50  # store scan every 50 scans for loop closure
LOOP_CLOSURE_COOLDOWN = 100   # minimum scans between consecutive closures

# Map persistence
MAP_DIR = "/home/jetson/RosMaster/maps"

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
        # Loop closure: store (pose, scan_points) at intervals
        self._scan_store = []  # list of (scan_idx, pose, points)
        self._loop_closure_count = 0
        # IMU-only heading: offset aligns IMU reference to SLAM coordinate system
        self._imu_yaw_offset = None   # set on first scan with IMU data
        # Debug: heading sources
        self._debug_icp_theta = 0.0   # last ICP heading change (rad)
        self._debug_imu_yaw = 0.0     # last raw IMU yaw (rad)
        self._debug_icp_quality = 0.0 # last ICP match quality
        self._debug_fused_theta = 0.0 # heading after fusion
        # Wall lines cache
        self._wall_lines = []
        self._wall_lines_scan = -1  # scan_count when last computed

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

    def _icp_with_quality(self, source, target):
        """Run ICP and return (transform, match_quality).

        match_quality = fraction of source points with a close match in target.
        Low quality (<0.3) means ICP likely failed.
        """
        if len(source) < 10 or len(target) < 10:
            return np.eye(3), 0.0

        # Subsample for speed
        if len(source) > 300:
            idx = np.random.choice(len(source), 300, replace=False)
            src_sub = source[idx]
        else:
            src_sub = source.copy()

        tree = KDTree(target)
        T = np.eye(3)

        for _ in range(ICP_MAX_ITER):
            dists, indices = tree.query(src_sub)
            mask = dists < ICP_MAX_DIST
            if mask.sum() < 5:
                break
            src = src_sub[mask]
            tgt = target[indices[mask]]
            src_mean = src.mean(axis=0)
            tgt_mean = tgt.mean(axis=0)
            H = (src - src_mean).T @ (tgt - tgt_mean)
            U, _, Vt = np.linalg.svd(H)
            R = Vt.T @ U.T
            if np.linalg.det(R) < 0:
                Vt[-1, :] *= -1
                R = Vt.T @ U.T
            t = tgt_mean - R @ src_mean
            src_sub = (R @ src_sub.T).T + t
            T_step = np.eye(3)
            T_step[:2, :2] = R
            T_step[:2, 2] = t
            T = T_step @ T
            if np.linalg.norm(t) < ICP_TOLERANCE:
                break

        # Compute match quality: fraction of points with close match
        final_dists, _ = tree.query(src_sub)
        quality = np.mean(final_dists < 100)  # fraction within 100mm
        return T, quality

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
            # Scan matching: ICP with quality check
            if self.prev_points is not None and len(self.prev_points) > 20:
                T, quality = self._icp_with_quality(local_points, self.prev_points)
                dx = T[0, 2]
                dy = T[1, 2]
                dtheta = math.atan2(T[1, 0], T[0, 0])

                trans = math.hypot(dx, dy)
                self._debug_icp_quality = quality
                self._debug_icp_theta = dtheta

                # --- Translation: ICP with reasonable cap ---
                # Max 50mm per scan (robot at 0.15m/s max, 5Hz = 30mm, plus margin)
                # Reject if quality too low or translation too large
                if quality < 0.2 or trans > 50:
                    dx, dy = 0, 0

                # Update position from ICP translation (rotated into world frame)
                cos_t = math.cos(self.pose[2])
                sin_t = math.sin(self.pose[2])
                self.pose[0] += cos_t * dx - sin_t * dy
                self.pose[1] += sin_t * dx + cos_t * dy

                # --- Heading: IMU primary + small ICP correction ---
                # Negate IMU yaw: IMU convention → math convention
                if imu_yaw is not None:
                    self._debug_imu_yaw = imu_yaw
                    imu_corrected = -imu_yaw

                    if self._imu_yaw_offset is None:
                        self._imu_yaw_offset = imu_corrected - self.pose[2]

                    # IMU heading (with offset)
                    imu_heading = imu_corrected - self._imu_yaw_offset
                    imu_heading = (imu_heading + math.pi) % (2 * math.pi) - math.pi

                    # If ICP quality is good and rotation is small, blend in ICP rotation
                    # 90% IMU + 10% ICP correction — IMU is primary, ICP just fine-tunes
                    if quality >= 0.4 and abs(math.degrees(dtheta)) < 10:
                        # Apply ICP rotation to current pose
                        icp_heading = self.pose[2] + dtheta
                        icp_heading = (icp_heading + math.pi) % (2 * math.pi) - math.pi
                        # Blend: 90% IMU, 10% ICP (via shortest angular path)
                        diff = (icp_heading - imu_heading + math.pi) % (2 * math.pi) - math.pi
                        self.pose[2] = imu_heading + 0.1 * diff
                    else:
                        # ICP unreliable — use IMU only
                        self.pose[2] = imu_heading

                    self.pose[2] = (self.pose[2] + math.pi) % (2 * math.pi) - math.pi

                self._debug_fused_theta = self.pose[2]

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

            # Store scan for loop closure (every Nth scan)
            if self.scan_count % LOOP_CLOSURE_SCAN_STORE == 0:
                self._scan_store.append((self.scan_count, self.pose.copy(), local_points.copy()))

            # Check loop closure
            self._check_loop_closure(local_points)

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
        """Return image with room boundary using concave hull (alpha shape).

        Extracts occupied cells, computes concave hull to find room outline,
        draws it as a closed polygon — showing the room boundary.
        """
        import cv2
        from scipy.spatial import Delaunay

        with self.lock:
            wall_mask = self.grid > 0.9
            wy, wx = np.where(wall_mask)

            img = np.zeros((GRID_SIZE, GRID_SIZE), dtype=np.uint8)

            if len(wx) < 10:
                return img

            points = np.column_stack((wx, wy)).astype(np.float64)

            # Compute concave hull via alpha shape
            boundary = self._alpha_shape(points, alpha=0.3)

            if boundary is not None and len(boundary) >= 3:
                # Draw filled boundary outline
                hull_pts = boundary.astype(np.int32).reshape((-1, 1, 2))
                cv2.polylines(img, [hull_pts], isClosed=True, color=255, thickness=2)

            # Also draw wall points as dim dots for reference
            for i in range(len(wx)):
                if 0 <= wx[i] < GRID_SIZE and 0 <= wy[i] < GRID_SIZE:
                    img[wy[i], wx[i]] = max(img[wy[i], wx[i]], 100)

            return img

    def _alpha_shape(self, points, alpha=0.3):
        """Compute concave hull (alpha shape) of 2D points.

        Args:
            points: Nx2 array of (x, y) coordinates
            alpha: controls concavity (smaller = more concave, 0 = convex hull)

        Returns:
            Ordered boundary points as Nx2 array, or None if failed.
        """
        if len(points) < 4:
            return None

        try:
            tri = Delaunay(points)
        except Exception:
            return None

        # Find boundary edges: edges that belong to triangles with
        # circumradius < 1/alpha
        edges = set()
        for simplex in tri.simplices:
            p0 = points[simplex[0]]
            p1 = points[simplex[1]]
            p2 = points[simplex[2]]

            # Circumradius of triangle
            a = np.linalg.norm(p0 - p1)
            b = np.linalg.norm(p1 - p2)
            c = np.linalg.norm(p2 - p0)
            s = (a + b + c) / 2.0

            area = max(s * (s - a) * (s - b) * (s - c), 1e-10)
            area = math.sqrt(area)
            if area < 1e-10:
                continue

            circumradius = (a * b * c) / (4.0 * area)

            if alpha > 0 and circumradius < 1.0 / alpha:
                # Add edges of this triangle
                for i, j in [(0, 1), (1, 2), (2, 0)]:
                    edge = tuple(sorted([simplex[i], simplex[j]]))
                    if edge in edges:
                        edges.remove(edge)  # internal edge (shared by 2 triangles)
                    else:
                        edges.add(edge)

        if not edges:
            # Fallback to convex hull
            from scipy.spatial import ConvexHull
            try:
                hull = ConvexHull(points)
                return points[hull.vertices]
            except Exception:
                return None

        # Order boundary edges into a polygon
        edge_list = list(edges)
        ordered = [edge_list[0][0], edge_list[0][1]]
        used = {0}

        for _ in range(len(edge_list)):
            last = ordered[-1]
            found = False
            for idx, (a, b) in enumerate(edge_list):
                if idx in used:
                    continue
                if a == last:
                    ordered.append(b)
                    used.add(idx)
                    found = True
                    break
                elif b == last:
                    ordered.append(a)
                    used.add(idx)
                    found = True
                    break
            if not found:
                break

        if len(ordered) < 3:
            return None

        return points[ordered]

    def _check_loop_closure(self, current_points):
        """Simple loop closure: when near a previously visited pose, correct drift.

        Strict conditions to avoid false triggers:
        - At least 200 scans (~40s) since the stored scan
        - At least 100 scans since last closure (cooldown)
        - Robot within 300mm of stored pose
        - ICP match quality > 0.4 (good overlap)
        - Correction < 150mm and < 8° (credible, not wild)
        """
        if len(self._scan_store) < 2:
            return
        cur_idx = self.scan_count

        # Cooldown: skip if we just did a closure
        last_closure_at = getattr(self, '_last_closure_scan', 0)
        if cur_idx - last_closure_at < LOOP_CLOSURE_COOLDOWN:
            return

        cur_pose = self.pose

        for stored_idx, stored_pose, stored_points in self._scan_store:
            if cur_idx - stored_idx < LOOP_CLOSURE_MIN_SCANS:
                continue
            dist = math.hypot(cur_pose[0] - stored_pose[0], cur_pose[1] - stored_pose[1])
            if dist > LOOP_CLOSURE_DIST:
                continue

            # Found revisit — run ICP with quality check
            T, quality = self._icp_with_quality(current_points, stored_points)
            if quality < 0.4:
                continue  # poor match, skip

            dx = T[0, 2]
            dy = T[1, 2]
            dtheta = math.atan2(T[1, 0], T[0, 0])
            correction = math.hypot(dx, dy)

            if correction < 150 and abs(math.degrees(dtheta)) < 8:
                self._loop_closure_count += 1
                self._last_closure_scan = cur_idx
                print(f"Loop closure #{self._loop_closure_count}: correction dx={dx:.0f} dy={dy:.0f} dtheta={math.degrees(dtheta):.1f}° quality={quality:.2f} (near scan {stored_idx})", flush=True)
                # Apply position correction only — heading comes from IMU
                cos_t = math.cos(self.pose[2])
                sin_t = math.sin(self.pose[2])
                self.pose[0] += cos_t * dx - sin_t * dy
                self.pose[1] += sin_t * dx + cos_t * dy
                # Do NOT apply dtheta — IMU handles heading
                break

    # --- Map Persistence ---

    def save_map(self, name="default"):
        """Save occupancy grid, pose, and history to disk."""
        os.makedirs(MAP_DIR, exist_ok=True)
        path = os.path.join(MAP_DIR, f"{name}.npz")
        with self.lock:
            np.savez_compressed(path,
                grid=self.grid,
                pose=self.pose,
                home_pose=self.home_pose,
                pose_history=np.array(self.pose_history),
                scan_count=np.array([self.scan_count]),
                ignore_angle=np.array([self.ignore_angle]),
            )
        print(f"Map saved to {path} ({os.path.getsize(path)} bytes)", flush=True)
        return {"ok": True, "path": path, "size": os.path.getsize(path)}

    def load_map(self, name="default"):
        """Load occupancy grid, pose, and history from disk."""
        path = os.path.join(MAP_DIR, f"{name}.npz")
        if not os.path.exists(path):
            return {"ok": False, "error": f"Map file not found: {path}"}
        with self.lock:
            data = np.load(path)
            self.grid = data["grid"]
            self.pose = data["pose"]
            self.home_pose = data["home_pose"]
            self.pose_history = [p for p in data["pose_history"]]
            self.scan_count = int(data["scan_count"][0])
            if "ignore_angle" in data:
                self.ignore_angle = float(data["ignore_angle"][0])
            self.prev_points = None
            self._scan_store.clear()
        print(f"Map loaded from {path}", flush=True)
        return {"ok": True, "path": path, "scan_count": self.scan_count}

    def list_maps(self):
        """List saved maps."""
        if not os.path.isdir(MAP_DIR):
            return []
        maps = []
        for f in os.listdir(MAP_DIR):
            if f.endswith(".npz"):
                path = os.path.join(MAP_DIR, f)
                maps.append({"name": f[:-4], "size": os.path.getsize(path),
                             "modified": os.path.getmtime(path)})
        return sorted(maps, key=lambda m: m["modified"], reverse=True)

    # --- Wall Line Extraction ---

    def extract_wall_lines(self, min_line_length=5):
        """Extract straight wall lines from occupancy grid using Hough Transform.

        Returns list of line segments: [(x1_mm, y1_mm, x2_mm, y2_mm), ...]
        Snaps lines to dominant angles (0°, 90°) when close.
        """
        import cv2

        # Only recompute every 20 scans
        if self.scan_count - self._wall_lines_scan < 20 and self._wall_lines:
            return self._wall_lines

        with self.lock:
            # Binary image: wall cells
            wall_img = np.zeros((GRID_SIZE, GRID_SIZE), dtype=np.uint8)
            wall_mask = self.grid > 1.5  # strong walls
            wall_img[wall_mask] = 255

        # Morphological cleanup: close small gaps
        kernel = np.ones((3, 3), dtype=np.uint8)
        wall_img = cv2.dilate(wall_img, kernel, iterations=1)
        wall_img = cv2.erode(wall_img, kernel, iterations=1)

        # Hough line detection
        lines = cv2.HoughLinesP(wall_img, rho=1, theta=np.pi/180,
                                threshold=8, minLineLength=min_line_length, maxLineGap=3)

        if lines is None:
            self._wall_lines = []
            self._wall_lines_scan = self.scan_count
            return []

        result = []
        for line in lines:
            x1, y1, x2, y2 = line[0]
            # Convert grid coords to world mm
            x1_mm = x1 * CELL_SIZE_MM
            y1_mm = y1 * CELL_SIZE_MM
            x2_mm = x2 * CELL_SIZE_MM
            y2_mm = y2 * CELL_SIZE_MM

            # Snap to 0° or 90° if close (within 5°)
            angle = math.atan2(y2_mm - y1_mm, x2_mm - x1_mm)
            angle_deg = math.degrees(angle) % 180
            if angle_deg < 5 or angle_deg > 175:
                # Horizontal — snap y values
                y_avg = (y1_mm + y2_mm) / 2
                y1_mm = y2_mm = y_avg
            elif 85 < angle_deg < 95:
                # Vertical — snap x values
                x_avg = (x1_mm + x2_mm) / 2
                x1_mm = x2_mm = x_avg

            result.append((round(x1_mm), round(y1_mm), round(x2_mm), round(y2_mm)))

        # Collapse parallel nearby lines into single lines, then merge collinear
        result = self._collapse_parallel_lines(result)
        result = self._merge_collinear_lines(result)

        self._wall_lines = result
        self._wall_lines_scan = self.scan_count
        return result

    def _collapse_parallel_lines(self, lines, angle_thresh=10, perp_dist_thresh=150):
        """Collapse nearby parallel lines into single representative lines.

        Groups lines by angle, then within each group finds clusters of
        parallel lines within perp_dist_thresh (perpendicular distance).
        Each cluster is replaced by one line at the average perpendicular
        position, spanning the full extent of all lines in the cluster.
        """
        if len(lines) < 2:
            return lines

        # Compute angle and perpendicular distance from origin for each line
        line_info = []
        for x1, y1, x2, y2 in lines:
            angle = math.atan2(y2 - y1, x2 - x1) % math.pi  # normalize to [0, pi)
            # Perpendicular distance from origin: project midpoint onto normal
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2
            nx, ny = -math.sin(angle), math.cos(angle)  # normal direction
            perp = mx * nx + my * ny
            length = math.hypot(x2 - x1, y2 - y1)
            line_info.append((angle, perp, x1, y1, x2, y2, length))

        # Group by angle
        angle_thresh_rad = math.radians(angle_thresh)
        used = [False] * len(line_info)
        result = []

        for i in range(len(line_info)):
            if used[i]:
                continue
            # Find all lines with similar angle
            group = [i]
            used[i] = True
            ai = line_info[i][0]

            for j in range(i + 1, len(line_info)):
                if used[j]:
                    continue
                aj = line_info[j][0]
                da = abs(ai - aj)
                if da > math.pi / 2:
                    da = math.pi - da
                if da < angle_thresh_rad:
                    group.append(j)
                    used[j] = True

            if len(group) == 1:
                idx = group[0]
                result.append((line_info[idx][2], line_info[idx][3],
                               line_info[idx][4], line_info[idx][5]))
                continue

            # Within this angle group, cluster by perpendicular distance
            group.sort(key=lambda idx: line_info[idx][1])
            clusters = []
            current_cluster = [group[0]]

            for k in range(1, len(group)):
                if abs(line_info[group[k]][1] - line_info[current_cluster[-1]][1]) < perp_dist_thresh:
                    current_cluster.append(group[k])
                else:
                    clusters.append(current_cluster)
                    current_cluster = [group[k]]
            clusters.append(current_cluster)

            # For each cluster, produce one merged line
            for cluster in clusters:
                if len(cluster) == 1:
                    idx = cluster[0]
                    result.append((line_info[idx][2], line_info[idx][3],
                                   line_info[idx][4], line_info[idx][5]))
                    continue

                # Average angle and perpendicular distance (weighted by length)
                total_len = sum(line_info[idx][6] for idx in cluster)
                if total_len < 1:
                    continue
                avg_angle = sum(line_info[idx][0] * line_info[idx][6] for idx in cluster) / total_len
                avg_perp = sum(line_info[idx][1] * line_info[idx][6] for idx in cluster) / total_len

                # Project all endpoints onto the averaged line direction
                cos_a = math.cos(avg_angle)
                sin_a = math.sin(avg_angle)
                projections = []
                for idx in cluster:
                    for px, py in [(line_info[idx][2], line_info[idx][3]),
                                   (line_info[idx][4], line_info[idx][5])]:
                        proj = px * cos_a + py * sin_a
                        projections.append(proj)

                proj_min = min(projections)
                proj_max = max(projections)

                # Reconstruct line from average perpendicular + projection extents
                nx, ny = -sin_a, cos_a  # normal
                base_x = avg_perp * nx
                base_y = avg_perp * ny
                x1 = round(base_x + proj_min * cos_a)
                y1 = round(base_y + proj_min * sin_a)
                x2 = round(base_x + proj_max * cos_a)
                y2 = round(base_y + proj_max * sin_a)
                result.append((x1, y1, x2, y2))

        return result

    def _merge_collinear_lines(self, lines, dist_thresh=200):
        """Merge collinear segments that overlap or are close end-to-end."""
        if len(lines) < 2:
            return lines

        merged = list(lines)
        changed = True
        while changed:
            changed = False
            new_merged = []
            used = set()
            for i in range(len(merged)):
                if i in used:
                    continue
                x1, y1, x2, y2 = merged[i]
                a1 = math.atan2(y2 - y1, x2 - x1)
                cos_a = math.cos(a1)
                sin_a = math.sin(a1)

                for j in range(i + 1, len(merged)):
                    if j in used:
                        continue
                    x3, y3, x4, y4 = merged[j]
                    a2 = math.atan2(y4 - y3, x4 - x3)
                    da = abs(a1 - a2)
                    if da > math.pi:
                        da = 2 * math.pi - da
                    if da > math.radians(10) and abs(da - math.pi) > math.radians(10):
                        continue

                    # Check perpendicular distance between line midpoints
                    mx, my = (x3 + x4) / 2 - (x1 + x2) / 2, (y3 + y4) / 2 - (y1 + y2) / 2
                    perp = abs(-sin_a * mx + cos_a * my)
                    if perp > 100:
                        continue

                    # Check endpoint proximity along line direction
                    d_min = min(math.hypot(x1-x3, y1-y3), math.hypot(x1-x4, y1-y4),
                                math.hypot(x2-x3, y2-y3), math.hypot(x2-x4, y2-y4))
                    if d_min < dist_thresh:
                        # Merge: project all 4 points, take extremes
                        pts = [(x1,y1), (x2,y2), (x3,y3), (x4,y4)]
                        projs = [(p[0]*cos_a + p[1]*sin_a, p) for p in pts]
                        projs.sort()
                        x1, y1 = projs[0][1]
                        x2, y2 = projs[-1][1]
                        merged[i] = (round(x1), round(y1), round(x2), round(y2))
                        used.add(j)
                        changed = True
                        break
                new_merged.append(merged[i])
            for i in range(len(merged)):
                if i not in used and merged[i] not in new_merged:
                    new_merged.append(merged[i])
            merged = new_merged

        return merged

    def get_heading_debug(self):
        """Return heading debug info for sensor debug UI."""
        return {
            "icp_dtheta": round(math.degrees(self._debug_icp_theta), 1),
            "imu_yaw": round(math.degrees(self._debug_imu_yaw), 1),
            "icp_quality": round(self._debug_icp_quality, 3),
            "fused_heading": round(math.degrees(self._debug_fused_theta), 1),
            "pose_heading": round(math.degrees(self.pose[2]), 1),
        }

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
            self._scan_store.clear()
            self._loop_closure_count = 0
            self._imu_yaw_offset = None
            self._wall_lines = []
            self._wall_lines_scan = -1
