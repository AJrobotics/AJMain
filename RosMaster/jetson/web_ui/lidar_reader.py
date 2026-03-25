"""
RPLidar S2 data reader for RosMaster X3.
Uses pyrplidar library for proper protocol handling.
Runs in a separate process to avoid GIL contention.
"""

import math
import time
import queue
import multiprocessing as mp


LIDAR_PORT = "/dev/rplidar"
LIDAR_BAUD = 1000000
MOTOR_PWM = 800

# Shared memory layout for lidar_shm (mp.Array('f', 730)):
# [0]       = point count
# [1]       = timestamp (monotonic)
# [2:722]   = 360 x (angle, dist) pairs
# [722:730] = 8 sector min distances
SHM_SCAN_OFFSET = 2
SHM_SCAN_MAX_POINTS = 360
SHM_SECTORS_OFFSET = 722
SHM_SIZE = 730

# Collision sector config (same as collision_avoidance.py)
NUM_SECTORS = 8
SECTOR_SIZE = 45.0
IGNORE_ANGLE = 140  # rear ignore zone degrees


def _angle_to_sector(angle_deg):
    """Convert angle (0=front, clockwise) to sector index."""
    adjusted = (angle_deg + SECTOR_SIZE / 2) % 360
    return int(adjusted / SECTOR_SIZE) % NUM_SECTORS


def _lidar_process(port, baud, scan_queue, counts_queue, debug_queue,
                   stop_event, mode_change_event, scan_mode_val,
                   raw_debug_flag, connected_flag, simulated_flag,
                   lidar_shm):
    """Runs in a separate process — no GIL contention with Tornado."""
    try:
        from pyrplidar import PyRPlidar
        import serial

        # Full reset: stop motor, flush serial, wait for clean state
        try:
            lidar_reset = PyRPlidar()
            lidar_reset.connect(port=port, baudrate=baud, timeout=3)
            lidar_reset.stop()
            lidar_reset.set_motor_pwm(0)
            time.sleep(0.5)
            lidar_reset.disconnect()
            time.sleep(0.5)
        except Exception:
            # If connect fails, flush serial manually
            try:
                ser = serial.Serial(port, baud, timeout=0.1)
                ser.reset_input_buffer()
                ser.reset_output_buffer()
                ser.close()
                time.sleep(0.5)
            except Exception:
                pass

        while not stop_event.is_set():
            lidar = PyRPlidar()
            lidar.connect(port=port, baudrate=baud, timeout=3)

            info = lidar.get_info()
            print(f"RPLidar connected: {info}", flush=True)
            connected_flag.value = 1
            simulated_flag.value = 0
            mode_change_event.clear()

            lidar.set_motor_pwm(MOTOR_PWM)
            time.sleep(1)

            # Start scan based on current mode
            mode = "express" if scan_mode_val.value == 1 else "standard"
            if mode == "express":
                print("LiDAR: Express scan mode (2x points)", flush=True)
                scan_generator = lidar.start_scan_express(0)
            else:
                print("LiDAR: Standard scan mode", flush=True)
                scan_generator = lidar.start_scan()

            angle_map = {}
            raw_count = 0
            rev_count = 0
            first_rev = True
            rev_counter = 0
            # Debug: per-revolution tracking
            dbg_quality_hist = [0] * 16
            dbg_angle_cov = [0] * 36
            dbg_samples = []
            dbg_sample_nth = 0

            rev_time = time.monotonic()

            for measurement in scan_generator():
                if stop_event.is_set() or mode_change_event.is_set():
                    break

                angle = measurement.angle
                distance = measurement.distance
                quality = measurement.quality
                start_flag = getattr(measurement, 'start_flag', False)
                rev_count += 1

                now = time.monotonic()

                # Debug: collect per-measurement stats
                if raw_debug_flag.value:
                    q = min(quality, 15)
                    dbg_quality_hist[q] += 1
                    if distance > 0 and (quality > 0 or mode == "express"):
                        bin_idx = int(angle / 10) % 36
                        dbg_angle_cov[bin_idx] += 1
                        dbg_sample_nth += 1
                        if dbg_sample_nth % 10 == 0:
                            dbg_samples.append([round(angle, 1), round(distance), quality])

                # Detect revolution: hardware start_flag (encoder sync pulse)
                if start_flag:
                    if first_rev:
                        first_rev = False
                    elif raw_count > 100:
                        scan = sorted(angle_map.values(), key=lambda p: p["angle"])

                        # Write scan + sector distances to shared memory
                        sectors = [9999.0] * NUM_SECTORS
                        half_ignore = IGNORE_ANGLE / 2.0
                        n = min(len(scan), SHM_SCAN_MAX_POINTS)
                        lidar_shm[0] = float(n)
                        lidar_shm[1] = now
                        for i in range(n):
                            a = scan[i]["angle"]
                            d = scan[i]["dist"]
                            lidar_shm[SHM_SCAN_OFFSET + i * 2] = a
                            lidar_shm[SHM_SCAN_OFFSET + i * 2 + 1] = d
                            # Compute sector distances (skip rear ignore + body noise)
                            if d >= 50:
                                angle_from_rear = abs(((a - 180) + 180) % 360 - 180)
                                if angle_from_rear >= half_ignore:
                                    s = _angle_to_sector(a)
                                    if d < sectors[s]:
                                        sectors[s] = d
                        for i in range(NUM_SECTORS):
                            lidar_shm[SHM_SECTORS_OFFSET + i] = sectors[i]

                        # Send scan via queue for web UI (drop if full)
                        try:
                            scan_queue.put_nowait(scan)
                        except Exception:
                            try:
                                scan_queue.get_nowait()
                            except Exception:
                                pass
                            try:
                                scan_queue.put_nowait(scan)
                            except Exception:
                                pass
                        # Send count
                        try:
                            counts_queue.put_nowait(raw_count)
                        except Exception:
                            pass
                        # Debug: emit revolution summary
                        if raw_debug_flag.value:
                            dt_ms = round((now - rev_time) * 1000, 1)
                            rev_counter += 1
                            summary = {
                                "rev_id": rev_counter,
                                "ts": round(now, 3),
                                "dt_ms": dt_ms,
                                "rev_count": rev_count,
                                "valid_count": raw_count,
                                "dedup_count": len(angle_map),
                                "quality_hist": dbg_quality_hist,
                                "angle_cov": dbg_angle_cov,
                                "samples": dbg_samples,
                            }
                            try:
                                debug_queue.put_nowait(summary)
                            except Exception:
                                pass
                        dbg_quality_hist = [0] * 16
                        dbg_angle_cov = [0] * 36
                        dbg_samples = []
                        dbg_sample_nth = 0
                    angle_map = {}
                    raw_count = 0
                    rev_count = 0
                    rev_time = now

                if distance > 0 and (quality > 0 or mode == "express"):
                    raw_count += 1
                    if mode == "express":
                        key = int(angle * 2) % 720
                    else:
                        key = int(angle) % 360
                    angle_map[key] = {
                        "angle": round(angle, 1),
                        "dist": round(distance),
                    }

            lidar.stop()
            lidar.set_motor_pwm(0)
            lidar.disconnect()

            if mode_change_event.is_set():
                mode_change_event.clear()
                print(f"LiDAR: switching mode...", flush=True)
                time.sleep(1)
                continue
            else:
                break

    except Exception as e:
        print(f"RPLidar error: {e}. Using simulated data.", flush=True)
        connected_flag.value = 0
        simulated_flag.value = 1
        _simulated_loop(scan_queue, stop_event)


def _simulated_loop(scan_queue, stop_event):
    """Fallback simulated data when LiDAR is not available."""
    import random
    while not stop_event.is_set():
        points = []
        t = time.time()
        for i in range(360):
            dist = 2000
            if -30 < i < 30:
                dist = 1500 + 100 * math.sin(t * 2 + i * 0.1)
            elif 60 < i < 120:
                dist = 2000 + 50 * math.sin(t * 3 + i * 0.1)
            elif 150 < i < 210:
                dist = 3000 + 100 * math.sin(t + i * 0.05)
            elif 240 < i < 300:
                dist = 1800 + 80 * math.sin(t * 1.5 + i * 0.1)
            else:
                dist = 2500 + 300 * math.sin(i * math.pi / 60 + t)
            dist += random.gauss(0, 20)
            dist = max(25, min(8000, dist))
            points.append({"angle": round(i, 1), "dist": round(dist)})
        try:
            scan_queue.put_nowait(points)
        except Exception:
            try:
                scan_queue.get_nowait()
            except Exception:
                pass
            try:
                scan_queue.put_nowait(points)
            except Exception:
                pass
        time.sleep(0.1)


class LidarReader:
    """Same public API as before, but runs LiDAR in a separate process."""

    def __init__(self, port=LIDAR_PORT, baud=LIDAR_BAUD):
        self.port = port
        self.baud = baud
        self._process = None
        self._last_scan = []

        # Multiprocessing shared state
        self._scan_queue = mp.Queue(maxsize=10)
        self._counts_queue = mp.Queue(maxsize=100)
        self._debug_queue = mp.Queue(maxsize=200)
        self._stop_event = mp.Event()
        self._mode_change_event = mp.Event()
        self._scan_mode_val = mp.Value('i', 0)      # 0=standard, 1=express
        self._raw_debug_flag = mp.Value('b', 0)
        self._connected_flag = mp.Value('b', 0)
        self._simulated_flag = mp.Value('b', 0)
        # Shared memory: scan data + sector distances
        self._lidar_shm = mp.Array('f', SHM_SIZE)

    @property
    def connected(self):
        return bool(self._connected_flag.value)

    @property
    def simulated(self):
        return bool(self._simulated_flag.value)

    @property
    def scan_mode(self):
        return "express" if self._scan_mode_val.value == 1 else "standard"

    def set_scan_mode(self, mode):
        if mode not in ("standard", "express"):
            return
        new_val = 1 if mode == "express" else 0
        if new_val != self._scan_mode_val.value:
            self._scan_mode_val.value = new_val
            self._mode_change_event.set()
            print(f"LiDAR scan mode changed to: {mode}")

    def start(self):
        self._stop_event.clear()
        self._process = mp.Process(
            target=_lidar_process,
            args=(self.port, self.baud, self._scan_queue, self._counts_queue,
                  self._debug_queue, self._stop_event, self._mode_change_event,
                  self._scan_mode_val, self._raw_debug_flag,
                  self._connected_flag, self._simulated_flag,
                  self._lidar_shm),
            daemon=True,
        )
        self._process.start()

    def get_scan(self):
        """Return latest scan data. Drains queue, keeps most recent."""
        latest = None
        while True:
            try:
                latest = self._scan_queue.get_nowait()
            except Exception:
                break
        if latest is not None:
            self._last_scan = latest
        return list(self._last_scan)

    def get_lidar_sectors(self):
        """Return 8 sector min distances from shared memory."""
        return [self._lidar_shm[SHM_SECTORS_OFFSET + i] for i in range(NUM_SECTORS)]

    def get_shm_timestamp(self):
        """Return monotonic timestamp of last shared memory write."""
        return self._lidar_shm[1]

    def flush_scan_counts(self):
        """Return and clear accumulated scan point counts."""
        counts = []
        while True:
            try:
                counts.append(self._counts_queue.get_nowait())
            except Exception:
                break
        return counts

    def enable_raw_debug(self, enabled):
        self._raw_debug_flag.value = 1 if enabled else 0
        if not enabled:
            # Drain debug queue
            while True:
                try:
                    self._debug_queue.get_nowait()
                except Exception:
                    break
        print(f"LiDAR raw debug: {'enabled' if enabled else 'disabled'}")

    def flush_raw_revs(self):
        """Return and clear accumulated revolution summaries."""
        revs = []
        while True:
            try:
                revs.append(self._debug_queue.get_nowait())
            except Exception:
                break
        return revs

    def stop(self):
        self._stop_event.set()
        if self._process and self._process.is_alive():
            self._process.join(timeout=5)
            if self._process.is_alive():
                self._process.terminate()
