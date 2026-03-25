"""
Autonomous explorer for RosMaster X3.
Uses SLAM map to find frontiers, navigate to them, and return home.
Only explores in directions with valid LiDAR coverage (avoids rear ignore zone).
"""

import math
import time
import threading
import heapq
import logging
import numpy as np

from slam_engine import GRID_SIZE, CELL_SIZE_MM

# File logger for explorer — persists across restarts
_log = logging.getLogger("explorer")
_log.setLevel(logging.DEBUG)
_fh = logging.FileHandler("/tmp/explorer.log")
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"))
_log.addHandler(_fh)
_log.addHandler(logging.StreamHandler())  # also print to stdout/journald

# Explorer parameters
EXPLORE_SPEED = 0.08       # m/s forward speed (slow for safety)
ROTATION_SPEED = 0.3       # rad/s — slow turn for sensor fusion accuracy
WAYPOINT_TOLERANCE = 150   # mm — how close to a waypoint before moving to next
FRONTIER_MIN_SIZE = 5      # minimum frontier cluster size to target
HEADING_TOLERANCE = 0.3    # radians — how aligned before moving forward
EXPLORE_UPDATE_HZ = 5      # how often the explorer loop runs
INITIAL_SCAN_SPEED = 0.2   # rad/s — very slow initial 360° scan


class Explorer:
    def __init__(self, slam=None, bot=None, collision=None, ignore_angle=120):
        self.slam = slam
        self.bot = bot
        self.collision = collision
        self.explore_speed = EXPLORE_SPEED  # can be changed live via API
        self.ignore_angle = ignore_angle

        self.state = "idle"  # idle, exploring, returning, arrived, stopped
        self.target = None   # (x_mm, y_mm) current target
        self.path = []       # list of (x_mm, y_mm) waypoints
        self.frontiers = []  # frontier cells for UI display
        self._abort = False
        self._thread = None
        self.lock = threading.Lock()

    def _move_filtered(self, vx, vy, vz):
        """Move with collision avoidance."""
        if self.collision and self.collision.enabled:
            self.collision.update_sectors()
            orig_vx, orig_vy = vx, vy
            vx, vy, vz = self.collision.filter_motion(vx, vy, vz)
            if orig_vx != 0 and vx == 0:
                sectors = self.collision.get_sector_distances()
                _log.warning(f"Collision BLOCKED vx={orig_vx:.2f} min={self.collision.min_dist:.0f}mm sectors={[round(s) for s in sectors]}")
        if self.bot:
            self.bot.set_car_motion(vx, vy, vz)
        return vx, vy, vz

    def _stop_motors(self):
        if self.bot:
            self.bot.set_car_motion(0, 0, 0)

    def _get_pose(self):
        if self.slam:
            return self.slam.get_pose()
        return np.array([0, 0, 0])

    def _angle_in_valid_zone(self, angle_rad):
        """Check if a heading is NOT in the rear ignore zone."""
        angle_deg = math.degrees(angle_rad) % 360
        angle_from_rear = abs(((angle_deg - 180) + 180) % 360 - 180)
        return angle_from_rear >= (self.ignore_angle / 2.0)

    def _find_frontiers(self):
        """Find frontier cells (free cells adjacent to unknown cells).

        Uses numpy vectorized operations for speed instead of per-pixel loops.
        """
        if not self.slam:
            return []

        grid = self.slam.get_map_image()

        # Free cells: pixel value > 200 (high probability of free)
        # Unknown cells: pixel value ~128
        free = grid > 200
        unknown = (grid > 100) & (grid < 160)

        # Vectorized neighbor check: free cell with any unknown neighbor
        unknown_neighbor = (
            unknown[:-2, 1:-1] |   # up
            unknown[2:, 1:-1] |    # down
            unknown[1:-1, :-2] |   # left
            unknown[1:-1, 2:]      # right
        )
        frontier_mask = free[1:-1, 1:-1] & unknown_neighbor

        # Get frontier coordinates
        fy, fx = np.where(frontier_mask)
        fx = fx + 1  # offset from 1:-1 slicing
        fy = fy + 1

        if len(fx) == 0:
            return []

        # Convert to world mm
        wx = fx * CELL_SIZE_MM
        wy = fy * CELL_SIZE_MM

        # Filter: only frontiers in valid direction (not rear ignore zone)
        pose = self._get_pose()
        dx = wx - pose[0]
        dy = wy - pose[1]
        angles = np.arctan2(dy, dx)
        relative = angles - pose[2]
        # Normalize to -pi..pi
        relative = (relative + np.pi) % (2 * np.pi) - np.pi
        # Convert to degrees and check ignore zone
        rel_deg = np.degrees(relative) % 360
        angle_from_rear = np.abs(((rel_deg - 180) + 180) % 360 - 180)
        valid = angle_from_rear >= (self.ignore_angle / 2.0)

        frontiers = list(zip(wx[valid].tolist(), wy[valid].tolist()))
        return frontiers

    def _cluster_frontiers(self, frontiers):
        """Group nearby frontier cells into clusters using grid-based binning.

        Fast O(n) approach: bin frontiers into coarse grid cells, then
        merge adjacent bins into clusters.
        """
        if not frontiers:
            return []

        bin_size = CELL_SIZE_MM * 4  # coarse bin = 200mm
        bins = {}

        for fx, fy in frontiers:
            key = (int(fx / bin_size), int(fy / bin_size))
            if key not in bins:
                bins[key] = []
            bins[key].append((fx, fy))

        # Merge adjacent bins into clusters
        visited = set()
        clusters = []

        for key in bins:
            if key in visited:
                continue
            # BFS through adjacent bins
            cluster_points = []
            queue = [key]
            visited.add(key)

            while queue:
                bk = queue.pop(0)
                if bk in bins:
                    cluster_points.extend(bins[bk])
                for dx in [-1, 0, 1]:
                    for dy in [-1, 0, 1]:
                        nk = (bk[0] + dx, bk[1] + dy)
                        if nk not in visited and nk in bins:
                            visited.add(nk)
                            queue.append(nk)

            if len(cluster_points) >= FRONTIER_MIN_SIZE:
                pts = np.array(cluster_points)
                cx, cy = pts.mean(axis=0)
                clusters.append((cx, cy, len(cluster_points)))

        # Sort by distance from robot (nearest first)
        pose = self._get_pose()
        clusters.sort(key=lambda c: math.hypot(c[0] - pose[0], c[1] - pose[1]))
        return clusters

    def _navigate_to(self, target_x, target_y):
        """Navigate to a target position using simple heading control."""
        turn_steps = 0  # count consecutive turn steps to detect spinning
        MAX_TURN_STEPS = 60  # ~12 seconds at 5 Hz — enough for full 180° turn
        while not self._abort:
            pose = self._get_pose()
            dx = target_x - pose[0]
            dy = target_y - pose[1]
            dist = math.hypot(dx, dy)

            if dist < WAYPOINT_TOLERANCE:
                return True  # reached

            # Compute heading to target
            target_heading = math.atan2(dy, dx)
            heading_error = target_heading - pose[2]
            # Normalize to [-pi, pi]
            heading_error = (heading_error + math.pi) % (2 * math.pi) - math.pi

            if abs(heading_error) > HEADING_TOLERANCE:
                # Rotate toward target — always turn the shorter direction
                vz = ROTATION_SPEED if heading_error > 0 else -ROTATION_SPEED
                # For large turns (>90°), use faster rotation
                if abs(heading_error) > math.radians(90):
                    vz *= 1.5
                self._move_filtered(0, 0, vz)
                turn_steps += 1
                _log.debug(f"TURN err={math.degrees(heading_error):.1f}° dist={dist:.0f}mm steps={turn_steps}")
                # Stuck spinning detection — if not converging after ~10 seconds
                if turn_steps > MAX_TURN_STEPS:
                    _log.warning(f"Stuck spinning ({turn_steps} steps), skipping target")
                    self._stop_motors()
                    return False
            else:
                # Move forward — check collision first
                turn_steps = 0  # reset spin counter
                vx = min(self.explore_speed, dist / 1000.0)
                actual_vx, _, _ = self._move_filtered(vx, 0, heading_error * 0.3)
                sectors = self.collision.get_sector_distances() if self.collision else [9999]*8
                _log.debug(f"FWD vx={vx:.3f}→{actual_vx:.3f} dist={dist:.0f}mm min_sec={min(sectors):.0f}mm")
                if actual_vx == 0 and vx > 0:
                    # Blocked by collision — skip this target
                    _log.warning(f"Collision blocked path to ({target_x:.0f}, {target_y:.0f}), sectors={[round(s) for s in sectors]}")
                    return False

            time.sleep(1.0 / EXPLORE_UPDATE_HZ)

        return False

    def _initial_scan(self):
        """Slow 360° scan at start to build initial map with both sensors."""
        _log.info("Initial scan: slow 360° rotation...")
        scan_time = 2 * math.pi / INITIAL_SCAN_SPEED  # time for full rotation
        start = time.time()
        while time.time() - start < scan_time and not self._abort:
            self._move_filtered(0, 0, INITIAL_SCAN_SPEED)
            time.sleep(1.0 / EXPLORE_UPDATE_HZ)
        self._stop_motors()
        time.sleep(0.5)
        _log.info("Initial scan complete")

    def _explore_loop(self):
        """Main exploration loop."""
        try:
            # Do initial slow scan to build map from both sensors
            self._initial_scan()

            while not self._abort and self.state == "exploring":
                # Find frontiers
                t0 = time.time()
                raw_frontiers = self._find_frontiers()
                with self.lock:
                    self.frontiers = raw_frontiers[:100]  # limit for UI

                clusters = self._cluster_frontiers(raw_frontiers)
                _log.info(f"Frontiers: {len(raw_frontiers)} raw, {len(clusters)} clusters ({time.time()-t0:.2f}s)")

                if not clusters:
                    # No more frontiers — exploration complete
                    self._stop_motors()
                    self.state = "arrived"
                    _log.info("Exploration complete — no more frontiers")
                    return

                # Navigate to nearest frontier cluster center
                target_x, target_y, size = clusters[0]
                with self.lock:
                    self.target = (target_x, target_y)

                pose = self._get_pose()
                _log.info(f"Navigate to ({target_x:.0f},{target_y:.0f}) size={size} from pose=({pose[0]:.0f},{pose[1]:.0f},{math.degrees(pose[2]):.0f}°)")
                reached = self._navigate_to(target_x, target_y)

                if not reached:
                    # Couldn't reach — try next frontier
                    time.sleep(0.5)
                    continue

                time.sleep(0.3)

        except Exception as e:
            _log.error(f"Explorer error: {e}", exc_info=True)
        finally:
            self._stop_motors()
            if self.state == "exploring":
                self.state = "stopped"

    def _return_loop(self):
        """Return to home position following pose history in reverse."""
        try:
            if not self.slam:
                self.state = "stopped"
                return

            waypoints = self.slam.get_pose_history()
            # Reverse and thin out (every 3rd waypoint)
            waypoints = waypoints[::-1][::3]

            # Add home pose as final destination
            home = self.slam.get_home_pose()
            waypoints.append(home)

            for wp in waypoints:
                if self._abort:
                    break
                with self.lock:
                    self.target = (wp[0], wp[1])
                reached = self._navigate_to(wp[0], wp[1])
                if not reached and self._abort:
                    break

            self._stop_motors()
            if not self._abort:
                self.state = "arrived"
                print("Returned home!")
            else:
                self.state = "stopped"

        except Exception as e:
            print(f"Return error: {e}")
            self._stop_motors()
            self.state = "stopped"

    def start_exploration(self):
        if self.state == "exploring" or self.state == "returning":
            return {"error": "Already running"}
        self._abort = False
        self.state = "exploring"
        self._thread = threading.Thread(target=self._explore_loop, daemon=True)
        self._thread.start()
        return {"ok": True}

    def return_home(self):
        if self.state == "returning":
            return {"error": "Already returning"}
        self.stop()
        time.sleep(0.5)
        self._abort = False
        self.state = "returning"
        self._thread = threading.Thread(target=self._return_loop, daemon=True)
        self._thread.start()
        return {"ok": True}

    def stop(self):
        self._abort = True
        self._stop_motors()
        self.state = "stopped"
        return {"ok": True}

    def get_status(self):
        with self.lock:
            return {
                "state": self.state,
                "target": self.target,
                "num_frontiers": len(self.frontiers),
            }

    def get_frontiers(self):
        with self.lock:
            return self.frontiers[:50]  # limit for WebSocket
