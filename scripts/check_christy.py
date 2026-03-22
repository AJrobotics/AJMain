"""Quick check if Christy app.py is running."""
import paramiko, os

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
try:
    ssh.connect("192.168.1.94", username="ajrobotics",
                key_filename=os.path.expanduser("~/.ssh/id_rsa"), timeout=10)
except:
    ssh.connect("192.168.1.94", username="ajrobotics", timeout=10)

# Check if app.py is running
_, stdout, _ = ssh.exec_command("ps aux | grep app.py | grep -v grep", timeout=10)
out = stdout.read().decode().strip()
if out:
    print("app.py is RUNNING:")
    print(out)
else:
    print("app.py NOT running, starting it...")
    ssh.exec_command("cd /home/ajrobotics/AJMain && nohup /home/ajrobotics/AJMain/venv/bin/python app.py > /home/ajrobotics/ajmain.log 2>&1 &")
    import time; time.sleep(4)
    _, stdout, _ = ssh.exec_command("ps aux | grep app.py | grep -v grep", timeout=10)
    out = stdout.read().decode().strip()
    print("Started:" if out else "FAILED to start")
    if out: print(out)

# Check if page has Training
_, stdout, _ = ssh.exec_command("curl -s http://localhost:5000/ 2>/dev/null | grep -c Training", timeout=10)
count = stdout.read().decode().strip()
print(f"\nTraining references in page: {count}")

ssh.close()
