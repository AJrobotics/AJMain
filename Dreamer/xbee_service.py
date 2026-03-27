#!/usr/bin/env python3
"""
AJ Robotics — Dreamer XBee Responder Service

Lightweight Flask service that runs on Dreamer (Windows) to control
the XBee Heartbeat Responder on COM18.

Christy's main dashboard proxies /api/heartbeat/* calls here.

Usage:
    python Dreamer/xbee_service.py
    # or via batch file:
    Dreamer\\Start XBee Responder.bat

Runs on port 5001 so it doesn't conflict with other services.
"""

import os
import sys

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from flask import Flask, jsonify, request
from shared.heartbeat_responder import get_responder

app = Flask(__name__)

PORT = 5001


@app.route("/api/heartbeat/status")
def heartbeat_status():
    """Get heartbeat responder status."""
    responder = get_responder()
    return jsonify(responder.get_status())


@app.route("/api/heartbeat/toggle", methods=["POST"])
def heartbeat_toggle():
    """Toggle heartbeat responder on/off."""
    responder = get_responder()
    action = request.json.get("action", "toggle") if request.json else "toggle"

    if action == "on" or (action == "toggle" and not responder.is_running):
        ok = responder.start()
        return jsonify({"ok": ok, "running": responder.is_running,
                        "error": responder._error_msg if not ok else ""})
    else:
        responder.stop()
        return jsonify({"ok": True, "running": False})


@app.route("/api/xbee/send", methods=["POST"])
def xbee_send():
    """Send a message via XBee to a specific address or broadcast."""
    responder = get_responder()
    data = request.json or {}
    msg = data.get("message", "")
    if not msg:
        return jsonify({"success": False, "error": "No message"}), 400
    target_addr = data.get("target_addr")
    if target_addr:
        ok = responder.send_data_to(msg, target_addr)
    else:
        ok = responder.send_data(msg)
    return jsonify({"success": ok})


@app.route("/api/heartbeat/health")
def health():
    """Health check endpoint."""
    return jsonify({"ok": True, "service": "dreamer-xbee-responder"})


if __name__ == "__main__":
    print()
    print("=" * 50)
    print("  AJ Robotics — Dreamer XBee Responder")
    print(f"  Running on port {PORT}")
    print("=" * 50)
    print()
    app.run(host="0.0.0.0", port=PORT, debug=False)
