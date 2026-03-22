"""Deploy Smart Trader code from Dreamer to CashCow via SSH/SCP.

Usage:
    python -m deploy.deploy_cashcow              # sync files + install deps
    python -m deploy.deploy_cashcow --run         # sync + start trader (ALERT mode)
    python -m deploy.deploy_cashcow --run --auto  # sync + start trader (AUTO mode)
    python -m deploy.deploy_cashcow --sync-only   # sync files only
    python -m deploy.deploy_cashcow --stop        # stop running trader
    python -m deploy.deploy_cashcow --status      # check if trader is running
    python -m deploy.deploy_cashcow --logs        # show recent trader logs
"""

import argparse
import os
import subprocess
import sys
from datetime import datetime

# CashCow connection info
REMOTE_USER = "dongchul"
REMOTE_HOST = "192.168.1.91"
REMOTE_DIR = "/home/dongchul/ib_smart_trader"
REMOTE_VENV = "/home/dongchul/trader_venv"

# Source: Smart Trader files
LOCAL_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADER_SOURCE = os.path.join(LOCAL_BASE, "trader", "ib_smart_trader")

SSH_OPTS = ["-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5"]

# Files to deploy
TRADER_FILES = [
    "run.py",
    "smart_trader.py",
    "auto_screener.py",
    "advanced_strategies.py",
    "signal_bridge.py",
    "portfolio_manager.py",
    "risk_shield.py",
    "tax_optimizer.py",
    "requirements.txt",
]


def ssh_cmd(command: str, quiet: bool = False) -> subprocess.CompletedProcess:
    """Execute a command on CashCow via SSH."""
    full_cmd = ["ssh"] + SSH_OPTS + [f"{REMOTE_USER}@{REMOTE_HOST}", command]
    if quiet:
        return subprocess.run(full_cmd, capture_output=True, text=True)
    else:
        return subprocess.run(full_cmd)


def scp_file(local_path: str, remote_path: str) -> int:
    """Copy a file to CashCow via SCP."""
    full_cmd = ["scp"] + SSH_OPTS + [local_path, f"{REMOTE_USER}@{REMOTE_HOST}:{remote_path}"]
    return subprocess.call(full_cmd)


def check_connection() -> bool:
    """Check if CashCow is reachable."""
    print(f"  Checking connection to CashCow ({REMOTE_HOST})...")
    result = ssh_cmd("echo ok", quiet=True)
    if result.returncode == 0:
        print(f"  [OK] Connected to CashCow")
        return True
    else:
        print(f"  [FAIL] Cannot reach CashCow at {REMOTE_HOST}")
        return False


def create_remote_dirs():
    """Create directory structure on CashCow."""
    print("  Creating remote directories...")
    ssh_cmd(f"mkdir -p {REMOTE_DIR}/logs", quiet=True)


def sync_files():
    """Copy Smart Trader files to CashCow."""
    print(f"  Syncing trader files -> CashCow:{REMOTE_DIR}/")

    synced = 0
    for fname in TRADER_FILES:
        local_path = os.path.join(TRADER_SOURCE, fname)
        if not os.path.exists(local_path):
            print(f"    [SKIP] {fname} (not found locally)")
            continue
        remote_path = f"{REMOTE_DIR}/{fname}"
        print(f"    {fname}")
        scp_file(local_path, remote_path)
        synced += 1

    print(f"  [OK] {synced} files synced")
    return synced


def check_local_files() -> bool:
    """Verify local trader files exist."""
    if not os.path.isdir(TRADER_SOURCE):
        print(f"  [ERROR] Trader source not found: {TRADER_SOURCE}")
        print(f"  Run the copy step first or check the path.")
        return False
    missing = [f for f in TRADER_FILES if not os.path.exists(os.path.join(TRADER_SOURCE, f))]
    if missing:
        print(f"  [WARN] Missing files: {', '.join(missing)}")
    return True


def install_deps():
    """Install Python dependencies on CashCow."""
    print("  Installing dependencies...")
    cmd = f"""
        if [ -d {REMOTE_VENV} ]; then
            source {REMOTE_VENV}/bin/activate && pip install -r {REMOTE_DIR}/requirements.txt -q
        else
            python3 -m venv {REMOTE_VENV} &&
            source {REMOTE_VENV}/bin/activate && pip install -r {REMOTE_DIR}/requirements.txt -q
        fi
    """
    ssh_cmd(cmd, quiet=True)
    print("  [OK] Dependencies installed")


def start_trader(auto: bool = False, port: int = 7497):
    """Start Smart Trader on CashCow (background with nohup)."""
    mode = "--auto" if auto else ""
    mode_name = "AUTO" if auto else "ALERT"
    print(f"  Starting Smart Trader on CashCow ({mode_name} mode, port {port})...")

    cmd = (
        f"source {REMOTE_VENV}/bin/activate && "
        f"cd {REMOTE_DIR} && "
        f"nohup python run.py {mode} --port {port} "
        f"> {REMOTE_DIR}/logs/trader_stdout.log 2>&1 &"
    )
    ssh_cmd(cmd, quiet=True)
    print(f"  [OK] Smart Trader started in background ({mode_name} mode)")
    print(f"  Logs: CashCow:{REMOTE_DIR}/logs/")


def stop_trader():
    """Stop Smart Trader on CashCow."""
    print("  Stopping Smart Trader on CashCow...")
    ssh_cmd("pkill -f 'python run.py' || echo 'Not running'", quiet=True)
    print("  [OK] Trader stopped")


def check_status():
    """Check if Smart Trader is running on CashCow."""
    print("  Checking trader status on CashCow...")
    result = ssh_cmd(
        "ps aux | grep 'python run.py' | grep -v grep || echo '  Trader is NOT running'",
        quiet=True
    )
    print(result.stdout or "  Trader is NOT running")

    print("\n  Recent log entries:")
    result = ssh_cmd(
        f"tail -10 {REMOTE_DIR}/smart_trader.log 2>/dev/null || echo '  No logs yet'",
        quiet=True
    )
    print(result.stdout or "  No logs yet")


def show_logs(lines: int = 30):
    """Show recent trader logs."""
    result = ssh_cmd(
        f"tail -{lines} {REMOTE_DIR}/smart_trader.log 2>/dev/null || echo 'No logs'",
        quiet=True
    )
    print(result.stdout or "No logs")


def main():
    parser = argparse.ArgumentParser(description="Deploy Smart Trader to CashCow")
    parser.add_argument("--run", action="store_true", help="Start trader after sync")
    parser.add_argument("--auto", action="store_true", help="Start in AUTO mode (executes trades)")
    parser.add_argument("--sync-only", action="store_true", help="Sync files only")
    parser.add_argument("--stop", action="store_true", help="Stop running trader")
    parser.add_argument("--status", action="store_true", help="Check trader status")
    parser.add_argument("--logs", action="store_true", help="Show recent logs")
    parser.add_argument("--port", type=int, default=7497, help="IB port (7497=paper, 7496=live)")
    args = parser.parse_args()

    print()
    print("=" * 50)
    print("  AJ Robotics — Deploy Smart Trader to CashCow")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)
    print()

    if not check_connection():
        sys.exit(1)

    if args.stop:
        stop_trader()
        return

    if args.status:
        check_status()
        return

    if args.logs:
        show_logs()
        return

    if not check_local_files():
        sys.exit(1)

    create_remote_dirs()
    sync_files()

    if not args.sync_only:
        install_deps()

    if args.run:
        stop_trader()  # stop old instance first
        start_trader(auto=args.auto, port=args.port)

    print()
    print("=" * 50)
    print("  Deploy complete!")
    if args.run:
        mode = "AUTO" if args.auto else "ALERT"
        print(f"  Trader running on CashCow ({mode} mode, port {args.port})")
    else:
        print("  Use --run to start the trader")
        print(f"  Or SSH: ssh {REMOTE_USER}@{REMOTE_HOST}")
    print("=" * 50)
    print()


if __name__ == "__main__":
    main()
