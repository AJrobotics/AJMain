"""Commit recent changes: combined mode fix, training nav link, gdrive setup."""
import subprocess, os

REPO = r"C:\Users\Dream\My Drive\AJ_Robotics\AJMain"
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

# Show current status
run(["status", "--short"])
run(["diff", "--stat"])
run(["log", "--oneline", "-3"])

# Stage changed files
run(["add", "gui/templates/index.html"])
run(["add", "gui/templates/training.html"])
run(["add", "gui/templates/vision.html"])
run(["add", "scripts/dreamer_training_service.py"])
run(["add", "scripts/setup_gdrive_christy.py"])
run(["add", "scripts/git_commit.py"])
run(["add", "scripts/git_commit2.py"])

# Show what will be committed
print("\n=== Staged changes ===")
run(["diff", "--cached", "--stat"])

# Commit
msg = """Add combined detection mode UI, training nav link, and Christy gdrive setup

- Combined Mode: added checkbox UI in Object Detection tab to run
  base yolov8n.pt + custom models simultaneously with merged results
- Fixed classification model crash in combined mode (skip non-detection models)
- Added model type field to available-models API (detection vs classification)
- Added Training link to dashboard nav bar and YOLO Training card on index page
- Added Training link to vision.html nav bar
- Created setup_gdrive_christy.py: installs rclone on Christy, creates mount
  helper script and systemd service for Google Drive access

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"""

run(["commit", "-m", msg])

print("\n=== Final status ===")
run(["log", "--oneline", "-5"])
run(["status", "--short"])
