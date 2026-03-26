"""GPS NMEA reader thread for u-blox 7 module."""

import os
import time
import threading
import serial


class GpsReader:
    def __init__(self, port="/dev/gps", baud=9600):
        self.port = port
        self.baud = baud
        self.running = False
        self.connected = False
        self._thread = None
        self._lock = threading.Lock()
        self._data = {
            "fix": False,
            "satellites": 0,
            "latitude": 0.0,
            "longitude": 0.0,
            "altitude_m": 0.0,
            "speed_knots": 0.0,
            "heading_deg": 0.0,
            "utc_time": "",
            "timestamp": 0.0,
        }

    def start(self):
        self.running = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def _read_loop(self):
        while self.running:
            if not os.path.exists(self.port):
                self.connected = False
                time.sleep(5)
                continue
            try:
                ser = serial.Serial(self.port, self.baud, timeout=1)
                self.connected = True
                print(f"GPS connected: {self.port}")
                while self.running:
                    line = ser.readline().decode("ascii", errors="ignore").strip()
                    if not line:
                        continue
                    if not self._verify_checksum(line):
                        continue
                    if line.startswith("$GPGGA") or line.startswith("$GNGGA"):
                        self._parse_gga(line)
                    elif line.startswith("$GPRMC") or line.startswith("$GNRMC"):
                        self._parse_rmc(line)
            except (serial.SerialException, OSError) as e:
                print(f"GPS error: {e}")
                self.connected = False
            time.sleep(5)

    def _verify_checksum(self, sentence):
        if "*" not in sentence:
            return False
        try:
            body, chk = sentence[1:].split("*", 1)
            calc = 0
            for c in body:
                calc ^= ord(c)
            return calc == int(chk, 16)
        except (ValueError, IndexError):
            return False

    def _parse_gga(self, sentence):
        """Parse $GPGGA — fix, satellites, altitude."""
        parts = sentence.split(",")
        if len(parts) < 10:
            return
        with self._lock:
            self._data["timestamp"] = time.monotonic()
            if parts[1]:
                self._data["utc_time"] = f"{parts[1][:2]}:{parts[1][2:4]}:{parts[1][4:6]}"
            fix_q = int(parts[6]) if parts[6] else 0
            self._data["fix"] = fix_q > 0
            self._data["satellites"] = int(parts[7]) if parts[7] else 0
            if fix_q > 0 and parts[9]:
                self._data["altitude_m"] = float(parts[9])

    def _parse_rmc(self, sentence):
        """Parse $GPRMC — lat, lon, speed, heading."""
        parts = sentence.split(",")
        if len(parts) < 10:
            return
        with self._lock:
            self._data["timestamp"] = time.monotonic()
            valid = parts[2] == "A"
            if valid and parts[3] and parts[5]:
                self._data["latitude"] = self._nmea_to_decimal(parts[3], parts[4])
                self._data["longitude"] = self._nmea_to_decimal(parts[5], parts[6])
            if parts[7]:
                try:
                    self._data["speed_knots"] = float(parts[7])
                except ValueError:
                    pass
            if parts[8]:
                try:
                    self._data["heading_deg"] = float(parts[8])
                except ValueError:
                    pass

    def _nmea_to_decimal(self, value, direction):
        """Convert NMEA coordinate (ddmm.mmmmm) to decimal degrees."""
        try:
            dot = value.index(".")
            degrees = int(value[:dot - 2])
            minutes = float(value[dot - 2:])
            dec = degrees + minutes / 60.0
            if direction in ("S", "W"):
                dec = -dec
            return dec
        except (ValueError, IndexError):
            return 0.0

    def get_data(self):
        with self._lock:
            return dict(self._data)

    def get_status_string(self):
        with self._lock:
            if self._data["fix"]:
                lat = self._data["latitude"]
                lon = self._data["longitude"]
                lat_s = f"{abs(lat):.3f}{'N' if lat >= 0 else 'S'}"
                lon_s = f"{abs(lon):.3f}{'E' if lon >= 0 else 'W'}"
                return (f"Fix=1 Sats={self._data['satellites']} "
                        f"{lat_s} {lon_s} {self._data['altitude_m']:.0f}m")
            else:
                return f"No fix, {self._data['satellites']} sats"

    def stop(self):
        self.running = False
        if self._thread:
            self._thread.join(timeout=3)
