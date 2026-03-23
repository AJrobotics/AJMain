"""
Local gamepad reader for Linux (evdev/jsdev).

Reads /dev/input/js* devices and produces gamepad data in the same
format as XBee gamepad packets:  {axes: {0: val, ...}, buttons: {0: val, ...}}

Runs as a background thread, feeding data into a callback at a configurable rate.
"""

from __future__ import annotations

import glob
import logging
import os
import struct
import threading
import time
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Linux joystick event struct:  timestamp(I), value(h), type(B), number(B)
JS_EVENT_FMT = "IhBB"
JS_EVENT_SIZE = struct.calcsize(JS_EVENT_FMT)

# Event types
JS_EVENT_BUTTON = 0x01
JS_EVENT_AXIS = 0x02
JS_EVENT_INIT = 0x80


def list_gamepads() -> List[dict]:
    """Return list of available joystick devices."""
    devices = []
    for path in sorted(glob.glob("/dev/input/js*")):
        try:
            name = "Unknown"
            name_path = f"/sys/class/input/{os.path.basename(path)}/device/name"
            if os.path.exists(name_path):
                with open(name_path) as f:
                    name = f.read().strip()
            # Count axes and buttons via ioctl
            import fcntl
            import array
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            try:
                buf = array.array("B", [0])
                fcntl.ioctl(fd, 0x80016A11, buf)  # JSIOCGAXES
                num_axes = buf[0]
                buf = array.array("B", [0])
                fcntl.ioctl(fd, 0x80016A12, buf)  # JSIOCGBUTTONS
                num_buttons = buf[0]
            except Exception:
                num_axes = 0
                num_buttons = 0
            finally:
                os.close(fd)
            devices.append({
                "path": path,
                "name": name,
                "axes": num_axes,
                "buttons": num_buttons,
                "index": int(path.replace("/dev/input/js", "")),
            })
        except Exception as e:
            logger.debug("Cannot probe %s: %s", path, e)
    return devices


class LocalGamepadReader:
    """Background thread that reads a Linux joystick device."""

    def __init__(self, callback: Callable[[dict], None],
                 rate_hz: float = 50.0):
        """
        Args:
            callback: called with {axes: {0: float, ...}, buttons: {0: int, ...}}
                      at `rate_hz` when gamepad has data.
            rate_hz: how often to deliver consolidated state.
        """
        self._callback = callback
        self._rate_hz = rate_hz
        self._interval = 1.0 / rate_hz

        self._device_path: Optional[str] = None
        self._device_name: str = ""
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # Current state
        self._axes: Dict[int, float] = {}
        self._buttons: Dict[int, int] = {}
        self._lock = threading.Lock()
        self._has_new_data = False

    @property
    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    @property
    def device_path(self) -> Optional[str]:
        return self._device_path

    @property
    def device_name(self) -> str:
        return self._device_name

    def start(self, device_path: str = "/dev/input/js0") -> bool:
        """Start reading from the specified joystick device."""
        if self.is_running:
            self.stop()

        if not os.path.exists(device_path):
            logger.warning("Gamepad device not found: %s", device_path)
            return False

        self._device_path = device_path
        # Get device name
        name_path = f"/sys/class/input/{os.path.basename(device_path)}/device/name"
        try:
            with open(name_path) as f:
                self._device_name = f.read().strip()
        except Exception:
            self._device_name = device_path

        self._running = True
        self._thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._thread.start()
        logger.info("Local gamepad started: %s (%s)", device_path, self._device_name)
        return True

    def stop(self):
        """Stop the reader thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._device_path = None
        self._device_name = ""
        logger.info("Local gamepad stopped")

    def get_state(self) -> dict:
        """Return current {axes, buttons} snapshot."""
        with self._lock:
            return {
                "axes": {str(k): v for k, v in self._axes.items()},
                "buttons": {str(k): v for k, v in self._buttons.items()},
                "hats": {},
            }

    def _reader_loop(self):
        """Read joystick events and deliver state at fixed rate."""
        import fcntl

        try:
            fd = os.open(self._device_path, os.O_RDONLY | os.O_NONBLOCK)
        except Exception as e:
            logger.error("Cannot open %s: %s", self._device_path, e)
            self._running = False
            return

        logger.info("Reader thread started, fd=%d, reading %s", fd, self._device_path)
        event_count = 0
        last_deliver = 0.0

        while self._running:
            # Read all pending events (raw unbuffered)
            try:
                while True:
                    event_data = os.read(fd, JS_EVENT_SIZE)
                    if len(event_data) < JS_EVENT_SIZE:
                        break
                    _ts, value, etype, number = struct.unpack(JS_EVENT_FMT, event_data)
                    etype &= ~JS_EVENT_INIT  # strip init flag

                    event_count += 1
                    with self._lock:
                        if etype == JS_EVENT_AXIS:
                            self._axes[number] = round(value / 32767.0, 4)
                            self._has_new_data = True
                        elif etype == JS_EVENT_BUTTON:
                            self._buttons[number] = value
                            self._has_new_data = True
                    if event_count <= 20 or event_count % 100 == 0:
                        logger.debug("Event #%d: type=%d num=%d val=%d",
                                     event_count, etype, number, value)
            except BlockingIOError:
                pass
            except OSError as e:
                if e.errno == 19:  # No such device — gamepad unplugged
                    logger.warning("Gamepad disconnected: %s", self._device_path)
                    break
                # Other OS errors — skip
                pass

            # Deliver at fixed rate
            now = time.time()
            if now - last_deliver >= self._interval:
                last_deliver = now
                with self._lock:
                    state = {
                        "axes": {str(k): v for k, v in self._axes.items()},
                        "buttons": {str(k): v for k, v in self._buttons.items()},
                        "hats": {},
                    }
                    self._has_new_data = False
                try:
                    self._callback(state)
                except Exception as e:
                    logger.error("Gamepad callback error: %s", e)

            time.sleep(0.005)

        os.close(fd)
        self._running = False
