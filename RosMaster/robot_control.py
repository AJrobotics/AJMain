"""
Remote control interface for RosMaster X3 (Mecanum wheel).

Sends commands to the Jetson over SSH, which executes them
using the Rosmaster_Lib driver library on the robot.
"""

import time
from remote import ssh_run, check_connection
from config import MAX_SPEED_X, MAX_SPEED_Y, MAX_SPEED_Z, REMOTE_PROJECT_DIR


class RosMasterX3:
    """Remote control for RosMaster X3 via SSH to Jetson Orin."""

    def __init__(self):
        if not check_connection():
            raise ConnectionError("Cannot connect to RosMaster. Check SSH.")
        # Ensure the remote control script exists
        self._ensure_remote_agent()

    def _ensure_remote_agent(self):
        """Check that the robot-side agent script is deployed."""
        try:
            ssh_run(f"test -f {REMOTE_PROJECT_DIR}/robot_agent.py")
        except RuntimeError:
            print("Robot agent not deployed yet. Run: python deploy.py")

    def _robot_cmd(self, python_code: str, timeout: int = 10) -> str:
        """Execute Python code on the robot via the agent."""
        escaped = python_code.replace("'", "'\\''")
        cmd = f"cd {REMOTE_PROJECT_DIR} && python3 -c '{escaped}'"
        return ssh_run(cmd, timeout=timeout)

    # --- Motion Control ---

    def move(self, vx: float = 0, vy: float = 0, vz: float = 0):
        """Set robot velocity (m/s for vx/vy, rad/s for vz).

        vx: forward (+) / backward (-)
        vy: left (+) / right (-)
        vz: rotate left (+) / rotate right (-)
        """
        vx = max(-MAX_SPEED_X, min(MAX_SPEED_X, vx))
        vy = max(-MAX_SPEED_Y, min(MAX_SPEED_Y, vy))
        vz = max(-MAX_SPEED_Z, min(MAX_SPEED_Z, vz))
        self._robot_cmd(
            f"from robot_agent import agent; agent.move({vx}, {vy}, {vz})"
        )

    def stop(self):
        """Stop all movement."""
        self.move(0, 0, 0)

    def forward(self, speed: float = 0.2):
        self.move(vx=speed)

    def backward(self, speed: float = 0.2):
        self.move(vx=-speed)

    def strafe_left(self, speed: float = 0.2):
        self.move(vy=speed)

    def strafe_right(self, speed: float = 0.2):
        self.move(vy=-speed)

    def rotate_left(self, speed: float = 1.0):
        self.move(vz=speed)

    def rotate_right(self, speed: float = 1.0):
        self.move(vz=-speed)

    # --- Peripherals ---

    def buzzer(self, duration: float = 0.5):
        """Sound the buzzer for given seconds."""
        self._robot_cmd(
            f"from robot_agent import agent; agent.buzzer({duration})"
        )

    def set_led(self, r: int, g: int, b: int):
        """Set RGB LED color (0-255 each)."""
        self._robot_cmd(
            f"from robot_agent import agent; agent.set_led({r}, {g}, {b})"
        )

    def get_battery(self) -> float:
        """Get battery voltage."""
        result = self._robot_cmd(
            "from robot_agent import agent; print(agent.get_battery())"
        )
        return float(result)

    def get_imu(self) -> str:
        """Get IMU data (accel, gyro, angles)."""
        return self._robot_cmd(
            "from robot_agent import agent; print(agent.get_imu())"
        )

    # --- Servo Control ---

    def set_servo(self, servo_id: int, angle: int):
        """Set PWM servo angle (0-180)."""
        angle = max(0, min(180, angle))
        self._robot_cmd(
            f"from robot_agent import agent; agent.set_servo({servo_id}, {angle})"
        )


if __name__ == "__main__":
    print("Connecting to RosMaster X3...")
    robot = RosMasterX3()
    print("Connected! Testing buzzer...")
    robot.buzzer(0.3)
    print("Battery:", robot.get_battery(), "V")
    print("Moving forward for 1 second...")
    robot.forward(0.15)
    time.sleep(1)
    robot.stop()
    print("Done!")
