"""
Heartbeat Module — Dreamer & Gram (Windows PCs with XBee).
Wraps the HeartbeatResponder and exposes control via REST API.
Machine-specific XBee port and reply are read from agent_config.json.
"""

from flask import Blueprint, jsonify, request


class HeartbeatModule:
    name = "heartbeat"

    def __init__(self):
        self._responder = None
        self._xbee_port = None
        self._xbee_reply = None

    def _get_responder(self):
        if self._responder is None:
            from shared.heartbeat_responder import HeartbeatResponder
            kwargs = {}
            if self._xbee_port:
                kwargs["port"] = self._xbee_port
            if self._xbee_reply:
                kwargs["reply"] = self._xbee_reply
            self._responder = HeartbeatResponder(**kwargs)
        return self._responder

    def register(self, app):
        # Read machine-specific XBee config from app
        machine_name = app.config.get("MACHINE_NAME", "")
        import json, os, logging
        logger = logging.getLogger(__name__)
        config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "agent", "agent_config.json")
        auto_start = False
        try:
            with open(config_path) as f:
                cfg = json.load(f)
            machine_cfg = cfg.get("machines", {}).get(machine_name, {})
            self._xbee_port = machine_cfg.get("xbee_port")
            self._xbee_reply = machine_cfg.get("xbee_reply")
            auto_start = machine_cfg.get("xbee_auto_start", False)
        except Exception:
            pass

        if auto_start:
            try:
                ok = self._get_responder().start()
                logger.info("XBee auto-start: %s (port=%s)", "OK" if ok else "FAILED", self._xbee_port)
            except Exception as e:
                logger.error("XBee auto-start error: %s", e)

        bp = Blueprint("heartbeat", __name__)

        @bp.route("/api/heartbeat/status")
        def hb_status():
            return jsonify(self._get_responder().get_status())

        @bp.route("/api/heartbeat/start", methods=["POST"])
        def hb_start():
            ok = self._get_responder().start()
            return jsonify({"success": ok, "status": self._get_responder().get_status()})

        @bp.route("/api/heartbeat/stop", methods=["POST"])
        def hb_stop():
            self._get_responder().stop()
            return jsonify({"success": True, "status": self._get_responder().get_status()})

        @bp.route("/api/heartbeat/toggle", methods=["POST"])
        def hb_toggle():
            r = self._get_responder()
            if r.is_running:
                r.stop()
            else:
                r.start()
            return jsonify({"status": r.get_status()})

        @bp.route("/api/xbee/send", methods=["POST"])
        def xbee_send():
            data = request.json or {}
            msg = data.get("message", "")
            if not msg:
                return jsonify({"error": "No message"}), 400
            target_addr = data.get("target_addr", None)
            if target_addr:
                ok = self._get_responder().send_data_to(msg, target_addr)
            else:
                ok = self._get_responder().send_data(msg)
            return jsonify({"success": ok})

        @bp.route("/api/gamepad/buffer", methods=["POST"])
        def gamepad_buffer():
            data = request.json or {}
            msg = data.get("data", "")
            self._get_responder().buffer_gamepad(msg)
            return jsonify({"ok": True})

        @bp.route("/api/gamepad/start", methods=["POST"])
        def gamepad_start():
            data = request.json or {}
            interval = data.get("interval", 0.5)
            target_addr = data.get("target_addr", None)
            self._get_responder().start_gamepad_sender(interval, target_addr)
            return jsonify({"status": self._get_responder().gamepad_status})

        @bp.route("/api/gamepad/stop", methods=["POST"])
        def gamepad_stop():
            self._get_responder().stop_gamepad_sender()
            return jsonify({"status": self._get_responder().gamepad_status})

        app.register_blueprint(bp)
