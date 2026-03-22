"""Check and restart Christy's app.py to pick up updated files from gdrive."""
import paramiko, os

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
try:
    ssh.connect("192.168.1.94", username="ajrobotics",
                key_filename=os.path.expanduser("~/.ssh/id_rsa"), timeout=10)
except:
    ssh.connect("192.168.1.94", username="ajrobotics", timeout=10)

def run(cmd, timeout=15):
    print(f"$ {cmd}")
    _, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    if out: print("  " + out.encode("ascii", errors="replace").decode("ascii"))
    if err and "WARNING" not in err: print("  [err] " + err.encode("ascii", errors="replace").decode("ascii"))
    return out

# Find running app.py process
print("=== Finding current app.py process ===")
run("ps aux | grep app.py | grep -v grep")

# Check which path it's running from
print("\n=== Checking AJMain locations ===")
run("ls ~/AJMain/app.py 2>/dev/null && echo 'LOCAL EXISTS' || echo 'NO LOCAL'")
run("ls ~/gdrive/AJ_Robotics/AJMain/app.py 2>/dev/null && echo 'GDRIVE EXISTS' || echo 'NO GDRIVE'")

# Check if nav has Training link in current running version
print("\n=== Check if current index.html has Training link ===")
run("grep -c 'Training' ~/AJMain/gui/templates/index.html 2>/dev/null || echo 'NOT FOUND in local'")
run("grep -c 'Training' ~/gdrive/AJ_Robotics/AJMain/gui/templates/index.html 2>/dev/null || echo 'NOT FOUND in gdrive'")

# Kill old app.py
print("\n=== Stopping old app.py ===")
run("pkill -f 'python.*app.py' 2>/dev/null; sleep 1; echo 'killed'")

# Copy updated files from gdrive to local AJMain
print("\n=== Syncing gdrive -> local AJMain ===")
run("cp ~/gdrive/AJ_Robotics/AJMain/gui/templates/index.html ~/AJMain/gui/templates/index.html 2>/dev/null")
run("cp ~/gdrive/AJ_Robotics/AJMain/gui/templates/training.html ~/AJMain/gui/templates/training.html 2>/dev/null")
run("cp ~/gdrive/AJ_Robotics/AJMain/gui/templates/vision.html ~/AJMain/gui/templates/vision.html 2>/dev/null")
run("cp ~/gdrive/AJ_Robotics/AJMain/app.py ~/AJMain/app.py 2>/dev/null")
run("cp -r ~/gdrive/AJ_Robotics/AJMain/scripts/ ~/AJMain/scripts/ 2>/dev/null")
run("cp -r ~/gdrive/AJ_Robotics/AJMain/shared/ ~/AJMain/shared/ 2>/dev/null")

# Verify Training link is now in local copy
print("\n=== Verify updated index.html ===")
run("grep 'Training' ~/AJMain/gui/templates/index.html 2>/dev/null || echo 'STILL NOT FOUND'")

# Restart app.py
print("\n=== Restarting app.py ===")
run("cd ~/AJMain && nohup python3 app.py > ~/ajmain.log 2>&1 &")

import time
time.sleep(3)

# Verify it's running
print("\n=== Verify app.py is running ===")
run("ps aux | grep app.py | grep -v grep")
run("curl -s http://localhost:5000/ | grep -o 'Training' | head -3")

ssh.close()
print("\nDone!")
