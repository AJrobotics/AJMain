"""
Orbbec Astra depth camera reader for RosMaster X3.
Uses OpenNI2 via primesense library for depth frames.
Runs in a separate process to avoid GIL contention.
Provides depth line, sector distances, and heatmap JPEG via shared memory + queue.
"""

import time
import multiprocessing as mp

OPENNI2_PATH = "/home/jetson/yahboomcar_ros2_ws/software/library_ws/install/astra_camera/include/openni2/openni2_redist/arm64"

# Depth shared memory layout (mp.Array('f', 326)):
# [0]       = point count
# [1]       = timestamp (monotonic)
# [2:322]   = 160 x (angle, dist) pairs — depth line
# [322:325] = 3 front sector distances (right-front=7, front=0, left-front=1)
# [325]     = connected flag (1.0 = connected)
DSHM_LINE_OFFSET = 2
DSHM_LINE_MAX_POINTS = 160
DSHM_SECTORS_OFFSET = 322
DSHM_CONNECTED_OFFSET = 325
DSHM_SIZE = 326

HFOV = 60.0


def _depth_process(depth_shm, frame_queue, stop_event, angle_offset_val):
    """Runs in a separate process — captures depth, extracts line + sectors."""
    try:
        import numpy as np
        import cv2
        import base64
        from primesense import openni2

        openni2.initialize(OPENNI2_PATH)
        dev = openni2.Device.open_any()
        info = dev.get_device_info()
        print(f"Depth camera: {info.name.decode()} by {info.vendor.decode()}", flush=True)

        depth_stream = dev.create_depth_stream()
        depth_stream.start()
        depth_shm[DSHM_CONNECTED_OFFSET] = 1.0
        print("Depth stream started", flush=True)

        while not stop_event.is_set():
            frame = depth_stream.read_frame()
            data = np.frombuffer(frame.get_buffer_as_uint16(), dtype=np.uint16)
            data = data.reshape((frame.height, frame.width))
            h, w = data.shape
            now = time.monotonic()

            # --- Extract depth line (rows 20%-30% from top) ---
            row_start = int(h * 0.20)
            row_end = int(h * 0.30)
            band = data[row_start:row_end, :]
            half_fov = HFOV / 2.0
            offset = angle_offset_val.value

            line = []
            for col in range(0, w, 4):
                col_data = band[:, col]
                valid = col_data[col_data > 100]
                if len(valid) < 2:
                    continue
                d = int(np.median(valid))
                if d > 8000:
                    continue
                angle = -(col - w / 2.0) / (w / 2.0) * half_fov + offset
                line.append((round(angle, 1), d))

            # Write depth line to shared memory
            n = min(len(line), DSHM_LINE_MAX_POINTS)
            depth_shm[0] = float(n)
            depth_shm[1] = now
            for i in range(n):
                depth_shm[DSHM_LINE_OFFSET + i * 2] = line[i][0]
                depth_shm[DSHM_LINE_OFFSET + i * 2 + 1] = float(line[i][1])

            # --- Compute 3 front sector distances (same as old _fuse_depth) ---
            # Wall-level band: rows 15%-35% from top
            wall_start = int(h * 0.15)
            wall_end = int(h * 0.35)
            wall_band = data[wall_start:wall_end, :]

            third = w // 3
            zones = [
                wall_band[:, :third],          # left image = right-front (mirrored)
                wall_band[:, third:2*third],   # center = front
                wall_band[:, 2*third:],        # right image = left-front (mirrored)
            ]

            for zi, zone_data in enumerate(zones):
                valid = zone_data[(zone_data > 300) & (zone_data < 8000)]
                if len(valid) > 100:
                    depth_shm[DSHM_SECTORS_OFFSET + zi] = float(np.percentile(valid, 10))
                else:
                    depth_shm[DSHM_SECTORS_OFFSET + zi] = 9999.0

            # --- Generate heatmap JPEG for web UI ---
            valid_all = data[data > 0]
            stats = {
                "min": int(valid_all.min()) if len(valid_all) > 0 else 0,
                "max": int(valid_all.max()) if len(valid_all) > 0 else 0,
                "mean": int(valid_all.mean()) if len(valid_all) > 0 else 0,
                "width": w, "height": h,
            }

            normalized = np.zeros_like(data, dtype=np.uint8)
            mask = data > 0
            if mask.any():
                d_min = valid_all.min()
                d_max = min(valid_all.max(), 5000)
                clipped = np.clip(data, d_min, d_max).astype(np.float32)
                clipped[~mask] = 0
                normalized[mask] = (255 * (1.0 - (clipped[mask] - d_min) / max(d_max - d_min, 1))).astype(np.uint8)

            colored = cv2.applyColorMap(normalized, cv2.COLORMAP_JET)
            colored[~mask] = [0, 0, 0]
            small = cv2.resize(colored, (240, 180))
            _, jpeg = cv2.imencode('.jpg', small, [cv2.IMWRITE_JPEG_QUALITY, 70])
            b64 = base64.b64encode(jpeg.tobytes()).decode('ascii')

            # Send heatmap via queue (drop if full)
            try:
                frame_queue.put_nowait((b64, stats))
            except Exception:
                try:
                    frame_queue.get_nowait()
                except Exception:
                    pass
                try:
                    frame_queue.put_nowait((b64, stats))
                except Exception:
                    pass

            time.sleep(0.1)  # ~10 FPS

        depth_stream.stop()
        dev.close()
        openni2.unload()

    except Exception as e:
        print(f"Depth camera error: {e}", flush=True)
        depth_shm[DSHM_CONNECTED_OFFSET] = 0.0


class DepthReader:
    """Same public API, but runs depth camera in a separate process."""

    def __init__(self):
        self._process = None
        self._last_frame = (None, {})

        # Shared memory for depth line + sector distances
        self._depth_shm = mp.Array('f', DSHM_SIZE)
        # Queue for heatmap JPEG (variable size, not suitable for shared memory)
        self._frame_queue = mp.Queue(maxsize=5)
        self._stop_event = mp.Event()
        self._angle_offset_val = mp.Value('f', 0.0)

    @property
    def connected(self):
        return self._depth_shm[DSHM_CONNECTED_OFFSET] > 0.5

    @property
    def angle_offset(self):
        return self._angle_offset_val.value

    def set_angle_offset(self, offset_deg):
        self._angle_offset_val.value = float(offset_deg)

    def start(self):
        self._stop_event.clear()
        self._process = mp.Process(
            target=_depth_process,
            args=(self._depth_shm, self._frame_queue,
                  self._stop_event, self._angle_offset_val),
            daemon=True,
        )
        self._process.start()

    def get_depth_line(self):
        """Returns list of (angle_deg, distance_mm) from shared memory."""
        n = int(self._depth_shm[0])
        line = []
        for i in range(min(n, DSHM_LINE_MAX_POINTS)):
            a = self._depth_shm[DSHM_LINE_OFFSET + i * 2]
            d = self._depth_shm[DSHM_LINE_OFFSET + i * 2 + 1]
            line.append((round(a, 1), int(d)))
        return line

    def get_depth_sectors(self):
        """Return 3 front sector distances: [right-front, front, left-front]."""
        return [self._depth_shm[DSHM_SECTORS_OFFSET + i] for i in range(3)]

    def get_shm_timestamp(self):
        """Return monotonic timestamp of last shared memory write."""
        return self._depth_shm[1]

    def get_frame(self):
        """Returns (base64_jpeg, stats_dict) or (None, {})."""
        latest = None
        while True:
            try:
                latest = self._frame_queue.get_nowait()
            except Exception:
                break
        if latest is not None:
            self._last_frame = latest
        return self._last_frame

    def stop(self):
        self._stop_event.set()
        if self._process and self._process.is_alive():
            self._process.join(timeout=5)
            if self._process.is_alive():
                self._process.terminate()
