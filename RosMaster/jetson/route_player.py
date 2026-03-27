"""Autonomous route replay for self-driving.

Loads a recorded route and follows it using multi-modal localization
(SLAM + GPS + visual matching). All motor commands go through collision
avoidance.

Usage: Called from explorer.py's route_follow mode.
"""

import os
import json
import math
import time
import base64
import numpy as np


class RoutePlayer:
    """Follows a recorded route autonomously."""

    def __init__(self, route_name, slam, lidar, collision, bot, gps, cam):
        self.route_name = route_name
        self.slam = slam
        self.lidar = lidar
        self.collision = collision
        self.bot = bot
        self.gps = gps
        self.cam = cam

        self.route_dir = os.path.join("/home/jetson/RosMaster/routes", route_name)
        self.waypoints = []
        self.state = "idle"  # idle, following, paused, arrived, error
        self.current_idx = 0
        self.target_idx = 0
        self.confidence = 0
        self.source = "none"
        self._fusion = None
        self._matcher = None
        self._visual_interval = 0  # counter for visual matching (every N steps)
        self._last_visual_match = None
        self._step_count = 0

        # Navigation parameters
        self.base_speed = 0.08      # m/s
        self.turn_speed = 0.3       # rad/s
        self.waypoint_tolerance = 300  # mm — distance to consider waypoint reached
        self.heading_tolerance = 0.3   # rad — heading error before moving forward
        self.lookahead = 5             # waypoints ahead to target

    def load(self):
        """Load route data. Returns True if successful."""
        wp_path = os.path.join(self.route_dir, "waypoints.json")
        if not os.path.exists(wp_path):
            print(f"Route not found: {wp_path}")
            return False

        try:
            with open(wp_path) as f:
                self.waypoints = json.load(f)
            print(f"Route '{self.route_name}' loaded: {len(self.waypoints)} waypoints")
        except Exception as e:
            print(f"Route load error: {e}")
            return False

        if len(self.waypoints) < 2:
            print("Route too short")
            return False

        # Initialize fusion
        from gps_slam_fusion import GpsSlamFusion
        self._fusion = GpsSlamFusion(self.waypoints)

        # Try to load visual features
        try:
            from visual_matcher import VisualMatcher
            self._matcher = VisualMatcher()
            if not self._matcher.load_route_features(self.route_dir):
                self._matcher = None
                print("Visual matching unavailable (no features)")
        except ImportError:
            self._matcher = None
            print("Visual matching unavailable (cv2 not available)")

        self.state = "idle"
        return True

    def start(self):
        """Begin following the route."""
        if not self.waypoints:
            self.state = "error"
            return
        self.state = "following"
        self.current_idx = 0
        self.target_idx = min(self.lookahead, len(self.waypoints) - 1)
        self._step_count = 0
        print(f"Route replay started: {self.route_name}")

    def pause(self):
        if self.state == "following":
            self.state = "paused"
            if self.bot:
                self.bot.set_car_motion(0, 0, 0)

    def resume(self):
        if self.state == "paused":
            self.state = "following"

    def stop(self):
        self.state = "idle"
        if self.bot:
            self.bot.set_car_motion(0, 0, 0)

    def step(self):
        """Execute one navigation step. Called at 5Hz from explorer loop.

        Returns (vx, vy, vz) motor command (already collision-filtered).
        """
        if self.state != "following":
            return 0, 0, 0

        self._step_count += 1

        # --- Localize ---
        slam_pose = self.slam.get_pose() if self.slam else None
        gps_data = self.gps.get_data() if self.gps else None

        # Visual matching every 5 steps (~1Hz) to save CPU
        visual_result = None
        if self._matcher and self._step_count % 5 == 0:
            visual_result = self._get_visual_match()

        # Fuse position sources
        if self._fusion:
            fusion_result = self._fusion.update(
                slam_pose=slam_pose,
                gps_data=gps_data,
                visual_match=visual_result or self._last_visual_match,
            )
            self.current_idx = fusion_result["waypoint_idx"]
            self.confidence = fusion_result["confidence"]
            self.source = fusion_result["source"]

        # --- Check if route complete ---
        if self._fusion and self._fusion.is_route_complete():
            self.state = "arrived"
            if self.bot:
                self.bot.set_car_motion(0, 0, 0)
            print(f"Route complete! {self._step_count} steps")
            return 0, 0, 0

        # --- Navigate toward target waypoint ---
        target_wp, self.target_idx = self._fusion.get_lookahead_waypoint(self.lookahead)
        target_x = target_wp["pose"][0]
        target_y = target_wp["pose"][1]

        # Current position from SLAM
        if slam_pose is None:
            return 0, 0, 0
        rx, ry, rtheta = slam_pose[0], slam_pose[1], slam_pose[2]

        # Heading to target
        dx = target_x - rx
        dy = target_y - ry
        dist = math.sqrt(dx * dx + dy * dy)
        target_angle = math.atan2(dy, dx)
        heading_error = math.atan2(
            math.sin(target_angle - rtheta),
            math.cos(target_angle - rtheta))

        # Speed based on confidence
        speed_scale = max(0.3, self.confidence)
        speed = self.base_speed * speed_scale

        # Generate motor command
        if dist < self.waypoint_tolerance:
            # Close enough — advance to next waypoint
            if self._fusion:
                self._fusion.waypoint_idx = min(
                    self._fusion.waypoint_idx + 1, len(self.waypoints) - 1)
            vx, vy, vz = speed * 0.3, 0, 0  # slow forward

        elif abs(heading_error) > self.heading_tolerance:
            # Turn to face target
            vz = self.turn_speed if heading_error > 0 else -self.turn_speed
            # Faster turn for large errors
            if abs(heading_error) > math.pi / 2:
                vz *= 1.5
            vx, vy = 0, 0
            vz = max(-1.0, min(1.0, vz))

        else:
            # Move toward target
            vx = speed
            vy = 0
            # Slight strafe correction using mecanum wheels
            lateral_error = math.sin(heading_error) * dist
            if abs(lateral_error) > 50:  # >50mm lateral offset
                vy = max(-0.08, min(0.08, lateral_error / 2000))
            vz = heading_error * 0.5  # proportional heading correction

        # Apply collision avoidance
        if self.collision and self.collision.enabled:
            vx, vy, vz = self.collision.filter_motion(vx, vy, vz)

        # Execute
        if self.bot:
            self.bot.set_car_motion(vx, vy, vz)

        return vx, vy, vz

    def _get_visual_match(self):
        """Get visual match from current camera frame."""
        if not self._matcher or not self.cam:
            return None
        try:
            import cv2
            rgb_b64 = self.cam.get_frame()
            if not rgb_b64:
                return None
            rgb_bytes = base64.b64decode(rgb_b64)
            rgb_arr = np.frombuffer(rgb_bytes, dtype=np.uint8)
            frame = cv2.imdecode(rgb_arr, cv2.IMREAD_COLOR)
            if frame is None:
                return None

            result = self._matcher.match(
                frame,
                expected_idx=self.current_idx,
                search_window=20)
            self._last_visual_match = result
            return result
        except Exception:
            return None

    def get_status(self):
        """Return current replay status for UI."""
        progress = self._fusion.get_progress() if self._fusion else 0
        return {
            "state": self.state,
            "route": self.route_name,
            "current_idx": self.current_idx,
            "target_idx": self.target_idx,
            "total_waypoints": len(self.waypoints),
            "progress": round(progress * 100, 1),
            "confidence": round(self.confidence, 3),
            "source": self.source,
            "step": self._step_count,
            "visual_match": self._last_visual_match,
        }
