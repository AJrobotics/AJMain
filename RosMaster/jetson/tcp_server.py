"""
TCP command server running on the Jetson.
Receives JSON commands from the Windows PC and executes them on the robot.
This is faster than SSH per-command for real-time control.

Deploy to Jetson and run: python3 tcp_server.py
"""

import json
import socket
import threading
import time
import signal
import sys

from robot_agent import RobotAgent
from collision_avoidance import CollisionAvoidance

HOST = "0.0.0.0"
PORT = 5555


class RobotServer:
    def __init__(self, host=HOST, port=PORT, collision=None):
        self.host = host
        self.port = port
        self.agent = RobotAgent()
        self.collision = collision
        self.running = False
        self.server_socket = None

    def handle_command(self, cmd: dict) -> dict:
        """Execute a command and return the result."""
        action = cmd.get("action", "")
        params = cmd.get("params", {})

        try:
            if action == "move":
                vx = params.get("vx", 0)
                vy = params.get("vy", 0)
                vz = params.get("vz", 0)
                # Apply collision filter
                if self.collision:
                    vx, vy, vz = self.collision.filter_motion(vx, vy, vz)
                self.agent.move(vx, vy, vz)
                return {"ok": True}

            elif action == "stop":
                self.agent.stop()
                return {"ok": True}

            elif action == "collision_enable":
                if self.collision:
                    self.collision.enabled = True
                return {"ok": True}

            elif action == "collision_disable":
                if self.collision:
                    self.collision.enabled = False
                return {"ok": True}

            elif action == "collision_status":
                if self.collision:
                    return {"ok": True, "collision": self.collision.get_status()}
                return {"ok": True, "collision": {"enabled": False}}

            elif action == "buzzer":
                self.agent.buzzer(params.get("duration", 0.5))
                return {"ok": True}

            elif action == "led":
                self.agent.set_led(
                    params.get("r", 0),
                    params.get("g", 0),
                    params.get("b", 0),
                )
                return {"ok": True}

            elif action == "battery":
                v = self.agent.get_battery()
                return {"ok": True, "voltage": v}

            elif action == "imu":
                data = self.agent.get_imu()
                return {"ok": True, "imu": data}

            elif action == "servo":
                self.agent.set_servo(params["id"], params["angle"])
                return {"ok": True}

            elif action == "encoder":
                speeds = self.agent.get_encoder()
                return {"ok": True, "encoder": speeds}

            elif action == "ping":
                return {"ok": True, "msg": "pong"}

            else:
                return {"ok": False, "error": f"Unknown action: {action}"}

        except Exception as e:
            return {"ok": False, "error": str(e)}

    def handle_client(self, conn, addr):
        """Handle a single client connection."""
        print(f"Client connected: {addr}")
        buf = b""
        try:
            while self.running:
                data = conn.recv(4096)
                if not data:
                    break
                buf += data
                # Process complete JSON messages (newline-delimited)
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        cmd = json.loads(line.decode())
                        result = self.handle_command(cmd)
                        response = json.dumps(result) + "\n"
                        conn.sendall(response.encode())
                    except json.JSONDecodeError:
                        err = json.dumps({"ok": False, "error": "Invalid JSON"}) + "\n"
                        conn.sendall(err.encode())
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            print(f"Client disconnected: {addr}")
            self.agent.stop()
            conn.close()

    def start(self):
        """Start the TCP server."""
        self.running = True
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind((self.host, self.port))
        self.server_socket.listen(2)
        self.server_socket.settimeout(1.0)

        print(f"RosMaster TCP server listening on {self.host}:{self.port}")

        while self.running:
            try:
                conn, addr = self.server_socket.accept()
                t = threading.Thread(target=self.handle_client, args=(conn, addr), daemon=True)
                t.start()
            except socket.timeout:
                continue

    def shutdown(self):
        print("\nShutting down...")
        self.running = False
        self.agent.stop()
        self.agent.close()
        if self.server_socket:
            self.server_socket.close()


def main():
    server = RobotServer()

    def signal_handler(sig, frame):
        server.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    server.start()


if __name__ == "__main__":
    main()
