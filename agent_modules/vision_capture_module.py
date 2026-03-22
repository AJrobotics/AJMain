"""
Vision Capture Module — runs on R1 (Raspberry Pi with camera).
Provides local camera capture, MJPEG streaming, YOLO analysis via Christy,
offline queue with auto-sync, and image history.
"""

import io
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime

from flask import Blueprint, Response, jsonify, request, send_file

logger = logging.getLogger(__name__)

# Paths
QUEUE_DIR = os.path.expanduser("~/vision_queue")
HISTORY_DIR = os.path.join(QUEUE_DIR, "history")
QUEUE_PENDING_DIR = os.path.join(QUEUE_DIR, "pending")

# Defaults (overridden by vision_config if available)
CHRISTY_VISION_URL = "http://192.168.1.94:5100"
CAMERA_DEVICE = "/dev/video0"
CAMERA_ROTATE = 180
STREAM_FPS = 10
JPEG_QUALITY = 75

try:
    from robotics.vision_config import (
        CHRISTY_VISION_URL as _url,
        CAMERA_ROBOTS,
    )
    CHRISTY_VISION_URL = _url
    _r1_cfg = CAMERA_ROBOTS.get("R1", {})
    CAMERA_DEVICE = _r1_cfg.get("camera", CAMERA_DEVICE)
    CAMERA_ROTATE = _r1_cfg.get("rotate", CAMERA_ROTATE)
except ImportError:
    pass


class VisionCaptureModule:
    name = "vision_capture"

    def __init__(self):
        self._cap = None
        self._cap_lock = threading.Lock()
        self._history = []
        self._history_lock = threading.Lock()
        self._sync_thread = None
        self._running = False

    def _get_camera(self):
        """Lazy-init OpenCV camera."""
        if self._cap is None:
            import cv2
            dev = int(CAMERA_DEVICE.replace("/dev/video", "")) if CAMERA_DEVICE.startswith("/dev/video") else CAMERA_DEVICE
            self._cap = cv2.VideoCapture(dev)
            if not self._cap.isOpened():
                logger.error("Cannot open camera: %s", CAMERA_DEVICE)
                self._cap = None
        return self._cap

    def _grab_frame(self):
        """Capture a single frame, return JPEG bytes or None."""
        import cv2
        with self._cap_lock:
            cap = self._get_camera()
            if cap is None:
                return None
            ret, frame = cap.read()
            if not ret:
                return None
            if CAMERA_ROTATE == 180:
                frame = cv2.rotate(frame, cv2.ROTATE_180)
            elif CAMERA_ROTATE == 90:
                frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
            elif CAMERA_ROTATE == 270:
                frame = cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
            _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
            return buf.tobytes()

    def _send_to_christy(self, image_bytes, robot_id="R1"):
        """Send image to Christy vision server for YOLO analysis. Returns result dict or None."""
        import requests
        try:
            url = CHRISTY_VISION_URL + "/api/vision/analyze"
            files = {"image": ("capture.jpg", image_bytes, "image/jpeg")}
            data = {"robot_id": robot_id}
            r = requests.post(url, files=files, data=data, timeout=15)
            if r.status_code == 200:
                return r.json()
        except Exception as e:
            logger.warning("Cannot reach Christy vision server: %s", e)
        return None

    def _save_to_queue(self, image_bytes, image_id, timestamp):
        """Save image to offline queue for later sync."""
        os.makedirs(QUEUE_PENDING_DIR, exist_ok=True)
        img_path = os.path.join(QUEUE_PENDING_DIR, f"{image_id}.jpg")
        meta_path = os.path.join(QUEUE_PENDING_DIR, f"{image_id}.json")
        with open(img_path, "wb") as f:
            f.write(image_bytes)
        with open(meta_path, "w") as f:
            json.dump({"image_id": image_id, "timestamp": timestamp, "synced": False}, f)
        logger.info("Saved to offline queue: %s", image_id)

    def _save_to_history(self, image_bytes, image_id, result):
        """Save captured image and result to local history."""
        os.makedirs(HISTORY_DIR, exist_ok=True)
        img_path = os.path.join(HISTORY_DIR, f"{image_id}.jpg")
        with open(img_path, "wb") as f:
            f.write(image_bytes)
        with self._history_lock:
            self._history.insert(0, result)
            if len(self._history) > 200:
                self._history = self._history[:200]

    def _sync_worker(self):
        """Background thread: periodically sync pending images to Christy."""
        while self._running:
            time.sleep(30)
            if not os.path.exists(QUEUE_PENDING_DIR):
                continue
            pending = [f for f in os.listdir(QUEUE_PENDING_DIR) if f.endswith(".jpg")]
            if not pending:
                continue
            for fname in pending:
                img_path = os.path.join(QUEUE_PENDING_DIR, fname)
                meta_path = img_path.replace(".jpg", ".json")
                try:
                    with open(img_path, "rb") as f:
                        image_bytes = f.read()
                    result = self._send_to_christy(image_bytes)
                    if result and result.get("ok"):
                        # Synced successfully — move to history
                        image_id = fname.replace(".jpg", "")
                        result["image_id"] = image_id
                        result["synced_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        self._save_to_history(image_bytes, image_id, result)
                        os.remove(img_path)
                        if os.path.exists(meta_path):
                            os.remove(meta_path)
                        logger.info("Synced queued image: %s", image_id)
                    else:
                        break  # Christy still unreachable, stop trying
                except Exception as e:
                    logger.error("Sync error for %s: %s", fname, e)
                    break

    def register(self, app):
        os.makedirs(QUEUE_DIR, exist_ok=True)
        os.makedirs(HISTORY_DIR, exist_ok=True)
        os.makedirs(QUEUE_PENDING_DIR, exist_ok=True)

        self._running = True
        self._sync_thread = threading.Thread(target=self._sync_worker, daemon=True)
        self._sync_thread.start()

        bp = Blueprint("vision_capture", __name__)
        mod = self  # closure ref

        @bp.route("/api/vision/capture", methods=["POST"])
        def vision_capture():
            """Capture frame, send to Christy for YOLO, return text result."""
            image_bytes = mod._grab_frame()
            if image_bytes is None:
                return jsonify({"ok": False, "error": "Camera capture failed"}), 500

            image_id = str(uuid.uuid4())[:12]
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # Try sending to Christy
            result = mod._send_to_christy(image_bytes)
            if result and result.get("ok"):
                result["image_id"] = image_id
                result["timestamp"] = timestamp
                result["mode"] = "online"
                mod._save_to_history(image_bytes, image_id, result)
                return jsonify(result)
            else:
                # Offline mode — queue for later
                mod._save_to_queue(image_bytes, image_id, timestamp)
                offline_result = {
                    "ok": True,
                    "image_id": image_id,
                    "timestamp": timestamp,
                    "mode": "offline",
                    "analysis": "Queued for analysis (Christy offline)",
                    "detections": [],
                }
                mod._save_to_history(image_bytes, image_id, offline_result)
                return jsonify(offline_result)

        @bp.route("/api/vision/stream")
        def vision_stream():
            """MJPEG real-time camera stream."""
            def generate():
                delay = 1.0 / STREAM_FPS
                while True:
                    frame = mod._grab_frame()
                    if frame is None:
                        time.sleep(0.5)
                        continue
                    yield (b"--frame\r\n"
                           b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")
                    time.sleep(delay)
            return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")

        @bp.route("/api/vision/snapshot")
        def vision_snapshot():
            """Single frame JPEG snapshot."""
            frame = mod._grab_frame()
            if frame is None:
                return jsonify({"error": "Camera capture failed"}), 500
            return Response(frame, mimetype="image/jpeg")

        @bp.route("/api/vision/queue")
        def vision_queue():
            """Offline queue status."""
            pending = []
            if os.path.exists(QUEUE_PENDING_DIR):
                pending = [f for f in os.listdir(QUEUE_PENDING_DIR) if f.endswith(".jpg")]
            return jsonify({
                "pending_count": len(pending),
                "queue_dir": QUEUE_PENDING_DIR,
            })

        @bp.route("/api/vision/history")
        def vision_history():
            """Recent capture+analysis results."""
            limit = request.args.get("limit", 20, type=int)
            with mod._history_lock:
                items = mod._history[:limit]
            return jsonify({"results": items, "count": len(items)})

        @bp.route("/api/vision/image/<image_id>")
        def vision_image(image_id):
            """Serve stored image by ID."""
            safe_id = image_id.replace("/", "").replace("\\", "").replace("..", "")
            path = os.path.join(HISTORY_DIR, f"{safe_id}.jpg")
            if not os.path.exists(path):
                return jsonify({"error": "Image not found"}), 404
            return send_file(path, mimetype="image/jpeg")

        @bp.route("/api/vision/status")
        def vision_status():
            """Vision module status."""
            pending = []
            if os.path.exists(QUEUE_PENDING_DIR):
                pending = [f for f in os.listdir(QUEUE_PENDING_DIR) if f.endswith(".jpg")]
            # Check Christy connectivity
            christy_online = False
            try:
                import requests
                r = requests.get(CHRISTY_VISION_URL + "/api/vision/status", timeout=3)
                christy_online = r.status_code == 200
            except Exception:
                pass
            with mod._history_lock:
                total_captures = len(mod._history)
            return jsonify({
                "camera": CAMERA_DEVICE,
                "christy_online": christy_online,
                "christy_url": CHRISTY_VISION_URL,
                "total_captures": total_captures,
                "pending_sync": len(pending),
                "stream_fps": STREAM_FPS,
            })

        app.register_blueprint(bp)
        logger.info("Vision capture module registered (camera=%s, christy=%s)", CAMERA_DEVICE, CHRISTY_VISION_URL)
