#!/usr/bin/env python3
"""
RosMaster X3 OLED Status Display.
Shows hardware status (LiDAR, Depth sensor, XBee) and system info
on the 128x32 SSD1306 OLED display.
"""

import os
import glob
import time
import signal
import sys
import serial
import subprocess

import Adafruit_SSD1306 as SSD
from PIL import Image, ImageDraw, ImageFont
from Rosmaster_Lib import Rosmaster

# Display config
WIDTH = 128
HEIGHT = 32
I2C_BUS_LIST = [1, 0, 7, 8]
FONT_SIZE = 10
FONT_PATH = "DejaVuSansMono-Bold.ttf"
PAGE_INTERVAL = 5  # seconds per page

# Robot
ROBOT_TYPE = 2  # X3 mecanum


class StatusDisplay:
    def __init__(self):
        self.oled = None
        self.image = Image.new("1", (WIDTH, HEIGHT))
        self.draw = ImageDraw.Draw(self.image)
        self.font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
        self.bot = None
        try:
            import os
            if os.path.exists("/dev/rosmaster"):
                self.bot = Rosmaster(car_type=ROBOT_TYPE, com="/dev/rosmaster")
                self.bot.create_receive_threading()
                time.sleep(1)
                if self.bot.get_battery_voltage() < 1:
                    del self.bot
                    self.bot = None
        except Exception:
            self.bot = None
        self.running = True
        self.page = 0

    def init_oled(self) -> bool:
        for bus in I2C_BUS_LIST:
            try:
                self.oled = SSD.SSD1306_128_32(rst=None, i2c_bus=bus, gpio=1)
                self.oled.begin()
                self.oled.clear()
                self.oled.display()
                return True
            except Exception:
                continue
        return False

    def clear(self):
        self.draw.rectangle((0, 0, WIDTH, HEIGHT), outline=0, fill=0)

    def text(self, x, y, msg):
        self.draw.text((x, y), msg, font=self.font, fill=255)

    def show(self):
        self.oled.image(self.image)
        self.oled.display()

    # --- Hardware checks ---

    def check_lidar(self) -> str:
        """Check RPLidar S2 — just verify the CP210x USB device exists."""
        try:
            out = subprocess.check_output("lsusb", timeout=3).decode()
            if "10c4:ea60" in out.lower():
                return "OK"
        except Exception:
            pass
        return "N/C"

    def check_depth(self) -> str:
        """Check Orbbec Astra depth camera via USB vendor ID 2bc5."""
        try:
            out = subprocess.check_output("lsusb", timeout=3).decode()
            if "2bc5" in out.lower():
                return "OK"
        except Exception:
            pass
        # Fallback: check for video devices
        if glob.glob("/dev/video*"):
            return "OK"
        return "N/C"

    def check_xbee(self) -> str:
        """Check XBee on /dev/xbee (FT231X at USB path 0:2.1.3)."""
        import os
        if not os.path.exists("/dev/xbee"):
            return "N/C"
        try:
            s = serial.Serial("/dev/xbee", 115200, timeout=0.5)
            s.close()
            return "OK"
        except Exception:
            return "N/C"

    def get_ip(self) -> str:
        try:
            ip = subprocess.check_output(
                "hostname -I", shell=True, timeout=3
            ).decode().strip().split()[0]
            return ip if ip else "x.x.x.x"
        except Exception:
            return "x.x.x.x"

    def get_battery(self) -> float:
        if not self.bot:
            return 0.0
        try:
            return self.bot.get_battery_voltage()
        except Exception:
            return 0.0

    def get_cpu(self) -> str:
        try:
            load = os.getloadavg()[0]
            return f"{load:.1f}"
        except Exception:
            return "?"

    # --- Display pages ---

    def draw_status_page(self, lidar, depth, xbee, ip, volts):
        self.clear()
        # Line 1: LiDAR + short IP
        short_ip = ip.split(".")[-1] if ip != "x.x.x.x" else "?"
        self.text(0, -2, f"LiDAR:{lidar:>3} .{short_ip}")
        # Line 2: Depth sensor
        self.text(0, 9, f"Depth:{depth:>3}")
        # Line 3: XBee + battery
        self.text(0, 20, f"XBee:{xbee:>4} {volts:.1f}V")
        self.show()

    def draw_info_page(self, ip, volts, cpu):
        self.clear()
        self.text(0, -2, f"IP:{ip}")
        self.text(0, 9, f"CPU:{cpu} Bat:{volts:.1f}V")
        now = time.strftime("%m/%d %H:%M")
        self.text(0, 20, now)
        self.show()

    # --- Main loop ---

    def run(self):
        if not self.init_oled():
            print("OLED not found!")
            sys.exit(1)

        print("Status display started")
        time.sleep(1)

        while self.running:
            try:
                lidar = self.check_lidar()
                depth = self.check_depth()
                xbee = self.check_xbee()
                ip = self.get_ip()
                volts = self.get_battery()
                cpu = self.get_cpu()

                if self.page == 0:
                    self.draw_status_page(lidar, depth, xbee, ip, volts)
                else:
                    self.draw_info_page(ip, volts, cpu)

                self.page = (self.page + 1) % 2
                time.sleep(PAGE_INTERVAL)
            except Exception as e:
                print(f"Display error: {e}")
                time.sleep(2)

    def shutdown(self):
        self.running = False
        try:
            self.clear()
            self.text(0, 9, "  Shutting down...")
            self.show()
        except Exception:
            pass
        del self.bot


def main():
    display = StatusDisplay()

    def handler(sig, frame):
        display.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)
    display.run()


if __name__ == "__main__":
    main()
