#!/usr/bin/env python3
"""
RosMaster X3 Web UI Server.
Tornado-based web server with WebSocket for real-time sensor data streaming.
Accessible from any device on the network at http://<jetson-ip>:8080
"""

import os
import json
import time
import math
import signal
import sys
import subprocess
import numpy as np

import tornado.ioloop
import tornado.web
import tornado.websocket

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lidar_reader import LidarReader
from depth_reader import DepthReader
from camera_reader import CameraReader
from collision_avoidance import CollisionAvoidance
from calibration import CalibrationRunner
from slam_engine import SLAMEngine, CELL_SIZE_MM, GRID_SIZE
from explorer import Explorer
from gps_reader import GpsReader
from xbee_comm import XBeeComm
from route_recorder import RouteRecorder, ROUTE_DIR

PORT = 8080
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# Global state
lidar = LidarReader()
depth = DepthReader()
cam_primary = CameraReader(0, name="Astra RGB", width=480, height=360, fps=10)
status_clients = set()
lidar_clients = set()
depth_clients = set()
cam_primary_clients = set()
collision_clients = set()
debug_clients = set()
timing_clients = set()
collision = CollisionAvoidance()
calibration = CalibrationRunner()
slam = SLAMEngine(ignore_angle=140)
explorer = Explorer(slam=slam, ignore_angle=120)
slam_clients = set()
gps = GpsReader()
xbee = XBeeComm(gps_reader=gps)
route_rec = None  # active RouteRecorder

# Persistent Rosmaster instance for status (uses CH340 on ttyUSB1/ttyUSB2, not ttyUSB0)
bot = None

# --- Exploration Video Recorder ---
RECORDING_DIR = "/home/jetson/RosMaster/maps/recordings"
_recorder = None  # active VideoWriter during exploration


class ExploreRecorder:
    """Records RGB + depth frames as MP4 during exploration, with pose log."""

    def __init__(self):
        import cv2
        os.makedirs(RECORDING_DIR, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        self.video_path = os.path.join(RECORDING_DIR, f"explore_{ts}.mp4")
        self.log_path = os.path.join(RECORDING_DIR, f"explore_{ts}.json")
        # Side-by-side: RGB (160x120) + Depth heatmap (160x120) = 320x120
        self.width, self.height = 320, 120
        fourcc = cv2.VideoWriter_fourcc(*'avc1')  # H.264 — browser compatible
        self.writer = cv2.VideoWriter(self.video_path, fourcc, 1.0, (self.width, self.height))
        self.pose_log = []  # list of {time, pose, sectors, frame_idx}
        self.frame_idx = 0
        self.start_time = time.time()
        print(f"Recording started: {self.video_path}", flush=True)

    def add_frame(self):
        import cv2, base64
        try:
            # Get RGB frame
            rgb_b64 = cam_primary.get_frame()
            rgb_img = None
            if rgb_b64:
                rgb_bytes = base64.b64decode(rgb_b64)
                rgb_arr = np.frombuffer(rgb_bytes, dtype=np.uint8)
                rgb_img = cv2.imdecode(rgb_arr, cv2.IMREAD_COLOR)
                rgb_img = cv2.resize(rgb_img, (160, 120))

            # Get depth heatmap
            depth_b64, depth_stats = depth.get_frame()
            depth_img = None
            if depth_b64:
                depth_bytes = base64.b64decode(depth_b64)
                depth_arr = np.frombuffer(depth_bytes, dtype=np.uint8)
                depth_img = cv2.imdecode(depth_arr, cv2.IMREAD_COLOR)
                depth_img = cv2.resize(depth_img, (160, 120))

            # Compose side-by-side frame
            frame = np.zeros((120, 320, 3), dtype=np.uint8)
            if rgb_img is not None:
                frame[:, :160] = rgb_img
            if depth_img is not None:
                frame[:, 160:] = depth_img

            # Overlay pose text + recording indicator
            pose = slam.get_pose()
            elapsed = time.time() - self.start_time
            text = f"T+{int(elapsed)}s ({pose[0]:.0f},{pose[1]:.0f},{math.degrees(pose[2]):.0f})"
            cv2.putText(frame, text, (4, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (0, 255, 255), 1)
            cv2.circle(frame, (310, 10), 5, (0, 0, 255), -1)  # red dot = recording

            self.writer.write(frame)

            # Get LiDAR scan
            scan = lidar.get_scan()
            scan_compact = [[p["angle"], p["dist"]] for p in scan] if scan else []

            # Get SLAM map stats for this frame
            sectors = collision.get_sector_distances() if collision else [9999]*8
            scan_count = slam.scan_count

            # Get landmark info
            landmark_info = None
            if slam.use_landmarks:
                landmark_info = {
                    "count": len(slam._landmarks),
                    "correction": slam._debug_landmark_correction,
                    "landmarks": [(round(x), round(y), t, round(c, 2))
                                  for x, y, t, c in slam._landmarks[:30]],
                }

            # Get raw IMU yaw for debugging
            imu_debug = slam.get_heading_debug()

            self.pose_log.append({
                "t": round(elapsed, 1),
                "pose": [round(pose[0]), round(pose[1]), round(math.degrees(pose[2]))],
                "imu_yaw": imu_debug.get("imu_yaw", 0),
                "icp_quality": imu_debug.get("icp_quality", 0),
                "sectors": [round(s) for s in sectors],
                "scan": scan_compact,  # full 360° LiDAR data
                "scan_count": scan_count,
                "frame": self.frame_idx,
                "landmarks": landmark_info,
            })
            self.frame_idx += 1
        except Exception as e:
            print(f"Recorder frame error: {e}", flush=True)

    def stop(self):
        try:
            self.writer.release()
        except Exception:
            pass
        # Save pose log
        try:
            with open(self.log_path, 'w') as f:
                json.dump({
                    "video": os.path.basename(self.video_path),
                    "frames": self.frame_idx,
                    "duration": round(time.time() - self.start_time, 1),
                    "log": self.pose_log,
                }, f)
        except Exception as e:
            print(f"Recorder log save error: {e}", flush=True)
        print(f"Recording saved: {self.video_path} ({self.frame_idx} frames)", flush=True)

    def __del__(self):
        """Safety net: release video writer if not properly stopped."""
        try:
            if self.writer and self.writer.isOpened():
                self.writer.release()
        except Exception:
            pass


def init_bot():
    global bot
    try:
        import os
        if not os.path.exists("/dev/rosmaster"):
            print("STM32 not found (/dev/rosmaster missing). Skipping bot init.")
            return
        from Rosmaster_Lib import Rosmaster
        print("Initializing Rosmaster_Lib on /dev/rosmaster...")
        bot = Rosmaster(car_type=2, com="/dev/rosmaster")
        bot.create_receive_threading()
        time.sleep(3)
        v = bot.get_battery_voltage()
        print(f"Rosmaster_Lib initialized. Battery: {v}V")
        bot.set_beep(0)
    except Exception as e:
        print(f"Rosmaster_Lib init failed: {e}")
        bot = None


class IndexHandler(tornado.web.RequestHandler):
    def get(self):
        self.render("static/index.html")


class CollisionConfigHandler(tornado.web.RequestHandler):
    def get(self):
        self.write(json.dumps({
            "stop": collision.stop_dist,
            "slow": collision.slow_dist,
            "caution": collision.caution_dist,
            "ignore_angle": collision.ignore_angle,
            "enabled": collision.enabled,
        }))

    def post(self):
        try:
            data = json.loads(self.request.body)
            if "ignore_angle" in data:
                val = max(0, min(180, int(data["ignore_angle"])))
                collision.ignore_angle = val
            if "enabled" in data:
                collision.enabled = bool(data["enabled"])
            if "stop" in data:
                collision.stop_dist = max(100, min(1000, int(data["stop"])))
            if "slow" in data:
                collision.slow_dist = max(200, min(1500, int(data["slow"])))
            if "caution" in data:
                collision.caution_dist = max(300, min(2000, int(data["caution"])))
            self.write(json.dumps({"ok": True, "stop": collision.stop_dist,
                                   "slow": collision.slow_dist, "caution": collision.caution_dist,
                                   "ignore_angle": collision.ignore_angle, "enabled": collision.enabled}))
        except Exception as e:
            self.write(json.dumps({"ok": False, "error": str(e)}))


class LidarModeHandler(tornado.web.RequestHandler):
    def post(self):
        try:
            data = json.loads(self.request.body)
            mode = data.get("mode", "standard")
            lidar.set_scan_mode(mode)
            self.write(json.dumps({"ok": True, "mode": mode}))
        except Exception as e:
            self.write(json.dumps({"ok": False, "error": str(e)}))

    def get(self):
        self.write(json.dumps({"mode": lidar.scan_mode}))


class DepthOffsetHandler(tornado.web.RequestHandler):
    def post(self):
        try:
            data = json.loads(self.request.body)
            offset = float(data.get("offset", 0.0))
            depth.set_angle_offset(offset)
            self.write(json.dumps({"ok": True, "offset": offset}))
        except Exception as e:
            self.write(json.dumps({"ok": False, "error": str(e)}))


# Current SLAM method: 'custom', 'slam_toolbox', 'cartographer'
current_slam_method = 'custom'  # Custom Python SLAM enabled by default
ros2_slam_process = None


class SlamMethodHandler(tornado.web.RequestHandler):
    def post(self):
        global current_slam_method, ros2_slam_process
        import subprocess, os, signal as _sig
        try:
            data = json.loads(self.request.body)
            method = data.get("method", "custom")

            if method not in ("custom", "slam_toolbox", "cartographer"):
                self.write(json.dumps({"ok": False, "error": f"Unknown method: {method}"}))
                return

            if method == current_slam_method:
                self.write(json.dumps({"ok": True, "method": method, "msg": "already active"}))
                return

            # --- Stop previous method ---
            # Kill any ROS2 SLAM processes
            if ros2_slam_process:
                try:
                    os.killpg(os.getpgid(ros2_slam_process.pid), _sig.SIGTERM)
                except Exception:
                    ros2_slam_process.terminate()
                try:
                    ros2_slam_process.wait(timeout=5)
                except Exception:
                    ros2_slam_process.kill()
                ros2_slam_process = None
                print("Stopped ROS2 SLAM processes")
                # Kill any leftover ROS2 nodes
                subprocess.run(["pkill", "-f", "sllidar_node"], capture_output=True)
                subprocess.run(["pkill", "-f", "slam_toolbox"], capture_output=True)
                subprocess.run(["pkill", "-f", "cartographer"], capture_output=True)
                subprocess.run(["pkill", "-f", "ros2_scan_filter"], capture_output=True)
                time.sleep(2)

            # If switching FROM ros2 TO custom, restart our LiDAR reader
            if current_slam_method != 'custom' and method == 'custom':
                print("Restarting pyrplidar reader...")
                lidar.start()
                time.sleep(2)

            # If switching FROM custom TO ros2, stop our LiDAR reader to free serial port
            if current_slam_method == 'custom' and method != 'custom':
                print("Stopping pyrplidar reader to free /dev/rplidar...")
                lidar.stop()
                time.sleep(2)

            current_slam_method = method

            # --- Start new method ---
            if method == "custom":
                print("SLAM method: Custom Python")
                slam.reset()

            elif method == "slam_toolbox":
                print("SLAM method: SLAM Toolbox (ROS2) — starting...")
                ignore = collision.ignore_angle if collision else 140
                ros2_slam_process = subprocess.Popen(
                    ["bash", "-c",
                     "source /opt/ros/humble/setup.bash && "
                     "source /home/jetson/yahboomcar_ros2_ws/software/library_ws/install/setup.bash && "
                     "ros2 launch sllidar_ros2 sllidar_s2_launch.py & "
                     f"python3 /home/jetson/RosMaster/web_ui/ros2_scan_filter.py --ignore-angle {ignore} & "
                     "sleep 5 && "
                     "ros2 launch slam_toolbox online_async_launch.py "
                     "params_file:=/home/jetson/RosMaster/web_ui/slam_toolbox_params.yaml"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    preexec_fn=os.setsid
                )
                print(f"SLAM Toolbox + scan filter started (PID {ros2_slam_process.pid})")

            elif method == "cartographer":
                print("SLAM method: Cartographer (ROS2) — starting...")
                ignore = collision.ignore_angle if collision else 140
                ros2_slam_process = subprocess.Popen(
                    ["bash", "-c",
                     "source /opt/ros/humble/setup.bash && "
                     "source /home/jetson/yahboomcar_ros2_ws/software/library_ws/install/setup.bash && "
                     "ros2 launch sllidar_ros2 sllidar_s2_launch.py & "
                     f"python3 /home/jetson/RosMaster/web_ui/ros2_scan_filter.py --ignore-angle {ignore} & "
                     "sleep 5 && "
                     "ros2 launch cartographer_ros demo_revo_lds.launch.py"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    preexec_fn=os.setsid
                )
                print(f"Cartographer + scan filter started (PID {ros2_slam_process.pid})")

            self.write(json.dumps({"ok": True, "method": method}))
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.write(json.dumps({"ok": False, "error": str(e)}))

    def get(self):
        self.write(json.dumps({"method": current_slam_method}))


class SlamWSHandler(tornado.websocket.WebSocketHandler):
    def check_origin(self, origin):
        return True

    def open(self):
        slam_clients.add(self)

    def on_close(self):
        slam_clients.discard(self)


class ExplorerHandler(tornado.web.RequestHandler):
    def get(self):
        self.write(json.dumps(explorer.get_status()))

    def post(self):
        try:
            data = json.loads(self.request.body)
            action = data.get("action", "")
            if action == "start":
                time_limit = int(data.get("time_limit", 300))
                result = explorer.start_exploration(time_limit=time_limit)
                _start_recording()
            elif action == "return_home":
                result = explorer.return_home()
            elif action == "floor_plan":
                time_limit = int(data.get("time_limit", 300))
                result = explorer.start_floor_plan(time_limit=time_limit)
                _start_recording()
            elif action == "wall_follow":
                time_limit = int(data.get("time_limit", 300))
                direction = data.get("direction", "right")
                wall_dist = int(data.get("wall_dist", 500))
                result = explorer.start_wall_follow(time_limit=time_limit, direction=direction, wall_dist=wall_dist)
                _start_recording()
            elif action == "spiral":
                time_limit = int(data.get("time_limit", 300))
                result = explorer.start_spiral(time_limit=time_limit)
                _start_recording()
            elif action == "scan_test":
                result = explorer.start_scan_test()
                _start_recording()
            elif action == "nn_explore":
                time_limit = int(data.get("time_limit", 300))
                result = explorer.start_nn_explore(time_limit=time_limit)
                _start_recording()
            elif action == "route_nav":
                time_limit = int(data.get("time_limit", 300))
                explorer.cam = cam_primary
                result = explorer.start_route_nav(time_limit=time_limit)
                _start_recording()
            elif action == "route_follow":
                route_name = data.get("route_name", "")
                time_limit = int(data.get("time_limit", 600))
                if not route_name:
                    result = {"error": "No route name specified"}
                else:
                    explorer.gps = gps
                    explorer.cam = cam_primary
                    result = explorer.start_route_follow(route_name, time_limit=time_limit)
            elif action == "stop":
                result = explorer.stop()
                _stop_recording()
            elif action == "reset_map":
                slam.reset()
                result = {"ok": True}
            elif action == "set_speed":
                speed = float(data.get("speed", 0.08))
                speed = max(0.02, min(0.15, speed))
                explorer.explore_speed = speed
                result = {"ok": True, "speed": speed}
            else:
                result = {"ok": False, "error": f"Unknown action: {action}"}
            self.write(json.dumps(result))
        except Exception as e:
            self.write(json.dumps({"ok": False, "error": str(e)}))


class SlamParamsHandler(tornado.web.RequestHandler):
    def get(self):
        self.write(json.dumps({
            "icp_trans_cap": slam.icp_trans_cap,
            "icp_min_quality": slam.icp_min_quality,
        }))

    def post(self):
        data = json.loads(self.request.body)
        if "icp_trans_cap" in data:
            slam.icp_trans_cap = max(0, min(200, int(data["icp_trans_cap"])))
        if "icp_min_quality" in data:
            slam.icp_min_quality = max(0, min(1.0, float(data["icp_min_quality"])))
        self.write(json.dumps({"ok": True, "icp_trans_cap": slam.icp_trans_cap,
                               "icp_min_quality": slam.icp_min_quality}))


class LandmarkHandler(tornado.web.RequestHandler):
    def get(self):
        """Get landmark status."""
        self.write(json.dumps({
            "use_landmarks": slam.use_landmarks,
            "count": len(slam._landmarks),
            "correction": slam._debug_landmark_correction,
            "landmarks": [(round(x), round(y), t, round(c, 2))
                          for x, y, t, c in slam._landmarks[:50]],
        }))

    def post(self):
        """Toggle landmark localization."""
        data = json.loads(self.request.body)
        enabled = data.get("enabled", None)
        if enabled is not None:
            slam.use_landmarks = bool(enabled)
            if enabled:
                print(f"Landmark localization enabled ({len(slam._landmarks)} landmarks)", flush=True)
            else:
                print("Landmark localization disabled", flush=True)
        if data.get("clear", False):
            slam._landmarks.clear()
            slam._debug_landmark_count = 0
            print("Landmarks cleared", flush=True)
        self.write(json.dumps({"ok": True, "use_landmarks": slam.use_landmarks,
                               "count": len(slam._landmarks)}))


class GpsHandler(tornado.web.RequestHandler):
    def get(self):
        self.write(json.dumps(gps.get_data()))


class GpsTestReceiveHandler(tornado.web.RequestHandler):
    def post(self):
        """Receive test GPS data from master, return ack + own GPS."""
        try:
            data = json.loads(self.request.body)
            print(f"GPS Test RX: {data}", flush=True)
            own_gps = gps.get_data()
            self.write(json.dumps({
                "ok": True,
                "received": data,
                "own_gps": own_gps,
                "timestamp": time.time(),
            }))
        except Exception as e:
            self.write(json.dumps({"ok": False, "error": str(e)}))


class XBeeStatusHandler(tornado.web.RequestHandler):
    def get(self):
        status = xbee.get_status()
        status["enabled"] = xbee.running
        self.write(json.dumps(status))

    def post(self):
        data = json.loads(self.request.body)
        action = data.get("action", "")
        if action == "enable":
            if not xbee.running:
                xbee.start()
            self.write(json.dumps({"ok": True, "enabled": True}))
        elif action == "disable":
            xbee.stop()
            self.write(json.dumps({"ok": True, "enabled": False}))
        elif data.get("message"):
            xbee.broadcast(data["message"])
            self.write(json.dumps({"ok": True}))
        else:
            self.write(json.dumps({"ok": False, "error": "no action or message"}))


class RouteHandler(tornado.web.RequestHandler):
    def get(self):
        action = self.get_argument("action", "list")
        if action == "list":
            routes = []
            if os.path.exists(ROUTE_DIR):
                for name in sorted(os.listdir(ROUTE_DIR)):
                    meta_path = os.path.join(ROUTE_DIR, name, "meta.json")
                    if os.path.exists(meta_path):
                        with open(meta_path) as f:
                            routes.append(json.load(f))
            self.write(json.dumps({"routes": routes}))
        elif action == "status":
            global route_rec
            if route_rec and route_rec.recording:
                self.write(json.dumps(route_rec.get_status()))
            else:
                self.write(json.dumps({"recording": False}))
        elif action == "detail":
            name = self.get_argument("name", "")
            meta_path = os.path.join(ROUTE_DIR, name, "meta.json")
            if os.path.exists(meta_path):
                with open(meta_path) as f:
                    self.write(f.read())
            else:
                self.write(json.dumps({"error": "not found"}))

    def post(self):
        global route_rec
        data = json.loads(self.request.body)
        action = data.get("action", "")

        if action == "start":
            name = data.get("name", time.strftime("route_%Y%m%d_%H%M%S"))
            if route_rec and route_rec.recording:
                self.write(json.dumps({"ok": False, "error": "Already recording"}))
                return
            route_rec = RouteRecorder(
                name=name, slam=slam, lidar=lidar, depth=depth,
                cam=cam_primary, gps=gps, collision=collision)
            self.write(json.dumps({"ok": True, "name": name}))

        elif action == "stop":
            if route_rec and route_rec.recording:
                route_rec.stop()
                status = route_rec.get_status()
                route_rec = None
                self.write(json.dumps({"ok": True, "status": status}))
            else:
                self.write(json.dumps({"ok": False, "error": "Not recording"}))

        else:
            self.write(json.dumps({"ok": False, "error": "Unknown action"}))


class RouteNavLiveHandler(tornado.web.RequestHandler):
    """Returns live camera frame + LiDAR + model prediction for route viewer."""
    def get(self):
        import base64
        result = {"ok": False}

        # Get camera frame
        rgb_b64 = cam_primary.get_frame() if cam_primary else None

        # Get LiDAR scan
        scan = lidar.get_scan() if lidar else []
        scan_compact = [[p["angle"], p["dist"]] for p in scan] if scan else []

        # Get sectors
        sectors = collision.get_sector_distances() if collision else [9999] * 8

        # Run model prediction if route_nav_runner is loaded in explorer
        pred = [0, 0, 0]
        if hasattr(explorer, '_route_nav_runner') and explorer._route_nav_runner and explorer._route_nav_runner.available and rgb_b64:
            try:
                vx, vy, vz = explorer._route_nav_runner.get_action(rgb_b64, scan)
                pred = [round(vx, 4), round(vy, 4), round(vz, 4)]
            except Exception:
                pass
        elif rgb_b64:
            # Lazy load runner for live preview
            try:
                from route_nav_runner import RouteNavRunner
                if not hasattr(self, '_runner'):
                    RouteNavLiveHandler._runner = RouteNavRunner()
                if RouteNavLiveHandler._runner.available:
                    vx, vy, vz = RouteNavLiveHandler._runner.get_action(rgb_b64, scan)
                    pred = [round(vx, 4), round(vy, 4), round(vz, 4)]
            except Exception:
                pass

        # LiDAR bins
        import numpy as np
        distances = np.full(360, 6000.0, dtype=np.float32)
        for s in scan_compact:
            idx = int(round(s[0])) % 360
            if 0 < s[1] < distances[idx]:
                distances[idx] = s[1]
        bins = distances.reshape(36, 10).min(axis=1)
        lidar_bins = [round(float(b / 6000.0), 3) for b in bins]

        # IMU
        imu_yaw = 0
        if bot:
            try:
                _, _, imu_yaw = bot.get_imu_attitude_data()
            except Exception:
                pass

        result = {
            "ok": True,
            "frame": rgb_b64[:200] + "..." if rgb_b64 and len(rgb_b64) > 200 else rgb_b64,
            "frame_full": rgb_b64,
            "pred": pred,
            "lidar_bins": lidar_bins,
            "sectors": [round(s) for s in sectors],
            "imu_yaw": round(imu_yaw, 1),
            "state": explorer.state,
        }
        self.set_header('Access-Control-Allow-Origin', '*')
        self.write(json.dumps(result))


class ShutdownHandler(tornado.web.RequestHandler):
    def post(self):
        """Shutdown the Jetson."""
        import subprocess
        self.write(json.dumps({"ok": True, "msg": "Shutting down..."}))
        self.flush()
        subprocess.Popen(["sudo", "shutdown", "-h", "now"])


class SlamDataHandler(tornado.web.RequestHandler):
    def get(self):
        """List saved maps, get wall lines, grid data, or stats."""
        import base64, zlib
        action = self.get_argument("action", "list_maps")
        if action == "list_maps":
            self.write(json.dumps({"maps": slam.list_maps()}))
        elif action == "wall_lines":
            lines = slam.extract_wall_lines()
            self.write(json.dumps({"lines": lines, "count": len(lines)}))
        elif action == "grid_data":
            with slam.lock:
                raw = slam.grid.tobytes()
            compressed = zlib.compress(raw)
            b64 = base64.b64encode(compressed).decode('ascii')
            self.write(json.dumps({
                "grid_b64": b64,
                "rows": GRID_SIZE, "cols": GRID_SIZE,
                "cell_mm": CELL_SIZE_MM,
                "compressed_size": len(compressed),
            }))
        elif action == "recordings":
            rec_dir = RECORDING_DIR
            recordings = []
            if os.path.isdir(rec_dir):
                for f in os.listdir(rec_dir):
                    if f.endswith(".json"):
                        path = os.path.join(rec_dir, f)
                        try:
                            with open(path) as jf:
                                meta = json.load(jf)
                            recordings.append({
                                "name": f[:-5],
                                "video": meta.get("video", ""),
                                "frames": meta.get("frames", 0),
                                "duration": meta.get("duration", 0),
                                "log_file": f,
                            })
                        except Exception:
                            pass
            recordings.sort(key=lambda r: r["name"], reverse=True)
            self.write(json.dumps({"recordings": recordings}))
        elif action == "recording_log":
            name = self.get_argument("name", "")
            path = os.path.join(RECORDING_DIR, name + ".json")
            if os.path.exists(path):
                with open(path) as f:
                    self.write(f.read())
            else:
                self.write(json.dumps({"error": "Not found"}))
        elif action == "stats":
            with slam.lock:
                pose = slam.pose.copy()
                grid = slam.grid
                explored = int(np.sum(np.abs(grid) > 0.5))
                total = GRID_SIZE * GRID_SIZE
                coverage = round(explored / total * 100, 1)
            self.write(json.dumps({
                "scan_count": slam.scan_count,
                "pose": [round(pose[0]), round(pose[1]), round(math.degrees(pose[2]))],
                "coverage_pct": coverage,
                "explored_cells": explored,
                "loop_closures": slam._loop_closure_count,
                "grid_size": GRID_SIZE,
                "cell_mm": CELL_SIZE_MM,
                "pose_history_len": len(slam.pose_history),
            }))
        else:
            self.write(json.dumps({"error": f"Unknown action: {action}"}))

    def post(self):
        try:
            data = json.loads(self.request.body)
            action = data.get("action", "")
            name = data.get("name", "default")
            if action == "save":
                result = slam.save_map(name)
            elif action == "load":
                result = slam.load_map(name)
            elif action == "reset":
                slam.reset()
                result = {"ok": True}
            else:
                result = {"ok": False, "error": f"Unknown action: {action}"}
            self.write(json.dumps(result))
        except Exception as e:
            self.write(json.dumps({"ok": False, "error": str(e)}))


class CalibrationHandler(tornado.web.RequestHandler):
    def get(self):
        self.write(json.dumps(calibration.get_status()))

    def post(self):
        try:
            data = json.loads(self.request.body)
            action = data.get("action", "")
            if action == "run":
                test = data.get("test", "forward")
                distance = int(data.get("distance", 500))
                result = calibration.run_test(test, distance)
                self.write(json.dumps(result))
            elif action == "run_all":
                distance = int(data.get("distance", 500))
                result = calibration.run_all(distance)
                self.write(json.dumps(result))
            elif action == "abort":
                result = calibration.abort()
                self.write(json.dumps(result))
            else:
                self.write(json.dumps({"ok": False, "error": f"Unknown action: {action}"}))
        except Exception as e:
            self.write(json.dumps({"ok": False, "error": str(e)}))


class LidarWSHandler(tornado.websocket.WebSocketHandler):
    def check_origin(self, origin):
        return True

    def open(self):
        lidar_clients.add(self)

    def on_close(self):
        lidar_clients.discard(self)


class DepthWSHandler(tornado.websocket.WebSocketHandler):
    def check_origin(self, origin):
        return True

    def open(self):
        depth_clients.add(self)

    def on_close(self):
        depth_clients.discard(self)


class CamPrimaryWSHandler(tornado.websocket.WebSocketHandler):
    def check_origin(self, origin):
        return True

    def open(self):
        cam_primary_clients.add(self)

    def on_close(self):
        cam_primary_clients.discard(self)


class CollisionWSHandler(tornado.websocket.WebSocketHandler):
    def check_origin(self, origin):
        return True

    def open(self):
        collision_clients.add(self)

    def on_close(self):
        collision_clients.discard(self)


class StatusWSHandler(tornado.websocket.WebSocketHandler):
    def check_origin(self, origin):
        return True

    def open(self):
        status_clients.add(self)

    def on_close(self):
        status_clients.discard(self)


class DebugWSHandler(tornado.websocket.WebSocketHandler):
    """Full debug client — receives raw revolution data + timing metrics.
    Enables raw debug buffering in LiDAR process (extra CPU overhead)."""
    def check_origin(self, origin):
        return True

    def open(self):
        debug_clients.add(self)
        lidar.enable_raw_debug(True)
        print(f"Debug client connected ({len(debug_clients)} total)")

    def on_close(self):
        debug_clients.discard(self)
        if not debug_clients:
            lidar.enable_raw_debug(False)
        print(f"Debug client disconnected ({len(debug_clients)} remaining)")


class TimingWSHandler(tornado.websocket.WebSocketHandler):
    """Lightweight client — receives only timing metrics (no raw data).
    No extra overhead on sensor processes."""
    def check_origin(self, origin):
        return True

    def open(self):
        timing_clients.add(self)

    def on_close(self):
        timing_clients.discard(self)


_last_timing_broadcast = 0.0

def broadcast_debug():
    global _last_timing_broadcast
    now = time.monotonic()
    all_clients = debug_clients | timing_clients

    # LiDAR revolution summaries — only to debug clients (raw data)
    if debug_clients:
        revs = lidar.flush_raw_revs()
        if revs:
            msg = json.dumps({"sensor": "lidar", "type": "rev_batch", "revs": revs})
            for client in debug_clients.copy():
                try:
                    client.write_message(msg)
                except Exception:
                    debug_clients.discard(client)

    # Timing metrics — sent to ALL clients (debug + timing) at ~2 Hz
    if all_clients and now - _last_timing_broadcast > 0.5:
        _last_timing_broadcast = now
        lidar_ts = lidar.get_shm_timestamp()
        depth_ts = depth.get_shm_timestamp()
        timing = {
            "sensor": "timing",
            "type": "metrics",
            "lidar_age_ms": round((now - lidar_ts) * 1000, 1) if lidar_ts > 0 else -1,
            "depth_age_ms": round((now - depth_ts) * 1000, 1) if depth_ts > 0 else -1,
            "lidar_connected": lidar.connected,
            "depth_connected": depth.connected,
            "lidar_simulated": lidar.simulated,
            "ts": round(now, 3),
        }
        t0 = time.monotonic()
        collision.update_sectors()
        t1 = time.monotonic()
        timing["collision_us"] = round((t1 - t0) * 1e6)
        timing["collision_sectors"] = collision.get_sector_distances()
        timing["collision_level"] = collision.get_status()["level"]
        timing["heading"] = slam.get_heading_debug()

        msg = json.dumps(timing)
        for client in (debug_clients | timing_clients).copy():
            try:
                client.write_message(msg)
            except Exception:
                debug_clients.discard(client)
                timing_clients.discard(client)


def _start_recording():
    global _recorder
    if _recorder:
        _stop_recording()
    _recorder = ExploreRecorder()

def _stop_recording():
    global _recorder
    if _recorder:
        _recorder.stop()
        _recorder = None

def _tick_recording():
    if _recorder:
        _recorder.add_frame()
        # Auto-stop recording when explorer/scan test finishes
        if explorer.state in ("idle", "stopped", "arrived") and _recorder.frame_idx > 5:
            _stop_recording()
    # Route recorder tick (5Hz from its own callback)


def _tick_route_recording():
    if route_rec and route_rec.recording:
        route_rec.tick()


def broadcast_lidar():
    if not lidar_clients:
        return
    scan = lidar.get_scan()
    if not scan:
        return
    # Get depth camera horizontal line for overlay
    depth_line = depth.get_depth_line() if depth.connected else []
    msg = json.dumps({
        "type": "scan",
        "points": scan,
        "depth_line": depth_line,
        "simulated": lidar.simulated,
        "connected": lidar.connected,
        "ts": time.time(),
    })
    dead = set()
    for client in lidar_clients:
        try:
            client.write_message(msg)
        except Exception:
            dead.add(client)
    lidar_clients.difference_update(dead)


def broadcast_depth():
    if not depth_clients:
        return
    jpeg_b64, stats = depth.get_frame()
    if not jpeg_b64:
        return
    msg = json.dumps({
        "type": "depth",
        "image": jpeg_b64,
        "stats": stats,
        "connected": depth.connected,
    })
    dead = set()
    for client in depth_clients:
        try:
            client.write_message(msg)
        except Exception:
            dead.add(client)
    depth_clients.difference_update(dead)


def _broadcast_camera(clients, cam, cam_type):
    if not clients:
        return
    jpeg_b64 = cam.get_frame()
    if not jpeg_b64:
        return
    msg = json.dumps({"type": cam_type, "image": jpeg_b64, "connected": cam.connected})
    dead = set()
    for client in clients:
        try:
            client.write_message(msg)
        except Exception:
            dead.add(client)
    clients.difference_update(dead)


def broadcast_cam_primary():
    _broadcast_camera(cam_primary_clients, cam_primary, "cam_primary")


def slam_update_thread():
    """Background thread: feed LiDAR + depth scans into SLAM engine.

    Uses sensor fusion: in the ±30° forward overlap zone, only marks
    cells as occupied when both LiDAR and depth camera agree.
    Only runs when using 'custom' SLAM method.
    """
    import threading
    while True:
        try:
            # Skip custom SLAM when using ROS2 methods
            if current_slam_method != 'custom':
                time.sleep(1)
                continue

            scan = lidar.get_scan()
            if scan and len(scan) > 50:
                imu_yaw = None
                if bot:
                    try:
                        _, _, yaw = bot.get_imu_attitude_data()
                        imu_yaw = math.radians(yaw)
                    except Exception:
                        pass
                # Get depth line for sensor fusion
                depth_line = depth.get_depth_line() if depth.connected else None
                slam.update(scan, imu_yaw, depth_line)
        except Exception:
            pass
        time.sleep(0.2)  # ~5 Hz


def broadcast_slam():
    if not slam_clients:
        return
    import base64
    import cv2

    map_img = slam.get_map_image()
    pose = slam.get_pose()
    home = slam.get_home_pose()

    # Colorize: unknown=dark gray, free=light, occupied=black
    colored = cv2.cvtColor(map_img, cv2.COLOR_GRAY2BGR)

    # Draw trajectory
    history = slam.get_pose_history()
    for i in range(1, len(history)):
        x1 = int(history[i-1][0] / CELL_SIZE_MM)
        y1 = int(history[i-1][1] / CELL_SIZE_MM)
        x2 = int(history[i][0] / CELL_SIZE_MM)
        y2 = int(history[i][1] / CELL_SIZE_MM)
        cv2.line(colored, (x1, y1), (x2, y2), (255, 100, 0), 1)

    # Draw home
    hx = int(home[0] / CELL_SIZE_MM)
    hy = int(home[1] / CELL_SIZE_MM)
    cv2.circle(colored, (hx, hy), 4, (0, 255, 0), -1)

    # Draw robot
    rx = int(pose[0] / CELL_SIZE_MM)
    ry = int(pose[1] / CELL_SIZE_MM)
    cv2.circle(colored, (rx, ry), 4, (0, 0, 255), -1)
    # Direction indicator
    dx = int(rx + 8 * math.cos(pose[2]))
    dy = int(ry + 8 * math.sin(pose[2]))
    cv2.line(colored, (rx, ry), (dx, dy), (0, 0, 255), 2)

    # Crop to explored area (with margin)
    non_gray = np.where(map_img != 128)
    if len(non_gray[0]) > 10:
        y_min = max(0, non_gray[0].min() - 20)
        y_max = min(GRID_SIZE, non_gray[0].max() + 20)
        x_min = max(0, non_gray[1].min() - 20)
        x_max = min(GRID_SIZE, non_gray[1].max() + 20)
        # Keep square aspect
        size = max(y_max - y_min, x_max - x_min, 50)
        cx = (x_min + x_max) // 2
        cy = (y_min + y_max) // 2
        half = size // 2
        x_min = max(0, cx - half)
        x_max = min(GRID_SIZE, cx + half)
        y_min = max(0, cy - half)
        y_max = min(GRID_SIZE, cy + half)
        cropped = colored[y_min:y_max, x_min:x_max]
    else:
        cropped = colored[250:350, 250:350]

    # Resize for display
    display = cv2.resize(cropped, (300, 300), interpolation=cv2.INTER_NEAREST)

    _, jpeg = cv2.imencode('.jpg', display, [cv2.IMWRITE_JPEG_QUALITY, 75])
    b64 = base64.b64encode(jpeg.tobytes()).decode('ascii')

    # Walls-only image (white walls on black background)
    walls_img = slam.get_walls_image()
    # Crop same region as main map
    if len(non_gray[0]) > 10:
        walls_cropped = walls_img[y_min:y_max, x_min:x_max]
    else:
        walls_cropped = walls_img[250:350, 250:350]
    walls_display = cv2.resize(walls_cropped, (300, 300), interpolation=cv2.INTER_NEAREST)

    # Draw robot position on walls map too
    walls_color = cv2.cvtColor(walls_display, cv2.COLOR_GRAY2BGR)
    # Scale robot position to cropped coordinates
    if len(non_gray[0]) > 10:
        wrx = int((pose[0] / CELL_SIZE_MM - x_min) * 300 / max(x_max - x_min, 1))
        wry = int((pose[1] / CELL_SIZE_MM - y_min) * 300 / max(y_max - y_min, 1))
        cv2.circle(walls_color, (wrx, wry), 4, (0, 100, 255), -1)

    _, walls_jpeg = cv2.imencode('.jpg', walls_color, [cv2.IMWRITE_JPEG_QUALITY, 75])
    walls_b64 = base64.b64encode(walls_jpeg.tobytes()).decode('ascii')

    # Count wall cells
    wall_count = int(np.sum(walls_img > 0))

    status = explorer.get_status()
    msg = json.dumps({
        "type": "slam",
        "image": b64,
        "walls_image": walls_b64,
        "wall_count": wall_count,
        "pose": {"x": round(pose[0]), "y": round(pose[1]), "theta": round(math.degrees(pose[2]), 1)},
        "explorer": status,
        "recording": os.path.basename(_recorder.video_path) if _recorder else None,
        "scans": slam.scan_count,
    })
    dead = set()
    for client in slam_clients:
        try:
            client.write_message(msg)
        except Exception:
            dead.add(client)
    slam_clients.difference_update(dead)


def broadcast_collision():
    if not collision_clients:
        return
    collision.update_sectors()
    status = collision.get_status()
    msg = json.dumps({"type": "collision", **status})
    dead = set()
    for client in collision_clients:
        try:
            client.write_message(msg)
        except Exception:
            dead.add(client)
    collision_clients.difference_update(dead)


def broadcast_status():
    if not status_clients:
        return

    voltage = 0.0
    ax = ay = az = roll = pitch = yaw = 0.0

    if bot:
        try:
            voltage = bot.get_battery_voltage()
            ax, ay, az = bot.get_accelerometer_data()
            roll, pitch, yaw = bot.get_imu_attitude_data()
        except Exception:
            pass

    try:
        ip = subprocess.check_output("hostname -I", shell=True, timeout=3).decode().strip().split()[0]
    except Exception:
        ip = "unknown"

    msg = json.dumps({
        "type": "status",
        "battery": round(voltage, 1),
        "ip": ip,
        "imu": {
            "accel": {"x": round(ax, 2), "y": round(ay, 2), "z": round(az, 2)},
            "angles": {"roll": round(roll, 2), "pitch": round(pitch, 2), "yaw": round(yaw, 2)},
        },
        "lidar_connected": lidar.connected,
        "depth_connected": depth.connected,
        "xbee_connected": xbee.connected,
        "gps": gps.get_data(),
        "scan_counts": lidar.flush_scan_counts(),
        "ts": time.time(),
    })
    dead = set()
    for client in status_clients:
        try:
            client.write_message(msg)
        except Exception:
            dead.add(client)
    status_clients.difference_update(dead)


def make_app():
    return tornado.web.Application([
        (r"/", IndexHandler),
        (r"/ws/lidar", LidarWSHandler),
        (r"/ws/depth", DepthWSHandler),
        (r"/ws/cam/primary", CamPrimaryWSHandler),
        (r"/ws/collision", CollisionWSHandler),
        (r"/api/collision", CollisionConfigHandler),
        (r"/api/lidar_mode", LidarModeHandler),
        (r"/api/depth_offset", DepthOffsetHandler),
        (r"/api/slam_method", SlamMethodHandler),
        (r"/api/calibration", CalibrationHandler),
        (r"/api/explorer", ExplorerHandler),
        (r"/api/slam_data", SlamDataHandler),
        (r"/api/slam_params", SlamParamsHandler),
        (r"/api/landmarks", LandmarkHandler),
        (r"/api/shutdown", ShutdownHandler),
        (r"/api/gps", GpsHandler),
        (r"/api/gps/test_receive", GpsTestReceiveHandler),
        (r"/api/xbee", XBeeStatusHandler),
        (r"/api/route", RouteHandler),
        (r"/api/route_nav/live", RouteNavLiveHandler),
        (r"/ws/slam", SlamWSHandler),
        (r"/ws/status", StatusWSHandler),
        (r"/ws/debug", DebugWSHandler),
        (r"/ws/timing", TimingWSHandler),
        (r"/static/(.*)", tornado.web.StaticFileHandler, {"path": STATIC_DIR}),
        (r"/recordings/(.*)", tornado.web.StaticFileHandler, {"path": RECORDING_DIR}),
        (r"/models/(.*)", tornado.web.StaticFileHandler, {"path": "/home/jetson/RosMaster/models"}),
    ], debug=False)


def main():
    print(f"Starting RosMaster Web UI on port {PORT}")

    # Start LiDAR reader FIRST (grabs /dev/ttyUSB0 before Rosmaster_Lib can)
    lidar.start()
    time.sleep(1)

    # Start depth camera
    depth.start()

    # Start camera
    cam_primary.start()

    # Connect collision avoidance to sensors
    collision.lidar = lidar
    collision.depth = depth

    # Then init Rosmaster on /dev/myserial (CH340 STM32 board)
    init_bot()

    # Stop beeper (send multiple times with delay)
    if bot:
        for _ in range(5):
            try:
                bot.set_beep(0)
                time.sleep(0.3)
            except Exception:
                pass
        print("Beeper silenced")

    # Connect calibration to bot and collision avoidance
    calibration.bot = bot
    calibration.collision = collision

    # Connect explorer to bot, collision avoidance, and LiDAR reader
    explorer.bot = bot
    explorer.collision = collision
    explorer.lidar_reader = lidar

    # Start GPS reader
    gps.start()

    # Start XBee communication
    xbee.bot = bot
    xbee.collision = collision
    xbee.explorer = explorer
    xbee.slam = slam
    xbee.start()

    app = make_app()
    app.listen(PORT, address="0.0.0.0")

    loop = tornado.ioloop.IOLoop.current()

    # Exploration video recording at 1 FPS
    tornado.ioloop.PeriodicCallback(_tick_recording, 1000).start()
    # Route recording at 5Hz
    tornado.ioloop.PeriodicCallback(_tick_route_recording, 200).start()
    # LiDAR at ~10 Hz
    tornado.ioloop.PeriodicCallback(broadcast_lidar, 1000).start()  # 1 Hz
    # Depth at ~5 Hz
    tornado.ioloop.PeriodicCallback(broadcast_depth, 1000).start()  # 1 Hz
    # Camera at ~5 Hz
    tornado.ioloop.PeriodicCallback(broadcast_cam_primary, 1000).start()  # 1 Hz
    # Collision at ~5 Hz
    tornado.ioloop.PeriodicCallback(broadcast_collision, 1000).start()  # 1 Hz
    # SLAM update in background thread (not in Tornado loop)
    import threading as _th
    _th.Thread(target=slam_update_thread, daemon=True).start()
    # SLAM map broadcast at ~2 Hz
    tornado.ioloop.PeriodicCallback(broadcast_slam, 2000).start()  # 0.5 Hz (disabled by default)
    # Status at ~1 Hz
    tornado.ioloop.PeriodicCallback(broadcast_status, 1000).start()
    # Debug sensor data at 10 Hz (only sends when debug clients connected)
    tornado.ioloop.PeriodicCallback(broadcast_debug, 100).start()

    _start_time = time.time()

    def shutdown():
        global bot
        print("\nShutting down...")
        _stop_recording()  # save any in-progress recording
        xbee.stop()
        gps.stop()
        lidar.stop()
        depth.stop()
        cam_primary.stop()
        if bot:
            try:
                bot = None
            except Exception:
                pass
        loop.stop()

    def on_signal(sig, frame):
        # Ignore SIGTERM during first 10 seconds (stale signal from systemctl restart)
        if time.time() - _start_time < 10:
            print(f"Ignoring early SIGTERM ({time.time() - _start_time:.1f}s after start)")
            return
        loop.add_callback_from_signal(shutdown)

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    print("Server ready!")
    loop.start()
    sys.exit(0)


if __name__ == "__main__":
    main()
