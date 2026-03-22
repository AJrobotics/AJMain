"""
AJ Robotics - Base Agent
Common Flask server that runs on every machine.
Provides system info, health check, log tail, service control, and network overview.
Machine-specific modules register their own blueprints/routes on top of this.
"""

import json
import logging
import os
import platform
import socket
import sys
import time
from collections import deque
from datetime import datetime
from threading import Lock, Thread

import psutil
from flask import Flask, Blueprint, jsonify, request

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(BASE_DIR, "configs")
HOSTS_PATH = os.path.join(CONFIG_DIR, "hosts.json")
TEMPLATE_DIR = os.path.join(BASE_DIR, "gui", "templates")
STATIC_DIR = os.path.join(BASE_DIR, "gui", "static")

# ---------------------------------------------------------------------------
# Auth token  (simple bearer token — keep machines on LAN/Tailscale)
# ---------------------------------------------------------------------------
AUTH_TOKEN = os.environ.get("AJ_AGENT_TOKEN", "")


def _check_auth():
    """Return True if request is authorised (token disabled when empty)."""
    if not AUTH_TOKEN:
        return True
    header = request.headers.get("Authorization", "")
    return header == f"Bearer {AUTH_TOKEN}"


# ---------------------------------------------------------------------------
# Activity log (in-memory ring buffer)
# ---------------------------------------------------------------------------
_log_entries: deque = deque(maxlen=500)
_log_lock = Lock()


def log_event(message: str, level: str = "info"):
    entry = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "message": message,
        "level": level,
    }
    with _log_lock:
        _log_entries.appendleft(entry)
    if level == "error":
        logger.error(message)
    else:
        logger.info(message)


def get_recent_logs(limit: int = 50) -> list:
    with _log_lock:
        return list(_log_entries)[:limit]


# ---------------------------------------------------------------------------
# Hosts config helpers
# ---------------------------------------------------------------------------
def load_hosts() -> dict:
    with open(HOSTS_PATH, "r") as f:
        return json.load(f)


def find_machine(name: str) -> dict | None:
    hosts = load_hosts()
    for category in ("computers", "raspberry_pis", "robots"):
        if name in hosts.get(category, {}):
            return hosts[category][name]
    return None


# ---------------------------------------------------------------------------
# Machine detection
# ---------------------------------------------------------------------------
_HOSTNAME = socket.gethostname()


def detect_local_machine() -> str:
    """Return the machine name from hosts.json that matches the current hostname."""
    hosts = load_hosts()
    for category in ("computers", "raspberry_pis"):
        for name, info in hosts.get(category, {}).items():
            if info.get("hostname", "").lower() == _HOSTNAME.lower():
                return name
    for category in ("computers", "raspberry_pis"):
        for name, info in hosts.get(category, {}).items():
            if _HOSTNAME.lower().startswith(name.lower()):
                return name
    return "Unknown"


# ---------------------------------------------------------------------------
# Network: ping & background monitor
# ---------------------------------------------------------------------------
_machine_status: dict = {}
_status_lock = Lock()


def ping_host(host: str, port: int = 5000, timeout: float = 2) -> dict:
    """TCP-connect to another agent's HTTP port to check liveness."""
    try:
        start = time.time()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((host, port))
        sock.close()
        latency = round((time.time() - start) * 1000, 1)
        return {"online": True, "latency_ms": latency}
    except (socket.timeout, socket.error, OSError):
        return {"online": False, "latency_ms": None}


def _is_local(info: dict) -> bool:
    hostname = info.get("hostname", "")
    return hostname.lower() == _HOSTNAME.lower()


def check_all_machines() -> dict:
    hosts = load_hosts()
    results = {}
    for category in ("computers", "raspberry_pis"):
        for name, info in hosts.get(category, {}).items():
            host = info.get("host", "")
            port = info.get("agent_port", 5000)
            if _is_local(info):
                status = {"online": True, "latency_ms": 0}
            else:
                status = ping_host(host, port)
            results[name] = {
                "name": name,
                "host": host,
                "port": port,
                "role": info.get("role", ""),
                "type": info.get("type", ""),
                "online": status["online"],
                "latency_ms": status["latency_ms"],
            }
    for name, info in hosts.get("robots", {}).items():
        results[name] = {
            "name": name,
            "host": info.get("host", "TBD"),
            "role": "Robot",
            "type": "robot",
            "online": False,
            "latency_ms": None,
            "status": info.get("status", "unknown"),
        }
    with _status_lock:
        _machine_status.update(results)
    return results


def get_machine_status() -> dict:
    with _status_lock:
        return dict(_machine_status)


def start_background_monitor(interval: int = 15):
    def _loop():
        while True:
            try:
                results = check_all_machines()
                online = sum(1 for r in results.values() if r.get("online"))
                total = len(results)
                log_event(f"Ping sweep: {online}/{total} machines online")
            except Exception as e:
                log_event(f"Monitor error: {e}", level="error")
            time.sleep(interval)

    Thread(target=_loop, daemon=True, name="bg-monitor").start()
    log_event("Background monitor started")


# ---------------------------------------------------------------------------
# System info (psutil)
# ---------------------------------------------------------------------------
def get_system_info() -> dict:
    cpu = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory()
    # Use C: on Windows, / on Linux/Mac
    disk_path = "C:\\" if platform.system() == "Windows" else "/"
    disk = psutil.disk_usage(disk_path)
    net = psutil.net_io_counters()
    boot = datetime.fromtimestamp(psutil.boot_time())
    uptime_sec = int(time.time() - psutil.boot_time())

    return {
        "hostname": _HOSTNAME,
        "platform": platform.system(),
        "platform_version": platform.version(),
        "cpu_percent": cpu,
        "cpu_count": psutil.cpu_count(),
        "memory": {
            "percent": mem.percent,
            "used_gb": round(mem.used / (1024 ** 3), 1),
            "total_gb": round(mem.total / (1024 ** 3), 1),
        },
        "disk": {
            "percent": disk.percent,
            "used_gb": round(disk.used / (1024 ** 3), 1),
            "total_gb": round(disk.total / (1024 ** 3), 1),
        },
        "network": {
            "bytes_sent": net.bytes_sent,
            "bytes_recv": net.bytes_recv,
        },
        "boot_time": boot.strftime("%Y-%m-%d %H:%M:%S"),
        "uptime_seconds": uptime_sec,
    }


# ---------------------------------------------------------------------------
# Flask app factory
# ---------------------------------------------------------------------------
def create_app(machine_name: str, modules: list | None = None) -> Flask:
    """
    Create the Flask application with common API routes.
    machine_name: detected machine name (e.g. "Dreamer")
    modules:      list of agent module instances to register
    """
    app = Flask(
        __name__,
        template_folder=TEMPLATE_DIR,
        static_folder=STATIC_DIR,
    )
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
    app.config["MACHINE_NAME"] = machine_name

    # ------ Common API blueprint ------
    api = Blueprint("common_api", __name__)

    @api.before_request
    def auth_check():
        if not _check_auth():
            return jsonify({"error": "Unauthorized"}), 401

    @api.route("/api/health")
    def health():
        return jsonify({
            "status": "ok",
            "machine": machine_name,
            "timestamp": datetime.now().isoformat(),
        })

    @api.route("/api/status")
    def status():
        mod_names = [m.name for m in (modules or []) if hasattr(m, "name")]
        return jsonify({
            "status": "ok",
            "machine": machine_name,
            "modules": mod_names,
            "timestamp": datetime.now().isoformat(),
        })

    @api.route("/api/system/info")
    def system_info():
        return jsonify(get_system_info())

    @api.route("/api/logs/tail")
    def logs_tail():
        limit = request.args.get("limit", 50, type=int)
        return jsonify(get_recent_logs(limit))

    @api.route("/api/network/machines")
    def network_machines():
        return jsonify(get_machine_status())

    @api.route("/api/network/refresh", methods=["POST"])
    def network_refresh():
        results = check_all_machines()
        return jsonify(results)

    @api.route("/api/machine/<name>/info")
    def machine_proxy_info(name):
        """Proxy: fetch system info from a remote machine's agent."""
        from shared.agent_client import AgentClient
        info = find_machine(name)
        if not info:
            return jsonify({"error": f"Machine '{name}' not found"}), 404
        if _is_local(info):
            return jsonify(get_system_info())
        host = info.get("host", "")
        port = info.get("agent_port", 5000)
        client = AgentClient(host, port)
        data, err = client.get("/api/system/info", timeout=5)
        if err:
            return jsonify({"error": err, "online": False}), 502
        return jsonify(data)

    app.register_blueprint(api)

    # ------ Deploy API ------
    deploy_bp = Blueprint("deploy_api", __name__)
    _deploy_processes = {}
    _deploy_lock2 = Lock()

    @deploy_bp.route("/api/deploy/<target>", methods=["POST"])
    def deploy_machine(target):
        """Start async deploy to a remote machine."""
        import subprocess as sp
        info = find_machine(target)
        if not info:
            return jsonify({"error": f"Machine '{target}' not found"}), 404

        with _deploy_lock2:
            existing = _deploy_processes.get(target)
            if existing and existing.get("status") == "running":
                return jsonify({"error": "Deploy already in progress"}), 409

        deploy_script = os.path.join(BASE_DIR, "deploy", "deploy_agent.py")
        if not os.path.isfile(deploy_script):
            return jsonify({"error": "deploy_agent.py not found"}), 500

        def _run_deploy():
            try:
                # Pass full environment so ssh/scp can be found
                env = os.environ.copy()
                proc = sp.Popen(
                    [sys.executable, deploy_script, target],
                    stdout=sp.PIPE, stderr=sp.STDOUT,
                    text=True, cwd=BASE_DIR, env=env,
                )
                with _deploy_lock2:
                    _deploy_processes[target] = {
                        "status": "running",
                        "output": "",
                        "started": datetime.now().isoformat(),
                    }
                output_lines = []
                for line in proc.stdout:
                    output_lines.append(line.rstrip())
                proc.wait()
                with _deploy_lock2:
                    _deploy_processes[target]["output"] = "\n".join(output_lines)
                    _deploy_processes[target]["status"] = (
                        "success" if proc.returncode == 0 else "failed"
                    )
                    _deploy_processes[target]["returncode"] = proc.returncode
                log_event(
                    f"Deploy to {target}: {'success' if proc.returncode == 0 else 'failed'}",
                    level="info" if proc.returncode == 0 else "error",
                )
            except Exception as e:
                with _deploy_lock2:
                    _deploy_processes[target] = {
                        "status": "failed", "output": str(e),
                    }
                log_event(f"Deploy to {target} error: {e}", level="error")

        Thread(target=_run_deploy, daemon=True, name=f"deploy-{target}").start()
        log_event(f"Deploy to {target} started")
        return jsonify({"message": f"Deploy to {target} started"})

    @deploy_bp.route("/api/deploy/<target>/status")
    def deploy_status(target):
        with _deploy_lock2:
            info = _deploy_processes.get(target)
        if not info:
            return jsonify({"status": "none"})
        return jsonify(info)

    app.register_blueprint(deploy_bp)

    # ------ Agent Restart API ------
    restart_bp = Blueprint("restart_api", __name__)
    _restart_results = {}
    _restart_lock = Lock()

    @restart_bp.route("/api/agent/restart/<target>", methods=["POST"])
    def restart_agent(target):
        """Restart a remote machine's agent via SSH (paramiko)."""
        info = find_machine(target)
        if not info:
            return jsonify({"error": f"Machine '{target}' not found"}), 404

        host = info.get("host", "")
        username = info.get("username", "")
        os_type = info.get("os", "").lower()
        is_rpi = info.get("type") == "raspberry_pi"

        if _is_local(info):
            return jsonify({"error": "Cannot restart self via this endpoint"}), 400

        def _do_restart():
            try:
                import paramiko
                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                ssh.connect(host, username=username, timeout=10)

                import time as _t
                if "windows" in os_type:
                    # Windows: 3-step — kill, create schtask, run schtask
                    ajmain = info.get("ajmain_path", "G:\\My Drive\\AJ_Robotics\\AJMain")
                    ps1 = ajmain + "\\scripts\\start_agent_bg.ps1"
                    ssh.exec_command("taskkill /F /IM python.exe")
                    _t.sleep(2)
                    create_cmd = f'schtasks /Create /TN "AJAgent_{target}" /TR "powershell -ExecutionPolicy Bypass -File \\"{ps1}\\" -Machine {target}" /SC ONCE /ST 00:00 /F'
                    ssh.exec_command(create_cmd)
                    _t.sleep(1)
                    ssh.exec_command(f'schtasks /Run /TN "AJAgent_{target}"')
                    _t.sleep(3)
                elif is_rpi:
                    ssh.exec_command("pkill -f 'agent.start_agent' || true")
                    _t.sleep(2)
                    ssh.exec_command("cd /home/{}/AJMain && nohup ./venv/bin/python -m agent.start_agent --machine {} > /tmp/agent.log 2>&1 &".format(username, target))
                    _t.sleep(3)
                else:
                    ssh.exec_command("pkill -f 'agent.start_agent' || true")
                    _t.sleep(2)
                    ssh.exec_command("cd /home/{}/AJMain && nohup python3 -m agent.start_agent --machine {} > /tmp/agent.log 2>&1 &".format(username, target))
                    _t.sleep(3)
                ssh.close()

                # Verify agent is up
                import requests as _req
                port = info.get("agent_port", 5000)
                try:
                    r = _req.get(f"http://{host}:{port}/api/health", timeout=5)
                    ok = r.status_code == 200
                except Exception:
                    ok = False

                with _restart_lock:
                    _restart_results[target] = {
                        "status": "success" if ok else "started",
                        "time": datetime.now().isoformat(),
                        "verified": ok,
                    }
                log_event(f"Agent restart {target}: {'verified' if ok else 'started (unverified)'}")

            except Exception as e:
                with _restart_lock:
                    _restart_results[target] = {
                        "status": "failed",
                        "error": str(e),
                        "time": datetime.now().isoformat(),
                    }
                log_event(f"Agent restart {target} failed: {e}", level="error")

        Thread(target=_do_restart, daemon=True, name=f"restart-{target}").start()
        log_event(f"Agent restart {target} initiated")
        return jsonify({"message": f"Restarting {target} agent..."})

    @restart_bp.route("/api/agent/restart/<target>/status")
    def restart_status(target):
        with _restart_lock:
            result = _restart_results.get(target)
        if not result:
            return jsonify({"status": "none"})
        return jsonify(result)

    app.register_blueprint(restart_bp)

    # ------ Page routes ------
    from flask import render_template

    @app.route("/")
    def index():
        return render_template("agent_dashboard.html", local_machine=machine_name)

    @app.route("/gamepad")
    def gamepad_page():
        return render_template("gamepad_test.html", local_machine=machine_name)

    @app.route("/machine/<name>")
    def machine_detail(name):
        from flask import redirect
        info = find_machine(name)
        if not info:
            return f"Machine '{name}' not found", 404
        # If the machine has its own agent, redirect to it directly
        host = info.get("host", "")
        port = info.get("agent_port", 5000)
        if host and not _is_local(info):
            return redirect(f"http://{host}:{port}/")
        return render_template("agent_machine_detail.html",
                               machine_name=name, machine_info=info,
                               local_machine=machine_name)

    # ------ Agent-to-agent proxy ------
    proxy_bp = Blueprint("agent_proxy", __name__)

    @proxy_bp.route("/api/proxy/<target>/xbee/<path:subpath>", methods=["GET", "POST"])
    def proxy_xbee(target, subpath):
        """Proxy /api/xbee/* requests to another machine's agent."""
        import requests as _req
        info = find_machine(target)
        if not info:
            return jsonify({"error": f"Machine '{target}' not found"}), 404
        host = info.get("host", "")
        port = info.get("agent_port", 5000)
        url = f"http://{host}:{port}/api/xbee/{subpath}"
        try:
            if request.method == "POST":
                resp = _req.post(url, json=request.json, timeout=3)
            else:
                resp = _req.get(url, timeout=3)
            return jsonify(resp.json()), resp.status_code
        except Exception as e:
            return jsonify({"error": f"{target} unreachable: {e}"}), 503

    @proxy_bp.route("/api/proxy/<target>/heartbeat/<path:subpath>", methods=["GET", "POST"])
    def proxy_heartbeat(target, subpath):
        """Proxy /api/heartbeat/* requests to another machine's agent."""
        import requests as _req
        info = find_machine(target)
        if not info:
            return jsonify({"error": f"Machine '{target}' not found"}), 404
        host = info.get("host", "")
        port = info.get("agent_port", 5000)
        url = f"http://{host}:{port}/api/heartbeat/{subpath}"
        try:
            if request.method == "POST":
                resp = _req.post(url, json=request.json, timeout=3)
            else:
                resp = _req.get(url, timeout=3)
            return jsonify(resp.json()), resp.status_code
        except Exception as e:
            return jsonify({"error": f"{target} unreachable: {e}"}), 503

    @proxy_bp.route("/api/proxy/<target>/gamepad/<path:subpath>", methods=["GET", "POST"])
    def proxy_gamepad(target, subpath):
        """Proxy /api/gamepad/* requests to another machine's agent."""
        import requests as _req
        info = find_machine(target)
        if not info:
            return jsonify({"error": f"Machine '{target}' not found"}), 404
        host = info.get("host", "")
        port = info.get("agent_port", 5000)
        url = f"http://{host}:{port}/api/gamepad/{subpath}"
        try:
            if request.method == "POST":
                resp = _req.post(url, json=request.json, timeout=3)
            else:
                resp = _req.get(url, timeout=3)
            return jsonify(resp.json()), resp.status_code
        except Exception as e:
            return jsonify({"error": f"{target} unreachable: {e}"}), 503

    app.register_blueprint(proxy_bp)

    # ------ Register machine-specific modules ------
    if modules:
        for mod in modules:
            mod.register(app)
            log_event(f"Module loaded: {mod.name}")

    # ------ Start background monitor ------
    start_background_monitor()

    log_event(f"Agent started on {machine_name} ({_HOSTNAME})")
    return app
