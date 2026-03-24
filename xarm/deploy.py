#!/usr/bin/env python3
"""
Deploy xarm/ package to R1 (Raspberry Pi) via SSH/SFTP.

Usage:
    python xarm/deploy.py              # Deploy and restart
    python xarm/deploy.py --no-restart # Deploy only, don't restart
"""

import argparse
import os
import sys
import stat

# R1 connection config
R1_HOST = "192.168.1.82"
R1_USER = "dream"
R1_DEST = "/home/dream/xarm"
R1_VENV = "/home/dream/xarm/venv"
SSH_KEY = os.path.expanduser("~/.ssh/id_ed25519")

# Files to deploy (relative to xarm/ directory)
DEPLOY_FILES = [
    "__init__.py",
    "controller.py",
    "kinematics.py",
    "hardware.py",
    "local_gamepad.py",
    "simulation.html",
    "config.json",
    "gamepads.json",
    "start.py",
]

REQUIREMENTS = ["flask>=3.0", "hidapi>=0.14", "psutil>=5.9"]


def deploy():
    parser = argparse.ArgumentParser(description="Deploy xarm/ to R1")
    parser.add_argument("--no-restart", action="store_true", help="Don't restart after deploy")
    parser.add_argument("--host", default=R1_HOST, help=f"Target host (default: {R1_HOST})")
    parser.add_argument("--user", default=R1_USER, help=f"SSH user (default: {R1_USER})")
    args = parser.parse_args()

    try:
        import paramiko
    except ImportError:
        print("  pip install paramiko")
        sys.exit(1)

    xarm_dir = os.path.dirname(os.path.abspath(__file__))

    print(f"\n  Deploying xarm/ to {args.user}@{args.host}:{R1_DEST}")
    print(f"  {'=' * 50}")

    # Connect
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(args.host, username=args.user, key_filename=SSH_KEY, timeout=10)
        print(f"  SSH connected to {args.host}")
    except Exception as e:
        print(f"  SSH connection failed: {e}")
        sys.exit(1)

    def ssh_exec(cmd):
        _, stdout, stderr = client.exec_command(cmd)
        out = stdout.read().decode().strip()
        err = stderr.read().decode().strip()
        return out, err

    # Create remote directory
    ssh_exec(f"mkdir -p {R1_DEST}")

    # Upload files
    sftp = client.open_sftp()
    for fname in DEPLOY_FILES:
        local_path = os.path.join(xarm_dir, fname)
        remote_path = f"{R1_DEST}/{fname}"
        if os.path.exists(local_path):
            sftp.put(local_path, remote_path)
            print(f"  UPLOAD {fname}")
        else:
            print(f"  SKIP   {fname} (not found)")

    # Create requirements file
    req_content = "\n".join(REQUIREMENTS) + "\n"
    with sftp.open(f"{R1_DEST}/requirements.txt", "w") as f:
        f.write(req_content)
    print(f"  UPLOAD requirements.txt")

    sftp.close()

    # Install dependencies
    print(f"\n  Installing dependencies...")
    out, err = ssh_exec(f"test -d {R1_VENV} || python3 -m venv {R1_VENV}")
    out, err = ssh_exec(f"{R1_VENV}/bin/pip install -r {R1_DEST}/requirements.txt -q")
    if out:
        print(f"  {out}")
    print(f"  Dependencies OK")

    if not args.no_restart:
        # Stop existing xarm process
        print(f"\n  Restarting xarm service...")
        ssh_exec("pkill -f 'xarm.start' 2>/dev/null || true")

        import time
        time.sleep(1)

        # Create start script
        start_script = f"""#!/bin/bash
cd {R1_DEST}
{R1_VENV}/bin/python -m xarm.start --port 5001 >> /tmp/xarm.log 2>&1 &
echo $! > /tmp/xarm.pid
"""
        with client.open_sftp() as sftp2:
            with sftp2.open(f"/tmp/start_xarm.sh", "w") as f:
                f.write(start_script)
            sftp2.chmod("/tmp/start_xarm.sh", stat.S_IRWXU)

        ssh_exec("bash /tmp/start_xarm.sh")
        time.sleep(2)

        # Verify
        out, _ = ssh_exec("cat /tmp/xarm.pid 2>/dev/null || echo NO_PID")
        pid = out.strip()
        out2, _ = ssh_exec(f"ps -p {pid} -o comm= 2>/dev/null || echo NOT_RUNNING")

        if "NOT_RUNNING" not in out2 and "NO_PID" not in pid:
            print(f"  xarm running (PID: {pid})")
            print(f"  URL: http://{args.host}:5001/simulation")
        else:
            print(f"  WARNING: xarm may not be running. Check /tmp/xarm.log")

    print(f"\n  Deploy complete!")
    client.close()


if __name__ == "__main__":
    deploy()
