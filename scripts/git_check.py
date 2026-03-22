"""Check if key files have uncommitted changes."""
import subprocess, os

REPO = r"C:\Users\Dream\AJMain"
GIT = r"c:\nrn901\mingw\usr\bin\git.exe"
os.environ["GIT_DIR"] = os.path.join(REPO, ".git")
os.environ["GIT_WORK_TREE"] = REPO

def run(args):
    r = subprocess.run([GIT] + args, capture_output=True, text=True, cwd=REPO)
    return r.stdout.strip()

# Check if these files differ from last commit
for f in ["scripts/dreamer_training_service.py", "gui/templates/training.html"]:
    diff = run(["diff", "HEAD", "--", f])
    if diff:
        print(f"UNCOMMITTED CHANGES in {f}:")
        # Show stat only
        stat = run(["diff", "HEAD", "--stat", "--", f])
        print(f"  {stat}")
    else:
        print(f"OK (no changes): {f}")
