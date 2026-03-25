"""
Deploy robot-side code to Jetson Orin on RosMaster X3.

Usage:
    python deploy.py           # Deploy jetson/ files and start TCP server
    python deploy.py --start   # Deploy and start the TCP server
    python deploy.py --stop    # Stop the TCP server
    python deploy.py --status  # Check if server is running
    python deploy.py --oled    # Deploy and enable OLED status display
"""

import subprocess
import sys
import os

from config import ROSMASTER_IP, ROSMASTER_USER, ROSMASTER_SSH_PORT, REMOTE_PROJECT_DIR

JETSON_DIR = os.path.join(os.path.dirname(__file__), "jetson")


def ssh(command: str, timeout: int = 15) -> subprocess.CompletedProcess:
    args = [
        "ssh",
        "-o", "ConnectTimeout=5",
        "-o", "StrictHostKeyChecking=no",
        "-p", str(ROSMASTER_SSH_PORT),
        f"{ROSMASTER_USER}@{ROSMASTER_IP}",
        command,
    ]
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def scp(local_path: str, remote_path: str):
    args = [
        "scp",
        "-o", "ConnectTimeout=5",
        "-o", "StrictHostKeyChecking=no",
        "-P", str(ROSMASTER_SSH_PORT),
        "-r",
        local_path,
        f"{ROSMASTER_USER}@{ROSMASTER_IP}:{remote_path}",
    ]
    result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        print(f"SCP failed: {result.stderr}")
        sys.exit(1)


def deploy():
    print(f"Deploying to {ROSMASTER_USER}@{ROSMASTER_IP}:{REMOTE_PROJECT_DIR}")

    # Create remote directory
    ssh(f"mkdir -p {REMOTE_PROJECT_DIR}")

    # Copy all files from jetson/
    for fname in os.listdir(JETSON_DIR):
        local = os.path.join(JETSON_DIR, fname)
        if os.path.isfile(local):
            print(f"  Copying {fname}...")
            scp(local, f"{REMOTE_PROJECT_DIR}/{fname}")

    print("Deploy complete!")


def start_server():
    print("Starting TCP server on Jetson...")
    # Deploy and run the startup script
    scp(os.path.join(JETSON_DIR, "start_server.sh"), f"{REMOTE_PROJECT_DIR}/start_server.sh")
    ssh(f"chmod +x {REMOTE_PROJECT_DIR}/start_server.sh")
    result = ssh(f"{REMOTE_PROJECT_DIR}/start_server.sh", timeout=10)
    print(result.stdout.strip() if result.stdout.strip() else "")
    import time
    time.sleep(3)
    # Verify it started
    result = ssh("pgrep -f 'python3.*tcp_server'")
    if result.stdout.strip():
        print("TCP server running!")
    else:
        log = ssh("tail -10 /tmp/rosmaster_server.log 2>/dev/null")
        print("WARNING: TCP server may not have started.")
        if log.stdout.strip():
            print(f"Log:\n{log.stdout.strip()}")


def stop_server():
    print("Stopping TCP server...")
    ssh("pkill -f 'python3.*tcp_server'")
    print("Stopped.")


def status():
    result = ssh("pgrep -af 'python3.*tcp_server'")
    if result.stdout.strip():
        print(f"TCP server is RUNNING:\n  {result.stdout.strip()}")
    else:
        print("TCP server is NOT running.")

    result = ssh(f"ls -la {REMOTE_PROJECT_DIR}/ 2>/dev/null")
    if result.stdout.strip():
        print(f"\nDeployed files:\n{result.stdout}")
    else:
        print(f"\nNo files deployed at {REMOTE_PROJECT_DIR}")


def setup_oled():
    """Deploy and enable the OLED status display service."""
    print("Setting up OLED status display...")
    deploy()

    # Copy service file
    scp(
        os.path.join(JETSON_DIR, "rosmaster-status.service"),
        "/tmp/rosmaster-status.service",
    )

    # Disable old OLED service, install new one
    ssh("sudo systemctl stop yahboom_oled.service 2>/dev/null")
    ssh("sudo systemctl disable yahboom_oled.service 2>/dev/null")
    ssh("sudo cp /tmp/rosmaster-status.service /etc/systemd/system/")
    ssh("sudo systemctl daemon-reload")
    ssh("sudo systemctl enable rosmaster-status.service")
    ssh("sudo systemctl restart rosmaster-status.service")

    import time
    time.sleep(2)
    result = ssh("systemctl is-active rosmaster-status.service")
    state = result.stdout.strip()
    if state == "active":
        print("OLED status display is running!")
    else:
        log = ssh("journalctl -u rosmaster-status -n 10 --no-pager 2>/dev/null")
        print(f"WARNING: Service state is '{state}'")
        if log.stdout.strip():
            print(f"Log:\n{log.stdout.strip()}")


def setup_webui():
    """Deploy and enable the Web UI dashboard."""
    print("Setting up Web UI dashboard...")
    deploy()

    # Copy web_ui directory
    webui_dir = os.path.join(JETSON_DIR, "web_ui")
    ssh(f"mkdir -p {REMOTE_PROJECT_DIR}/web_ui/static")
    for root, dirs, files in os.walk(webui_dir):
        for fname in files:
            local = os.path.join(root, fname)
            rel = os.path.relpath(local, JETSON_DIR)
            remote = f"{REMOTE_PROJECT_DIR}/{rel}".replace("\\", "/")
            print(f"  Copying {rel}...")
            scp(local, remote)

    # Install service
    scp(
        os.path.join(webui_dir, "rosmaster-webui.service"),
        "/tmp/rosmaster-webui.service",
    )
    ssh("sudo cp /tmp/rosmaster-webui.service /etc/systemd/system/")
    ssh("sudo systemctl daemon-reload")
    ssh("sudo systemctl enable rosmaster-webui.service")
    ssh("sudo systemctl restart rosmaster-webui.service")

    import time
    time.sleep(3)
    result = ssh("systemctl is-active rosmaster-webui.service")
    state = result.stdout.strip()
    if state == "active":
        print(f"Web UI running at http://192.168.1.99:8080")
    else:
        log = ssh("journalctl -u rosmaster-webui -n 10 --no-pager 2>/dev/null")
        print(f"WARNING: Service state is '{state}'")
        if log.stdout.strip():
            print(f"Log:\n{log.stdout.strip()}")


def main():
    args = sys.argv[1:]

    if "--stop" in args:
        stop_server()
    elif "--status" in args:
        status()
    elif "--oled" in args:
        setup_oled()
    elif "--webui" in args:
        setup_webui()
    elif "--start" in args:
        deploy()
        start_server()
    else:
        deploy()
        print("\nTo start the TCP server, run: python deploy.py --start")


if __name__ == "__main__":
    main()
