"""Finish restarting Christy app.py after sync."""
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
    if out: print("  " + out.encode("ascii", errors="replace").decode("ascii"))
    return out

# Verify Training link is in local copy
print("=== Verify updated files ===")
run("grep 'Training' ~/AJMain/gui/templates/index.html | head -3")

# Kill any remaining app.py
run("pkill -f 'python.*app.py' 2>/dev/null; echo done")
time.sleep(2)

# Start app.py using the venv python
print("\n=== Starting app.py ===")
run("cd /home/ajrobotics/AJMain && nohup /home/ajrobotics/AJMain/venv/bin/python app.py > /home/ajrobotics/ajmain.log 2>&1 & echo started")
time.sleep(4)

# Verify
print("\n=== Verify ===")
run("ps aux | grep app.py | grep -v grep")
run("curl -s http://localhost:5000/ | grep -o 'Training' | head -3")

ssh.close()
print("\nDone!")
