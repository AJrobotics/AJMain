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

# Use Python 3.12 for training (has torch/sb3 installed)
TRAIN_PYTHON = os.environ.get("TRAIN_PYTHON", r"C:\Users\Dream\AppData\Local\Programs\Python\Python312\python.exe")
if not os.path.exists(TRAIN_PYTHON):
    TRAIN_PYTHON = sys.executable  # fallback to default

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

# Track running subprocesses
# General process (generate, evaluate, deploy) — single at a time
_process = None        # subprocess.Popen
_process_log = []      # captured output lines
_process_label = ""    # "generate" / "evaluate" / "deploy"
_process_lock = threading.Lock()

# Per-task training processes (can run in parallel)
_train_processes = {}  # task_name -> {process, log, target, current, reward}
_train_lock = threading.Lock()


def _run_async(label, cmd, cwd=None):
    """Run a non-training command in a background thread, capture output."""
    global _process, _process_log, _process_label

    with _process_lock:
        if _process and _process.poll() is None:
            return False, f"Already running: {_process_label}"
        _process_log = []
        _process_label = label

    def _worker():
        global _process
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
                if len(_process_log) > 200:
                    _process_log.pop(0)
            _process.wait()
        except Exception as e:
            _process_log.append(f"ERROR: {e}")

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return True, "Started"


def _run_train_async(task, cmd, target_steps, cwd=None):
    """Run a training process for a specific task. Multiple tasks can train in parallel."""
    with _train_lock:
        if task in _train_processes:
            tp = _train_processes[task]
            if tp["process"] and tp["process"].poll() is None:
                return False, f"Already training: {task}"

        _train_processes[task] = {
            "process": None,
            "log": [],
            "target": target_steps,
            "current": 0,
            "reward": "",
        }

    def _worker():
        tp = _train_processes[task]
        try:
            env = os.environ.copy()
            env['PYTHONIOENCODING'] = 'utf-8'
            tp["process"] = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                cwd=cwd or ROOT_DIR, text=True, bufsize=1,
                encoding='utf-8', errors='replace', env=env
            )
            for line in tp["process"].stdout:
                line = line.rstrip()
                tp["log"].append(line)
                if 'total_timesteps' in line and '|' in line:
                    try:
                        tp["current"] = int(line.split('|')[2].strip())
                    except (IndexError, ValueError):
                        pass
                if 'ep_rew_mean' in line and '|' in line:
                    try:
                        tp["reward"] = line.split('|')[2].strip()
                    except (IndexError, ValueError):
                        pass
                if len(tp["log"]) > 200:
                    tp["log"].pop(0)
            tp["process"].wait()
        except Exception as e:
            tp["log"].append(f"ERROR: {e}")

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return True, "Started"


def _get_status(task=None):
    """Get current process status + log.
    If task is specified, return that task's training status.
    """
    running = _process is not None and _process.poll() is None
    exit_code = None if running else (_process.returncode if _process else None)

    # Check what models/maps exist
    n_maps = len(glob.glob(os.path.join(MAPS_DIR, "*.npz")))

    # Check models per task
    tasks = ["explore", "floor_plan", "complete_map", "wall_follow", "wall_confirm", "gap_fill"]
    models_info = {}
    for t in tasks:
        task_dir = os.path.join(MODELS_DIR, t)
        tp = _train_processes.get(t)
        tp_running = tp and tp["process"] and tp["process"].poll() is None if tp else False
        tp_exit = None
        if tp and tp["process"] and not tp_running:
            tp_exit = tp["process"].returncode
        models_info[t] = {
            "onnx": os.path.exists(os.path.join(task_dir, f"{t}_policy.onnx")),
            "pt": os.path.exists(os.path.join(task_dir, f"{t}_policy.pt")),
            "checkpoints": len(glob.glob(os.path.join(task_dir, f"{t}_ppo_*_steps.zip"))),
            "training": tp_running,
            "train_target": tp["target"] if tp else 0,
            "train_current": tp["current"] if tp else 0,
            "train_reward": tp["reward"] if tp else "",
            "train_exit": tp_exit,
        }

    # Legacy check (old single-model files)
    onnx_exists = os.path.exists(os.path.join(MODELS_DIR, "nav_policy.onnx"))
    pt_exists = os.path.exists(os.path.join(MODELS_DIR, "nav_policy.pt"))
    n_checkpoints = sum(m["checkpoints"] for m in models_info.values())

    # Per-task log and progress (for the selected task or first running)
    sel_task = task
    if not sel_task:
        for t in tasks:
            if models_info[t]["training"]:
                sel_task = t
                break
    tp = _train_processes.get(sel_task) if sel_task else None
    train_log = tp["log"][-30:] if tp else []
    train_target = tp["target"] if tp else 0
    train_current = tp["current"] if tp else 0
    train_reward = tp["reward"] if tp else ""
    any_training = any(m["training"] for m in models_info.values())

    return {
        "running": running or any_training,
        "label": _process_label if running else (f"train:{sel_task}" if any_training else _process_label),
        "exit_code": exit_code if running or not any_training else None,
        "log": train_log if any_training else _process_log[-30:],
        "maps_count": n_maps,
        "checkpoints": n_checkpoints,
        "onnx_ready": onnx_exists or any(m["onnx"] for m in models_info.values()),
        "pt_ready": pt_exists or any(m["pt"] for m in models_info.values()),
        "models": models_info,
        "train_target": train_target,
        "train_current": train_current,
        "train_reward": train_reward,
    }


class LocalHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE_DIR, **kwargs)

    def end_headers(self):
        # Disable caching for all responses
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        super().end_headers()

    def do_GET(self):
        # Root redirect
        if self.path == '/':
            self.send_response(302)
            self.send_header('Location', '/static/mapping_debug.html')
            self.end_headers()
            return

        # Training status API
        if self.path.startswith('/api/train/status'):
            # Parse ?task=floor_plan from query string
            task_param = None
            if '?' in self.path:
                from urllib.parse import parse_qs, urlparse
                qs = parse_qs(urlparse(self.path).query)
                task_param = qs.get('task', [None])[0]
            self._json_response(_get_status(task=task_param))
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

        # Serve route files (frames, predictions)
        if self.path.startswith('/routes/'):
            fname = self.path.replace('/routes/', '')
            route_path = os.path.join(ROOT_DIR, "routes", fname)
            if os.path.exists(route_path) and os.path.isfile(route_path):
                self.send_response(200)
                if route_path.endswith('.json'):
                    self.send_header('Content-Type', 'application/json')
                elif route_path.endswith('.jpg'):
                    self.send_header('Content-Type', 'image/jpeg')
                else:
                    self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Access-Control-Allow-Origin', '*')
                size = os.path.getsize(route_path)
                self.send_header('Content-Length', str(size))
                self.end_headers()
                with open(route_path, 'rb') as f:
                    self.wfile.write(f.read())
            else:
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

        # Floor plan processing API
        if self.path == '/api/floor_plan/process':
            content_len = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(content_len)) if content_len else {}
            grid_flat = body.get('grid')
            grid_size = body.get('grid_size', 600)
            cell_size = body.get('cell_size_mm', 50)
            if not grid_flat:
                self._json_response({"ok": False, "error": "No grid data"})
                return
            try:
                import numpy as np
                from training.floor_plan_processor import process_grid
                grid = np.array(grid_flat, dtype=np.float32).reshape(grid_size, grid_size)
                fp = process_grid(grid, cell_size_mm=cell_size)
                result = fp.to_json()
                result['ok'] = True
                self._json_response(result)
            except Exception as e:
                import traceback
                self._json_response({"ok": False, "error": str(e),
                                     "trace": traceback.format_exc()})
            return

        if self.path == '/api/floor_plan/svg':
            content_len = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(content_len)) if content_len else {}
            grid_flat = body.get('grid')
            grid_size = body.get('grid_size', 600)
            cell_size = body.get('cell_size_mm', 50)
            if not grid_flat:
                self._json_response({"ok": False, "error": "No grid data"})
                return
            try:
                import numpy as np
                import tempfile
                from training.floor_plan_processor import process_grid
                grid = np.array(grid_flat, dtype=np.float32).reshape(grid_size, grid_size)
                fp = process_grid(grid, cell_size_mm=cell_size)
                # Write SVG to temp file and return contents
                svg_path = os.path.join(tempfile.gettempdir(), 'rosmaster_floor_plan.svg')
                fp.to_svg(svg_path)
                with open(svg_path, 'r') as f:
                    svg_content = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'image/svg+xml')
                self.send_header('Content-Disposition', 'attachment; filename="floor_plan.svg"')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(svg_content.encode('utf-8'))
            except Exception as e:
                self._json_response({"ok": False, "error": str(e)})
            return

        # Route nav corrections API
        if self.path == '/api/route_nav/corrections':
            content_len = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(content_len)) if content_len else {}
            route = body.get('route', '')
            corrs = body.get('corrections', {})
            if not route or not corrs:
                self._json_response({"ok": False, "error": "No route or corrections"})
                return
            try:
                corr_dir = os.path.join(ROOT_DIR, 'routes', route, 'corrections')
                os.makedirs(corr_dir, exist_ok=True)
                corr_path = os.path.join(corr_dir, 'corrections.json')
                # Merge with existing
                existing = {}
                if os.path.exists(corr_path):
                    with open(corr_path) as f:
                        existing = json.load(f)
                existing.update(corrs)
                with open(corr_path, 'w') as f:
                    json.dump(existing, f, indent=2)
                self._json_response({"ok": True, "saved": len(existing)})
            except Exception as e:
                self._json_response({"ok": False, "error": str(e)})
            return

        if self.path == '/api/route_nav/retrain':
            content_len = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(content_len)) if content_len else {}
            route = body.get('route', '')
            # Find all routes with corrections or use all routes
            routes_dir = os.path.join(ROOT_DIR, 'routes')
            all_routes = [d for d in os.listdir(routes_dir)
                         if os.path.isdir(os.path.join(routes_dir, d))
                         and os.path.exists(os.path.join(routes_dir, d, 'waypoints.json'))]
            route_arg = ','.join(all_routes) if all_routes else route
            cmd = [
                TRAIN_PYTHON, os.path.join(TRAINING_DIR, "behavior_cloning.py"),
                "--route", route_arg,
                "--routes-dir", routes_dir,
                "--epochs", "200",
                "--model-dir", os.path.join(MODELS_DIR, "route_nav"),
                "--use-corrections",
            ]
            ok, msg = _run_async("retrain_route_nav", cmd)
            self._json_response({"ok": ok, "message": msg or "Retraining started"})
            return

        # Training pipeline API
        if self.path == '/api/train/generate':
            content_len = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(content_len)) if content_len else {}
            count = body.get('count', 30)
            seed = body.get('seed', 42)
            ok, msg = _run_async("generate", [
                TRAIN_PYTHON, os.path.join(TRAINING_DIR, "map_generator.py"),
                "--output", MAPS_DIR,
                "--count", str(count),
                "--seed", str(seed),
            ])
            self._json_response({"ok": ok, "message": msg})
            return

        if self.path == '/api/train/train':
            content_len = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(content_len)) if content_len else {}
            timesteps = body.get('timesteps', 100000)
            task = body.get('task', 'explore')
            fresh = body.get('fresh', False)
            cmd = [
                TRAIN_PYTHON, os.path.join(TRAINING_DIR, "train.py"),
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
            ok, msg = _run_train_async(task, cmd, timesteps)
            self._json_response({"ok": ok, "message": msg})
            return

        if self.path == '/api/train/evaluate':
            model_path = os.path.join(MODELS_DIR, "nav_ppo_final")
            if not os.path.exists(model_path + ".zip"):
                self._json_response({"ok": False, "message": "No trained model found. Train first."})
                return
            ok, msg = _run_async("evaluate", [
                TRAIN_PYTHON, os.path.join(TRAINING_DIR, "evaluate.py"),
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
            content_len = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(content_len)) if content_len else {}
            stop_task = body.get('task', None)

            if stop_task and stop_task in _train_processes:
                tp = _train_processes[stop_task]
                if tp["process"] and tp["process"].poll() is None:
                    tp["process"].terminate()
                    self._json_response({"ok": True, "message": f"Stopped {stop_task}"})
                else:
                    self._json_response({"ok": False, "message": f"{stop_task} not running"})
            elif _process and _process.poll() is None:
                _process.terminate()
                self._json_response({"ok": True, "message": f"Stopped {_process_label}"})
            else:
                # Stop all running training
                stopped = []
                for t, tp in _train_processes.items():
                    if tp["process"] and tp["process"].poll() is None:
                        tp["process"].terminate()
                        stopped.append(t)
                if stopped:
                    self._json_response({"ok": True, "message": f"Stopped {', '.join(stopped)}"})
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
