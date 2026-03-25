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
collision = CollisionAvoidance()
calibration = CalibrationRunner()
slam = SLAMEngine(ignore_angle=120)
explorer = Explorer(slam=slam, ignore_angle=120)
slam_clients = set()

# Persistent Rosmaster instance for status (uses CH340 on ttyUSB1/ttyUSB2, not ttyUSB0)
bot = None


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
    def post(self):
        try:
            data = json.loads(self.request.body)
            if "ignore_angle" in data:
                val = max(0, min(180, int(data["ignore_angle"])))
                collision.ignore_angle = val
            if "enabled" in data:
                collision.enabled = bool(data["enabled"])
            self.write(json.dumps({"ok": True, "ignore_angle": collision.ignore_angle, "enabled": collision.enabled}))
        except Exception as e:
            self.write(json.dumps({"ok": False, "error": str(e)}))


class DepthOffsetHandler(tornado.web.RequestHandler):
    def post(self):
        try:
            data = json.loads(self.request.body)
            offset = float(data.get("offset", 0.0))
            depth.set_angle_offset(offset)
            self.write(json.dumps({"ok": True, "offset": offset}))
        except Exception as e:
            self.write(json.dumps({"ok": False, "error": str(e)}))


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
                result = explorer.start_exploration()
            elif action == "return_home":
                result = explorer.return_home()
            elif action == "stop":
                result = explorer.stop()
            elif action == "reset_map":
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


def broadcast_lidar():
    if not lidar_clients:
        return
    # Only broadcast when there's a new complete revolution
    with lidar.lock:
        if not lidar.has_new_scan:
            return
        lidar.has_new_scan = False
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
    """Background thread: feed LiDAR scans into SLAM engine."""
    import threading
    while True:
        try:
            scan = lidar.get_scan()
            if scan and len(scan) > 50:
                imu_yaw = None
                if bot:
                    try:
                        _, _, yaw = bot.get_imu_attitude_data()
                        imu_yaw = math.radians(yaw)
                    except Exception:
                        pass
                slam.update(scan, imu_yaw)
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

    status = explorer.get_status()
    msg = json.dumps({
        "type": "slam",
        "image": b64,
        "pose": {"x": round(pose[0]), "y": round(pose[1]), "theta": round(math.degrees(pose[2]), 1)},
        "explorer": status,
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
        (r"/api/depth_offset", DepthOffsetHandler),
        (r"/api/calibration", CalibrationHandler),
        (r"/api/explorer", ExplorerHandler),
        (r"/ws/slam", SlamWSHandler),
        (r"/ws/status", StatusWSHandler),
        (r"/static/(.*)", tornado.web.StaticFileHandler, {"path": STATIC_DIR}),
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

    # Connect explorer to bot and collision avoidance
    explorer.bot = bot
    explorer.collision = collision

    app = make_app()
    app.listen(PORT, address="0.0.0.0")

    loop = tornado.ioloop.IOLoop.current()

    # LiDAR at ~10 Hz
    tornado.ioloop.PeriodicCallback(broadcast_lidar, 200).start()
    # Depth at ~5 Hz
    tornado.ioloop.PeriodicCallback(broadcast_depth, 200).start()
    # Camera at ~5 Hz
    tornado.ioloop.PeriodicCallback(broadcast_cam_primary, 200).start()
    # Collision at ~5 Hz
    tornado.ioloop.PeriodicCallback(broadcast_collision, 200).start()
    # SLAM update in background thread (not in Tornado loop)
    import threading as _th
    _th.Thread(target=slam_update_thread, daemon=True).start()
    # SLAM map broadcast at ~2 Hz
    tornado.ioloop.PeriodicCallback(broadcast_slam, 500).start()
    # Status at ~1 Hz
    tornado.ioloop.PeriodicCallback(broadcast_status, 1000).start()

    def shutdown():
        global bot
        print("\nShutting down...")
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
        loop.add_callback_from_signal(shutdown)

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    print("Server ready!")
    loop.start()
    sys.exit(0)


if __name__ == "__main__":
    main()
