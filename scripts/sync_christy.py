"""Sync updated files to Christy and restart app.py."""
import paramiko, os, time

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
try:
    ssh.connect("192.168.1.94", username="ajrobotics",
                key_filename=os.path.expanduser("~/.ssh/id_rsa"), timeout=10)
except:
    ssh.connect("192.168.1.94", username="ajrobotics", timeout=10)

def run(cmd, timeout=20):
    print(f"$ {cmd}")
    _, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace").strip()
    if out: print("  " + out[:200])
    return out

# Sync from gdrive
print("=== Syncing templates from gdrive ===")
run("cp ~/gdrive/AJ_Robotics/AJMain/gui/templates/index.html ~/AJMain/gui/templates/index.html")
run("cp ~/gdrive/AJ_Robotics/AJMain/gui/templates/training.html ~/AJMain/gui/templates/training.html")
run("cp ~/gdrive/AJ_Robotics/AJMain/gui/templates/vision.html ~/AJMain/gui/templates/vision.html")
run("cp ~/gdrive/AJ_Robotics/AJMain/app.py ~/AJMain/app.py")

# Restart
print("\n=== Restarting app.py ===")
run("pkill -f 'python.*app.py' 2>/dev/null; echo killed")
time.sleep(2)
ssh.exec_command("cd /home/ajrobotics/AJMain && nohup /home/ajrobotics/AJMain/venv/bin/python app.py > /home/ajrobotics/ajmain.log 2>&1 &")
time.sleep(4)

# Reconnect and verify
ssh2 = paramiko.SSHClient()
ssh2.set_missing_host_key_policy(paramiko.AutoAddPolicy())
try:
    ssh2.connect("192.168.1.94", username="ajrobotics",
                 key_filename=os.path.expanduser("~/.ssh/id_rsa"), timeout=10)
except:
    ssh2.connect("192.168.1.94", username="ajrobotics", timeout=10)

_, stdout, _ = ssh2.exec_command("ps aux | grep app.py | grep -v grep", timeout=10)
out = stdout.read().decode().strip()
print(f"\napp.py running: {'YES' if out else 'NO'}")
if out: print(out[:150])

_, stdout, _ = ssh2.exec_command("curl -s http://localhost:5000/ | grep -c Training", timeout=10)
count = stdout.read().decode().strip()
print(f"Training references: {count}")

ssh2.close()
ssh.close()
print("\nDone!")
