"""
Vision Server — runs on Christy (192.168.1.94:5100).
Receives images from any robot via HTTP POST, analyzes with AI,
stores results, and serves them to the AJMain dashboard.

Backends:
    yolo   — FREE, local, no internet (default)
    gemini — FREE, cloud, needs API key
    claude — paid, cloud, needs API key

Usage:
    python -m robotics.vision_server
    python -m robotics.vision_server --backend yolo     (default, FREE, local)
    python -m robotics.vision_server --backend gemini   (FREE, cloud)
    python -m robotics.vision_server --backend claude   (paid, cloud)
"""

import argparse
import base64
import io
import json
import os
import time
import uuid
import logging
from collections import deque
from datetime import datetime
from threading import Lock

from flask import Flask, request, jsonify, send_file

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("vision_server")

app = Flask(__name__)

# ---------------------------------------------------------------------------
# In-memory storage
# ---------------------------------------------------------------------------
_results_lock = Lock()
_results = {}          # robot_id -> deque of result dicts
_robot_status = {}     # robot_id -> {last_seen, total_analyses, ...}
_server_start = datetime.now()

# Config defaults
try:
    from robotics.vision_config import (
        VISION_SERVER_PORT, VISION_BACKEND,
        YOLO_MODEL, YOLO_CONFIDENCE,
        GEMINI_MODEL, GEMINI_MAX_TOKENS,
        CLAUDE_MODEL, CLAUDE_MAX_TOKENS,
        DEFAULT_PROMPT, THUMBNAIL_SIZE, IMAGE_STORE_DIR,
        MAX_HISTORY_PER_ROBOT,
    )
except ImportError:
    VISION_SERVER_PORT = 5100
    VISION_BACKEND = "yolo"
    YOLO_MODEL = "yolov8n.pt"
    YOLO_CONFIDENCE = 0.4
    GEMINI_MODEL = "gemini-2.0-flash"
    GEMINI_MAX_TOKENS = 1024
    CLAUDE_MODEL = "claude-sonnet-4-20250514"
    CLAUDE_MAX_TOKENS = 1024
    DEFAULT_PROMPT = "Describe what you see. Identify objects, obstacles, or notable features."
    THUMBNAIL_SIZE = (320, 240)
    IMAGE_STORE_DIR = "/home/ajrobotics/vision/images"
    MAX_HISTORY_PER_ROBOT = 200

# Active backend (can be overridden by --backend arg)
_active_backend = VISION_BACKEND

# Ensure image store exists
os.makedirs(IMAGE_STORE_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# AI Backends
# ---------------------------------------------------------------------------

# --- YOLO (FREE, local) ---

_yolo_model = None


def _get_yolo():
    """Lazy-init YOLO model."""
    global _yolo_model
    if _yolo_model is None:
        from ultralytics import YOLO
        log.info(f"Loading YOLO model: {YOLO_MODEL}...")
        _yolo_model = YOLO(YOLO_MODEL)
        log.info("YOLO model loaded")
    return _yolo_model


def analyze_yolo(image_bytes: bytes, prompt: str, robot_id: str) -> tuple:
    """Detect objects using YOLO. Returns (analysis_text, annotated_image_bytes)."""
    from PIL import Image
    import numpy as np

    model = _get_yolo()
    img = Image.open(io.BytesIO(image_bytes))

    log.info(f"[{robot_id}] Running YOLO detection ({len(image_bytes)} bytes)...")
    t0 = time.time()

    results = model(img, conf=YOLO_CONFIDENCE, verbose=False)
    elapsed = round((time.time() - t0) * 1000)

    # Parse detections
    detections = []
    for r in results:
        for box in r.boxes:
            cls_id = int(box.cls[0])
            cls_name = model.names[cls_id]
            conf = float(box.conf[0])
            x1, y1, x2, y2 = box.xyxy[0].tolist()
            w, h = x2 - x1, y2 - y1
            detections.append({
                "class": cls_name,
                "confidence": round(conf, 2),
                "bbox": [round(x1), round(y1), round(w), round(h)],
            })

    # Build text summary
    if detections:
        # Count objects
        obj_counts = {}
        for d in detections:
            name = d["class"]
            obj_counts[name] = obj_counts.get(name, 0) + 1

        summary_parts = []
        for name, count in sorted(obj_counts.items(), key=lambda x: -x[1]):
            if count > 1:
                summary_parts.append(f"{count}x {name}")
            else:
                summary_parts.append(name)

        analysis = f"Detected {len(detections)} object(s): {', '.join(summary_parts)}\n\n"
        for i, d in enumerate(detections, 1):
            analysis += f"  {i}. {d['class']} ({d['confidence']:.0%}) at [{d['bbox'][0]}, {d['bbox'][1]}]\n"
    else:
        analysis = "No objects detected."

    # Generate annotated image
    annotated_bytes = None
    try:
        annotated = results[0].plot()  # numpy array with boxes drawn
        annotated_img = Image.fromarray(annotated)
        buf = io.BytesIO()
        annotated_img.save(buf, format="JPEG", quality=85)
        annotated_bytes = buf.getvalue()
    except Exception as e:
        log.warning(f"Could not generate annotated image: {e}")

    log.info(f"[{robot_id}] YOLO: {len(detections)} detections in {elapsed}ms")
    return analysis, detections, annotated_bytes


# --- Gemini (FREE, cloud) ---

_gemini_model = None


def _get_gemini():
    global _gemini_model
    if _gemini_model is None:
        from google import genai
        _gemini_model = genai.Client()
    return _gemini_model


def analyze_gemini(image_bytes: bytes, prompt: str, robot_id: str) -> str:
    from google.genai import types
    from PIL import Image

    client = _get_gemini()
    log.info(f"[{robot_id}] Calling Gemini ({GEMINI_MODEL}, {len(image_bytes)} bytes)...")
    t0 = time.time()

    img = Image.open(io.BytesIO(image_bytes))
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[img, prompt],
    )

    elapsed = round((time.time() - t0) * 1000)
    result_text = response.text
    log.info(f"[{robot_id}] Gemini responded in {elapsed}ms ({len(result_text)} chars)")
    return result_text


# --- Claude (Paid, cloud) ---

_anthropic_client = None


def _get_claude():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic
        _anthropic_client = anthropic.Anthropic()
    return _anthropic_client


def analyze_claude(image_bytes: bytes, prompt: str, robot_id: str) -> str:
    client = _get_claude()
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    log.info(f"[{robot_id}] Calling Claude ({CLAUDE_MODEL}, {len(image_bytes)} bytes)...")
    t0 = time.time()

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=CLAUDE_MAX_TOKENS,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                {"type": "text", "text": prompt},
            ],
        }],
    )

    elapsed = round((time.time() - t0) * 1000)
    result_text = message.content[0].text
    log.info(f"[{robot_id}] Claude responded in {elapsed}ms ({len(result_text)} chars)")
    return result_text


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

def save_thumbnail(image_bytes: bytes, image_id: str) -> str:
    from PIL import Image
    img = Image.open(io.BytesIO(image_bytes))
    img.thumbnail(THUMBNAIL_SIZE)
    path = os.path.join(IMAGE_STORE_DIR, f"{image_id}.jpg")
    img.save(path, "JPEG", quality=80)
    return path


def save_full_image(image_bytes: bytes, image_id: str) -> str:
    path = os.path.join(IMAGE_STORE_DIR, f"{image_id}_full.jpg")
    with open(path, "wb") as f:
        f.write(image_bytes)
    return path


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

@app.route("/api/vision/analyze", methods=["POST"])
def api_analyze():
    """Receive image, analyze with active backend, return result."""
    if "image" not in request.files:
        return jsonify({"error": "No image file provided"}), 400

    robot_id = request.form.get("robot_id", "unknown")
    prompt = request.form.get("prompt", DEFAULT_PROMPT)
    image_file = request.files["image"]
    image_bytes = image_file.read()

    if len(image_bytes) == 0:
        return jsonify({"error": "Empty image"}), 400

    image_id = str(uuid.uuid4())[:12]
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        # Save original
        save_full_image(image_bytes, image_id)

        t0 = time.time()
        detections = None
        annotated_bytes = None

        if _active_backend == "yolo":
            analysis, detections, annotated_bytes = analyze_yolo(image_bytes, prompt, robot_id)
            # Save annotated image as thumbnail (with bounding boxes)
            if annotated_bytes:
                save_thumbnail(annotated_bytes, image_id)
            else:
                save_thumbnail(image_bytes, image_id)
        elif _active_backend == "gemini":
            analysis = analyze_gemini(image_bytes, prompt, robot_id)
            save_thumbnail(image_bytes, image_id)
        elif _active_backend == "claude":
            analysis = analyze_claude(image_bytes, prompt, robot_id)
            save_thumbnail(image_bytes, image_id)
        else:
            return jsonify({"error": f"Unknown backend: {_active_backend}"}), 400

        processing_ms = round((time.time() - t0) * 1000)

        result = {
            "id": image_id,
            "robot_id": robot_id,
            "timestamp": timestamp,
            "prompt": prompt if _active_backend != "yolo" else "YOLO object detection",
            "analysis": analysis,
            "image_id": image_id,
            "processing_time_ms": processing_ms,
            "image_size": len(image_bytes),
            "backend": _active_backend,
        }
        if detections is not None:
            result["detections"] = detections

        with _results_lock:
            if robot_id not in _results:
                _results[robot_id] = deque(maxlen=MAX_HISTORY_PER_ROBOT)
            _results[robot_id].appendleft(result)

            if robot_id not in _robot_status:
                _robot_status[robot_id] = {"total_analyses": 0}
            _robot_status[robot_id]["last_seen"] = timestamp
            _robot_status[robot_id]["last_analysis"] = timestamp
            _robot_status[robot_id]["total_analyses"] += 1
            _robot_status[robot_id]["last_result_id"] = image_id

        log.info(f"[{robot_id}] Analysis complete: {image_id} ({processing_ms}ms, {_active_backend})")
        return jsonify({"ok": True, **result})

    except Exception as e:
        log.error(f"[{robot_id}] Analysis failed: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/vision/upload", methods=["POST"])
def api_upload():
    """Upload image without analysis."""
    if "image" not in request.files:
        return jsonify({"error": "No image file provided"}), 400

    robot_id = request.form.get("robot_id", "unknown")
    image_file = request.files["image"]
    image_bytes = image_file.read()

    if len(image_bytes) == 0:
        return jsonify({"error": "Empty image"}), 400

    image_id = str(uuid.uuid4())[:12]
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        save_thumbnail(image_bytes, image_id)
        save_full_image(image_bytes, image_id)

        with _results_lock:
            if robot_id not in _robot_status:
                _robot_status[robot_id] = {"total_analyses": 0}
            _robot_status[robot_id]["last_seen"] = timestamp
            _robot_status[robot_id]["last_image_id"] = image_id

        return jsonify({"ok": True, "image_id": image_id, "timestamp": timestamp})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/vision/status")
def api_status():
    """Return status for all robots."""
    with _results_lock:
        robots = {}
        for robot_id, status in _robot_status.items():
            robots[robot_id] = {**status, "history_count": len(_results.get(robot_id, []))}

    uptime_s = int((datetime.now() - _server_start).total_seconds())
    return jsonify({
        "server": "running",
        "backend": _active_backend,
        "uptime_seconds": uptime_s,
        "uptime_human": f"{uptime_s // 3600}h {(uptime_s % 3600) // 60}m",
        "robots": robots,
    })


@app.route("/api/vision/history")
def api_history():
    robot_id = request.args.get("robot_id", "")
    limit = request.args.get("limit", 20, type=int)
    with _results_lock:
        if robot_id:
            items = list(_results.get(robot_id, []))[:limit]
        else:
            all_items = []
            for rid, dq in _results.items():
                all_items.extend(list(dq))
            all_items.sort(key=lambda x: x["timestamp"], reverse=True)
            items = all_items[:limit]
    return jsonify({"results": items, "count": len(items)})


@app.route("/api/vision/latest")
def api_latest():
    robot_id = request.args.get("robot_id", "R1")
    with _results_lock:
        dq = _results.get(robot_id)
        if dq and len(dq) > 0:
            result = dict(dq[0])
            thumb_path = os.path.join(IMAGE_STORE_DIR, f"{result['image_id']}.jpg")
            if os.path.exists(thumb_path):
                with open(thumb_path, "rb") as f:
                    result["thumbnail_b64"] = base64.b64encode(f.read()).decode("utf-8")
            return jsonify({"ok": True, **result})
        else:
            return jsonify({"ok": False, "error": f"No results for {robot_id}"})


@app.route("/api/vision/image/<image_id>")
def api_image(image_id):
    safe_id = image_id.replace("/", "").replace("\\", "").replace("..", "")
    full = request.args.get("full", "0") == "1"
    suffix = "_full" if full else ""
    path = os.path.join(IMAGE_STORE_DIR, f"{safe_id}{suffix}.jpg")
    if not os.path.exists(path):
        return jsonify({"error": "Image not found"}), 404
    return send_file(path, mimetype="image/jpeg")


@app.route("/api/vision/robots")
def api_robots():
    try:
        from robotics.vision_config import CAMERA_ROBOTS
    except ImportError:
        CAMERA_ROBOTS = {}
    robots = {}
    for rid, info in CAMERA_ROBOTS.items():
        robots[rid] = {"configured": True, **info}
    with _results_lock:
        for rid in _robot_status:
            if rid not in robots:
                robots[rid] = {"configured": False}
            robots[rid]["status"] = _robot_status[rid]
    return jsonify({"robots": robots})


@app.route("/api/vision/backend", methods=["GET", "POST"])
def api_backend():
    global _active_backend
    if request.method == "POST":
        new_backend = request.json.get("backend", "yolo") if request.json else "yolo"
        if new_backend in ("yolo", "gemini", "claude"):
            _active_backend = new_backend
            log.info(f"Backend switched to: {_active_backend}")
            return jsonify({"ok": True, "backend": _active_backend})
        else:
            return jsonify({"error": f"Unknown backend: {new_backend}"}), 400
    return jsonify({"backend": _active_backend})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global _active_backend
    parser = argparse.ArgumentParser(description="Vision Server")
    parser.add_argument("--port", type=int, default=VISION_SERVER_PORT)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--backend", default=VISION_BACKEND,
                        choices=["yolo", "gemini", "claude"],
                        help="AI backend: yolo (free/local), gemini (free/cloud), claude (paid)")
    args = parser.parse_args()

    _active_backend = args.backend
    labels = {"yolo": "FREE, local", "gemini": "FREE, cloud", "claude": "PAID, cloud"}

    log.info(f"Starting Vision Server on {args.host}:{args.port}")
    log.info(f"AI Backend: {_active_backend} ({labels.get(_active_backend, '')})")
    log.info(f"Image store: {IMAGE_STORE_DIR}")

    # Pre-load YOLO model at startup
    if _active_backend == "yolo":
        _get_yolo()

    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
