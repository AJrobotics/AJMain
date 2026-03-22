"""
AJ Robotics - Agent Entry Point
Detects which machine we're on, loads the right modules, starts the Flask server.

Usage:
    python -m agent.start_agent          # auto-detect machine
    python -m agent.start_agent --port 5000
"""

import argparse
import json
import logging
import os
import sys

# Ensure project root is on sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from agent.base_agent import create_app, detect_local_machine, log_event

# ---------------------------------------------------------------------------
# Module registry — maps config name to module class
# ---------------------------------------------------------------------------
MODULE_MAP = {
    "heartbeat": "agent_modules.heartbeat_module.HeartbeatModule",
    "trader": "agent_modules.trader_module.TraderModule",
    "xbee_monitor": "agent_modules.xbee_monitor_module.XbeeMonitorModule",
    "watchdog": "agent_modules.watchdog_module.WatchdogModule",
    "robot_registry": "agent_modules.robot_registry_module.RobotRegistryModule",
    "xbee_responder": "agent_modules.xbee_responder_module.XbeeResponderModule",
    "vision_capture": "agent_modules.vision_capture_module.VisionCaptureModule",
}


def _load_module(module_path: str):
    """Dynamically import and instantiate a module class."""
    module_name, class_name = module_path.rsplit(".", 1)
    try:
        mod = __import__(module_name, fromlist=[class_name])
        cls = getattr(mod, class_name)
        return cls()
    except (ImportError, AttributeError) as e:
        log_event(f"Could not load module {module_path}: {e}", level="error")
        return None


def _load_agent_config() -> dict:
    config_path = os.path.join(os.path.dirname(__file__), "agent_config.json")
    with open(config_path, "r") as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(description="AJ Robotics Agent")
    parser.add_argument("--machine", type=str, default=None,
                        help="Machine name (e.g. Gram, Dreamer, R1). Auto-detect if omitted.")
    parser.add_argument("--port", type=int, default=None, help="HTTP port (default from config)")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default 0.0.0.0)")
    parser.add_argument("--debug", action="store_true", help="Flask debug mode")
    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Detect or use specified machine
    machine_name = args.machine or detect_local_machine()
    config = _load_agent_config()
    machine_cfg = config.get("machines", {}).get(machine_name, {})
    defaults = config.get("defaults", {})

    port = args.port or machine_cfg.get("port", defaults.get("port", 5000))
    module_names = machine_cfg.get("modules", [])

    print(f"\n  ============================================")
    print(f"    AJ Robotics Agent - {machine_name}")
    print(f"    http://127.0.0.1:{port}")
    print(f"    Modules: {module_names or ['(common only)']}")
    print(f"  ============================================\n")

    # Load modules
    modules = []
    for mod_name in module_names:
        mod_path = MODULE_MAP.get(mod_name)
        if not mod_path:
            log_event(f"Unknown module: {mod_name}", level="error")
            continue
        instance = _load_module(mod_path)
        if instance:
            modules.append(instance)

    # Create and run app
    app = create_app(machine_name, modules=modules)
    app.run(host=args.host, port=port, debug=args.debug)


if __name__ == "__main__":
    main()
