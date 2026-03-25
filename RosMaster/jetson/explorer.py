"""
Autonomous explorer for RosMaster X3.
Uses SLAM map to find frontiers, navigate to them, and return home.
Only explores in directions with valid LiDAR coverage (avoids rear ignore zone).
"""

import math
import time
import threading
import heapq
import numpy as np

from slam_engine import GRID_SIZE, CELL_SIZE_MM

# Explorer parameters
EXPLORE_SPEED = 0.12       # m/s
ROTATION_SPEED = 0.8       # rad/s
WAYPOINT_TOLERANCE = 150   # mm — how close to a waypoint before moving to next
FRONTIER_MIN_SIZE = 5      # minimum frontier cluster size to target
HEADING_TOLERANCE = 0.3    # radians — how aligned before moving forward
EXPLORE_UPDATE_HZ = 5      # how often the explorer loop runs


class Explorer:
    def __init__(self, slam=None, bot=None, collision=None, ignore_angle=120):
        self.slam = slam
        self.bot = bot
        self.collision = collision
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
            vx, vy, vz = self.collision.filter_motion(vx, vy, vz)
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
        """Find frontier cells (free cells adjacent to unknown cells)."""
        if not self.slam:
            return []

        grid = self.slam.get_map_image()
        frontiers = []

        # Free cells: pixel value > 200 (high probability of free)
        # Unknown cells: pixel value ~128
        # Occupied: pixel value < 50
        free = grid > 200
        unknown = (grid > 100) & (grid < 160)

        for y in range(1, GRID_SIZE - 1):
            for x in range(1, GRID_SIZE - 1):
                if not free[y, x]:
                    continue
                # Check if any neighbor is unknown
                if (unknown[y-1, x] or unknown[y+1, x] or
                    unknown[y, x-1] or unknown[y, x+1]):
                    # Convert to world mm
                    wx = x * CELL_SIZE_MM
                    wy = y * CELL_SIZE_MM

                    # Check if this frontier is in a valid direction (not rear)
                    pose = self._get_pose()
                    dx = wx - pose[0]
                    dy = wy - pose[1]
                    angle_to_frontier = math.atan2(dy, dx)
                    relative_angle = angle_to_frontier - pose[2]

                    if self._angle_in_valid_zone(relative_angle):
                        frontiers.append((wx, wy))

        return frontiers

    def _cluster_frontiers(self, frontiers):
        """Group nearby frontier cells into clusters, return largest cluster centers."""
        if not frontiers:
            return []

        points = np.array(frontiers)
        visited = [False] * len(points)
        clusters = []

        for i in range(len(points)):
            if visited[i]:
                continue
            cluster = [i]
            visited[i] = True
            queue = [i]

            while queue:
                idx = queue.pop(0)
                for j in range(len(points)):
                    if not visited[j]:
                        dist = np.linalg.norm(points[idx] - points[j])
                        if dist < CELL_SIZE_MM * 3:
                            visited[j] = True
                            cluster.append(j)
                            queue.append(j)

            if len(cluster) >= FRONTIER_MIN_SIZE:
                center = points[cluster].mean(axis=0)
                clusters.append((center[0], center[1], len(cluster)))

        # Sort by distance from robot (nearest first)
        pose = self._get_pose()
        clusters.sort(key=lambda c: math.hypot(c[0] - pose[0], c[1] - pose[1]))
        return clusters

    def _navigate_to(self, target_x, target_y):
        """Navigate to a target position using simple heading control."""
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
                # Rotate toward target
                vz = ROTATION_SPEED if heading_error > 0 else -ROTATION_SPEED
                self._move_filtered(0, 0, vz)
            else:
                # Move forward
                vx = min(EXPLORE_SPEED, dist / 1000.0)
                self._move_filtered(vx, 0, heading_error * 0.5)  # slight correction

            time.sleep(1.0 / EXPLORE_UPDATE_HZ)

        return False

    def _explore_loop(self):
        """Main exploration loop."""
        try:
            while not self._abort and self.state == "exploring":
                # Find frontiers
                raw_frontiers = self._find_frontiers()
                with self.lock:
                    self.frontiers = raw_frontiers[:100]  # limit for UI

                clusters = self._cluster_frontiers(raw_frontiers)

                if not clusters:
                    # No more frontiers — exploration complete
                    self._stop_motors()
                    self.state = "arrived"
                    print("Exploration complete — no more frontiers")
                    return

                # Navigate to nearest frontier cluster center
                target_x, target_y, size = clusters[0]
                with self.lock:
                    self.target = (target_x, target_y)

                print(f"Navigating to frontier at ({target_x:.0f}, {target_y:.0f}), cluster size={size}")
                reached = self._navigate_to(target_x, target_y)

                if not reached:
                    # Couldn't reach — try next frontier
                    time.sleep(0.5)
                    continue

                time.sleep(0.3)

        except Exception as e:
            print(f"Explorer error: {e}")
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
