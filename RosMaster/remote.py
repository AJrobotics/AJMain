"""SSH remote execution helper for RosMaster X3."""

import subprocess
import sys
from pathlib import Path

from config import ROSMASTER_IP, ROSMASTER_USER, ROSMASTER_SSH_PORT


def ssh_cmd(command: str, timeout: int = 10) -> subprocess.CompletedProcess:
    """Run a command on RosMaster via SSH."""
    ssh_args = [
        "ssh",
        "-o", "ConnectTimeout=5",
        "-o", "StrictHostKeyChecking=no",
        "-p", str(ROSMASTER_SSH_PORT),
        f"{ROSMASTER_USER}@{ROSMASTER_IP}",
        command,
    ]
    return subprocess.run(ssh_args, capture_output=True, text=True, timeout=timeout)


def ssh_run(command: str, timeout: int = 10) -> str:
    """Run command on RosMaster, return stdout. Raises on failure."""
    result = ssh_cmd(command, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"SSH command failed: {result.stderr.strip()}")
    return result.stdout.strip()


def scp_to(local_path: str, remote_path: str) -> None:
    """Copy a file to RosMaster via SCP."""
    scp_args = [
        "scp",
        "-o", "ConnectTimeout=5",
        "-o", "StrictHostKeyChecking=no",
        "-P", str(ROSMASTER_SSH_PORT),
        local_path,
        f"{ROSMASTER_USER}@{ROSMASTER_IP}:{remote_path}",
    ]
    result = subprocess.run(scp_args, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"SCP failed: {result.stderr.strip()}")


def scp_from(remote_path: str, local_path: str) -> None:
    """Copy a file from RosMaster via SCP."""
    scp_args = [
        "scp",
        "-o", "ConnectTimeout=5",
        "-o", "StrictHostKeyChecking=no",
        "-P", str(ROSMASTER_SSH_PORT),
        f"{ROSMASTER_USER}@{ROSMASTER_IP}:{remote_path}",
        local_path,
    ]
    result = subprocess.run(scp_args, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"SCP failed: {result.stderr.strip()}")


def check_connection() -> bool:
    """Test SSH connectivity to RosMaster."""
    try:
        output = ssh_run("echo OK")
        return output == "OK"
    except Exception:
        return False


if __name__ == "__main__":
    print(f"Testing connection to RosMaster at {ROSMASTER_IP}...")
    if check_connection():
        print("Connected!")
        info = ssh_run("hostname; uname -m; cat /etc/nv_tegra_release 2>/dev/null || echo 'N/A'")
        print(f"Robot info:\n{info}")
    else:
        print("Connection failed. Run this first:")
        print(f'  ssh {ROSMASTER_USER}@{ROSMASTER_IP}')
        print(f"  Password: yahboom")
        print("Then set up key auth so no password is needed.")
        sys.exit(1)
