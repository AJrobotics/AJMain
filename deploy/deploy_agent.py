"""
Deploy AJ Agent to a remote Linux machine (CashCow or Christy).
Uses paramiko for SSH/SFTP (no subprocess ssh needed).

Usage:
    python deploy/deploy_agent.py cashcow    # Deploy to CashCow
    python deploy/deploy_agent.py christy    # Deploy to Christy
    python deploy/deploy_agent.py cashcow --restart  # Just restart service
"""

import argparse
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
HOSTS_PATH = os.path.join(PROJECT_DIR, "configs", "hosts.json")

# Files/dirs to sync to remote
SYNC_ITEMS = [
    "agent/",
    "agent_modules/",
    "shared/",
    "robotics/",
    "configs/",
    "gui/templates/",
    "gui/static/",
    "requirements_agent.txt",
]

# Skip these patterns during upload
SKIP_PATTERNS = ("__pycache__", ".pyc", ".pyo")


def load_target(name: str) -> dict:
    with open(HOSTS_PATH, "r") as f:
        hosts = json.load(f)
    for cat in ("computers", "raspberry_pis"):
        if name in hosts.get(cat, {}):
            return hosts[cat][name]
    raise ValueError(f"Machine '{name}' not found in hosts.json")


def _get_ssh_key_path() -> str:
    """Find the user's SSH private key."""
    ssh_dir = os.path.expanduser("~/.ssh")
    for name in ("id_ed25519", "id_rsa", "id_ecdsa"):
        path = os.path.join(ssh_dir, name)
        if os.path.isfile(path):
            return path
    return ""


def connect_ssh(user: str, host: str):
    """Create paramiko SSH connection."""
    import paramiko
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    key_path = _get_ssh_key_path()
    connect_kwargs = {
        "hostname": host,
        "username": user,
        "timeout": 10,
    }
    if key_path:
        print(f"  Using SSH key: {key_path}", flush=True)
        connect_kwargs["key_filename"] = key_path
    else:
        print("  WARNING: No SSH key found, trying agent/password", flush=True)

    client.connect(**connect_kwargs)
    return client


def ssh_exec(client, cmd: str, timeout: int = 30) -> tuple[int, str, str]:
    """Execute command over SSH, return (exit_code, stdout, stderr)."""
    print(f"  SSH> {cmd}", flush=True)
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    exit_code = stdout.channel.recv_exit_status()
    out = stdout.read().decode(errors="replace").strip()
    err = stderr.read().decode(errors="replace").strip()
    if out:
        lines = out.splitlines()
        for line in lines[:5]:
            print(f"  OUT> {line}".encode("ascii", "replace").decode(), flush=True)
        if len(lines) > 5:
            print(f"  OUT> ... ({len(lines)} lines total)", flush=True)
    if exit_code != 0 and err:
        for line in err.splitlines()[:5]:
            print(f"  ERR> {line}".encode("ascii", "replace").decode(), flush=True)
    return exit_code, out, err


def sftp_upload_dir(sftp, local_dir: str, remote_dir: str):
    """Recursively upload a local directory to remote via SFTP."""
    # Ensure remote dir exists
    try:
        sftp.stat(remote_dir)
    except FileNotFoundError:
        sftp.mkdir(remote_dir)

    for item in os.listdir(local_dir):
        if any(skip in item for skip in SKIP_PATTERNS):
            continue
        local_path = os.path.join(local_dir, item)
        remote_path = f"{remote_dir}/{item}"

        if os.path.isdir(local_path):
            sftp_upload_dir(sftp, local_path, remote_path)
        else:
            sftp.put(local_path, remote_path)


def sftp_makedirs(sftp, path: str):
    """Recursively create remote directories."""
    parts = path.split("/")
    current = ""
    for part in parts:
        if not part:
            current = "/"
            continue
        current = f"{current}/{part}" if current != "/" else f"/{part}"
        try:
            sftp.stat(current)
        except FileNotFoundError:
            sftp.mkdir(current)


def sync_files(client, remote_dir: str):
    """Upload project files to remote machine via SFTP."""
    print("\n--- Syncing files via SFTP ---", flush=True)
    sftp = client.open_sftp()

    try:
        for item in SYNC_ITEMS:
            local = os.path.join(PROJECT_DIR, item)
            if not os.path.exists(local):
                print(f"  SKIP {item} (not found locally)", flush=True)
                continue

            if item.endswith("/"):
                dir_name = item.rstrip("/")
                remote_path = f"{remote_dir}/{dir_name}"
                print(f"  UPLOAD {item} -> {remote_path}/", flush=True)
                sftp_makedirs(sftp, remote_path)
                sftp_upload_dir(sftp, local.rstrip("/\\"), remote_path)
                print(f"  OK", flush=True)
            else:
                remote_path = f"{remote_dir}/{item}"
                print(f"  UPLOAD {item} -> {remote_path}", flush=True)
                sftp.put(local, remote_path)
                print(f"  OK", flush=True)
    finally:
        sftp.close()


def start_agent(client, remote_dir: str):
    """Start the agent on the remote machine (no sudo needed)."""
    print("\n--- Starting agent ---", flush=True)
    venv_py = f"{remote_dir}/venv/bin/python"

    # Kill any existing agent
    ssh_exec(client, 'pkill -f "agent.start_agent" 2>/dev/null || true')

    # Write start script
    start_script = (
        f"#!/bin/bash\n"
        f"cd {remote_dir}\n"
        f"{venv_py} -m agent.start_agent >> /tmp/ajagent.log 2>&1 &\n"
        f"echo $! > /tmp/ajagent.pid\n"
    )
    sftp = client.open_sftp()
    with sftp.open("/tmp/start_ajagent.sh", "w") as f:
        f.write(start_script)
    sftp.close()
    ssh_exec(client, "chmod +x /tmp/start_ajagent.sh")

    # Execute via transport channel (avoids paramiko hanging on background process)
    import time
    transport = client.get_transport()
    channel = transport.open_session()
    channel.exec_command("/tmp/start_ajagent.sh")
    time.sleep(1)
    channel.close()

    time.sleep(3)

    # Verify
    code, pid, _ = ssh_exec(client, "cat /tmp/ajagent.pid 2>/dev/null || echo NO_PID")
    code, ps_out, _ = ssh_exec(client, 'ps aux | grep agent.start_agent | grep -v grep || echo NOT_RUNNING')
    code, port_out, _ = ssh_exec(client, 'ss -tlnp | grep 5000 || echo PORT_NOT_LISTENING')

    if "NOT_RUNNING" in ps_out:
        print("  FAILED: Agent not running!", flush=True)
        ssh_exec(client, "tail -10 /tmp/ajagent.log 2>/dev/null")
        return False

    print(f"  Agent running (PID: {pid.strip()})", flush=True)
    return True


def install_deps(client, remote_dir: str):
    print("\n--- Installing dependencies ---", flush=True)
    # Create venv if it doesn't exist, then install deps
    venv_dir = f"{remote_dir}/venv"
    ssh_exec(client, f"test -d {venv_dir} || python3 -m venv {venv_dir}", timeout=30)
    ssh_exec(client,
             f"{venv_dir}/bin/pip install -r {remote_dir}/requirements_agent.txt",
             timeout=120)


def main():
    parser = argparse.ArgumentParser(description="Deploy AJ Agent")
    parser.add_argument("target", help="Machine name (e.g. CashCow, Christy)")
    parser.add_argument("--restart", action="store_true", help="Just restart the agent")
    args = parser.parse_args()

    name = args.target
    name_map = {"cashcow": "CashCow", "christy": "Christy", "r1": "R1"}
    name = name_map.get(name.lower(), name)

    info = load_target(name)
    user = info["username"]
    host = info["host"]
    remote_dir = f"/home/{user}/AJMain"

    print(f"Deploying to {name} ({user}@{host}:{remote_dir})", flush=True)

    # Connect
    print("\n--- Connecting via SSH (paramiko) ---", flush=True)
    client = connect_ssh(user, host)
    print(f"  Connected to {host}", flush=True)

    try:
        if args.restart:
            start_agent(client, remote_dir)
            return

        # Create remote dir
        ssh_exec(client, f"mkdir -p {remote_dir}")

        # Sync files
        sync_files(client, remote_dir)

        # Install deps
        install_deps(client, remote_dir)

        # Start agent
        ok = start_agent(client, remote_dir)

        if ok:
            print(f"\nDone! Agent running at http://{host}:5000", flush=True)
        else:
            print(f"\nDeploy completed but agent failed to start. Check /tmp/ajagent.log", flush=True)

    finally:
        client.close()


if __name__ == "__main__":
    main()
