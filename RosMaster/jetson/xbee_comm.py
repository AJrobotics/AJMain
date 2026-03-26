"""XBee communication handler for RosMaster X3."""

import os
import math
import time
import threading


# Known XBee addresses
XBEE_ADDRESSES = {
    "R3":      "0013A20041BB8E1F",
    "Robot1":  "0013A20041BB8D5E",
    "DeskTop": "0013A20041741E51",
}


class XBeeComm:
    def __init__(self, gps_reader=None, bot=None, collision=None,
                 explorer=None, slam=None, port="/dev/xbee", baud=115200):
        self.gps = gps_reader
        self.bot = bot
        self.collision = collision
        self.explorer = explorer
        self.slam = slam
        self.port = port
        self.baud = baud
        self.device = None
        self.connected = False
        self.running = False
        self._thread = None
        self._rx_count = 0
        self._tx_count = 0
        self._error_count = 0
        self._last_rx_time = 0
        self._last_rx_addr = ""
        self._handlers = {}
        self._register_default_handlers()

    def start(self):
        self.running = True
        self._thread = threading.Thread(target=self._connect_loop, daemon=True)
        self._thread.start()

    def _connect_loop(self):
        while self.running:
            if not os.path.exists(self.port):
                self.connected = False
                time.sleep(5)
                continue
            try:
                from digi.xbee.devices import XBeeDevice
                self.device = XBeeDevice(self.port, self.baud)
                self.device.open()
                self.device.add_data_received_callback(self._on_data_received)
                self.connected = True
                print(f"XBee connected: {self.device.get_64bit_addr()}")
                while self.running and self.device.is_open():
                    time.sleep(1)
            except Exception as e:
                print(f"XBee error: {e}")
                self._error_count += 1
                self.connected = False
            finally:
                self._safe_close()
                self.connected = False
            time.sleep(5)

    def _safe_close(self):
        try:
            if self.device and self.device.is_open():
                self.device.close()
        except Exception:
            pass
        self.device = None

    def _on_data_received(self, xbee_message):
        try:
            sender = str(xbee_message.remote_device.get_64bit_addr())
            data = xbee_message.data.decode("utf-8", errors="replace").strip()
            self._last_rx_time = time.monotonic()
            self._last_rx_addr = sender
            self._rx_count += 1
            sender_name = self._addr_to_name(sender)
            print(f"XBee RX [{sender_name}]: {data}")
            response = self._handle_message(sender, data)
            if response:
                self._send_to(xbee_message.remote_device, f"R3: {response}")
        except Exception as e:
            print(f"XBee RX error: {e}")
            self._error_count += 1

    def _addr_to_name(self, addr_str):
        for name, addr in XBEE_ADDRESSES.items():
            if addr.upper() in addr_str.upper():
                return name
        return addr_str[-8:]

    def _handle_message(self, sender_addr, message):
        msg = message.strip().lower()
        priority = "normal"
        if msg.startswith("!"):
            priority = "high"
            msg = msg[1:].strip()
        for pattern, handler in self._handlers.items():
            if msg == pattern or msg.startswith(pattern + " "):
                args = msg[len(pattern):].strip()
                return handler(sender_addr, args, priority)
        return f"Unknown: {message[:50]}"

    def _register_default_handlers(self):
        self._handlers["all good?"] = self._handle_status
        self._handlers["status"] = self._handle_status
        self._handlers["gps"] = self._handle_gps
        self._handlers["battery"] = self._handle_battery
        self._handlers["slam"] = self._handle_slam
        self._handlers["ping"] = self._handle_ping
        self._handlers["move"] = self._handle_move
        self._handlers["stop"] = self._handle_stop
        self._handlers["explore"] = self._handle_explore
        self._handlers["buzzer"] = self._handle_buzzer
        self._handlers["led"] = self._handle_led
        self._handlers["gps_test"] = self._handle_gps_test

    # --- Query handlers ---

    def _handle_status(self, sender, args, priority):
        gps_str = self.gps.get_status_string() if self.gps else "No GPS"
        bat = ""
        if self.bot:
            try:
                v = self.bot.get_battery_voltage()
                bat = f" Bat={v:.1f}V"
            except Exception:
                pass
        return f"{gps_str}{bat}"

    def _handle_gps(self, sender, args, priority):
        if not self.gps:
            return "GPS not available"
        return self.gps.get_status_string()

    def _handle_battery(self, sender, args, priority):
        if not self.bot:
            return "Bot N/C"
        try:
            v = self.bot.get_battery_voltage()
            return f"Bat={v:.1f}V"
        except Exception:
            return "Bat=ERR"

    def _handle_slam(self, sender, args, priority):
        if not self.slam:
            return "SLAM N/A"
        pose = self.slam.pose
        return (f"Pose=({pose[0]:.0f},{pose[1]:.0f},"
                f"{math.degrees(pose[2]):.0f}) Scans={self.slam.scan_count}")

    def _handle_ping(self, sender, args, priority):
        return "pong"

    def _handle_gps_test(self, sender, args, priority):
        """Receive test GPS coordinates, compare with own GPS, return result."""
        if not self.gps:
            return "GPS N/A"
        own = self.gps.get_data()
        if args:
            parts = args.split(",")
            if len(parts) >= 2:
                try:
                    test_lat = float(parts[0])
                    test_lon = float(parts[1])
                    if own["fix"]:
                        dlat = test_lat - own["latitude"]
                        dlon = test_lon - own["longitude"]
                        dist_m = ((dlat * 111000)**2 + (dlon * 88000)**2)**0.5
                        return (f"RX OK diff={dist_m:.1f}m "
                                f"own={own['latitude']:.6f},{own['longitude']:.6f}")
                    else:
                        return f"RX OK no_fix sats={own['satellites']}"
                except ValueError:
                    pass
        return self.gps.get_status_string()

    # --- Control handlers ---

    def _handle_move(self, sender, args, priority):
        parts = args.split()
        if len(parts) < 3:
            return "ERR: move <vx> <vy> <vz>"
        try:
            vx = max(-0.45, min(0.45, float(parts[0])))
            vy = max(-0.45, min(0.45, float(parts[1])))
            vz = max(-3.0, min(3.0, float(parts[2])))
            if self.collision and self.collision.enabled:
                vx, vy, vz = self.collision.filter_motion(vx, vy, vz)
            if self.bot:
                self.bot.set_car_motion(vx, vy, vz)
                return f"Move({vx:.2f},{vy:.2f},{vz:.2f})"
            return "Bot N/C"
        except ValueError:
            return "ERR: invalid numbers"

    def _handle_stop(self, sender, args, priority):
        if self.bot:
            self.bot.set_car_motion(0, 0, 0)
        if self.explorer and self.explorer.state not in ("idle", "stopped", "arrived"):
            self.explorer.stop()
        return "Stopped"

    def _handle_explore(self, sender, args, priority):
        if not self.explorer:
            return "Explorer N/A"
        cmd = args.strip().lower() if args else "status"
        if cmd == "start":
            self.explorer.start_exploration(time_limit=300)
            return "Explore started"
        elif cmd == "stop":
            self.explorer.stop()
            return "Explore stopped"
        elif cmd == "status":
            return f"Explore: {self.explorer.state}"
        return "ERR: explore start|stop|status"

    def _handle_buzzer(self, sender, args, priority):
        if not self.bot:
            return "Bot N/C"
        duration = 0.5
        if args:
            try:
                duration = float(args)
            except ValueError:
                pass
        bot = self.bot
        threading.Thread(
            target=lambda: (bot.set_beep(1), time.sleep(duration), bot.set_beep(0)),
            daemon=True
        ).start()
        return f"Buzzer {duration}s"

    def _handle_led(self, sender, args, priority):
        if not self.bot:
            return "Bot N/C"
        parts = args.split()
        if len(parts) < 3:
            return "ERR: led <r> <g> <b>"
        try:
            r, g, b = int(parts[0]), int(parts[1]), int(parts[2])
            self.bot.set_colorful_effect(0, 6, parm=1)
            self.bot.set_colorful_lamps(0xFF, r, g, b)
            return f"LED({r},{g},{b})"
        except (ValueError, AttributeError):
            return "ERR: invalid RGB"

    # --- Send helpers ---

    def _send_to(self, remote_device, message):
        if not self.device or not self.device.is_open():
            return
        try:
            data = message[:255].encode("utf-8")
            self.device.send_data(remote_device, data)
            self._tx_count += 1
        except Exception as e:
            print(f"XBee TX error: {e}")
            self._error_count += 1

    def broadcast(self, message):
        if not self.device or not self.device.is_open():
            return
        try:
            data = f"R3: {message}"[:255].encode("utf-8")
            self.device.send_data_broadcast(data)
            self._tx_count += 1
        except Exception as e:
            print(f"XBee broadcast error: {e}")
            self._error_count += 1

    def get_status(self):
        return {
            "connected": self.connected,
            "rx_count": self._rx_count,
            "tx_count": self._tx_count,
            "error_count": self._error_count,
            "last_rx_addr": self._addr_to_name(self._last_rx_addr) if self._last_rx_addr else "",
        }

    def stop(self):
        self.running = False
        self._safe_close()
        if self._thread:
            self._thread.join(timeout=5)
