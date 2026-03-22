"""Configure rclone on Christy with the OAuth token from Dreamer."""
import paramiko
import os
import json

CHRISTY_HOST = "192.168.1.94"
CHRISTY_USER = "ajrobotics"
SSH_KEY = os.path.expanduser("~/.ssh/id_rsa")

# The token obtained from 'rclone authorize "drive"' on Dreamer
# Run: rclone authorize "drive" on a machine with a browser, then paste the token here
TOKEN = os.environ.get("RCLONE_GDRIVE_TOKEN", "{}")  # Set via environment variable


def ssh_exec(ssh, cmd, timeout=30):
    print(f"  $ {cmd}")
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    if out:
        print("    " + out.encode("ascii", errors="replace").decode("ascii"))
    if err:
        print("    [stderr] " + err.encode("ascii", errors="replace").decode("ascii"))
    return out, err


def main():
    print("=" * 60)
    print("  Configuring rclone on Christy with Google Drive token")
    print("=" * 60)

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        ssh.connect(CHRISTY_HOST, username=CHRISTY_USER, key_filename=SSH_KEY, timeout=10)
    except Exception:
        ssh.connect(CHRISTY_HOST, username=CHRISTY_USER, timeout=10)

    print("  Connected to Christy!")

    # Create rclone config directory
    ssh_exec(ssh, "mkdir -p ~/.config/rclone")

    # Write rclone config file directly
    rclone_conf = f"""[gdrive]
type = drive
scope = drive
token = {TOKEN}
team_drive =
"""

    print("\n[1] Writing rclone.conf...")
    sftp = ssh.open_sftp()
    with sftp.file("/home/ajrobotics/.config/rclone/rclone.conf", "w") as f:
        f.write(rclone_conf)
    sftp.close()
    print("  Config written!")

    # Verify config
    print("\n[2] Verifying rclone remotes...")
    ssh_exec(ssh, "rclone listremotes")

    # Test access
    print("\n[3] Testing Google Drive access...")
    out, _ = ssh_exec(ssh, "rclone lsd gdrive: --max-depth 1 2>&1", timeout=30)

    if "AJ_Robotics" in out:
        print("\n  SUCCESS! Google Drive is accessible. AJ_Robotics folder found!")
    else:
        print("\n  Connected but checking for AJ_Robotics...")

    # Create mount point and mount
    print("\n[4] Mounting Google Drive...")
    ssh_exec(ssh, "mkdir -p ~/gdrive")
    ssh_exec(ssh, "fusermount -u ~/gdrive 2>/dev/null")  # unmount if already mounted
    ssh_exec(ssh, "rclone mount gdrive: ~/gdrive --vfs-cache-mode full --vfs-cache-max-age 1h --daemon --log-file=$HOME/rclone.log --log-level INFO", timeout=15)

    import time
    time.sleep(3)

    # Verify mount
    print("\n[5] Verifying mount...")
    ssh_exec(ssh, "mountpoint ~/gdrive && echo MOUNTED || echo NOT_MOUNTED")
    ssh_exec(ssh, "ls ~/gdrive/ 2>&1")

    # Check AJMain
    print("\n[6] Checking AJMain path...")
    ssh_exec(ssh, "ls ~/gdrive/AJ_Robotics/AJMain/ 2>&1")

    # Enable systemd service for auto-mount on boot
    print("\n[7] Enabling auto-mount on boot...")
    ssh_exec(ssh, "sudo systemctl daemon-reload 2>/dev/null")
    ssh_exec(ssh, "sudo systemctl enable rclone-gdrive 2>/dev/null")
    ssh_exec(ssh, "sudo systemctl start rclone-gdrive 2>/dev/null")

    print("\n" + "=" * 60)
    print("  Google Drive Setup Complete!")
    print("=" * 60)
    print("  Mount point: ~/gdrive")
    print("  AJMain:      ~/gdrive/AJ_Robotics/AJMain")
    print("  Auto-mount:  enabled via systemd")
    print()

    ssh.close()


if __name__ == "__main__":
    main()
