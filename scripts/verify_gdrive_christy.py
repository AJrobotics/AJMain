"""Verify Google Drive mount on Christy."""
import paramiko, os

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
try:
    ssh.connect("192.168.1.94", username="ajrobotics",
                key_filename=os.path.expanduser("~/.ssh/id_rsa"), timeout=10)
except:
    ssh.connect("192.168.1.94", username="ajrobotics", timeout=10)

def run(cmd):
    print(f"$ {cmd}")
    _, stdout, stderr = ssh.exec_command(cmd, timeout=20)
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    if out: print("  " + out.encode("ascii", errors="replace").decode("ascii"))
    if err and "WARNING" not in err: print("  [err] " + err.encode("ascii", errors="replace").decode("ascii"))
    return out

# Check mount
run("mountpoint ~/gdrive 2>&1 || echo NOT_MOUNTED")
run("ls ~/gdrive/ 2>&1 | head -5")
run("ls ~/gdrive/AJ_Robotics/AJMain/ 2>&1 | head -10")

# Enable systemd
run("sudo systemctl daemon-reload 2>/dev/null")
run("sudo systemctl enable rclone-gdrive 2>/dev/null")

ssh.close()
print("\nDone!")
