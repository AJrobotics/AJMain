"""RosMaster X3 configuration."""

# Network
ROSMASTER_IP = "192.168.1.99"
ROSMASTER_USER = "jetson"
ROSMASTER_PASSWORD = "yahboom"  # default, used only if SSH key not set up
ROSMASTER_SSH_PORT = 22

# Robot hardware
ROBOT_TYPE = "X3"  # Mecanum wheel robot
WHEEL_TYPE = "mecanum"

# Remote project path on Jetson
REMOTE_PROJECT_DIR = "/home/jetson/RosMaster"

# Stable device symlinks (created by /etc/udev/rules.d/99-rosmaster.rules)
# /dev/rplidar   → RPLidar S2 (CP210x, USB path 0:2.3)
# /dev/rosmaster → STM32 driver board (CH340, USB path 0:2.1.1)
# /dev/ftserial  → FT231X serial (USB path 0:2.1.3)
# /dev/gps       → U-Blox GPS (USB path 0:2.1.4)

# Rosmaster_Lib motion parameters
# v_x: forward/backward speed (m/s), positive = forward
# v_y: lateral speed (m/s), positive = left
# v_z: rotation speed (rad/s), positive = left rotation
MAX_SPEED_X = 0.45   # max forward/backward m/s
MAX_SPEED_Y = 0.45   # max lateral m/s
MAX_SPEED_Z = 3.0    # max rotation rad/s
