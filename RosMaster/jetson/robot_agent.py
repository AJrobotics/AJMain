"""
Robot-side agent that runs on the Jetson Orin.
Uses Rosmaster_Lib to control hardware directly.

This file gets deployed to the Jetson at ~/RosMaster/robot_agent.py
"""

import time
import threading

from Rosmaster_Lib import Rosmaster

# X3 uses mecanum wheels (type=2)
ROBOT_TYPE = 2


class RobotAgent:
    """Direct hardware interface via Rosmaster_Lib on the Jetson."""

    def __init__(self):
        self.bot = Rosmaster(car_type=ROBOT_TYPE)
        self.bot.create_receive_threading()
        time.sleep(0.1)

    def move(self, vx: float, vy: float, vz: float):
        """Set car motion: vx (fwd), vy (left), vz (rotate left)."""
        self.bot.set_car_motion(vx, vy, vz)

    def stop(self):
        self.bot.set_car_motion(0, 0, 0)

    def buzzer(self, duration: float = 0.5):
        """Sound buzzer for duration seconds."""
        self.bot.set_beep(1)
        time.sleep(duration)
        self.bot.set_beep(0)

    def set_led(self, r: int, g: int, b: int):
        """Set RGB LED strip color."""
        self.bot.set_colorful_effect(0, 6, parm=1)
        self.bot.set_colorful_lamps(0xFF, r, g, b)

    def get_battery(self) -> float:
        """Get battery voltage in volts."""
        return self.bot.get_battery_voltage()

    def get_imu(self) -> dict:
        """Get IMU data: accelerometer, gyroscope, and angles."""
        ax, ay, az = self.bot.get_accelerometer_data()
        gx, gy, gz = self.bot.get_gyroscope_data()
        roll, pitch, yaw = self.bot.get_imu_attitude_data()
        return {
            "accel": {"x": ax, "y": ay, "z": az},
            "gyro": {"x": gx, "y": gy, "z": gz},
            "angles": {"roll": roll, "pitch": pitch, "yaw": yaw},
        }

    def set_servo(self, servo_id: int, angle: int):
        """Set PWM servo angle (0-180 degrees)."""
        self.bot.set_pwm_servo(servo_id, angle)

    def set_serial_servo(self, servo_id: int, position: int, duration: int = 500):
        """Set serial bus servo position (0-1000) over duration ms."""
        self.bot.set_uart_servo(servo_id, position, duration)

    def get_encoder(self) -> tuple:
        """Get motor encoder speeds."""
        return self.bot.get_motor_encoder()

    def close(self):
        self.stop()
        self.bot.reset_flash_value()
        del self.bot


# Singleton instance for remote commands
agent = RobotAgent()
