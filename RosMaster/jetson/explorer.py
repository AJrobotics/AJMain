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

        self.state = "idle"  # idle, exploring, scanning, returning, arrived, stopped
        self.target = None   # (x_mm, y_mm) current target
        self.path = []       # list of (x_mm, y_mm) waypoints
        self.frontiers = []  # frontier cells for UI display
        self._abort = False
        self._thread = None
        self.lock = threading.Lock()
        self._blocked_zones = []  # list of (angle_rad, expire_time)
        self.time_limit = 300     # seconds (default 5 minutes, 0 = no limit)

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
        MAX_TURN_STEPS = 30  # ~6 seconds at 5 Hz
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
                    # Blocked by collision — back up then skip
                    _log.warning(f"Collision blocked path to ({target_x:.0f}, {target_y:.0f}), sectors={[round(s) for s in sectors]}")
                    _log.info("Backing up 100mm...")
                    for _ in range(10):  # 10 steps at 5Hz = 2 seconds
                        if self._abort:
                            break
                        self._move_filtered(-0.05, 0, 0)
                        time.sleep(1.0 / EXPLORE_UPDATE_HZ)
                    self._stop_motors()
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

    def _is_blocked_direction(self, target_x, target_y):
        """Check if direction to target is in a blocked zone."""
        now = time.time()
        pose = self._get_pose()
        target_angle = math.atan2(target_y - pose[1], target_x - pose[0])

        # Clean expired blocked zones
        self._blocked_zones = [(a, t) for a, t in self._blocked_zones if t > now]

        for blocked_angle, expire in self._blocked_zones:
            diff = abs(target_angle - blocked_angle)
            if diff > math.pi:
                diff = 2 * math.pi - diff
            if diff < math.radians(30):  # within ±30° of blocked direction
                return True
        return False

    def _block_direction(self, target_x, target_y):
        """Block the direction to a failed target for 30 seconds."""
        pose = self._get_pose()
        angle = math.atan2(target_y - pose[1], target_x - pose[0])
        self._blocked_zones.append((angle, time.time() + 30))
        _log.info(f"Blocked direction {math.degrees(angle):.0f}° for 30s")

    def _explore_loop(self):
        """Main exploration loop with blocked zones, time limit, and periodic re-scan."""
        try:
            explore_start = time.time()
            last_rescan = time.time()
            consecutive_failures = 0

            # Do initial slow scan to build map from both sensors
            self._initial_scan()

            while not self._abort and self.state == "exploring":
                # Check time limit
                if self.time_limit > 0 and (time.time() - explore_start) > self.time_limit:
                    self._stop_motors()
                    self.state = "arrived"
                    elapsed = time.time() - explore_start
                    _log.info(f"Time limit reached ({elapsed:.0f}s). Exploration complete.")
                    return

                # Periodic re-scan every 60 seconds
                if time.time() - last_rescan > 60:
                    _log.info("Periodic re-scan...")
                    self._rotate_by(360, speed=0.3)
                    last_rescan = time.time()

                # Find frontiers
                t0 = time.time()
                raw_frontiers = self._find_frontiers()
                with self.lock:
                    self.frontiers = raw_frontiers[:100]

                clusters = self._cluster_frontiers(raw_frontiers)
                _log.info(f"Frontiers: {len(raw_frontiers)} raw, {len(clusters)} clusters ({time.time()-t0:.2f}s)")

                if not clusters:
                    self._stop_motors()
                    self.state = "arrived"
                    _log.info("Exploration complete — no more frontiers")
                    return

                # Try up to 3 clusters, skip blocked directions
                navigated = False
                for cluster_idx, (target_x, target_y, size) in enumerate(clusters[:5]):
                    if self._is_blocked_direction(target_x, target_y):
                        _log.debug(f"Skipping blocked cluster ({target_x:.0f},{target_y:.0f})")
                        continue

                    with self.lock:
                        self.target = (target_x, target_y)

                    pose = self._get_pose()
                    _log.info(f"Navigate to ({target_x:.0f},{target_y:.0f}) size={size} [#{cluster_idx}] from pose=({pose[0]:.0f},{pose[1]:.0f},{math.degrees(pose[2]):.0f}°)")
                    reached = self._navigate_to(target_x, target_y)

                    if reached:
                        consecutive_failures = 0
                        navigated = True
                        break
                    else:
                        # Block this direction
                        self._block_direction(target_x, target_y)
                        consecutive_failures += 1

                if not navigated:
                    # All clusters blocked — do a re-scan to find new paths
                    if consecutive_failures >= 5:
                        _log.info(f"All directions blocked ({consecutive_failures} failures). Re-scanning...")
                        self._blocked_zones.clear()
                        self._rotate_by(360, speed=0.3)
                        last_rescan = time.time()
                        consecutive_failures = 0
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

    def start_exploration(self, time_limit=None):
        if self.state == "exploring" or self.state == "returning":
            return {"error": "Already running"}
        if time_limit is not None:
            self.time_limit = time_limit
        self._abort = False
        self._blocked_zones.clear()
        self.state = "exploring"
        self._thread = threading.Thread(target=self._explore_loop, daemon=True)
        self._thread.start()
        _log.info(f"Exploration started (time_limit={self.time_limit}s)")
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

    def start_floor_plan(self, time_limit=None):
        """Random walk with collision avoidance to fill wall gaps."""
        if self.state not in ("idle", "stopped"):
            return {"error": f"Explorer busy: {self.state}"}
        if time_limit is not None:
            self.time_limit = time_limit
        self._abort = False
        self.state = "exploring"
        self._thread = threading.Thread(target=self._floor_plan_loop, daemon=True)
        self._thread.start()
        _log.info(f"Floor plan started (time_limit={self.time_limit}s)")
        return {"ok": True}

    def _floor_plan_loop(self):
        """Random walk: move forward, turn on collision, repeat."""
        import random
        try:
            explore_start = time.time()

            # Initial scan
            self._initial_scan()

            while not self._abort and self.state == "exploring":
                # Check time limit
                if self.time_limit > 0 and (time.time() - explore_start) > self.time_limit:
                    self._stop_motors()
                    self.state = "arrived"
                    _log.info(f"Floor plan time limit reached ({time.time() - explore_start:.0f}s)")
                    return

                # Move forward until collision
                blocked = False
                fwd_start = time.time()
                while not self._abort and not blocked:
                    if self.time_limit > 0 and (time.time() - explore_start) > self.time_limit:
                        break
                    vx = self.explore_speed
                    actual_vx, _, _ = self._move_filtered(vx, 0, 0)
                    if actual_vx == 0:
                        blocked = True
                        _log.info(f"Blocked after {time.time() - fwd_start:.1f}s forward")
                    time.sleep(1.0 / EXPLORE_UPDATE_HZ)

                if self._abort:
                    break

                self._stop_motors()

                # Back up a little
                _log.debug("Backing up...")
                for _ in range(5):
                    if self._abort:
                        break
                    self._move_filtered(-0.04, 0, 0)
                    time.sleep(1.0 / EXPLORE_UPDATE_HZ)
                self._stop_motors()

                # Turn random angle (90-270 degrees, random direction)
                turn_deg = random.randint(90, 270)
                if random.random() < 0.5:
                    turn_deg = -turn_deg
                _log.info(f"Turning {turn_deg}°")
                self._rotate_by(turn_deg, speed=0.3)

                # Check if forward is clear after turning
                if self.collision:
                    self.collision.update_sectors()
                    sectors = self.collision.get_sector_distances()
                    front_min = min(sectors[0], sectors[7], sectors[1])
                    if front_min < 500:
                        # Still blocked — try another direction
                        _log.debug(f"Still blocked after turn (front={front_min}mm), turning more")
                        self._rotate_by(random.choice([-90, 90, 180]), speed=0.3)

                time.sleep(0.3)

            _log.info("Floor plan complete")
        except Exception as e:
            _log.error(f"Floor plan error: {e}", exc_info=True)
        finally:
            self._stop_motors()
            if self.state == "exploring":
                self.state = "idle"

    def start_wall_follow(self, time_limit=None, direction="right", wall_dist=500):
        """Wall-following exploration: follow walls to trace room perimeter."""
        if self.state not in ("idle", "stopped"):
            return {"error": f"Explorer busy: {self.state}"}
        if time_limit is not None:
            self.time_limit = time_limit
        self._wall_follow_dir = direction  # "right" or "left"
        self._wall_follow_dist = wall_dist
        self._abort = False
        self.state = "exploring"
        self._thread = threading.Thread(target=self._wall_follow_loop, daemon=True)
        self._thread.start()
        _log.info(f"Wall follow started (dir={direction}, dist={wall_dist}mm, time_limit={self.time_limit}s)")
        return {"ok": True}

    def _wall_follow_loop(self):
        """Follow walls: keep wall on one side at target distance."""
        try:
            explore_start = time.time()
            target_wall_dist = getattr(self, '_wall_follow_dist', 500)
            wall_tolerance = 150    # mm — acceptable range ±

            # Initial scan
            self._initial_scan()

            # Find the nearest wall and orient toward it
            _log.info("Finding nearest wall...")
            if self.collision:
                self.collision.update_sectors()
                sectors = self.collision.get_sector_distances()
                # Find sector with closest wall (excluding rear ignore)
                min_idx = min(range(8), key=lambda i: sectors[i])
                # Turn to face the wall
                target_angle = min_idx * 45  # sector center angle
                _log.info(f"Nearest wall in sector {min_idx} ({target_angle}°) at {sectors[min_idx]}mm")
                self._rotate_by(target_angle, speed=0.3)
                # Now turn 90° so wall is on the chosen side
                if self._wall_follow_dir == "right":
                    self._rotate_by(-90, speed=0.3)
                else:
                    self._rotate_by(90, speed=0.3)

            while not self._abort and self.state == "exploring":
                # Check time limit
                if self.time_limit > 0 and (time.time() - explore_start) > self.time_limit:
                    self._stop_motors()
                    self.state = "arrived"
                    _log.info(f"Wall follow time limit reached ({time.time() - explore_start:.0f}s)")
                    return

                if not self.collision:
                    time.sleep(0.2)
                    continue

                self.collision.update_sectors()
                sectors = self.collision.get_sector_distances()

                # Wall sensor: sector to the right (sector 2) or left (sector 6)
                if self._wall_follow_dir == "right":
                    wall_dist = sectors[2]  # right side
                    wall_front_dist = sectors[1]  # right-front
                else:
                    wall_dist = sectors[6]  # left side
                    wall_front_dist = sectors[7]  # left-front

                front_dist = sectors[0]

                # Steering logic — use lateral strafe (mecanum) instead of turning
                vx = self.explore_speed
                vy = 0
                vz = 0

                if front_dist < 500:
                    # Wall ahead — stop and turn 90° away from wall side
                    self._stop_motors()
                    _log.info(f"Corner: wall ahead ({front_dist}mm), turning 90°")
                    if self._wall_follow_dir == "right":
                        self._rotate_by(90, speed=0.3)  # turn left
                    else:
                        self._rotate_by(-90, speed=0.3)  # turn right
                    continue
                elif wall_dist > 1500:
                    # Lost wall — inside corner, turn 90° toward wall to follow it
                    self._stop_motors()
                    _log.info(f"Inside corner: lost wall ({wall_dist}mm), turning 90° toward wall")
                    if self._wall_follow_dir == "right":
                        self._rotate_by(-90, speed=0.3)  # turn right toward wall
                    else:
                        self._rotate_by(90, speed=0.3)  # turn left toward wall
                    continue
                elif wall_dist < target_wall_dist - wall_tolerance:
                    # Too close to wall — strafe away (no turning)
                    if self._wall_follow_dir == "right":
                        vy = -0.05  # strafe left (away from right wall)
                    else:
                        vy = 0.05   # strafe right (away from left wall)
                    _log.debug(f"Too close ({wall_dist}mm), strafing away")
                elif wall_dist > target_wall_dist + wall_tolerance:
                    # Too far from wall — strafe toward (no turning)
                    if self._wall_follow_dir == "right":
                        vy = 0.05   # strafe right (toward right wall)
                    else:
                        vy = -0.05  # strafe left (toward left wall)
                    _log.debug(f"Too far ({wall_dist}mm), strafing toward")

                actual_vx, _, _ = self._move_filtered(vx, vy, vz)
                if actual_vx == 0 and vx > 0:
                    # Collision — turn 90° away from wall side
                    _log.info("Collision during wall follow, turning away")
                    if self._wall_follow_dir == "right":
                        self._rotate_by(90, speed=0.3)
                    else:
                        self._rotate_by(-90, speed=0.3)

                time.sleep(1.0 / EXPLORE_UPDATE_HZ)

        except Exception as e:
            _log.error(f"Wall follow error: {e}", exc_info=True)
        finally:
            self._stop_motors()
            if self.state == "exploring":
                self.state = "idle"

    def start_spiral(self, time_limit=None):
        """Spiral outward exploration from current position."""
        if self.state not in ("idle", "stopped"):
            return {"error": f"Explorer busy: {self.state}"}
        if time_limit is not None:
            self.time_limit = time_limit
        self._abort = False
        self.state = "exploring"
        self._thread = threading.Thread(target=self._spiral_loop, daemon=True)
        self._thread.start()
        _log.info(f"Spiral started (time_limit={self.time_limit}s)")
        return {"ok": True}

    def _spiral_loop(self):
        """Spiral outward: move forward increasing distance, turn 90° right, repeat."""
        try:
            explore_start = time.time()

            # Initial scan
            self._initial_scan()

            leg_distance = 500   # mm — start with short legs
            leg_increment = 250  # mm — increase each pair of legs
            leg_count = 0

            while not self._abort and self.state == "exploring":
                # Check time limit
                if self.time_limit > 0 and (time.time() - explore_start) > self.time_limit:
                    self._stop_motors()
                    self.state = "arrived"
                    _log.info(f"Spiral time limit reached ({time.time() - explore_start:.0f}s)")
                    return

                # Move forward for current leg distance
                target_dist = leg_distance
                moved = 0
                _log.info(f"Spiral leg {leg_count + 1}: {target_dist}mm forward")

                while moved < target_dist and not self._abort:
                    if self.time_limit > 0 and (time.time() - explore_start) > self.time_limit:
                        break
                    vx = self.explore_speed
                    actual_vx, _, _ = self._move_filtered(vx, 0, 0)
                    if actual_vx == 0:
                        _log.info(f"Spiral blocked at {moved:.0f}mm of {target_dist}mm")
                        # Back up and turn
                        for _ in range(5):
                            if self._abort: break
                            self._move_filtered(-0.04, 0, 0)
                            time.sleep(1.0 / EXPLORE_UPDATE_HZ)
                        self._stop_motors()
                        break
                    moved += actual_vx * (1.0 / EXPLORE_UPDATE_HZ) * 1000  # convert to mm
                    time.sleep(1.0 / EXPLORE_UPDATE_HZ)

                self._stop_motors()
                if self._abort:
                    break

                # Turn 90° right
                self._rotate_by(-90, speed=0.3)
                time.sleep(0.3)

                leg_count += 1
                # Increase distance every 2 legs (completing one side of spiral)
                if leg_count % 2 == 0:
                    leg_distance += leg_increment
                    _log.info(f"Spiral leg distance increased to {leg_distance}mm")

            _log.info("Spiral complete")
        except Exception as e:
            _log.error(f"Spiral error: {e}", exc_info=True)
        finally:
            self._stop_motors()
            if self.state == "exploring":
                self.state = "idle"

    def start_scan_test(self):
        """Stationary 360° scan test: 2 CW + 2 CCW rotations for mapping debug."""
        if self.state not in ("idle", "stopped"):
            return {"error": f"Explorer busy: {self.state}"}
        self._abort = False
        self.state = "scanning"
        self._thread = threading.Thread(target=self._scan_test_loop, daemon=True)
        self._thread.start()
        return {"ok": True}

    def _rotate_by(self, target_degrees, speed=0.2):
        """Rotate by exact degrees using IMU feedback.

        Args:
            target_degrees: positive = CCW, negative = CW
            speed: rotation speed in rad/s (always positive, sign from target)
        Returns True if completed, False if aborted.
        """
        if not self.slam:
            return False

        vz = speed if target_degrees > 0 else -speed
        target_rad = abs(math.radians(target_degrees))
        accumulated = 0.0
        prev_heading = self._get_pose()[2]
        timeout = abs(target_degrees) / math.degrees(speed) * 3  # 3x safety timeout

        start = time.time()
        while accumulated < target_rad and not self._abort:
            if time.time() - start > timeout:
                _log.warning(f"Rotation timeout after {timeout:.0f}s, accumulated {math.degrees(accumulated):.0f}°")
                break

            self._move_filtered(0, 0, vz)
            time.sleep(1.0 / EXPLORE_UPDATE_HZ)

            cur_heading = self._get_pose()[2]
            # Compute delta with wrapping
            delta = cur_heading - prev_heading
            delta = (delta + math.pi) % (2 * math.pi) - math.pi
            accumulated += abs(delta)
            prev_heading = cur_heading

            if int(accumulated * 10) % 10 == 0:  # log every ~6°
                _log.debug(f"Rotate: {math.degrees(accumulated):.0f}°/{abs(target_degrees)}° heading={math.degrees(cur_heading):.0f}°")

        self._stop_motors()
        _log.info(f"Rotation done: {math.degrees(accumulated):.0f}° of {abs(target_degrees)}°")
        return not self._abort

    def _scan_test_loop(self):
        try:
            # Phase 1: 2x clockwise (negative degrees)
            _log.info("Scan test: 2x clockwise rotation")
            for turn in range(2):
                _log.info(f"  CW rotation {turn + 1}/2")
                if not self._rotate_by(-360):
                    break
                time.sleep(0.5)

            if not self._abort:
                # Phase 2: 2x counterclockwise (positive degrees)
                _log.info("Scan test: 2x counterclockwise rotation")
                for turn in range(2):
                    _log.info(f"  CCW rotation {turn + 1}/2")
                    if not self._rotate_by(360):
                        break
                    time.sleep(0.5)

            _log.info("Scan test complete")
        except Exception as e:
            _log.error(f"Scan test error: {e}", exc_info=True)
        finally:
            self._stop_motors()
            self.state = "idle"

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
