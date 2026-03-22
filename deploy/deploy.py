#!/usr/bin/env python3
"""
AJ Robotics — Universal Deploy Tool

Deploy code from Dreamer to any machine in the fleet.

Usage:
    python -m deploy.deploy --to Christy                    # deploy all to Christy
    python -m deploy.deploy --to R1 --dirs robotics         # deploy robotics/ only to R1
    python -m deploy.deploy --to Christy --restart           # deploy + restart service
    python -m deploy.deploy --to CashCow --files app.py     # deploy single file
    python -m deploy.deploy --to R1 --cmd "ls -la"          # run command on R1
    python -m deploy.deploy --list                           # show all machines
    python -m deploy.deploy --to Christy --status            # check service status
    python -m deploy.deploy --to Christy --setup             # first-time setup (venv + service)

Reads machine info from configs/hosts.json automatically.
"""

import argparse
import json
import os
import sys
from datetime import datetime

import paramiko

# ── Paths ──

LOCAL_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOSTS_JSON = os.path.join(LOCAL_BASE, "configs", "hosts.json")

# Default directories to deploy
DEFAULT_DIRS = ["gui", "shared", "configs", "robotics", "scripts", "deploy"]
DEFAULT_FILES = ["app.py", "requirements.txt"]

# Skip patterns
SKIP_DIRS = {"__pycache__", "venv", "node_modules", ".git", "trader", "agent",
             "agent_modules", ".claude"}
SKIP_EXTENSIONS = {".pyc", ".pyo"}

# Pip packages for the Flask app
PIP_DEPS = "flask requests paramiko pytz psutil"

# SSH key discovery
_SSH_DIR = os.path.join(
    os.environ.get("USERPROFILE", os.path.expanduser("~")), ".ssh"
) if os.name == "nt" else os.path.join(os.path.expanduser("~"), ".ssh")

_SSH_KEY_PATHS = []
for _kn in ["id_ed25519", "id_rsa"]:
    _kp = os.path.join(_SSH_DIR, _kn)
    if os.path.exists(_kp):
        _SSH_KEY_PATHS.append(_kp)

# Systemd service template
SERVICE_TEMPLATE = """\
[Unit]
Description=AJ Robotics - {name}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={user}
WorkingDirectory={remote_dir}
Environment=GMAIL_USER=Dreamittogether@gmail.com
Environment=GMAIL_APP_PASSWORD=ybxgmceixhqbscas
ExecStart={venv}/bin/python app.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
"""


# ── Machine Discovery ──

def load_hosts():
    """Load all machines from hosts.json."""
    with open(HOSTS_JSON, "r") as f:
        data = json.load(f)
    machines = {}
    for category in ["computers", "raspberry_pis"]:
        for name, info in data.get(category, {}).items():
            if info.get("host") and info.get("host") not in ("localhost", "TBD"):
                machines[name] = info
    return machines


def find_machine(name, machines=None):
    """Find a machine by name (case-insensitive)."""
    if machines is None:
        machines = load_hosts()
    for mname, info in machines.items():
        if mname.lower() == name.lower():
            return mname, info
    return None, None


# ── SSH/SFTP ──

def connect(host, user):
    """Create paramiko SSH client, trying all available keys."""
    last_err = None
    for key_path in (_SSH_KEY_PATHS or [None]):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            kwargs = {
                "hostname": host,
                "username": user,
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
    raise last_err or paramiko.AuthenticationException("No valid SSH key")


def ssh_run(client, command, quiet=False, timeout=30):
    """Run a command via SSH, return (stdout, rc)."""
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    rc = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    if not quiet:
        if out.strip():
            print(out.strip())
        if err.strip() and rc != 0:
            print(f"  STDERR: {err.strip()}", file=sys.stderr)
    return out.strip(), rc


def sftp_put(client, local_path, remote_path):
    """Upload a file via SFTP."""
    sftp = client.open_sftp()
    try:
        sftp.put(local_path, remote_path)
    finally:
        sftp.close()


def sftp_mkdir_p(client, remote_path):
    """Create remote directory recursively."""
    ssh_run(client, f"mkdir -p {remote_path}", quiet=True)


# ── Deploy Actions ──

def list_machines():
    """Print all available machines."""
    machines = load_hosts()
    print()
    print("  Available Machines:")
    print("  " + "-" * 45)
    for name, info in machines.items():
        host = info.get("host", "?")
        user = info.get("username", "?")
        os_name = info.get("os", info.get("type", "?"))
        role = info.get("role", info.get("description", ""))[:30]
        print(f"  {name:<12} {user}@{host:<16} {os_name:<10} {role}")
    print()


def check_connection(host, user):
    """Test SSH connection."""
    print(f"  Connecting to {user}@{host}...")
    try:
        client = connect(host, user)
        out, rc = ssh_run(client, "echo OK && hostname", quiet=True)
        client.close()
        if rc == 0:
            hostname = out.split("\n")[-1].strip()
            print(f"  [OK] Connected ({hostname})")
            return True
    except Exception as e:
        print(f"  [FAIL] {e}")
    return False


def deploy_files(host, user, remote_dir, dirs=None, files=None):
    """Deploy directories and files to remote machine."""
    if dirs is None:
        dirs = DEFAULT_DIRS
    if files is None:
        files = DEFAULT_FILES

    client = connect(host, user)
    sftp_mkdir_p(client, remote_dir)

    # Collect all files to sync
    to_sync = []

    # Individual files
    for fname in files:
        local_path = os.path.join(LOCAL_BASE, fname)
        if os.path.exists(local_path):
            to_sync.append((local_path, f"{remote_dir}/{fname}"))

    # Directories
    for dirname in dirs:
        local_dir = os.path.join(LOCAL_BASE, dirname)
        if not os.path.isdir(local_dir):
            print(f"  [SKIP] {dirname}/ not found locally")
            continue
        for root, subdirs, fnames in os.walk(local_dir):
            subdirs[:] = [d for d in subdirs if d not in SKIP_DIRS]
            for f in fnames:
                ext = os.path.splitext(f)[1]
                if ext in SKIP_EXTENSIONS:
                    continue
                local_path = os.path.join(root, f)
                rel_path = os.path.relpath(local_path, LOCAL_BASE).replace("\\", "/")
                remote_path = f"{remote_dir}/{rel_path}"
                to_sync.append((local_path, remote_path))

    if not to_sync:
        print("  [WARN] No files to deploy!")
        client.close()
        return 0

    # Create all needed remote directories
    remote_dirs = set()
    for _, rp in to_sync:
        remote_dirs.add(os.path.dirname(rp).replace("\\", "/"))
    if remote_dirs:
        ssh_run(client, "mkdir -p " + " ".join(remote_dirs), quiet=True)

    # Upload files
    print(f"  Uploading {len(to_sync)} files...")
    sftp = client.open_sftp()
    errors = 0
    for i, (lp, rp) in enumerate(to_sync):
        try:
            sftp.put(lp, rp)
            rel = os.path.relpath(lp, LOCAL_BASE).replace("\\", "/")
            if len(to_sync) <= 20 or (i + 1) % 10 == 0 or i == len(to_sync) - 1:
                print(f"    [{i+1}/{len(to_sync)}] {rel}")
        except Exception as e:
            print(f"    [ERR] {rp}: {e}")
            errors += 1
    sftp.close()
    client.close()

    print(f"  [OK] {len(to_sync) - errors}/{len(to_sync)} files deployed")
    return len(to_sync) - errors


def setup_venv(host, user, remote_dir):
    """Create venv and install dependencies on remote machine."""
    print("  Setting up Python venv...")
    client = connect(host, user)
    venv = f"{remote_dir}/venv"
    cmds = [
        f"test -d {venv} || python3 -m venv {venv}",
        f"{venv}/bin/pip install --upgrade pip -q",
        f"{venv}/bin/pip install {PIP_DEPS} -q",
    ]
    for cmd in cmds:
        ssh_run(client, cmd, quiet=True, timeout=120)
    # Show installed packages
    out, _ = ssh_run(client, f"{venv}/bin/pip list --format=columns 2>/dev/null | head -20", quiet=True)
    client.close()
    print(f"  [OK] Venv ready at {venv}")
    return venv


def setup_service(name, host, user, remote_dir):
    """Create and enable systemd service on remote machine."""
    print(f"  Setting up systemd service: ajmain.service")
    venv = f"{remote_dir}/venv"
    service_content = SERVICE_TEMPLATE.format(
        name=name, user=user, remote_dir=remote_dir, venv=venv
    )
    client = connect(host, user)
    # Write service file
    ssh_run(client, f"echo '{service_content}' | sudo tee /etc/systemd/system/ajmain.service > /dev/null", quiet=True)
    ssh_run(client, "sudo systemctl daemon-reload", quiet=True)
    ssh_run(client, "sudo systemctl enable ajmain.service", quiet=True)
    client.close()
    print("  [OK] Service created and enabled")


def restart_service(host, user):
    """Restart the Flask service on remote machine."""
    print("  Restarting ajmain.service...")
    client = connect(host, user)
    ssh_run(client, "sudo systemctl restart ajmain.service", quiet=True)
    import time
    time.sleep(2)
    out, rc = ssh_run(client, "sudo systemctl is-active ajmain.service", quiet=True)
    client.close()
    if "active" in out:
        print("  [OK] Service is running")
    else:
        print(f"  [WARN] Service status: {out}")


def check_status(host, user):
    """Check service status on remote machine."""
    client = connect(host, user)
    out, _ = ssh_run(client, "sudo systemctl status ajmain.service --no-pager -l 2>&1 | head -15", quiet=True)
    print(out)
    client.close()


def run_command(host, user, command):
    """Run an arbitrary command on remote machine."""
    client = connect(host, user)
    out, rc = ssh_run(client, command)
    client.close()
    return rc


# ── Main ──

def main():
    parser = argparse.ArgumentParser(
        description="AJ Robotics — Universal Deploy Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m deploy.deploy --list                          Show all machines
  python -m deploy.deploy --to Christy                    Deploy all to Christy
  python -m deploy.deploy --to R1 --dirs robotics shared  Deploy specific dirs
  python -m deploy.deploy --to Christy --restart          Deploy + restart service
  python -m deploy.deploy --to Christy --setup            First-time setup
  python -m deploy.deploy --to R1 --cmd "uname -a"        Run command on R1
  python -m deploy.deploy --to Christy --status            Check service status
        """
    )
    parser.add_argument("--to", metavar="MACHINE", help="Target machine name")
    parser.add_argument("--list", action="store_true", help="List all machines")
    parser.add_argument("--dirs", nargs="+", metavar="DIR", help="Directories to deploy (default: all)")
    parser.add_argument("--files", nargs="+", metavar="FILE", help="Extra files to deploy")
    parser.add_argument("--restart", action="store_true", help="Restart ajmain service after deploy")
    parser.add_argument("--setup", action="store_true", help="First-time setup (venv + service)")
    parser.add_argument("--status", action="store_true", help="Check service status")
    parser.add_argument("--cmd", metavar="COMMAND", help="Run a command on the machine")
    parser.add_argument("--no-deploy", action="store_true", help="Skip file deployment")
    args = parser.parse_args()

    print()
    print("=" * 55)
    print("  AJ Robotics — Deploy Tool")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 55)

    if args.list:
        list_machines()
        return

    if not args.to:
        parser.print_help()
        return

    # Find target machine
    machines = load_hosts()
    name, info = find_machine(args.to, machines)
    if not info:
        print(f"\n  [ERROR] Machine '{args.to}' not found!")
        list_machines()
        return

    host = info["host"]
    user = info["username"]
    home = f"/home/{user}"
    remote_dir = f"{home}/AJMain"

    print(f"\n  Target: {name} ({user}@{host})")
    print()

    if not check_connection(host, user):
        sys.exit(1)

    # Run command
    if args.cmd:
        print(f"\n  Running: {args.cmd}\n")
        rc = run_command(host, user, args.cmd)
        sys.exit(rc)

    # Check status
    if args.status:
        print()
        check_status(host, user)
        return

    # First-time setup
    if args.setup:
        deploy_files(host, user, remote_dir, args.dirs, args.files)
        setup_venv(host, user, remote_dir)
        setup_service(name, host, user, remote_dir)
        restart_service(host, user)
        print(f"\n  Setup complete! Access at http://{host}:5000")
        return

    # Deploy files
    if not args.no_deploy:
        deploy_files(host, user, remote_dir, args.dirs, args.files)

    # Restart service
    if args.restart:
        restart_service(host, user)

    print()
    print("=" * 55)
    print("  Deploy complete!")
    if not args.restart:
        print("  Use --restart to restart the service")
    print("=" * 55)
    print()


if __name__ == "__main__":
    main()
