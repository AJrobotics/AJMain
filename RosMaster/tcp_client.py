"""
TCP client for real-time control of RosMaster X3.
Connects to tcp_server.py running on the Jetson.
"""

import json
import socket
from config import ROSMASTER_IP

TCP_PORT = 5555


class RosMasterClient:
    """Fast TCP client for real-time robot control."""

    def __init__(self, ip: str = ROSMASTER_IP, port: int = TCP_PORT):
        self.ip = ip
        self.port = port
        self.sock = None

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(5.0)
        self.sock.connect((self.ip, self.port))
        # Verify connection
        resp = self._send({"action": "ping"})
        if not resp.get("ok"):
            raise ConnectionError("Server did not respond to ping")
        print(f"Connected to RosMaster at {self.ip}:{self.port}")

    def _send(self, cmd: dict) -> dict:
        """Send a JSON command and wait for response."""
        msg = json.dumps(cmd) + "\n"
        self.sock.sendall(msg.encode())
        buf = b""
        while b"\n" not in buf:
            data = self.sock.recv(4096)
            if not data:
                raise ConnectionError("Server closed connection")
            buf += data
        line = buf.split(b"\n")[0]
        return json.loads(line.decode())

    def move(self, vx: float = 0, vy: float = 0, vz: float = 0):
        return self._send({"action": "move", "params": {"vx": vx, "vy": vy, "vz": vz}})

    def stop(self):
        return self._send({"action": "stop"})

    def buzzer(self, duration: float = 0.5):
        return self._send({"action": "buzzer", "params": {"duration": duration}})

    def set_led(self, r: int, g: int, b: int):
        return self._send({"action": "led", "params": {"r": r, "g": g, "b": b}})

    def get_battery(self) -> float:
        resp = self._send({"action": "battery"})
        return resp.get("voltage", 0)

    def get_imu(self) -> dict:
        resp = self._send({"action": "imu"})
        return resp.get("imu", {})

    def set_servo(self, servo_id: int, angle: int):
        return self._send({"action": "servo", "params": {"id": servo_id, "angle": angle}})

    def get_encoder(self) -> tuple:
        resp = self._send({"action": "encoder"})
        return resp.get("encoder", ())

    def close(self):
        if self.sock:
            self.stop()
            self.sock.close()
            self.sock = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.close()


if __name__ == "__main__":
    import time

    with RosMasterClient() as robot:
        print("Battery:", robot.get_battery(), "V")
        print("Buzzer test...")
        robot.buzzer(0.3)
        print("Forward 1 sec...")
        robot.move(vx=0.15)
        time.sleep(1)
        robot.stop()
        print("Done!")
