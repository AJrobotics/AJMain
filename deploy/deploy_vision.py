"""Deploy Vision Server to Christy and Vision Client to robots via SSH/SCP.

Usage:
    python -m deploy.deploy_vision                  # sync + install deps on Christy
    python -m deploy.deploy_vision --run             # sync + start vision server
    python -m deploy.deploy_vision --stop            # stop vision server
    python -m deploy.deploy_vision --status          # check server status
    python -m deploy.deploy_vision --deploy-client R1  # deploy client to R1
"""

import argparse
import os
import subprocess
import sys
from datetime import datetime

# Christy (vision server)
CHRISTY_USER = "ajrobotics"
CHRISTY_HOST = "192.168.1.94"
CHRISTY_DIR = "/home/ajrobotics/AJMain"
CHRISTY_VENV = "/home/ajrobotics/robot_fleet_venv"
CHRISTY_IMAGE_DIR = "/home/ajrobotics/vision/images"

# Robot connections
ROBOTS = {
    "R1": {"host": "192.168.1.82", "user": "dream", "dir": "/home/dream/GoodSon", "venv": "/home/dream/GoodSon/venv"},
}

LOCAL_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SSH_OPTS = ["-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5"]

# Prevent CMD window popups on Windows
_SUBPROCESS_KWARGS = {}
if sys.platform == "win32":
    _SUBPROCESS_KWARGS["creationflags"] = subprocess.CREATE_NO_WINDOW


def ssh_cmd(user, host, command, quiet=False):
    full_cmd = ["ssh"] + SSH_OPTS + [f"{user}@{host}", command]
    if quiet:
        result = subprocess.run(full_cmd, capture_output=True, text=True, **_SUBPROCESS_KWARGS)
        return result.returncode, result.stdout.strip()
    else:
        return subprocess.call(full_cmd, **_SUBPROCESS_KWARGS), ""


def scp_file(local_path, user, host, remote_path):
    full_cmd = ["scp"] + SSH_OPTS + [local_path, f"{user}@{host}:{remote_path}"]
    return subprocess.call(full_cmd, **_SUBPROCESS_KWARGS)


def check_connection(user, host, name):
    print(f"  Checking connection to {name} ({host})...")
    rc, _ = ssh_cmd(user, host, "echo ok", quiet=True)
    if rc == 0:
        print(f"  [OK] Connected to {name}")
        return True
    else:
        print(f"  [FAIL] Cannot reach {name}")
        return False


# --- Christy (Vision Server) ---

def sync_server_files():
    """Sync vision server files to Christy."""
    print("  Syncing vision files to Christy...")
    files = [
        ("robotics/vision_server.py", f"{CHRISTY_DIR}/robotics/vision_server.py"),
        ("robotics/vision_config.py", f"{CHRISTY_DIR}/robotics/vision_config.py"),
        ("robotics/__init__.py", f"{CHRISTY_DIR}/robotics/__init__.py"),
    ]
    ssh_cmd(CHRISTY_USER, CHRISTY_HOST,
            f"mkdir -p {CHRISTY_DIR}/robotics {CHRISTY_IMAGE_DIR}", quiet=True)

    for local_rel, remote_path in files:
        local_path = os.path.join(LOCAL_BASE, local_rel)
        if os.path.exists(local_path):
            print(f"    {local_rel}")
            scp_file(local_path, CHRISTY_USER, CHRISTY_HOST, remote_path)
    print(f"  [OK] Files synced")


def install_server_deps():
    """Install Python dependencies on Christy for vision server."""
    print("  Installing vision server dependencies...")
    deps = "google-genai flask Pillow"
    cmd = (
        f"if [ -d {CHRISTY_VENV} ]; then "
        f"  source {CHRISTY_VENV}/bin/activate && pip install {deps} -q; "
        f"else "
        f"  python3 -m venv {CHRISTY_VENV} && "
        f"  source {CHRISTY_VENV}/bin/activate && pip install {deps} -q; "
        f"fi"
    )
    ssh_cmd(CHRISTY_USER, CHRISTY_HOST, cmd)
    print("  [OK] Dependencies installed")


def start_server():
    """Start vision server on Christy."""
    print("  Starting Vision Server on Christy...")
    # Check if ANTHROPIC_API_KEY is set
    rc, key_check = ssh_cmd(CHRISTY_USER, CHRISTY_HOST,
                            'grep -c ANTHROPIC_API_KEY ~/.bashrc 2>/dev/null || echo "0"',
                            quiet=True)
    if key_check.strip() == "0":
        print("  [WARNING] ANTHROPIC_API_KEY not found in ~/.bashrc on Christy")
        print("  Set it with: ssh ajrobotics@192.168.1.94 'echo export ANTHROPIC_API_KEY=sk-... >> ~/.bashrc'")

    cmd = (
        f"source {CHRISTY_VENV}/bin/activate && "
        f"cd {CHRISTY_DIR} && "
        f"nohup python -m robotics.vision_server "
        f"> /home/ajrobotics/logs/vision_server.log 2>&1 &"
    )
    ssh_cmd(CHRISTY_USER, CHRISTY_HOST, cmd)
    print("  [OK] Vision Server started on port 5100")
    print(f"  URL: http://{CHRISTY_HOST}:5100/api/vision/status")


def stop_server():
    """Stop vision server on Christy."""
    print("  Stopping Vision Server on Christy...")
    ssh_cmd(CHRISTY_USER, CHRISTY_HOST,
            "pkill -f 'python -m robotics.vision_server' || echo 'Not running'")
    print("  [OK] Server stopped")


def check_status():
    """Check vision server status on Christy."""
    print("  Checking Vision Server status...")
    ssh_cmd(CHRISTY_USER, CHRISTY_HOST,
            "ps aux | grep 'vision_server' | grep -v grep || echo '  Server is NOT running'")
    print()
    print("  Recent log:")
    ssh_cmd(CHRISTY_USER, CHRISTY_HOST,
            "tail -10 /home/ajrobotics/logs/vision_server.log 2>/dev/null || echo '  No logs yet'")
    print()
    # Try to hit the status endpoint
    rc, output = ssh_cmd(CHRISTY_USER, CHRISTY_HOST,
                         "curl -s http://localhost:5100/api/vision/status 2>/dev/null || echo 'Server not responding'",
                         quiet=True)
    print(f"  Status API: {output[:200]}")


# --- Robot (Vision Client) ---

def deploy_client(robot_id):
    """Deploy vision client to a robot."""
    if robot_id not in ROBOTS:
        print(f"  [ERROR] Unknown robot: {robot_id}")
        print(f"  Known robots: {', '.join(ROBOTS.keys())}")
        return

    robot = ROBOTS[robot_id]
    host = robot["host"]
    user = robot["user"]
    rdir = robot["dir"]
    venv = robot["venv"]

    print(f"  Deploying vision client to {robot_id} ({host})...")

    if not check_connection(user, host, robot_id):
        return

    # Create dirs
    ssh_cmd(user, host, f"mkdir -p {rdir}/robotics", quiet=True)

    # Sync files
    files = [
        ("robotics/vision_client.py", f"{rdir}/robotics/vision_client.py"),
        ("robotics/vision_config.py", f"{rdir}/robotics/vision_config.py"),
    ]
    for local_rel, remote_path in files:
        local_path = os.path.join(LOCAL_BASE, local_rel)
        if os.path.exists(local_path):
            print(f"    {local_rel}")
            scp_file(local_path, user, host, remote_path)

    # Ensure __init__.py exists
    ssh_cmd(user, host, f"touch {rdir}/robotics/__init__.py", quiet=True)

    # Install deps
    print(f"  Installing dependencies on {robot_id}...")
    cmd = f"source {venv}/bin/activate && pip install requests Pillow -q 2>/dev/null"
    ssh_cmd(user, host, cmd)
    # Also ensure opencv is available
    ssh_cmd(user, host, "dpkg -l python3-opencv >/dev/null 2>&1 || sudo apt install -y python3-opencv 2>/dev/null",
            quiet=True)

    print(f"  [OK] Vision client deployed to {robot_id}")
    print(f"  Run: cd {rdir} && python -m robotics.vision_client --robot-id {robot_id} --once")


def main():
    parser = argparse.ArgumentParser(description="Deploy Vision Pipeline")
    parser.add_argument("--run", action="store_true", help="Start vision server on Christy")
    parser.add_argument("--stop", action="store_true", help="Stop vision server")
    parser.add_argument("--status", action="store_true", help="Check server status")
    parser.add_argument("--sync-only", action="store_true", help="Sync files only")
    parser.add_argument("--deploy-client", metavar="ROBOT", help="Deploy client to robot (e.g. R1)")
    args = parser.parse_args()

    print()
    print("=" * 50)
    print("  AJ Robotics — Vision Pipeline Deploy")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)
    print()

    if args.deploy_client:
        deploy_client(args.deploy_client)
        return

    if not check_connection(CHRISTY_USER, CHRISTY_HOST, "Christy"):
        sys.exit(1)

    if args.stop:
        stop_server()
        return

    if args.status:
        check_status()
        return

    sync_server_files()

    if not args.sync_only:
        install_server_deps()

    if args.run:
        stop_server()
        start_server()

    print()
    print("=" * 50)
    print("  Deploy complete!")
    if args.run:
        print(f"  Vision Server running on Christy:5100")
    else:
        print("  Use --run to start the server")
    print("=" * 50)
    print()


if __name__ == "__main__":
    main()
