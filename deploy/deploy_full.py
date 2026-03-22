"""Deploy the full AJMain Flask application to Christy (Ubuntu).

Usage:
    python -m deploy.deploy_full --deploy       # sync files + install deps
    python -m deploy.deploy_full --restart      # restart Flask systemd service
    python -m deploy.deploy_full --full         # deploy + restart
    python -m deploy.deploy_full --status       # check service status
    python -m deploy.deploy_full --setup-service # create systemd service file

Copies: app.py, gui/, shared/, configs/, robotics/, scripts/, deploy/
Skips:  __pycache__, .pyc, venv/, node_modules/, .git/, trader/, agent/
"""

import argparse
import os
import stat
import sys
from datetime import datetime

import paramiko

# Christy connection info
REMOTE_USER = "ajrobotics"
REMOTE_HOST = "192.168.1.94"
REMOTE_DIR = "/home/ajrobotics/AJMain"
REMOTE_VENV = f"{REMOTE_DIR}/venv"

# Local paths
LOCAL_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Directories/files to deploy
DEPLOY_DIRS = ["gui", "shared", "configs", "robotics", "scripts", "deploy"]
DEPLOY_FILES = ["app.py", "requirements.txt"]

# Skip patterns
SKIP_DIRS = {"__pycache__", "venv", "node_modules", ".git", "trader", "agent",
             "agent_modules"}
SKIP_EXTENSIONS = {".pyc", ".pyo"}

# Pip packages needed for the Flask app on Christy
PIP_DEPS = "flask requests paramiko pytz psutil"

# SSH key discovery (works on both Windows and Linux)
_SSH_DIR = os.path.join(
    os.environ.get("USERPROFILE", os.path.expanduser("~")), ".ssh"
) if os.name == "nt" else os.path.join(os.path.expanduser("~"), ".ssh")

_SSH_KEY_PATHS = []
for _kn in ["id_ed25519", "id_rsa"]:
    _kp = os.path.join(_SSH_DIR, _kn)
    if os.path.exists(_kp):
        _SSH_KEY_PATHS.append(_kp)

# Systemd service content
SYSTEMD_SERVICE = """\
[Unit]
Description=AJ Robotics Main Control Hub
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ajrobotics
WorkingDirectory=/home/ajrobotics/AJMain
Environment=GMAIL_USER=Dreamittogether@gmail.com
Environment=GMAIL_APP_PASSWORD=ybxgmceixhqbscas
ExecStart=/home/ajrobotics/AJMain/venv/bin/python app.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
"""


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
                "timeout": 10,
                "banner_timeout": 10,
                "auth_timeout": 10,
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


def ssh_cmd(command, quiet=False, timeout=60):
    """Execute a command on Christy via paramiko SSH."""
    client = _get_client()
    try:
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        rc = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        if not quiet:
            if out.strip():
                print(out.strip().encode("ascii", "replace").decode("ascii"))
            if err.strip():
                print(err.strip().encode("ascii", "replace").decode("ascii"),
                      file=sys.stderr)
        return rc, out, err
    finally:
        client.close()


def ssh_cmd_output(command, timeout=30):
    """Execute a command and return stdout."""
    rc, out, err = ssh_cmd(command, quiet=True, timeout=timeout)
    return out.strip()


def check_connection():
    """Check if Christy is reachable."""
    print(f"  Checking connection to Christy ({REMOTE_HOST})...")
    try:
        rc, out, err = ssh_cmd("echo ok", quiet=True, timeout=8)
        if rc == 0:
            print("  [OK] Connected to Christy")
            return True
    except Exception as e:
        print(f"  [FAIL] Cannot reach Christy: {e}")
    return False


def collect_files():
    """Collect all files to deploy."""
    files = []

    # Top-level files
    for fname in DEPLOY_FILES:
        local_path = os.path.join(LOCAL_BASE, fname)
        if os.path.exists(local_path):
            files.append((local_path, fname))

    # Directories
    for dirname in DEPLOY_DIRS:
        dir_path = os.path.join(LOCAL_BASE, dirname)
        if not os.path.isdir(dir_path):
            continue
        for root, dirs, filenames in os.walk(dir_path):
            # Skip unwanted directories
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in filenames:
                # Skip unwanted files
                _, ext = os.path.splitext(f)
                if ext in SKIP_EXTENSIONS:
                    continue
                local_path = os.path.join(root, f)
                rel_path = os.path.relpath(local_path, LOCAL_BASE)
                # Normalize path separators for Linux
                rel_path = rel_path.replace("\\", "/")
                files.append((local_path, rel_path))

    return files


def sync_files():
    """Copy all AJMain files to Christy."""
    files = collect_files()
    print(f"  Syncing {len(files)} files to Christy:{REMOTE_DIR}/")

    # Collect all unique remote directories
    remote_dirs = set()
    for _, rel_path in files:
        remote_dir = os.path.dirname(f"{REMOTE_DIR}/{rel_path}")
        if remote_dir:
            remote_dirs.add(remote_dir)

    # Create directories in one SSH call
    if remote_dirs:
        mkdir_cmd = "mkdir -p " + " ".join(sorted(remote_dirs))
        ssh_cmd(mkdir_cmd, quiet=True)

    # Upload files via SFTP (reuse single connection)
    client = _get_client()
    try:
        sftp = client.open_sftp()
        uploaded = 0
        errors = 0
        for local_path, rel_path in files:
            remote_path = f"{REMOTE_DIR}/{rel_path}"
            try:
                sftp.put(local_path, remote_path)
                uploaded += 1
                # Print every file for visibility
                print(f"    {rel_path}")
            except Exception as e:
                print(f"    [ERROR] {rel_path}: {e}", file=sys.stderr)
                errors += 1
        sftp.close()
        print(f"  [OK] {uploaded} files synced ({errors} errors)")
    finally:
        client.close()


def setup_venv():
    """Create Python venv and install dependencies on Christy."""
    print("  Setting up Python venv on Christy...")
    cmd = f"""
        if [ ! -d {REMOTE_VENV} ]; then
            echo '  Creating venv...'
            python3 -m venv {REMOTE_VENV}
        fi
        echo '  Installing dependencies...'
        {REMOTE_VENV}/bin/pip install --upgrade pip -q 2>&1 | tail -1
        {REMOTE_VENV}/bin/pip install {PIP_DEPS} -q 2>&1 | tail -1
        echo '  Installed packages:'
        {REMOTE_VENV}/bin/pip list 2>/dev/null | head -20
    """
    ssh_cmd(cmd, timeout=120)
    print("  [OK] Venv ready")


def setup_service():
    """Create and enable the systemd service on Christy."""
    print("  Setting up systemd service...")

    # Write service file via SSH (using tee with sudo)
    escaped = SYSTEMD_SERVICE.replace("'", "'\\''")
    cmd = f"echo '{escaped}' | sudo tee /etc/systemd/system/ajmain.service > /dev/null"
    rc, _, err = ssh_cmd(cmd, quiet=True, timeout=15)
    if rc != 0:
        print(f"  [ERROR] Failed to write service file: {err}", file=sys.stderr)
        return False

    # Reload systemd and enable
    rc, _, _ = ssh_cmd(
        "sudo systemctl daemon-reload && sudo systemctl enable ajmain.service",
        quiet=True, timeout=15
    )
    if rc == 0:
        print("  [OK] ajmain.service created and enabled")
    else:
        print("  [WARN] Service may not be fully enabled")

    return True


def restart_service():
    """Restart the ajmain Flask service on Christy."""
    print("  Restarting ajmain.service...")
    rc, _, _ = ssh_cmd(
        "sudo systemctl restart ajmain.service",
        quiet=True, timeout=15
    )
    if rc == 0:
        print("  [OK] Service restarted")
    else:
        print("  [WARN] Restart may have failed")

    # Show status
    import time
    time.sleep(2)
    check_status()


def check_status():
    """Check the status of the ajmain service on Christy."""
    print("  Checking ajmain.service status...")
    ssh_cmd("sudo systemctl status ajmain.service --no-pager -l 2>&1 | head -20")
    print()
    # Check if port 5000 is listening
    print("  Checking port 5000...")
    ssh_cmd("ss -tlnp | grep ':5000' || echo '  Port 5000 is NOT listening'")


def check_deps():
    """Check what Python packages are installed on Christy."""
    print("  Checking installed packages on Christy...")
    ssh_cmd(f"""
        if [ -d {REMOTE_VENV} ]; then
            echo '  Venv exists at {REMOTE_VENV}'
            {REMOTE_VENV}/bin/pip list 2>/dev/null
        else
            echo '  No venv found at {REMOTE_VENV}'
            echo '  System Python packages:'
            python3 -m pip list 2>/dev/null | head -20 || echo '  pip not available'
        fi
    """)


def main():
    parser = argparse.ArgumentParser(
        description="Deploy full AJMain Flask app to Christy"
    )
    parser.add_argument("--deploy", action="store_true",
                        help="Sync files + install deps")
    parser.add_argument("--restart", action="store_true",
                        help="Restart Flask systemd service")
    parser.add_argument("--full", action="store_true",
                        help="Deploy + setup service + restart")
    parser.add_argument("--status", action="store_true",
                        help="Check service status")
    parser.add_argument("--setup-service", action="store_true",
                        help="Create/update systemd service file")
    parser.add_argument("--check-deps", action="store_true",
                        help="Check installed packages")
    args = parser.parse_args()

    # Default to --full if no args
    if not any([args.deploy, args.restart, args.full, args.status,
                args.setup_service, args.check_deps]):
        args.full = True

    print()
    print("=" * 55)
    print("  AJ Robotics - Full Deploy to Christy")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 55)
    print()

    if not check_connection():
        sys.exit(1)

    if args.check_deps:
        check_deps()
        return

    if args.status:
        check_status()
        return

    if args.deploy or args.full:
        sync_files()
        setup_venv()

    if args.setup_service or args.full:
        setup_service()

    if args.restart or args.full:
        restart_service()

    print()
    print("=" * 55)
    print("  Deploy complete!")
    print(f"  Flask app: http://{REMOTE_HOST}:5000/")
    print("=" * 55)
    print()


if __name__ == "__main__":
    main()
