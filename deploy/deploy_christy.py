"""Deploy robotics code from Dreamer to Christy via SSH/SCP (paramiko).

Usage:
    python -m deploy.deploy_christy [--run] [--sync-only]

Actions:
    1. Creates remote directory structure
    2. Copies robotics/ package to Christy
    3. Installs dependencies (if needed)
    4. Optionally starts xbee_monitor

Examples:
    python -m deploy.deploy_christy              # sync + install deps
    python -m deploy.deploy_christy --run         # sync + start monitor
    python -m deploy.deploy_christy --sync-only   # sync files only
    python -m deploy.deploy_christy --stop        # stop running monitor
    python -m deploy.deploy_christy --status      # check if monitor is running
"""

import argparse
import os
import sys
from datetime import datetime

import paramiko

# Christy connection info
REMOTE_USER = "ajrobotics"
REMOTE_HOST = "192.168.1.94"
REMOTE_DIR = "/home/ajrobotics/AJMain"
REMOTE_VENV = "/home/ajrobotics/robot_fleet_venv"

# Local paths
LOCAL_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROBOTICS_DIR = os.path.join(LOCAL_BASE, "robotics")

# SSH key discovery
_SSH_DIR = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")), ".ssh")
_SSH_KEY_PATHS = []
for _kn in ["id_ed25519", "id_rsa"]:
    _kp = os.path.join(_SSH_DIR, _kn)
    if os.path.exists(_kp):
        _SSH_KEY_PATHS.append(_kp)


def _get_client():
    """Create and connect a paramiko SSH client to Christy."""
    last_err = None
    for key_path in (_SSH_KEY_PATHS or [None]):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            kwargs = {
                "hostname": REMOTE_HOST,
                "username": REMOTE_USER,
                "timeout": 5,
                "banner_timeout": 5,
                "auth_timeout": 5,
                "allow_agent": False,
                "look_for_keys": False,
            }
            if key_path:
                kwargs["key_filename"] = key_path
            client.connect(**kwargs)
            return client
        except paramiko.AuthenticationException as e:
            last_err = e
            try:
                client.close()
            except Exception:
                pass
            continue
        except Exception:
            raise
    raise last_err or paramiko.AuthenticationException("No valid SSH key")


def ssh_cmd(command: str, quiet: bool = False, timeout: int = 30) -> int:
    """Execute a command on Christy via paramiko SSH."""
    client = _get_client()
    try:
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        rc = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        if not quiet:
            if out.strip():
                print(out.strip())
            if err.strip():
                print(err.strip(), file=sys.stderr)
        return rc
    finally:
        client.close()


def ssh_cmd_output(command: str, timeout: int = 30) -> str:
    """Execute a command and return stdout."""
    client = _get_client()
    try:
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        stdout.channel.recv_exit_status()
        return stdout.read().decode("utf-8", errors="replace").strip()
    finally:
        client.close()


def scp_file(local_path: str, remote_path: str) -> int:
    """Copy a file to Christy via SFTP."""
    client = _get_client()
    try:
        sftp = client.open_sftp()
        sftp.put(local_path, remote_path)
        sftp.close()
        return 0
    except Exception as e:
        print(f"  SCP error: {e}", file=sys.stderr)
        return 1
    finally:
        client.close()


def check_connection() -> bool:
    """Check if Christy is reachable."""
    print(f"  Checking connection to Christy ({REMOTE_HOST})...")
    try:
        rc = ssh_cmd("echo ok", quiet=True, timeout=8)
        if rc == 0:
            print(f"  [OK] Connected to Christy")
            return True
    except Exception as e:
        print(f"  [FAIL] Cannot reach Christy: {e}")
    return False


def create_remote_dirs():
    """Create directory structure on Christy."""
    print("  Creating remote directories...")
    ssh_cmd(f"mkdir -p {REMOTE_DIR}/robotics {REMOTE_DIR}/logs/xbee", quiet=True)


def sync_files():
    """Copy robotics package to Christy."""
    print(f"  Syncing robotics/ -> Christy:{REMOTE_DIR}/")

    # Get list of Python files to sync
    files_to_sync = []
    for root, dirs, files in os.walk(ROBOTICS_DIR):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            if f.endswith(".py"):
                local_path = os.path.join(root, f)
                rel_path = os.path.relpath(local_path, LOCAL_BASE)
                files_to_sync.append((local_path, rel_path))

    # Create all needed remote directories first
    remote_dirs = set()
    for _, rel_path in files_to_sync:
        remote_dir = os.path.dirname(f"{REMOTE_DIR}/{rel_path}")
        remote_dirs.add(remote_dir)
    if remote_dirs:
        ssh_cmd("mkdir -p " + " ".join(remote_dirs), quiet=True)

    for local_path, rel_path in files_to_sync:
        remote_path = f"{REMOTE_DIR}/{rel_path}"
        print(f"    {rel_path}")
        scp_file(local_path, remote_path)

    print(f"  [OK] {len(files_to_sync)} files synced")


def install_deps():
    """Install Python dependencies on Christy."""
    print("  Installing dependencies...")
    deps = "digi-xbee pyserial"
    cmd = f"""
        if [ -d {REMOTE_VENV} ]; then
            source {REMOTE_VENV}/bin/activate && pip install {deps} -q
        else
            python3 -m venv {REMOTE_VENV} &&
            source {REMOTE_VENV}/bin/activate && pip install {deps} -q
        fi
    """
    ssh_cmd(cmd, quiet=True)
    print("  [OK] Dependencies installed")


def start_monitor(interval: int = 10):
    """Start xbee_monitor on Christy (background with nohup)."""
    print("  Starting XBee Monitor on Christy...")
    cmd = (
        f"mkdir -p /home/ajrobotics/logs/xbee && "
        f"source {REMOTE_VENV}/bin/activate && "
        f"cd {REMOTE_DIR} && "
        f"nohup python -m robotics.xbee_monitor --interval {interval} "
        f"--log-dir /home/ajrobotics/logs/xbee "
        f"> /home/ajrobotics/logs/xbee/monitor_stdout.log 2>&1 &"
    )
    ssh_cmd(cmd, quiet=True)
    print("  [OK] XBee Monitor started in background")
    print(f"  Logs: Christy:{REMOTE_DIR}/logs/xbee/")


def stop_monitor():
    """Stop xbee_monitor on Christy."""
    print("  Stopping XBee Monitor on Christy...")
    ssh_cmd("pkill -f 'python -m robotics.xbee_monitor' || echo 'Not running'", quiet=True)
    print("  [OK] Monitor stopped")


def check_status():
    """Check if xbee_monitor is running on Christy."""
    print("  Checking monitor status on Christy...")
    out = ssh_cmd_output("ps aux | grep 'xbee_monitor' | grep -v grep || echo '  Monitor is NOT running'")
    print(out)
    print()
    print("  Recent log entries:")
    out = ssh_cmd_output(f"tail -5 /home/ajrobotics/logs/xbee/xbee_events.log 2>/dev/null || echo '  No logs yet'")
    print(out)


def main():
    parser = argparse.ArgumentParser(description="Deploy robotics code to Christy")
    parser.add_argument("--run", action="store_true", help="Start xbee_monitor after sync")
    parser.add_argument("--sync-only", action="store_true", help="Sync files only (no deps)")
    parser.add_argument("--stop", action="store_true", help="Stop running monitor")
    parser.add_argument("--status", action="store_true", help="Check monitor status")
    parser.add_argument("--interval", type=int, default=10, help="Heartbeat interval (default: 10)")
    args = parser.parse_args()

    print()
    print("=" * 50)
    print("  AJ Robotics — Deploy to Christy")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)
    print()

    if not check_connection():
        sys.exit(1)

    if args.stop:
        stop_monitor()
        return

    if args.status:
        check_status()
        return

    create_remote_dirs()
    sync_files()

    if not args.sync_only:
        install_deps()

    if args.run:
        stop_monitor()  # stop old instance first
        start_monitor(args.interval)

    print()
    print("=" * 50)
    print("  Deploy complete!")
    if args.run:
        print(f"  Monitor running on Christy (interval: {args.interval}s)")
    else:
        print("  Use --run to start the monitor")
        print(f"  Or SSH: ssh {REMOTE_USER}@{REMOTE_HOST}")
    print("=" * 50)
    print()


if __name__ == "__main__":
    main()
