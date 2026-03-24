"""
Standalone Flask entry point for the xArm controller.

Usage:
    python -m xarm.start              # Auto-detect (hardware if available)
    python -m xarm.start --sim        # Simulation only (no hardware)
    python -m xarm.start --port 5001  # Custom port
"""

import argparse
import logging
import os
import sys

from flask import Flask


def main():
    parser = argparse.ArgumentParser(description="xArm Controller Server")
    parser.add_argument("--sim", action="store_true",
                        help="Simulation only, no hardware")
    parser.add_argument("--port", type=int, default=5001,
                        help="HTTP port (default: 5001)")
    parser.add_argument("--host", default="0.0.0.0",
                        help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--debug", action="store_true",
                        help="Enable Flask debug mode")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    # Config dir is the xarm/ package directory (where this file lives)
    config_dir = os.path.dirname(os.path.abspath(__file__))

    hardware = not args.sim

    from xarm.controller import XArmController

    app = Flask(__name__)
    controller = XArmController(config_dir=config_dir, hardware=hardware)
    controller.register_routes(app)
    controller.start()

    @app.route("/")
    def index():
        return (f'<h2>xArm Controller</h2>'
                f'<p>Hardware: {"enabled" if hardware else "simulation only"}</p>'
                f'<p><a href="/simulation">Simulation UI</a></p>'
                f'<p><a href="/api/xarm/status">API Status</a></p>')

    print(f"xArm server starting on {args.host}:{args.port} "
          f"(hardware={'ON' if hardware else 'OFF'})")

    try:
        app.run(host=args.host, port=args.port, debug=args.debug,
                use_reloader=False)
    finally:
        controller.stop()


if __name__ == "__main__":
    main()
