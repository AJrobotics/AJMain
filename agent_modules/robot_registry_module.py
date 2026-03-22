"""
Robot Registry Module — Christy only.
Manages robot registration (robots.json), provides CRUD API for the robot fleet.
"""

import json
import logging
import os
from threading import Lock

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

ROBOTS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "configs", "robots.json"
)


class RobotRegistryModule:
    name = "robot_registry"

    def __init__(self):
        self._lock = Lock()

    def _load(self) -> list:
        try:
            with open(ROBOTS_PATH, "r") as f:
                data = json.load(f)
                return data.get("robots", [])
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _save(self, robots: list):
        with open(ROBOTS_PATH, "w") as f:
            json.dump({"robots": robots}, f, indent=4)

    def register(self, app):
        bp = Blueprint("robot_registry", __name__)

        @bp.route("/api/robots")
        def list_robots():
            with self._lock:
                return jsonify({"robots": self._load()})

        @bp.route("/api/robots/<int:robot_id>")
        def get_robot(robot_id):
            with self._lock:
                robots = self._load()
                for r in robots:
                    if r.get("id") == robot_id:
                        return jsonify(r)
                return jsonify({"error": "Robot not found"}), 404

        @bp.route("/api/robots", methods=["POST"])
        def add_robot():
            data = request.json or {}
            required = ("name", "mac", "type")
            for field in required:
                if field not in data:
                    return jsonify({"error": f"Missing field: {field}"}), 400

            with self._lock:
                robots = self._load()
                new_id = max((r.get("id", 0) for r in robots), default=-1) + 1
                robot = {
                    "id": new_id,
                    "name": data["name"],
                    "mac": data["mac"],
                    "type": data["type"],
                    "status": data.get("status", "registered"),
                    "description": data.get("description", ""),
                }
                robots.append(robot)
                self._save(robots)
            logger.info("Robot added: %s (id=%d)", robot["name"], new_id)
            return jsonify(robot), 201

        @bp.route("/api/robots/<int:robot_id>", methods=["PUT"])
        def update_robot(robot_id):
            data = request.json or {}
            with self._lock:
                robots = self._load()
                for r in robots:
                    if r.get("id") == robot_id:
                        for k in ("name", "mac", "type", "status", "description"):
                            if k in data:
                                r[k] = data[k]
                        self._save(robots)
                        return jsonify(r)
                return jsonify({"error": "Robot not found"}), 404

        @bp.route("/api/robots/<int:robot_id>", methods=["DELETE"])
        def delete_robot(robot_id):
            with self._lock:
                robots = self._load()
                before = len(robots)
                robots = [r for r in robots if r.get("id") != robot_id]
                if len(robots) == before:
                    return jsonify({"error": "Robot not found"}), 404
                self._save(robots)
            return jsonify({"message": f"Robot {robot_id} deleted"})

        app.register_blueprint(bp)
