"""Route recorder for self-driving.

Records RGB video, LiDAR scans, GPS, IMU, and depth data while the robot
is physically moved along a route. Data is saved as a route directory
that can later be replayed autonomously.

Usage: Start/stop via /api/route/start and /api/route/stop endpoints.
"""

import os
import json
import time
import math
import numpy as np

ROUTE_DIR = "/home/jetson/RosMaster/routes"


class RouteRecorder:
    """Records all sensor data during manual route teaching."""

    def __init__(self, name, slam, lidar, depth, cam, gps, collision):
        import cv2
        self.name = name
        self.slam = slam
        self.lidar = lidar
        self.depth = depth
        self.cam = cam
        self.gps = gps
        self.collision = collision

        # Create route directory
        self.route_dir = os.path.join(ROUTE_DIR, name)
        self.frames_dir = os.path.join(self.route_dir, "frames")
        self.segments_dir = os.path.join(self.route_dir, "segments")
        os.makedirs(self.frames_dir, exist_ok=True)
        os.makedirs(self.segments_dir, exist_ok=True)

        # Video writer — RGB at 5fps, 640x480
        self.video_path = os.path.join(self.route_dir, "route_video.mp4")
        fourcc = cv2.VideoWriter_fourcc(*'avc1')
        self.writer = cv2.VideoWriter(self.video_path, fourcc, 5.0, (640, 480))

        # State
        self.waypoints = []
        self.frame_idx = 0
        self.start_time = time.time()
        self.recording = True
        self.segment_idx = 0
        self.segment_origin = None  # (x, y) of current segment start
        self._last_keyframe_pos = None
        self._last_keyframe_time = 0
        self._total_distance = 0.0
        self._prev_pos = None

        # ORB feature storage
        self._orb_keypoints = []
        self._orb_descriptors = []

        print(f"Route recording started: {self.route_dir}", flush=True)

    def tick(self):
        """Called at 5Hz from Tornado periodic callback."""
        if not self.recording:
            return

        import cv2
        elapsed = time.time() - self.start_time
        pose = self.slam.get_pose() if self.slam else [0, 0, 0]

        # Track distance
        if self._prev_pos is not None:
            dx = pose[0] - self._prev_pos[0]
            dy = pose[1] - self._prev_pos[1]
            self._total_distance += math.sqrt(dx * dx + dy * dy)
        self._prev_pos = [pose[0], pose[1]]

        # Set segment origin on first tick
        if self.segment_origin is None:
            self.segment_origin = (pose[0], pose[1])

        # --- Record waypoint ---
        # LiDAR scan
        scan = self.lidar.get_scan() if self.lidar else []
        scan_compact = [[p["angle"], p["dist"]] for p in scan] if scan else []

        # Collision sectors
        sectors = self.collision.get_sector_distances() if self.collision else [9999] * 8

        # IMU
        imu_debug = self.slam.get_heading_debug() if self.slam else {}

        # GPS
        gps_data = self.gps.get_data() if self.gps else {}

        waypoint = {
            "idx": len(self.waypoints),
            "t": round(elapsed, 2),
            "pose": [round(pose[0]), round(pose[1]), round(math.degrees(pose[2]))],
            "imu_yaw": round(imu_debug.get("imu_yaw", 0), 2),
            "sectors": [round(s) for s in sectors],
            "scan": scan_compact,
            "gps": {
                "fix": gps_data.get("fix", False),
                "lat": round(gps_data.get("latitude", 0), 7),
                "lon": round(gps_data.get("longitude", 0), 7),
                "alt": round(gps_data.get("altitude_m", 0), 1),
                "sats": gps_data.get("satellites", 0),
                "speed": round(gps_data.get("speed_knots", 0), 2),
                "heading": round(gps_data.get("heading_deg", 0), 1),
            },
            "segment": self.segment_idx,
            "frame_idx": -1,  # updated if keyframe saved
        }

        # --- Record RGB video frame ---
        rgb_frame = self._get_rgb_frame(cv2)
        if rgb_frame is not None:
            # Overlay info
            text = f"T+{int(elapsed)}s  GPS:{'FIX' if gps_data.get('fix') else 'NO'}  Seg:{self.segment_idx}"
            cv2.putText(rgb_frame, text, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 1)
            cv2.circle(rgb_frame, (620, 16), 8, (0, 0, 255), -1)  # red dot
            self.writer.write(rgb_frame)

        # --- Save keyframe (every 0.5s or 500mm) ---
        save_keyframe = False
        if self._last_keyframe_pos is None:
            save_keyframe = True
        else:
            dist_from_last = math.sqrt(
                (pose[0] - self._last_keyframe_pos[0]) ** 2 +
                (pose[1] - self._last_keyframe_pos[1]) ** 2)
            time_since_last = elapsed - self._last_keyframe_time
            if dist_from_last >= 500 or time_since_last >= 0.5:
                save_keyframe = True

        if save_keyframe and rgb_frame is not None:
            frame_name = f"frame_{self.frame_idx:06d}.jpg"
            frame_path = os.path.join(self.frames_dir, frame_name)
            cv2.imwrite(frame_path, rgb_frame, [cv2.IMWRITE_JPEG_QUALITY, 85])

            # Extract ORB features
            self._extract_orb(cv2, rgb_frame, self.frame_idx)

            waypoint["frame_idx"] = self.frame_idx
            self.frame_idx += 1
            self._last_keyframe_pos = [pose[0], pose[1]]
            self._last_keyframe_time = elapsed

        self.waypoints.append(waypoint)

        # --- Check segment transition (>15m from segment origin) ---
        if self.segment_origin:
            dist_from_origin = math.sqrt(
                (pose[0] - self.segment_origin[0]) ** 2 +
                (pose[1] - self.segment_origin[1]) ** 2)
            if dist_from_origin > 15000:  # 15m in mm
                self._save_segment()
                self.segment_idx += 1
                self.segment_origin = (pose[0], pose[1])

    def _get_rgb_frame(self, cv2):
        """Get current RGB frame at 640x480."""
        import base64
        try:
            rgb_b64 = self.cam.get_frame() if self.cam else None
            if not rgb_b64:
                return None
            rgb_bytes = base64.b64decode(rgb_b64)
            rgb_arr = np.frombuffer(rgb_bytes, dtype=np.uint8)
            img = cv2.imdecode(rgb_arr, cv2.IMREAD_COLOR)
            if img is not None:
                img = cv2.resize(img, (640, 480))
            return img
        except Exception:
            return None

    def _extract_orb(self, cv2, frame, frame_idx):
        """Extract ORB features from a keyframe."""
        try:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.resize(gray, (320, 240))
            # CLAHE for lighting invariance
            clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
            gray = clahe.apply(gray)

            orb = cv2.ORB_create(nfeatures=500)
            kps, descs = orb.detectAndCompute(gray, None)
            if descs is not None:
                self._orb_keypoints.append({
                    "frame_idx": frame_idx,
                    "count": len(kps),
                })
                self._orb_descriptors.append(descs)
            else:
                self._orb_keypoints.append({"frame_idx": frame_idx, "count": 0})
                self._orb_descriptors.append(np.zeros((1, 32), dtype=np.uint8))
        except Exception:
            pass

    def _save_segment(self):
        """Save current SLAM grid as a segment."""
        if not self.slam:
            return
        try:
            seg_path = os.path.join(self.segments_dir, f"seg_{self.segment_idx:03d}.npz")
            grid = self.slam.grid.copy()
            pose = self.slam.get_pose()
            np.savez_compressed(seg_path, grid=grid,
                                pose=pose, origin=list(self.segment_origin))
            print(f"Route segment {self.segment_idx} saved: {seg_path}", flush=True)
        except Exception as e:
            print(f"Segment save error: {e}", flush=True)

    def stop(self):
        """Finalize and save route."""
        self.recording = False

        # Save final segment
        self._save_segment()

        # Release video
        try:
            self.writer.release()
        except Exception:
            pass

        # Save ORB features
        try:
            if self._orb_descriptors:
                all_descs = np.array(self._orb_descriptors, dtype=object)
                np.savez_compressed(
                    os.path.join(self.route_dir, "features.npz"),
                    descriptors=all_descs,
                    keypoints=self._orb_keypoints,
                )
        except Exception as e:
            print(f"Feature save error: {e}", flush=True)

        # Save waypoints
        try:
            with open(os.path.join(self.route_dir, "waypoints.json"), 'w') as f:
                json.dump(self.waypoints, f)
        except Exception as e:
            print(f"Waypoint save error: {e}", flush=True)

        # Save metadata
        duration = time.time() - self.start_time
        meta = {
            "name": self.name,
            "date": time.strftime("%Y-%m-%d %H:%M:%S"),
            "duration": round(duration, 1),
            "distance_m": round(self._total_distance / 1000, 1),
            "waypoints": len(self.waypoints),
            "keyframes": self.frame_idx,
            "segments": self.segment_idx + 1,
            "has_gps": any(w["gps"]["fix"] for w in self.waypoints),
        }
        try:
            with open(os.path.join(self.route_dir, "meta.json"), 'w') as f:
                json.dump(meta, f, indent=2)
        except Exception as e:
            print(f"Meta save error: {e}", flush=True)

        print(f"Route '{self.name}' saved: {len(self.waypoints)} waypoints, "
              f"{self.frame_idx} keyframes, {self.segment_idx + 1} segments, "
              f"{meta['distance_m']}m, {meta['duration']}s", flush=True)

    def get_status(self):
        """Return current recording status for UI."""
        elapsed = time.time() - self.start_time
        return {
            "recording": self.recording,
            "name": self.name,
            "elapsed": round(elapsed, 1),
            "waypoints": len(self.waypoints),
            "keyframes": self.frame_idx,
            "segments": self.segment_idx + 1,
            "distance_m": round(self._total_distance / 1000, 1),
            "has_gps": self.gps.get_data().get("fix", False) if self.gps else False,
        }
