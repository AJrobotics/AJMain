"""
Local HTTP server for Simulation + NN Training pipeline on Dreamer.

Serves mapping_debug.html and provides API endpoints to run training steps:
  Step 1: Generate training maps
  Step 2: Train PPO policy
  Step 3: Evaluate trained model
  Step 4: (Browser simulation — no API needed)
  Step 5: Deploy model to Jetson

Usage:
    python serve_local.py
    Then open: http://localhost:8080/static/mapping_debug.html
    Select 'Simulation' from the dropdown.

Press Ctrl+C to stop.
"""

import http.server
import json
import os
import subprocess
import sys
import threading
import time
import glob

PORT = 8080
JETSON_HOST = "192.168.1.99:8080"  # Robot's Tornado server for live data/recordings

# Agent hosts for comm test proxy
AGENT_HOSTS = {
    "dreamer": ["localhost:5000", "localhost:5001"],  # agent, then standalone xbee service
    "christy": ["192.168.1.94:5000"],
}
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.join(ROOT_DIR, "jetson", "web_ui")
TRAINING_DIR = os.path.join(ROOT_DIR, "training")
MODELS_DIR = os.path.join(TRAINING_DIR, "models")
MAPS_DIR = os.path.join(TRAINING_DIR, "maps")

# Track running subprocess
_process = None        # subprocess.Popen
_process_log = []      # captured output lines
_process_label = ""    # "generate" / "train" / "evaluate" / "deploy"
_process_lock = threading.Lock()
_train_target = 0      # target timesteps for training
_train_current = 0     # current timesteps (parsed from PPO output)
_train_reward = ""     # latest ep_rew_mean


def _run_async(label, cmd, cwd=None):
    """Run a command in a background thread, capture output."""
    global _process, _process_log, _process_label

    with _process_lock:
        if _process and _process.poll() is None:
            return False, f"Already running: {_process_label}"
        _process_log = []
        _process_label = label

    def _worker():
        global _process, _train_current, _train_reward
        try:
            env = os.environ.copy()
            env['PYTHONIOENCODING'] = 'utf-8'
            _process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                cwd=cwd or ROOT_DIR, text=True, bufsize=1,
                encoding='utf-8', errors='replace', env=env
            )
            for line in _process.stdout:
                line = line.rstrip()
                _process_log.append(line)
                # Parse PPO progress: "| time/total_timesteps  | 2048  |"
                if 'total_timesteps' in line and '|' in line:
                    try:
                        _train_current = int(line.split('|')[2].strip())
                    except (IndexError, ValueError):
                        pass
                # Parse reward: "| rollout/ep_rew_mean   | 12.5  |"
                if 'ep_rew_mean' in line and '|' in line:
                    try:
                        _train_reward = line.split('|')[2].strip()
                    except (IndexError, ValueError):
                        pass
                # Keep only last 200 lines
                if len(_process_log) > 200:
                    _process_log.pop(0)
            _process.wait()
        except Exception as e:
            _process_log.append(f"ERROR: {e}")

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return True, "Started"


def _get_status():
    """Get current process status + log."""
    running = _process is not None and _process.poll() is None
    exit_code = None if running else (_process.returncode if _process else None)

    # Check what models/maps exist
    n_maps = len(glob.glob(os.path.join(MAPS_DIR, "*.npz")))

    # Check models per task
    tasks = ["explore", "floor_plan", "wall_follow"]
    models_info = {}
    for t in tasks:
        task_dir = os.path.join(MODELS_DIR, t)
        models_info[t] = {
            "onnx": os.path.exists(os.path.join(task_dir, f"{t}_policy.onnx")),
            "pt": os.path.exists(os.path.join(task_dir, f"{t}_policy.pt")),
            "checkpoints": len(glob.glob(os.path.join(task_dir, f"{t}_ppo_*_steps.zip"))),
        }

    # Legacy check (old single-model files)
    onnx_exists = os.path.exists(os.path.join(MODELS_DIR, "nav_policy.onnx"))
    pt_exists = os.path.exists(os.path.join(MODELS_DIR, "nav_policy.pt"))
    n_checkpoints = sum(m["checkpoints"] for m in models_info.values())

    return {
        "running": running,
        "label": _process_label,
        "exit_code": exit_code,
        "log": _process_log[-30:],
        "maps_count": n_maps,
        "checkpoints": n_checkpoints,
        "onnx_ready": onnx_exists or any(m["onnx"] for m in models_info.values()),
        "pt_ready": pt_exists or any(m["pt"] for m in models_info.values()),
        "models": models_info,
        "train_target": _train_target,
        "train_current": _train_current,
        "train_reward": _train_reward,
    }


class LocalHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE_DIR, **kwargs)

    def do_GET(self):
        # Root redirect
        if self.path == '/':
            self.send_response(302)
            self.send_header('Location', '/static/mapping_debug.html')
            self.end_headers()
            return

        # Training status API
        if self.path == '/api/train/status':
            self._json_response(_get_status())
            return

        # Comm test: proxy to agent APIs (Dreamer/Christy)
        if self.path.startswith('/api/comm/'):
            self._proxy_comm('GET')
            return

        # Proxy robot API calls to Jetson if reachable, else stub
        if self.path.startswith('/api/') and not self.path.startswith('/api/train/'):
            try:
                import urllib.request
                jetson_url = f"http://{JETSON_HOST}{self.path}"
                req = urllib.request.Request(jetson_url, headers={'Accept': 'application/json'})
                with urllib.request.urlopen(req, timeout=3) as resp:
                    data = resp.read()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(data)
            except Exception:
                # Jetson not reachable — return stubs
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                if 'grid_data' in self.path:
                    self.wfile.write(b'{"error": "offline"}')
                elif 'stats' in self.path:
                    self.wfile.write(b'{"scan_count":0,"pose":[15000,15000,0],"coverage_pct":"0","loop_closures":0,"explored_cells":0,"grid_size":600}')
                elif 'wall_lines' in self.path:
                    self.wfile.write(b'{"lines":[]}')
                elif 'list_maps' in self.path:
                    self.wfile.write(b'{"maps":[]}')
                elif 'recordings' in self.path:
                    self.wfile.write(b'{"recordings":[]}')
                else:
                    self.wfile.write(b'{"ok":true}')
            return

        # Proxy recording video files from Jetson
        if self.path.startswith('/recordings/'):
            try:
                import urllib.request
                jetson_url = f"http://{JETSON_HOST}{self.path}"
                req = urllib.request.Request(jetson_url)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = resp.read()
                    self.send_response(200)
                    self.send_header('Content-Type', 'video/mp4')
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
            except Exception:
                self.send_response(404)
                self.end_headers()
            return

        # Serve ONNX model files
        if self.path.startswith('/models/'):
            fname = self.path.replace('/models/', '')
            model_path = os.path.join(MODELS_DIR, fname)
            if os.path.exists(model_path):
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                size = os.path.getsize(model_path)
                self.send_header('Content-Length', str(size))
                self.end_headers()
                with open(model_path, 'rb') as f:
                    self.wfile.write(f.read())
            else:
                self.send_response(404)
                self.end_headers()
            return

        super().do_GET()

    def do_POST(self):
        # Comm test: proxy to agent APIs
        if self.path.startswith('/api/comm/'):
            self._proxy_comm('POST')
            return

        # Training pipeline API
        if self.path == '/api/train/generate':
            content_len = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(content_len)) if content_len else {}
            count = body.get('count', 30)
            seed = body.get('seed', 42)
            ok, msg = _run_async("generate", [
                sys.executable, os.path.join(TRAINING_DIR, "map_generator.py"),
                "--output", MAPS_DIR,
                "--count", str(count),
                "--seed", str(seed),
            ])
            self._json_response({"ok": ok, "message": msg})
            return

        if self.path == '/api/train/train':
            global _train_target, _train_current, _train_reward
            content_len = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(content_len)) if content_len else {}
            timesteps = body.get('timesteps', 100000)
            task = body.get('task', 'explore')
            fresh = body.get('fresh', False)
            _train_target = timesteps
            _train_current = 0
            _train_reward = ""
            cmd = [
                sys.executable, os.path.join(TRAINING_DIR, "train.py"),
                "--task", task,
                "--timesteps", str(timesteps),
                "--maps", MAPS_DIR,
                "--export",
                "--curriculum-interval", "10",
            ]
            # Auto-resume from latest checkpoint (unless fresh=true)
            task_model_dir = os.path.join(MODELS_DIR, task)
            if not fresh:
                checkpoints = sorted(glob.glob(os.path.join(task_model_dir, f"{task}_ppo_*_steps.zip")))
                if checkpoints:
                    latest = checkpoints[-1].replace(".zip", "")
                    cmd.extend(["--resume", latest])
            ok, msg = _run_async("train", cmd)
            self._json_response({"ok": ok, "message": msg})
            return

        if self.path == '/api/train/evaluate':
            model_path = os.path.join(MODELS_DIR, "nav_ppo_final")
            if not os.path.exists(model_path + ".zip"):
                self._json_response({"ok": False, "message": "No trained model found. Train first."})
                return
            ok, msg = _run_async("evaluate", [
                sys.executable, os.path.join(TRAINING_DIR, "evaluate.py"),
                "--model", model_path,
                "--maps", MAPS_DIR,
                "--episodes", "5",
                "--compare-rule-based",
            ])
            self._json_response({"ok": ok, "message": msg})
            return

        if self.path == '/api/train/deploy':
            pt_path = os.path.join(MODELS_DIR, "nav_policy.pt")
            onnx_path = os.path.join(MODELS_DIR, "nav_policy.onnx")
            if not os.path.exists(pt_path):
                self._json_response({"ok": False, "message": "No nav_policy.pt found. Train with --export first."})
                return
            # Deploy model + code to Jetson
            ok, msg = _run_async("deploy", [
                sys.executable, os.path.join(ROOT_DIR, "deploy.py"),
            ])
            # Also copy model file
            if ok:
                _process_log.append("Will also scp nav_policy.pt to Jetson...")
                def _scp():
                    time.sleep(2)
                    subprocess.run([
                        "scp", pt_path,
                        "jetson@192.168.1.99:/home/jetson/RosMaster/models/"
                    ], capture_output=True, text=True)
                    _process_log.append("Model deployed to Jetson.")
                    if os.path.exists(onnx_path):
                        subprocess.run([
                            "scp", onnx_path,
                            "jetson@192.168.1.99:/home/jetson/RosMaster/models/"
                        ], capture_output=True, text=True)
                        _process_log.append("ONNX model deployed to Jetson.")
                threading.Thread(target=_scp, daemon=True).start()
            self._json_response({"ok": ok, "message": msg})
            return

        if self.path == '/api/train/stop':
            global _process
            if _process and _process.poll() is None:
                _process.terminate()
                self._json_response({"ok": True, "message": f"Stopped {_process_label}"})
            else:
                self._json_response({"ok": False, "message": "Nothing running"})
            return

        # Proxy other POST API calls to Jetson
        if self.path.startswith('/api/') and not self.path.startswith('/api/train/'):
            try:
                import urllib.request
                content_len = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_len) if content_len else b''
                jetson_url = f"http://{JETSON_HOST}{self.path}"
                req = urllib.request.Request(jetson_url, data=body, headers={
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                }, method='POST')
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = resp.read()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(data)
            except Exception as e:
                self._json_response({"ok": False, "error": str(e)})
            return

        # Default POST handler
        self._json_response({"ok": False, "error": "Unknown endpoint"})

    def do_OPTIONS(self):
        """Handle CORS preflight for comm test proxy."""
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def _proxy_comm(self, method):
        """Proxy /api/comm/<machine>/<subpath> to the machine's agent API."""
        import urllib.request
        # Parse: /api/comm/dreamer/heartbeat/status -> machine=dreamer, subpath=api/heartbeat/status
        parts = self.path.split('/')  # ['', 'api', 'comm', 'dreamer', 'heartbeat', 'status']
        if len(parts) < 5:
            self._json_response({"ok": False, "error": "Invalid comm path"})
            return
        machine = parts[3]
        subpath = '/'.join(parts[4:])  # 'heartbeat/status' -> 'api/heartbeat/status'
        api_path = '/api/' + subpath

        hosts = AGENT_HOSTS.get(machine)
        if not hosts:
            self._json_response({"ok": False, "error": f"Unknown machine: {machine}"})
            return

        # Read POST body if needed
        body = None
        if method == 'POST':
            content_len = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_len) if content_len else b''

        # Try each host (fallback for Dreamer: agent:5000, then xbee-service:5001)
        last_error = None
        for host in hosts:
            try:
                url = f"http://{host}{api_path}"
                req = urllib.request.Request(url, data=body, headers={
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                }, method=method)
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = resp.read()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(data)
                    return
            except Exception as e:
                last_error = str(e)
                continue

        self._json_response({"ok": False, "error": f"{machine} unreachable: {last_error}"})

    def _json_response(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, format, *args):
        msg = str(args)
        if '404' in msg or 'favicon' in msg or '/api/train/status' in msg:
            return
        super().log_message(format, *args)


if __name__ == "__main__":
    os.makedirs(MODELS_DIR, exist_ok=True)
    os.makedirs(MAPS_DIR, exist_ok=True)

    print(f"=" * 55)
    print(f"  RosMaster Simulation + Training Server (Local)")
    print(f"=" * 55)
    print(f"  URL: http://localhost:{PORT}/static/mapping_debug.html")
    print(f"  Serving from: {BASE_DIR}")
    print(f"  Training dir: {TRAINING_DIR}")
    print(f"  Select 'Simulation' from the dropdown")
    print(f"  Press Ctrl+C to stop")
    print(f"=" * 55)

    with http.server.HTTPServer(("", PORT), LocalHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nServer stopped.")
