"""XBee Monitor — runs on Christy (192.168.1.94)

Main responsibilities:
  1. Broadcast "All Good?" every 10 seconds
  2. Listen for responses from all robots
  3. Log ALL XBee traffic to rotating log files
  4. Print live status to console

Usage:
    python -m robotics.xbee_monitor [--interval 10] [--port /dev/ttyUSB0]

Logs are stored in: ~/logs/xbee/
  - xbee_events.log    : all events (heartbeat, status, yolo, text)
  - xbee_heartbeat.log : heartbeat send/receive only
  - xbee_errors.log    : errors only
"""

import argparse
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from robotics import config
from robotics.xbee_bridge import XBeeBridge


# -- Logging Setup --

def setup_logging(log_dir: str) -> tuple:
    """Set up rotating log files for different event types."""
    os.makedirs(log_dir, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-5s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    json_formatter = logging.Formatter("%(message)s")

    # Main event logger
    event_logger = logging.getLogger("xbee.events")
    event_logger.setLevel(logging.INFO)
    event_handler = RotatingFileHandler(
        os.path.join(log_dir, "xbee_events.log"),
        maxBytes=config.LOG_MAX_SIZE_MB * 1024 * 1024,
        backupCount=config.LOG_BACKUP_COUNT,
    )
    event_handler.setFormatter(formatter)
    event_logger.addHandler(event_handler)

    # JSON event log (machine-readable)
    json_logger = logging.getLogger("xbee.json")
    json_logger.setLevel(logging.INFO)
    json_handler = RotatingFileHandler(
        os.path.join(log_dir, "xbee_events.jsonl"),
        maxBytes=config.LOG_MAX_SIZE_MB * 1024 * 1024,
        backupCount=config.LOG_BACKUP_COUNT,
    )
    json_handler.setFormatter(json_formatter)
    json_logger.addHandler(json_handler)

    # Heartbeat-only logger
    hb_logger = logging.getLogger("xbee.heartbeat")
    hb_logger.setLevel(logging.INFO)
    hb_handler = RotatingFileHandler(
        os.path.join(log_dir, "xbee_heartbeat.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
    )
    hb_handler.setFormatter(formatter)
    hb_logger.addHandler(hb_handler)

    # Error logger
    err_logger = logging.getLogger("xbee.errors")
    err_logger.setLevel(logging.ERROR)
    err_handler = RotatingFileHandler(
        os.path.join(log_dir, "xbee_errors.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
    )
    err_handler.setFormatter(formatter)
    err_logger.addHandler(err_handler)

    # Root logger for xbee_bridge module
    root = logging.getLogger("robotics.xbee_bridge")
    root.setLevel(logging.INFO)
    root.addHandler(event_handler)

    return event_logger, json_logger, hb_logger, err_logger


# -- Console Display --

class ConsoleDisplay:
    """Live console status display."""

    def __init__(self):
        self.start_time = time.time()

    def print_header(self, port: str, interval: int):
        print("=" * 60)
        print("  AJ Robotics — XBee Monitor")
        print(f"  Host: Christy (192.168.1.94)")
        print(f"  XBee: {port} @ {config.XBEE_BAUD} baud")
        print(f"  Heartbeat: every {interval}s")
        print(f"  Log dir: {config.LOG_DIR}")
        print("=" * 60)
        print(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"  Press Ctrl+C to stop")
        print("=" * 60)
        print()

    def print_event(self, msg: dict):
        """Print a single event to console."""
        ts = datetime.now().strftime("%H:%M:%S")
        msg_type = msg.get("type", "unknown")

        if msg_type == "heartbeat_sent":
            sent = msg.get("sent_count", 0)
            recv = msg.get("recv_count", 0)
            ratio = f"{recv}/{sent}" if sent > 0 else "0/0"
            print(f"  [{ts}] >> SENT heartbeat #{sent}  (replies: {ratio})")

        elif msg_type == "heartbeat_reply":
            mac = msg.get("mac", "?")[-8:]  # last 8 chars
            recv = msg.get("recv_count", 0)
            rssi = msg.get("rssi", 0)
            reply_text = msg.get("reply_text", "?")
            print(f"  [{ts}] << REPLY #{recv} '{reply_text}' from ...{mac}  RSSI: {rssi} dBm")

        elif msg_type == "status":
            name = msg.get("robot_name", "?")
            lat = msg.get("lat", 0)
            lon = msg.get("lon", 0)
            batt = msg.get("battery_v", 0)
            rssi = msg.get("rssi", 0)
            print(f"  [{ts}] << STATUS {name}: "
                  f"GPS({lat:.4f}, {lon:.4f}) Batt:{batt:.1f}V RSSI:{rssi}dBm")

        elif msg_type == "yolo":
            name = msg.get("robot_name", "?")
            cls = msg.get("class", "?")
            conf = msg.get("confidence", 0)
            print(f"  [{ts}] << YOLO {name}: {cls} ({conf:.1%})")

        elif msg_type == "text":
            mac = msg.get("mac", "?")[-8:]
            text = msg.get("text", "")
            print(f"  [{ts}] << TEXT from ...{mac}: {text}")


# -- Main --

def main():
    parser = argparse.ArgumentParser(description="XBee Monitor for Christy")
    parser.add_argument("--interval", type=int, default=config.HEARTBEAT_INTERVAL,
                        help=f"Heartbeat interval in seconds (default: {config.HEARTBEAT_INTERVAL})")
    parser.add_argument("--port", type=str, default=config.XBEE_PORT,
                        help=f"XBee serial port (default: {config.XBEE_PORT})")
    parser.add_argument("--log-dir", type=str, default=config.LOG_DIR,
                        help=f"Log directory (default: {config.LOG_DIR})")
    args = parser.parse_args()

    # Setup logging
    event_log, json_log, hb_log, err_log = setup_logging(args.log_dir)

    # Console display
    display = ConsoleDisplay()
    display.print_header(args.port, args.interval)

    # Create bridge
    bridge = XBeeBridge(
        port=args.port,
        baud=config.XBEE_BAUD,
        heartbeat_interval=args.interval,
    )

    # Register callbacks
    def on_message(msg: dict):
        """Log every message to files and console."""
        msg_type = msg.get("type", "unknown")
        ts = datetime.now().isoformat()
        msg["timestamp"] = ts

        # JSON log (every message)
        json_log.info(json.dumps(msg, default=str))

        # Event log (human-readable)
        if msg_type == "heartbeat_sent":
            event_log.info("HEARTBEAT SENT #%d (recv: %d)",
                           msg.get("sent_count"), msg.get("recv_count"))
            hb_log.info("SENT #%d (recv: %d)",
                        msg.get("sent_count"), msg.get("recv_count"))
        elif msg_type == "heartbeat_reply":
            event_log.info("HEARTBEAT REPLY #%d from %s RSSI:%d",
                           msg.get("recv_count"), msg.get("mac"), msg.get("rssi", 0))
            hb_log.info("REPLY #%d from %s RSSI:%d",
                        msg.get("recv_count"), msg.get("mac"), msg.get("rssi", 0))
        elif msg_type == "status":
            event_log.info("STATUS %s: GPS(%.4f,%.4f) Batt:%.1fV RSSI:%d",
                           msg.get("robot_name"), msg.get("lat", 0), msg.get("lon", 0),
                           msg.get("battery_v", 0), msg.get("rssi", 0))
        elif msg_type == "yolo":
            event_log.info("YOLO %s: %s (%.2f) RSSI:%d",
                           msg.get("robot_name"), msg.get("class"),
                           msg.get("confidence", 0), msg.get("rssi", 0))
        elif msg_type == "text":
            event_log.info("TEXT from %s: %s", msg.get("mac"), msg.get("text"))

        # Console
        display.print_event(msg)

    def on_update(robot):
        """Log robot state changes."""
        event_log.info("ROBOT UPDATE: %s -> %s", robot.name, robot.status_text)

    bridge.set_on_message(on_message)
    bridge.set_on_update(on_update)

    # Graceful shutdown
    def shutdown(sig, frame):
        print("\n\n  Shutting down...")
        bridge.close()
        status = bridge.get_status()
        event_log.info("SHUTDOWN: sent=%d recv=%d",
                       status["heartbeat_sent"], status["heartbeat_recv"])
        print(f"  Total: {status['heartbeat_sent']} sent, "
              f"{status['heartbeat_recv']} received")
        print(f"  Logs saved to: {args.log_dir}")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Start
    try:
        event_log.info("STARTUP: port=%s interval=%ds", args.port, args.interval)
        bridge.open()
        print("  [OK] XBee connected. Monitoring...\n")

        # Keep main thread alive
        while True:
            time.sleep(1)

    except Exception as e:
        err_log.error("FATAL: %s", e)
        event_log.error("FATAL: %s", e)
        print(f"\n  [ERROR] {e}")
        bridge.close()
        sys.exit(1)


if __name__ == "__main__":
    main()
