"""
Vision Client — runs on a robot (R1, Rosmaster, etc.).
Captures camera images and sends to Christy's vision server for analysis.

Usage:
    python -m robotics.vision_client --robot-id R1 --interval 30
    python -m robotics.vision_client --robot-id R1 --once
    python -m robotics.vision_client --robot-id R1 --once --prompt "What obstacles do you see?"
"""

import argparse
import io
import os
import sys
import time
import logging
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("vision_client")

# Defaults (can be overridden by vision_config)
try:
    from robotics.vision_config import (
        CHRISTY_VISION_URL, JPEG_QUALITY, MAX_IMAGE_SIZE,
    )
except ImportError:
    CHRISTY_VISION_URL = "http://192.168.1.94:5100"
    JPEG_QUALITY = 75
    MAX_IMAGE_SIZE = (1280, 960)


def capture_image(camera_index=0):
    """Capture a single frame from the camera using OpenCV. Returns raw JPEG bytes."""
    import cv2

    cap = cv2.VideoCapture(camera_index)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open camera {camera_index}")

    ret, frame = cap.read()
    cap.release()

    if not ret:
        raise RuntimeError("Failed to capture frame")

    # Resize if too large
    h, w = frame.shape[:2]
    max_w, max_h = MAX_IMAGE_SIZE
    if w > max_w or h > max_h:
        scale = min(max_w / w, max_h / h)
        new_w, new_h = int(w * scale), int(h * scale)
        frame = cv2.resize(frame, (new_w, new_h))
        log.info(f"Resized {w}x{h} -> {new_w}x{new_h}")

    # Encode as JPEG
    _, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    return jpeg.tobytes()


def send_image(image_bytes, robot_id, prompt=None, analyze=True):
    """Send image to Christy's vision server.

    Args:
        image_bytes: JPEG bytes
        robot_id: Robot identifier (e.g. "R1")
        prompt: Custom analysis prompt (None for default)
        analyze: If True, request Claude analysis. If False, just upload.

    Returns:
        Server response dict
    """
    endpoint = "/api/vision/analyze" if analyze else "/api/vision/upload"
    url = f"{CHRISTY_VISION_URL}{endpoint}"

    files = {"image": ("capture.jpg", io.BytesIO(image_bytes), "image/jpeg")}
    data = {"robot_id": robot_id}
    if prompt:
        data["prompt"] = prompt

    log.info(f"Sending {len(image_bytes)} bytes to {url}...")
    t0 = time.time()

    try:
        resp = requests.post(url, files=files, data=data, timeout=60)
        elapsed = round((time.time() - t0) * 1000)

        if resp.status_code == 200:
            result = resp.json()
            log.info(f"Response in {elapsed}ms: {result.get('analysis', 'uploaded')[:100]}...")
            return result
        else:
            log.error(f"Server error {resp.status_code}: {resp.text[:200]}")
            return {"error": resp.text, "status_code": resp.status_code}
    except requests.exceptions.ConnectionError:
        log.error(f"Cannot connect to vision server at {CHRISTY_VISION_URL}")
        return {"error": "Connection refused"}
    except requests.exceptions.Timeout:
        log.error("Request timed out (60s)")
        return {"error": "Timeout"}
    except Exception as e:
        log.error(f"Send failed: {e}")
        return {"error": str(e)}


def stream_loop(robot_id, interval, camera_index=0, analyze=False):
    """Continuously capture and upload images (no analysis, just streaming)."""
    log.info(f"Starting stream: robot={robot_id}, interval={interval}s, camera={camera_index}")
    count = 0
    while True:
        try:
            image_bytes = capture_image(camera_index)
            count += 1
            result = send_image(image_bytes, robot_id, analyze=analyze)
            if result.get("error"):
                log.warning(f"Frame {count} failed: {result['error']}")
            else:
                log.info(f"Frame {count} sent ({len(image_bytes)} bytes)")
        except KeyboardInterrupt:
            log.info("Stream stopped by user")
            break
        except Exception as e:
            log.error(f"Frame {count} error: {e}")

        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description="Vision Client — capture and send images")
    parser.add_argument("--robot-id", default="R1", help="Robot identifier")
    parser.add_argument("--camera", type=int, default=0, help="Camera index (default: 0)")
    parser.add_argument("--interval", type=float, default=30, help="Capture interval in seconds")
    parser.add_argument("--once", action="store_true", help="Capture once and exit")
    parser.add_argument("--stream", action="store_true", help="Stream mode (upload only, no analysis)")
    parser.add_argument("--prompt", default=None, help="Custom analysis prompt")
    parser.add_argument("--server", default=None, help="Vision server URL override")
    parser.add_argument("--no-analyze", action="store_true", help="Upload only, skip analysis")
    args = parser.parse_args()

    global CHRISTY_VISION_URL
    if args.server:
        CHRISTY_VISION_URL = args.server

    log.info(f"Vision Client: robot={args.robot_id}, server={CHRISTY_VISION_URL}")

    if args.once:
        # Single capture + analyze
        image_bytes = capture_image(args.camera)
        log.info(f"Captured {len(image_bytes)} bytes from camera {args.camera}")
        result = send_image(image_bytes, args.robot_id, args.prompt, analyze=not args.no_analyze)
        if result.get("analysis"):
            print(f"\n--- Analysis ---\n{result['analysis']}\n")
        elif result.get("error"):
            print(f"\nError: {result['error']}\n")
            sys.exit(1)

    elif args.stream:
        # Stream mode: upload frames without analysis
        stream_loop(args.robot_id, args.interval, args.camera, analyze=False)

    else:
        # Periodic capture + analyze
        log.info(f"Starting periodic capture every {args.interval}s")
        count = 0
        while True:
            try:
                image_bytes = capture_image(args.camera)
                count += 1
                result = send_image(image_bytes, args.robot_id, args.prompt)
                if result.get("analysis"):
                    log.info(f"[{count}] {result['analysis'][:120]}...")
                elif result.get("error"):
                    log.warning(f"[{count}] {result['error']}")
            except KeyboardInterrupt:
                log.info("Stopped by user")
                break
            except Exception as e:
                log.error(f"[{count}] Error: {e}")

            time.sleep(args.interval)


if __name__ == "__main__":
    main()
