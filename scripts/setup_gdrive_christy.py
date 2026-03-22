"""
Setup Google Drive access on Christy via rclone.

Steps:
1. Install rclone on Christy (if not installed)
2. Guide user through rclone config for Google Drive
3. Mount Google Drive to ~/gdrive

Usage: python scripts/setup_gdrive_christy.py
"""
import paramiko
import sys
import os

CHRISTY_HOST = "192.168.1.94"
CHRISTY_USER = "ajrobotics"
SSH_KEY = os.path.expanduser("~/.ssh/id_rsa")


def ssh_exec(ssh, cmd, timeout=30):
    """Execute command and return output."""
    print(f"  $ {cmd}")
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    if out:
        print(f"    {out}")
    if err and "WARNING" not in err:
        print(f"    [stderr] {err}")
    return out, err


def main():
    print("=" * 60)
    print("  Google Drive Setup for Christy (rclone)")
    print("=" * 60)

    # Connect to Christy
    print(f"\n[1] Connecting to Christy ({CHRISTY_HOST})...")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        ssh.connect(CHRISTY_HOST, username=CHRISTY_USER, key_filename=SSH_KEY, timeout=10)
    except Exception:
        try:
            ssh.connect(CHRISTY_HOST, username=CHRISTY_USER, timeout=10)
        except Exception as e:
            print(f"  ERROR: Cannot connect to Christy: {e}")
            print("  Make sure SSH key is set up or password auth is enabled.")
            sys.exit(1)

    print("  Connected!")

    # Check if rclone is installed
    print("\n[2] Checking rclone installation...")
    out, _ = ssh_exec(ssh, "which rclone 2>/dev/null || echo NOT_FOUND")

    if "NOT_FOUND" in out:
        print("  rclone not found. Installing...")
        ssh_exec(ssh, "curl https://rclone.org/install.sh | sudo bash", timeout=120)
        out, _ = ssh_exec(ssh, "which rclone 2>/dev/null || echo STILL_NOT_FOUND")
        if "STILL_NOT_FOUND" in out:
            print("  ERROR: rclone installation failed.")
            print("  Try manually: sudo apt install rclone")
            ssh.close()
            sys.exit(1)
        print("  rclone installed successfully!")
    else:
        print(f"  rclone found at: {out}")

    # Check rclone version
    ssh_exec(ssh, "rclone version | head -1")

    # Check if gdrive remote already configured
    print("\n[3] Checking existing rclone remotes...")
    out, _ = ssh_exec(ssh, "rclone listremotes 2>/dev/null")

    if "gdrive:" in out:
        print("  'gdrive' remote already configured!")
        # Test access
        print("\n[4] Testing Google Drive access...")
        out, err = ssh_exec(ssh, "rclone lsd gdrive: --max-depth 1 2>&1 | head -10", timeout=30)
        if "AJ_Robotics" in out:
            print("  Google Drive access working! AJ_Robotics folder found.")
        elif "error" in out.lower() or "error" in err.lower():
            print("  Access test failed. You may need to re-authorize.")
            print("  Run on Christy: rclone config reconnect gdrive:")
        else:
            print("  Connected but AJ_Robotics folder not found in root.")
    else:
        print("  No 'gdrive' remote configured yet.")
        print()
        print("=" * 60)
        print("  MANUAL SETUP REQUIRED")
        print("=" * 60)
        print()
        print("  Since rclone needs browser-based OAuth, you need to")
        print("  run the config on a machine with a browser first.")
        print()
        print("  Option A: Headless setup (recommended)")
        print("  -----------------------------------------")
        print("  1. On Dreamer (this PC), run:")
        print("       rclone authorize \"drive\"")
        print("     This opens a browser for Google login.")
        print("     Copy the token it gives you.")
        print()
        print("  2. Then SSH into Christy and run:")
        print("       rclone config")
        print("     - Name: gdrive")
        print("     - Type: drive (Google Drive)")
        print("     - client_id: (leave blank)")
        print("     - client_secret: (leave blank)")
        print("     - scope: 1 (Full access)")
        print("     - Auto config? No")
        print("     - Paste the token from step 1")
        print()
        print("  Option B: Mount after config")
        print("  -----------------------------------------")
        print("  After configuring, create mount point and systemd service:")
        print("       mkdir -p ~/gdrive")
        print("       rclone mount gdrive: ~/gdrive --vfs-cache-mode full --daemon")
        print()

    # Create mount helper script on Christy
    print("\n[5] Creating mount helper script on Christy...")
    mount_script = """#!/bin/bash
# Mount Google Drive via rclone
# Usage: ~/mount_gdrive.sh [mount|unmount|status]

MOUNT_POINT="$HOME/gdrive"
REMOTE="gdrive:"

case "${1:-mount}" in
    mount)
        if mountpoint -q "$MOUNT_POINT" 2>/dev/null; then
            echo "Already mounted at $MOUNT_POINT"
        else
            mkdir -p "$MOUNT_POINT"
            rclone mount "$REMOTE" "$MOUNT_POINT" \\
                --vfs-cache-mode full \\
                --vfs-cache-max-age 1h \\
                --vfs-read-chunk-size 16M \\
                --buffer-size 32M \\
                --daemon \\
                --log-file="$HOME/rclone.log" \\
                --log-level INFO
            sleep 2
            if mountpoint -q "$MOUNT_POINT" 2>/dev/null; then
                echo "Mounted at $MOUNT_POINT"
                ls "$MOUNT_POINT"
            else
                echo "Mount failed. Check ~/rclone.log"
            fi
        fi
        ;;
    unmount|umount)
        fusermount -u "$MOUNT_POINT" 2>/dev/null || umount "$MOUNT_POINT" 2>/dev/null
        echo "Unmounted"
        ;;
    status)
        if mountpoint -q "$MOUNT_POINT" 2>/dev/null; then
            echo "Mounted at $MOUNT_POINT"
            df -h "$MOUNT_POINT"
        else
            echo "Not mounted"
        fi
        ;;
    *)
        echo "Usage: $0 {mount|unmount|status}"
        ;;
esac
"""
    # Write script to Christy
    sftp = ssh.open_sftp()
    with sftp.file("/home/ajrobotics/mount_gdrive.sh", "w") as f:
        f.write(mount_script)
    sftp.close()
    ssh_exec(ssh, "chmod +x ~/mount_gdrive.sh")
    print("  Created ~/mount_gdrive.sh on Christy")

    # Create systemd service for auto-mount
    print("\n[6] Creating systemd service for auto-mount...")
    service = """[Unit]
Description=Mount Google Drive via rclone
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ajrobotics
ExecStart=/usr/bin/rclone mount gdrive: /home/ajrobotics/gdrive --vfs-cache-mode full --vfs-cache-max-age 1h --vfs-read-chunk-size 16M --buffer-size 32M --log-file=/home/ajrobotics/rclone.log --log-level INFO
ExecStop=/bin/fusermount -u /home/ajrobotics/gdrive
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
"""
    sftp = ssh.open_sftp()
    with sftp.file("/home/ajrobotics/rclone-gdrive.service", "w") as f:
        f.write(service)
    sftp.close()

    # Install service (needs sudo)
    ssh_exec(ssh, "sudo cp ~/rclone-gdrive.service /etc/systemd/system/rclone-gdrive.service 2>/dev/null")
    print("  Service file created. After rclone config, enable with:")
    print("    sudo systemctl enable rclone-gdrive")
    print("    sudo systemctl start rclone-gdrive")

    print("\n" + "=" * 60)
    print("  Setup Complete!")
    print("=" * 60)
    print(f"  Mount script: ~/mount_gdrive.sh")
    print(f"  Mount point:  ~/gdrive")
    print(f"  AJMain path:  ~/gdrive/AJ_Robotics/AJMain")
    print()

    ssh.close()


if __name__ == "__main__":
    main()
