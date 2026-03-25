"""
Camera reader for RosMaster X3.
Captures frames from Astra RGB (primary) and USB webcam (secondary).
Streams as base64 JPEG via WebSocket.
"""

import time
import threading
import base64
import cv2


class CameraReader:
    def __init__(self, device_id, name="camera", width=640, height=480, fps=10):
        self.device_id = device_id
        self.name = name
        self.target_width = width
        self.target_height = height
        self.target_fps = fps
        self.running = False
        self.connected = False
        self.frame_jpeg = None
        self.lock = threading.Lock()
        self._thread = None

    def start(self):
        self.running = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def _read_loop(self):
        cap = cv2.VideoCapture(self.device_id)
        if not cap.isOpened():
            print(f"{self.name}: Cannot open /dev/video{self.device_id}")
            self.connected = False
            return

        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.target_width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.target_height)

        actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        print(f"{self.name}: Opened /dev/video{self.device_id} at {actual_w}x{actual_h}")
        self.connected = True

        interval = 1.0 / self.target_fps

        while self.running:
            ret, frame = cap.read()
            if not ret:
                time.sleep(0.1)
                continue

            # Resize if needed
            h, w = frame.shape[:2]
            if w > self.target_width or h > self.target_height:
                frame = cv2.resize(frame, (self.target_width, self.target_height))

            _, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 65])
            b64 = base64.b64encode(jpeg.tobytes()).decode('ascii')

            with self.lock:
                self.frame_jpeg = b64

            time.sleep(interval)

        cap.release()
        print(f"{self.name}: Closed")

    def get_frame(self):
        with self.lock:
            return self.frame_jpeg

    def stop(self):
        self.running = False
        if self._thread:
            self._thread.join(timeout=3)
