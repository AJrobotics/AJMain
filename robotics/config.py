"""Robotics configuration — XBee, robots, packet types."""

# XBee serial settings
XBEE_PORT = "/dev/ttyUSB0"
XBEE_BAUD = 115200

# Heartbeat settings
HEARTBEAT_INTERVAL = 10      # seconds between "All Good?" broadcasts
HEARTBEAT_MSG = "All Good?"
HEARTBEAT_REPLY = "Roger!"    # legacy — devices now reply with their own name

# Device-specific heartbeat replies (device replies with its own call sign)
DEVICE_REPLIES = {
    "R1": "R1!",
    "R3": "R3!",       # Gram
    "R4": "R4!",       # Dreamer
}

# Valid heartbeat replies (any of these count as a heartbeat response)
VALID_REPLIES = {"Roger!", "R1!", "R3!", "R4!"}

# Packet types
PKT_STATUS = 0x01    # Robot -> PC: GPS + battery status
PKT_COMMAND = 0x02   # PC -> Robot: movement command
PKT_YOLO = 0x03      # Robot -> PC: YOLO detection

# Command flags
CMD_STOP = 0x01
CMD_ESTOP = 0xFF

# Robot roster (id -> info)
ROBOTS = {
    0: {"name": "ROSMASTER X3", "mac": None},
    1: {"name": "Caterpillar", "mac": None},
    2: {"name": "Go2", "mac": None},
}

# Known XBee MAC addresses
COORDINATOR_MAC = "0013A20041BB8CF4"  # Christy
KNOWN_XBEE_MACS = {
    "0013A20041BB8CF4": "Christy",
    "0013A20041BB8D5E": "R1",
    "0013A20041BB8E1F": "Gram (R3)",
    "0013A20041741E51": "Dreamer",
}

# DigiMesh network settings
NETWORK_ID = 0x1116
PREAMBLE_ID = 2

# Logging
LOG_DIR = "/home/ajrobotics/logs/xbee"
LOG_MAX_SIZE_MB = 50
LOG_BACKUP_COUNT = 10
