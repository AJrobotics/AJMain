"""One-time script to create initial git commit."""
import subprocess, os, sys

REPO = r"C:\Users\Dream\AJMain"
GIT = r"c:\nrn901\mingw\usr\bin\git.exe"

os.environ["GIT_DIR"] = os.path.join(REPO, ".git")
os.environ["GIT_WORK_TREE"] = REPO

def run(args):
    cmd = [GIT] + args
    print(f">>> git {' '.join(args)}")
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO)
    if r.stdout: print(r.stdout.strip())
    if r.stderr: print(r.stderr.strip())
    return r.returncode

# Check if .git exists, if not init
git_dir = os.path.join(REPO, ".git")
if not os.path.isdir(git_dir):
    print("Initializing git repo...")
    run(["init", REPO])

# Set user info for this repo
run(["config", "user.email", "dream@ajrobotics.local"])
run(["config", "user.name", "Dream"])

# Stage files
run(["add", ".gitignore"])
run(["add", "app.py", "deploy_cashcow.py"])
run(["add", "scripts/"])
run(["add", "gui/"])
run(["add", "robotics/"])
run(["add", "trader/"])
run(["add", "deploy/"])
run(["add", "shared/"])
run(["add", "agent/"])
run(["add", "agent_modules/"])

# Add dataset configs (not images)
for f in ["datasets/red_pepper/data.yaml",
          "datasets/red_pepper/train/labels/",
          "datasets/red_pepper/val/labels/",
          "runs/red_pepper_20260321_183141/args.yaml",
          "runs/DLearnTest_20260321_180256/args.yaml"]:
    run(["add", f])

# Show status
run(["status"])

# Commit
msg = """Add YOLO training service with classification, detection, and combined mode

- Training service (dreamer_training_service.py) on port 5002:
  dataset management, YOLO training, object detection from R1 robot
- Training UI (training.html): 4 tabs (Datasets, Training, Results, Object Detection)
- Classification training: folder import, auto train/val split, metrics display
- Object detection: SSH capture from R1, inline labeling tool, custom model training
- Combined detection mode: base yolov8n.pt (80 classes) + custom models simultaneously
- Red pepper custom model trained (mAP50: 99.5%)
- All existing project files (robotics, trader, deploy, agent modules)

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"""

run(["commit", "-m", msg])
run(["log", "--oneline", "-3"])
