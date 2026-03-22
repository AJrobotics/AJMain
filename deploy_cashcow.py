"""
Deploy AJ Robotics agent to CashCow (192.168.1.91) via paramiko.
"""
import paramiko
import os
import stat
import time
import getpass

HOST = "192.168.1.91"
USERNAME = "dongchul"
REMOTE_BASE = "/home/dongchul/AJMain"
LOCAL_BASE = r"G:\My Drive\AJ_Robotics\AJMain"

# Files to transfer: (local_relative_path, remote_relative_path)
FILES_TO_TRANSFER = [
    ("agent/base_agent.py",            "agent/base_agent.py"),
    ("agent/start_agent.py",           "agent/start_agent.py"),
    ("agent/agent_config.json",        "agent/agent_config.json"),
    ("agent/__init__.py",              "agent/__init__.py"),
    ("agent_modules/trader_module.py", "agent_modules/trader_module.py"),
    ("agent_modules/__init__.py",      "agent_modules/__init__.py"),
    ("configs/hosts.json",             "configs/hosts.json"),
    ("gui/templates/agent_dashboard.html", "gui/templates/agent_dashboard.html"),
    ("gui/templates/gamepad_test.html",    "gui/templates/gamepad_test.html"),
    ("gui/static/.gitkeep",           "gui/static/.gitkeep"),
    ("shared/__init__.py",            "shared/__init__.py"),
    ("shared/monitor.py",             "shared/monitor.py"),
    ("shared/agent_client.py",        "shared/agent_client.py"),
    ("shared/heartbeat_responder.py", "shared/heartbeat_responder.py"),
    ("requirements_agent.txt",        "requirements_agent.txt"),
]

def mkdir_p(sftp, remote_dir):
    """Recursively create remote directories."""
    dirs_to_create = []
    d = remote_dir
    while True:
        try:
            sftp.stat(d)
            break
        except FileNotFoundError:
            dirs_to_create.append(d)
            d = os.path.dirname(d)  # go up
            if d == "/" or d == "":
                break
    for d in reversed(dirs_to_create):
        print(f"  mkdir {d}")
        sftp.mkdir(d)


def run_cmd(ssh, cmd, print_output=True, timeout=120):
    """Run a command via SSH and return stdout."""
    print(f"  $ {cmd}")
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    stdout.channel.settimeout(timeout)
    stderr.channel.settimeout(timeout)
    try:
        out = stdout.read().decode("utf-8", errors="replace")
    except Exception:
        out = ""
    try:
        err = stderr.read().decode("utf-8", errors="replace")
    except Exception:
        err = ""
    exit_code = stdout.channel.recv_exit_status()
    if print_output:
        if out.strip():
            safe_out = out.strip().encode("ascii", errors="replace").decode("ascii")
            # Truncate very long output
            if len(safe_out) > 2000:
                safe_out = safe_out[:1000] + "\n    ... (truncated) ...\n" + safe_out[-500:]
            print(f"    stdout: {safe_out}")
        if err.strip():
            safe_err = err.strip().encode("ascii", errors="replace").decode("ascii")
            if len(safe_err) > 2000:
                safe_err = safe_err[:1000] + "\n    ... (truncated) ...\n" + safe_err[-500:]
            print(f"    stderr: {safe_err}")
    return out, err, exit_code


def main():
    # --- Connect ---
    print(f"[1] Connecting to {HOST} as {USERNAME}...")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    connected = False
    # Try key-based auth first
    try:
        ssh.connect(HOST, username=USERNAME, timeout=10)
        print("  Connected via key-based auth.")
        connected = True
    except Exception as e:
        print(f"  Key auth failed: {e}")

    if not connected:
        # Try password
        pw = getpass.getpass(f"  Enter password for {USERNAME}@{HOST}: ")
        try:
            ssh.connect(HOST, username=USERNAME, password=pw, timeout=10)
            print("  Connected via password auth.")
            connected = True
        except Exception as e:
            print(f"  Password auth failed: {e}")
            return

    # --- Step 1: Check / create remote directory ---
    print(f"\n[2] Ensuring remote directory {REMOTE_BASE} exists...")
    run_cmd(ssh, f"mkdir -p {REMOTE_BASE}")

    # --- Step 2: SFTP files ---
    print(f"\n[3] Transferring files via SFTP...")
    sftp = ssh.open_sftp()

    for local_rel, remote_rel in FILES_TO_TRANSFER:
        local_path = os.path.join(LOCAL_BASE, local_rel.replace("/", os.sep))
        remote_path = f"{REMOTE_BASE}/{remote_rel}"

        if not os.path.exists(local_path):
            print(f"  SKIP (not found locally): {local_rel}")
            continue

        # Ensure remote directory exists
        remote_dir = os.path.dirname(remote_path).replace("\\", "/")
        mkdir_p(sftp, remote_dir)

        print(f"  PUT {local_rel} -> {remote_path}")
        sftp.put(local_path, remote_path)

    sftp.close()
    print("  File transfer complete.")

    # --- Step 3: Create venv and pip install ---
    print(f"\n[4] Setting up Python venv and installing dependencies...")
    run_cmd(ssh, f"python3 -m venv {REMOTE_BASE}/venv")
    run_cmd(ssh, f"{REMOTE_BASE}/venv/bin/pip install flask psutil paramiko", timeout=300)

    # --- Step 4: Kill existing agent on port 5000 ---
    print(f"\n[5] Killing any existing agent on port 5000...")
    # Find and kill python processes running start_agent
    run_cmd(ssh, "pkill -f 'python.*start_agent' || true")
    # Also kill anything on port 5000
    run_cmd(ssh, "fuser -k 5000/tcp 2>/dev/null || true")
    time.sleep(1)

    # --- Step 5: Start agent ---
    print(f"\n[6] Starting agent...")
    start_cmd = (
        f"cd {REMOTE_BASE} && "
        f"nohup {REMOTE_BASE}/venv/bin/python -m agent.start_agent > /tmp/agent.log 2>&1 &"
    )
    run_cmd(ssh, start_cmd)

    # --- Step 6: Wait and verify ---
    print(f"\n[7] Waiting 4 seconds for agent to start...")
    time.sleep(4)

    print(f"[8] Verifying agent status...")
    out, err, code = run_cmd(ssh, f"{REMOTE_BASE}/venv/bin/python -c \"import urllib.request; print(urllib.request.urlopen('http://localhost:5000/api/status').read().decode())\"")

    if code == 0 and out.strip():
        print(f"\n  SUCCESS - Agent is running on CashCow!")
        print(f"  Response: {out.strip()}")
    else:
        print(f"\n  Agent may not have started. Checking logs...")
        run_cmd(ssh, "tail -30 /tmp/agent.log")

    ssh.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
