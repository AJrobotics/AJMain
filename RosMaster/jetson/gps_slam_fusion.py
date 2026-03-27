"""GPS + SLAM position fusion for route following.

Fuses multiple position sources into a single estimate:
- SLAM: high-frequency local accuracy (sub-100mm), drifts over time
- GPS: low-frequency global accuracy (2-5m), no drift
- Visual matching: discrete anchors ("I'm at waypoint N")

Indoor: SLAM primary (70%) + visual (30%)
Outdoor with GPS: GPS (50%) + SLAM (30%) + visual (20%)
"""

import math


class GpsSlamFusion:
    """Fuses GPS, SLAM, and visual matching into a position estimate."""

    def __init__(self, waypoints):
        """
        Args:
            waypoints: list of route waypoints, each with 'pose' and 'gps' fields
        """
        self.waypoints = waypoints
        self._gps_origin = None  # (lat, lon) of first GPS fix — local coordinate origin
        self._setup_gps_origin()

        # Current estimate
        self.waypoint_idx = 0  # estimated position as waypoint index
        self.local_offset = [0, 0, 0]  # offset from waypoint (dx_mm, dy_mm, dtheta)
        self.confidence = 0.0
        self.source = "none"  # "slam", "gps", "visual", "fused"

    def _setup_gps_origin(self):
        """Find first GPS fix in route to use as coordinate origin."""
        for wp in self.waypoints:
            if wp.get("gps", {}).get("fix"):
                self._gps_origin = (wp["gps"]["lat"], wp["gps"]["lon"])
                break

    def gps_to_local(self, lat, lon):
        """Convert GPS lat/lon to local XY in mm relative to route origin.

        Uses simple Haversine approximation (accurate at house scale).
        """
        if not self._gps_origin:
            return None, None
        olat, olon = self._gps_origin
        # Approximate meters per degree at this latitude
        m_per_deg_lat = 111000  # ~111km per degree latitude
        m_per_deg_lon = 111000 * math.cos(math.radians(olat))
        dx_m = (lon - olon) * m_per_deg_lon
        dy_m = (lat - olat) * m_per_deg_lat
        return dx_m * 1000, dy_m * 1000  # convert to mm

    def find_nearest_waypoint_by_gps(self, lat, lon, max_dist_m=10):
        """Find the route waypoint nearest to a GPS coordinate.

        Returns (waypoint_index, distance_m) or (-1, inf) if none within max_dist.
        """
        if not self._gps_origin:
            return -1, float('inf')

        gx, gy = self.gps_to_local(lat, lon)
        if gx is None:
            return -1, float('inf')

        best_idx = -1
        best_dist = float('inf')

        for i, wp in enumerate(self.waypoints):
            if not wp.get("gps", {}).get("fix"):
                continue
            wx, wy = self.gps_to_local(wp["gps"]["lat"], wp["gps"]["lon"])
            if wx is None:
                continue
            dist = math.sqrt((gx - wx) ** 2 + (gy - wy) ** 2) / 1000  # mm to m
            if dist < best_dist:
                best_dist = dist
                best_idx = i

        if best_dist > max_dist_m:
            return -1, best_dist

        return best_idx, best_dist

    def find_nearest_waypoint_by_slam(self, slam_pose):
        """Find nearest waypoint by SLAM pose (local coordinates).

        Searches around current estimated waypoint index.
        """
        search_start = max(0, self.waypoint_idx - 30)
        search_end = min(len(self.waypoints), self.waypoint_idx + 30)

        best_idx = self.waypoint_idx
        best_dist = float('inf')

        for i in range(search_start, search_end):
            wp = self.waypoints[i]
            dx = slam_pose[0] - wp["pose"][0]
            dy = slam_pose[1] - wp["pose"][1]
            dist = math.sqrt(dx * dx + dy * dy)
            if dist < best_dist:
                best_dist = dist
                best_idx = i

        return best_idx, best_dist

    def update(self, slam_pose=None, gps_data=None, visual_match=None):
        """Fuse all position sources and update the estimated waypoint index.

        Args:
            slam_pose: [x_mm, y_mm, theta_rad] from SLAM engine
            gps_data: dict with 'fix', 'lat', 'lon', 'sats' from GPS reader
            visual_match: dict with 'match_idx', 'confidence' from visual matcher

        Returns:
            dict with estimated position info
        """
        has_gps = gps_data and gps_data.get("fix") and self._gps_origin
        has_slam = slam_pose is not None
        has_visual = visual_match and visual_match.get("match_idx", -1) >= 0

        # Collect waypoint index estimates from each source
        estimates = []

        if has_slam:
            idx, dist = self.find_nearest_waypoint_by_slam(slam_pose)
            slam_conf = max(0, 1.0 - dist / 5000)  # confidence drops over 5m
            estimates.append(("slam", idx, slam_conf))

        if has_gps:
            idx, dist = self.find_nearest_waypoint_by_gps(
                gps_data["lat"], gps_data["lon"])
            if idx >= 0:
                gps_conf = max(0, 1.0 - dist / 10)  # confidence drops over 10m
                gps_sats = gps_data.get("sats", 0)
                gps_conf *= min(gps_sats / 8, 1.0)  # more sats = more confident
                estimates.append(("gps", idx, gps_conf))

        if has_visual:
            vis_conf = visual_match["confidence"]
            vis_idx = visual_match["match_idx"]
            if vis_conf > 0.1:
                estimates.append(("visual", vis_idx, vis_conf))

        if not estimates:
            return {
                "waypoint_idx": self.waypoint_idx,
                "confidence": 0,
                "source": "dead_reckoning",
            }

        # Weighted fusion
        if has_gps:
            # Outdoor: GPS 50%, SLAM 30%, visual 20%
            weights = {"gps": 0.50, "slam": 0.30, "visual": 0.20}
        else:
            # Indoor: SLAM 70%, visual 30%
            weights = {"slam": 0.70, "visual": 0.30}

        weighted_idx = 0
        total_weight = 0
        best_source = "none"
        best_conf = 0

        for source, idx, conf in estimates:
            w = weights.get(source, 0.1) * conf
            weighted_idx += idx * w
            total_weight += w
            if conf > best_conf:
                best_conf = conf
                best_source = source

        if total_weight > 0:
            fused_idx = int(round(weighted_idx / total_weight))
            fused_idx = max(0, min(fused_idx, len(self.waypoints) - 1))

            # Enforce forward progress (don't jump backward more than 5 waypoints)
            if fused_idx < self.waypoint_idx - 5:
                fused_idx = self.waypoint_idx

            self.waypoint_idx = fused_idx
            self.confidence = min(1.0, total_weight)
            self.source = best_source if len(estimates) == 1 else "fused"

        return {
            "waypoint_idx": self.waypoint_idx,
            "confidence": round(self.confidence, 3),
            "source": self.source,
            "estimates": [(s, i, round(c, 3)) for s, i, c in estimates],
        }

    def get_lookahead_waypoint(self, lookahead=5):
        """Get the waypoint a few steps ahead for smooth following.

        Returns the target waypoint dict.
        """
        target_idx = min(self.waypoint_idx + lookahead, len(self.waypoints) - 1)
        return self.waypoints[target_idx], target_idx

    def is_route_complete(self):
        """Check if robot has reached the end of the route."""
        return self.waypoint_idx >= len(self.waypoints) - 3

    def get_progress(self):
        """Get route completion percentage."""
        return self.waypoint_idx / max(len(self.waypoints) - 1, 1)
