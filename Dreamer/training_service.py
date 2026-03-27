#!/usr/bin/env python3
"""
AJ Robotics — Dreamer YOLO Training Service

Lightweight Flask API that runs on Dreamer (Windows/GPU) to manage
YOLO datasets, training runs, and trained models.

Christy's main dashboard proxies /api/proxy/training/* calls here.

Usage:
    python Dreamer/training_service.py
    # or via batch file:
    Dreamer\\Start YOLO Training Service.bat

Runs on port 5002 so it doesn't conflict with other services.
"""

import glob
import os
import random
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from flask import Flask, jsonify, request

app = Flask(__name__)

PORT = 5002

# --- Serve Training UI directly from Dreamer ---
@app.route("/")
def training_page():
    """Serve the training UI directly (no need for Christy proxy)."""
    html_path = os.path.join(PROJECT_ROOT, "gui", "templates", "training.html")
    if not os.path.exists(html_path):
        return "training.html not found", 404
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()
    # When served directly from Dreamer, use local API paths instead of proxy
    html = html.replace("const API = '/api/proxy/training'", "const API = '/api/training'")
    return html

# --- Paths ---
DATASETS_DIR = os.path.join(PROJECT_ROOT, "datasets")
RUNS_DIR = os.path.join(PROJECT_ROOT, "runs")
os.makedirs(DATASETS_DIR, exist_ok=True)
os.makedirs(RUNS_DIR, exist_ok=True)

# --- Training State ---
_training_lock = threading.Lock()
_training_state = {
    "active": False,
    "process": None,
    "dataset": None,
    "model": None,
    "epochs": 0,
    "task_type": "detection",
    "started_at": None,
    "log": [],
    "run_name": None,
    "progress": {},
}


# ============================================================
#  Dataset Management
# ============================================================

def _dataset_path(name):
    return os.path.join(DATASETS_DIR, name)


def _read_data_yaml(ds_path):
    """Read data.yaml and return parsed info."""
    yaml_path = os.path.join(ds_path, "data.yaml")
    if not os.path.exists(yaml_path):
        return None
    info = {}
    with open(yaml_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if ":" in line:
                key, val = line.split(":", 1)
                info[key.strip()] = val.strip()
    return info


def _write_data_yaml(ds_path, classes, ds_name):
    """Write YOLO-format data.yaml."""
    yaml_path = os.path.join(ds_path, "data.yaml")
    lines = [
        f"# Dataset: {ds_name}",
        f"path: {ds_path}",
        f"train: train/images",
        f"val: val/images",
        f"",
        f"nc: {len(classes)}",
        f"names: [{', '.join(repr(c) for c in classes)}]",
    ]
    with open(yaml_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _dataset_stats(ds_path):
    """Count images and labels in a dataset."""
    stats = {}
    for split in ["train", "val"]:
        img_dir = os.path.join(ds_path, split, "images")
        lbl_dir = os.path.join(ds_path, split, "labels")
        img_count = len(glob.glob(os.path.join(img_dir, "*.*"))) if os.path.isdir(img_dir) else 0
        lbl_count = len(glob.glob(os.path.join(lbl_dir, "*.txt"))) if os.path.isdir(lbl_dir) else 0
        stats[split] = {"images": img_count, "labels": lbl_count}
    return stats


def _parse_classes_from_yaml(ds_path):
    """Parse class names from data.yaml."""
    yaml_path = os.path.join(ds_path, "data.yaml")
    if not os.path.exists(yaml_path):
        return []
    with open(yaml_path, "r", encoding="utf-8") as f:
        content = f.read()
    # Parse names: ['class1', 'class2', ...]
    import re
    m = re.search(r"names:\s*\[(.+?)\]", content)
    if m:
        raw = m.group(1)
        # Handle both 'class' and "class"
        names = re.findall(r"['\"]([^'\"]+)['\"]", raw)
        return names
    return []


def _get_dataset_type(ds_path):
    """Detect if dataset is 'classification' or 'detection'."""
    info_path = os.path.join(ds_path, "dataset_info.json")
    if os.path.exists(info_path):
        try:
            import json
            with open(info_path, "r") as f:
                return json.load(f).get("type", "detection")
        except Exception:
            pass
    # Check folder structure: classification has train/classA/, detection has train/images/
    if os.path.isdir(os.path.join(ds_path, "train")):
        train_sub = os.path.join(ds_path, "train")
        for entry in os.listdir(train_sub):
            sub = os.path.join(train_sub, entry)
            if os.path.isdir(sub) and entry not in ("images", "labels"):
                return "classification"
    return "detection"


def _cls_dataset_stats(ds_path):
    """Count images per class for classification datasets."""
    IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff", ".gif"}
    stats = {"train": {}, "val": {}}
    total_train = 0
    total_val = 0
    classes = []
    for split in ["train", "val"]:
        split_dir = os.path.join(ds_path, split)
        if not os.path.isdir(split_dir):
            continue
        for cls_name in sorted(os.listdir(split_dir)):
            cls_dir = os.path.join(split_dir, cls_name)
            if not os.path.isdir(cls_dir):
                continue
            count = len([f for f in os.listdir(cls_dir) if os.path.splitext(f)[1].lower() in IMG_EXTS])
            stats[split][cls_name] = count
            if split == "train":
                total_train += count
                if cls_name not in classes:
                    classes.append(cls_name)
            else:
                total_val += count
                if cls_name not in classes:
                    classes.append(cls_name)
    return stats, classes, total_train, total_val


@app.route("/api/training/datasets")
def list_datasets():
    """List all datasets (detection and classification)."""
    datasets = []
    if os.path.isdir(DATASETS_DIR):
        for name in sorted(os.listdir(DATASETS_DIR)):
            ds_path = os.path.join(DATASETS_DIR, name)
            if not os.path.isdir(ds_path):
                continue

            ds_type = _get_dataset_type(ds_path)

            if ds_type == "classification":
                cls_stats, classes, total_train, total_val = _cls_dataset_stats(ds_path)
                total_images = total_train + total_val

                try:
                    info_path = os.path.join(ds_path, "dataset_info.json")
                    mtime = os.path.getmtime(info_path) if os.path.exists(info_path) else os.path.getmtime(ds_path)
                    modified = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    modified = "--"

                datasets.append({
                    "name": name,
                    "type": "classification",
                    "classes": classes,
                    "num_classes": len(classes),
                    "total_images": total_images,
                    "total_labels": total_images,  # for classification, every image is labeled by folder
                    "stats": {"train": {"images": total_train, "labels": total_train},
                              "val": {"images": total_val, "labels": total_val}},
                    "cls_stats": cls_stats,
                    "modified": modified,
                })
            else:
                classes = _parse_classes_from_yaml(ds_path)
                stats = _dataset_stats(ds_path)
                total_images = stats["train"]["images"] + stats["val"]["images"]
                total_labels = stats["train"]["labels"] + stats["val"]["labels"]

                try:
                    mtime = os.path.getmtime(os.path.join(ds_path, "data.yaml"))
                    modified = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    modified = "--"

                datasets.append({
                    "name": name,
                    "type": "detection",
                    "classes": classes,
                    "num_classes": len(classes),
                    "total_images": total_images,
                    "total_labels": total_labels,
                    "stats": stats,
                    "modified": modified,
                })
    return jsonify({"ok": True, "datasets": datasets})


@app.route("/api/training/datasets", methods=["POST"])
def create_dataset():
    """Create a new dataset with folder structure."""
    data = request.json or {}
    name = data.get("name", "").strip()
    classes = data.get("classes", [])

    if not name:
        return jsonify({"ok": False, "error": "Dataset name is required"}), 400

    # Sanitize name
    name = name.replace(" ", "_").replace("/", "_").replace("\\", "_")
    ds_path = _dataset_path(name)

    if os.path.exists(ds_path):
        return jsonify({"ok": False, "error": f"Dataset '{name}' already exists"}), 400

    # Create folder structure
    for split in ["train", "val"]:
        os.makedirs(os.path.join(ds_path, split, "images"), exist_ok=True)
        os.makedirs(os.path.join(ds_path, split, "labels"), exist_ok=True)

    # Write data.yaml
    _write_data_yaml(ds_path, classes, name)

    return jsonify({"ok": True, "name": name, "message": f"Dataset '{name}' created"})


@app.route("/api/training/datasets/<name>")
def dataset_detail(name):
    """Get detailed info about a dataset."""
    ds_path = _dataset_path(name)
    if not os.path.isdir(ds_path):
        return jsonify({"ok": False, "error": f"Dataset '{name}' not found"}), 404

    classes = _parse_classes_from_yaml(ds_path)
    stats = _dataset_stats(ds_path)

    # List sample images
    train_imgs = []
    img_dir = os.path.join(ds_path, "train", "images")
    if os.path.isdir(img_dir):
        for f in sorted(os.listdir(img_dir))[:20]:
            train_imgs.append(f)

    return jsonify({
        "ok": True,
        "name": name,
        "classes": classes,
        "stats": stats,
        "sample_images": train_imgs,
        "path": ds_path,
    })


@app.route("/api/training/datasets/<name>/delete", methods=["POST"])
def delete_dataset(name):
    """Delete a dataset."""
    ds_path = _dataset_path(name)
    if not os.path.isdir(ds_path):
        return jsonify({"ok": False, "error": f"Dataset '{name}' not found"}), 404

    import shutil
    import stat

    def _on_rm_error(func, path, exc_info):
        """Handle read-only or locked files (common with Google Drive sync)."""
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except Exception:
            pass

    try:
        shutil.rmtree(ds_path, onexc=_on_rm_error)
    except TypeError:
        # Python < 3.12 uses onerror instead of onexc
        shutil.rmtree(ds_path, onerror=lambda f, p, e: _on_rm_error(f, p, e))

    if os.path.exists(ds_path):
        return jsonify({"ok": False, "error": "Could not fully delete (files may be locked by Google Drive sync). Try again."}), 500

    return jsonify({"ok": True, "message": f"Dataset '{name}' deleted"})


@app.route("/api/training/datasets/<name>/classes", methods=["POST"])
def update_classes(name):
    """Update classes for a dataset."""
    ds_path = _dataset_path(name)
    if not os.path.isdir(ds_path):
        return jsonify({"ok": False, "error": f"Dataset '{name}' not found"}), 404

    data = request.json or {}
    classes = data.get("classes", [])
    _write_data_yaml(ds_path, classes, name)
    return jsonify({"ok": True, "classes": classes})


@app.route("/api/training/datasets/<name>/upload", methods=["POST"])
def upload_images(name):
    """Upload images to a dataset (train or val split)."""
    ds_path = _dataset_path(name)
    if not os.path.isdir(ds_path):
        return jsonify({"ok": False, "error": f"Dataset '{name}' not found"}), 404

    split = request.form.get("split", "train")
    if split not in ("train", "val"):
        split = "train"

    img_dir = os.path.join(ds_path, split, "images")
    os.makedirs(img_dir, exist_ok=True)

    saved = 0
    for key in request.files:
        f = request.files[key]
        if f.filename:
            fname = f.filename.replace(" ", "_")
            f.save(os.path.join(img_dir, fname))
            saved += 1

    return jsonify({"ok": True, "saved": saved, "split": split})


@app.route("/api/training/import-classification", methods=["POST"])
def import_classification():
    """Import a classification folder (subfolders = classes, containing images).

    Expects JSON: {"source": "C:/path/to/DLearnTest", "name": "my_dataset", "split_ratio": 0.8}
    """
    data = request.json or {}
    source = data.get("source", "").strip()
    name = data.get("name", "").strip()
    split_ratio = float(data.get("split_ratio", 0.8))

    if not source or not name:
        return jsonify({"ok": False, "error": "Both 'source' path and 'name' are required"}), 400

    # Normalize path for Windows
    source = source.replace("/", os.sep).replace("\\", os.sep)
    if not os.path.isdir(source):
        return jsonify({"ok": False, "error": f"Source folder not found: {source}"}), 400

    name = name.replace(" ", "_").replace("/", "_").replace("\\", "_")
    ds_path = _dataset_path(name)

    if os.path.exists(ds_path):
        return jsonify({"ok": False, "error": f"Dataset '{name}' already exists"}), 400

    # Scan source for class subfolders
    classes = []
    class_images = {}  # class_name -> [file_paths]
    IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff", ".gif"}

    for entry in sorted(os.listdir(source)):
        sub = os.path.join(source, entry)
        if not os.path.isdir(sub):
            continue
        imgs = [
            os.path.join(sub, f) for f in os.listdir(sub)
            if os.path.splitext(f)[1].lower() in IMG_EXTS
        ]
        if imgs:
            classes.append(entry)
            class_images[entry] = sorted(imgs)

    if not classes:
        return jsonify({"ok": False, "error": "No class subfolders with images found"}), 400

    # Create dataset folder structure for YOLO classification
    # Structure: dataset/train/classA/*.jpg, dataset/val/classA/*.jpg
    os.makedirs(ds_path, exist_ok=True)
    total_copied = 0
    train_count = 0
    val_count = 0

    for cls_name in classes:
        imgs = class_images[cls_name]
        random.shuffle(imgs)
        split_idx = max(1, int(len(imgs) * split_ratio))
        train_imgs = imgs[:split_idx]
        val_imgs = imgs[split_idx:] if split_idx < len(imgs) else imgs[-1:]  # At least 1 in val

        train_dir = os.path.join(ds_path, "train", cls_name)
        val_dir = os.path.join(ds_path, "val", cls_name)
        os.makedirs(train_dir, exist_ok=True)
        os.makedirs(val_dir, exist_ok=True)

        for src_img in train_imgs:
            dst = os.path.join(train_dir, os.path.basename(src_img))
            shutil.copy2(src_img, dst)
            total_copied += 1
            train_count += 1

        for src_img in val_imgs:
            dst = os.path.join(val_dir, os.path.basename(src_img))
            shutil.copy2(src_img, dst)
            total_copied += 1
            val_count += 1

    # Write info file (not data.yaml - classification doesn't need it, but useful for our UI)
    info_path = os.path.join(ds_path, "dataset_info.json")
    import json
    info = {
        "type": "classification",
        "name": name,
        "source": source,
        "classes": classes,
        "num_classes": len(classes),
        "split_ratio": split_ratio,
        "train_count": train_count,
        "val_count": val_count,
        "total_images": total_copied,
        "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(info_path, "w", encoding="utf-8") as f:
        json.dump(info, f, indent=2)

    return jsonify({
        "ok": True,
        "name": name,
        "classes": classes,
        "train_count": train_count,
        "val_count": val_count,
        "total_copied": total_copied,
        "message": f"Imported {total_copied} images ({len(classes)} classes) into '{name}'"
    })


@app.route("/api/training/scan-folder", methods=["POST"])
def scan_folder():
    """Scan a folder to preview classification data before importing.

    Expects JSON: {"path": "C:/path/to/folder"}
    """
    data = request.json or {}
    path = data.get("path", "").strip()
    if not path:
        return jsonify({"ok": False, "error": "Path is required"}), 400

    path = path.replace("/", os.sep).replace("\\", os.sep)
    if not os.path.isdir(path):
        return jsonify({"ok": False, "error": f"Folder not found: {path}"}), 400

    IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff", ".gif"}
    classes = []
    total_images = 0

    for entry in sorted(os.listdir(path)):
        sub = os.path.join(path, entry)
        if not os.path.isdir(sub):
            continue
        imgs = [f for f in os.listdir(sub) if os.path.splitext(f)[1].lower() in IMG_EXTS]
        if imgs:
            classes.append({"name": entry, "count": len(imgs)})
            total_images += len(imgs)

    return jsonify({
        "ok": True,
        "path": path,
        "classes": classes,
        "total_images": total_images,
        "is_classification": len(classes) > 0,
    })


# ============================================================
#  Training Execution
# ============================================================

def _read_results_csv(run_dir):
    """Read YOLO results.csv for training progress."""
    csv_path = os.path.join(run_dir, "results.csv")
    if not os.path.exists(csv_path):
        return []
    rows = []
    with open(csv_path, "r") as f:
        header = None
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split(",")]
            if header is None:
                header = parts
                continue
            row = {}
            for i, h in enumerate(header):
                if i < len(parts):
                    try:
                        row[h] = float(parts[i])
                    except ValueError:
                        row[h] = parts[i]
            rows.append(row)
    return rows


def _training_worker(dataset_name, model, epochs, batch, imgsz, run_name, task_type="detect"):
    """Background thread that runs YOLO training."""
    global _training_state

    ds_path = _dataset_path(dataset_name)
    project_dir = RUNS_DIR

    # Use yolo CLI or Python script for training
    # Try to find yolo executable
    yolo_exe = shutil.which("yolo")

    if yolo_exe:
        if task_type == "classification":
            cmd = [
                yolo_exe, "classify", "train",
                f"data={ds_path}",
                f"model={model}",
                f"epochs={epochs}",
                f"batch={batch}",
                f"imgsz={imgsz}",
                f"project={project_dir}",
                f"name={run_name}",
                "exist_ok=True",
                "verbose=True",
            ]
        else:
            data_yaml = os.path.join(ds_path, "data.yaml")
            cmd = [
                yolo_exe, "detect", "train",
                f"data={data_yaml}",
                f"model={model}",
                f"epochs={epochs}",
                f"batch={batch}",
                f"imgsz={imgsz}",
                f"project={project_dir}",
                f"name={run_name}",
                "exist_ok=True",
                "verbose=True",
            ]
    else:
        # Fallback: use Python one-liner
        if task_type == "classification":
            script = (
                f"from ultralytics import YOLO; "
                f"m = YOLO('{model}'); "
                f"m.train(data=r'{ds_path}', epochs={epochs}, batch={batch}, "
                f"imgsz={imgsz}, project=r'{project_dir}', name='{run_name}', "
                f"exist_ok=True, verbose=True)"
            )
        else:
            data_yaml = os.path.join(ds_path, "data.yaml")
            script = (
                f"from ultralytics import YOLO; "
                f"m = YOLO('{model}'); "
                f"m.train(data=r'{data_yaml}', epochs={epochs}, batch={batch}, "
                f"imgsz={imgsz}, project=r'{project_dir}', name='{run_name}', "
                f"exist_ok=True, verbose=True)"
            )
        cmd = [sys.executable, "-c", script]

    with _training_lock:
        _training_state["log"].append(f"[{_ts()}] Starting: {' '.join(cmd)}")

    try:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=PROJECT_ROOT,
            encoding="utf-8",
            errors="replace",
            env=env,
        )

        with _training_lock:
            _training_state["process"] = proc

        # Read output line by line
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                with _training_lock:
                    _training_state["log"].append(line)
                    # Keep only last 200 lines
                    if len(_training_state["log"]) > 200:
                        _training_state["log"] = _training_state["log"][-200:]

        proc.wait()

        with _training_lock:
            rc = proc.returncode
            _training_state["log"].append(f"[{_ts()}] Training finished (exit code: {rc})")
            _training_state["active"] = False
            _training_state["process"] = None

    except Exception as e:
        with _training_lock:
            _training_state["log"].append(f"[{_ts()}] ERROR: {e}")
            _training_state["active"] = False
            _training_state["process"] = None


def _ts():
    return datetime.now().strftime("%H:%M:%S")


@app.route("/api/training/start", methods=["POST"])
def start_training():
    """Start a YOLO training run."""
    with _training_lock:
        if _training_state["active"]:
            return jsonify({"ok": False, "error": "Training already in progress"}), 409

    data = request.json or {}
    dataset = data.get("dataset", "")
    model = data.get("model", "yolov8n.pt")
    epochs = int(data.get("epochs", 100))
    batch = int(data.get("batch", 16))
    imgsz = int(data.get("imgsz", 640))
    task_type = data.get("task_type", "")  # "detection" or "classification"

    # Validate dataset
    ds_path = _dataset_path(dataset)
    if not os.path.isdir(ds_path):
        return jsonify({"ok": False, "error": f"Dataset '{dataset}' not found"}), 400

    # Auto-detect task type if not specified
    if not task_type:
        task_type = _get_dataset_type(ds_path)

    if task_type == "classification":
        # Classification: check train/ folder has class subfolders
        train_dir = os.path.join(ds_path, "train")
        if not os.path.isdir(train_dir) or not os.listdir(train_dir):
            return jsonify({"ok": False, "error": "No training data in dataset (train/ folder empty)"}), 400
        # Auto-select classification model if detection model was chosen
        if "-cls" not in model:
            model = model.replace(".pt", "-cls.pt")  # yolov8n.pt -> yolov8n-cls.pt
    else:
        data_yaml = os.path.join(ds_path, "data.yaml")
        if not os.path.exists(data_yaml):
            return jsonify({"ok": False, "error": f"data.yaml not found in dataset '{dataset}'"}), 400
        stats = _dataset_stats(ds_path)
        if stats["train"]["images"] == 0:
            return jsonify({"ok": False, "error": "No training images in dataset"}), 400

    run_name = f"{dataset}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    with _training_lock:
        _training_state["active"] = True
        _training_state["dataset"] = dataset
        _training_state["model"] = model
        _training_state["epochs"] = epochs
        _training_state["task_type"] = task_type
        _training_state["started_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _training_state["log"] = [f"[{_ts()}] Preparing {task_type} training: {dataset} / {model} / {epochs} epochs"]
        _training_state["run_name"] = run_name
        _training_state["progress"] = {}

    # Start training in background thread
    t = threading.Thread(
        target=_training_worker,
        args=(dataset, model, epochs, batch, imgsz, run_name, task_type),
        daemon=True,
    )
    t.start()

    return jsonify({
        "ok": True,
        "message": "Training started",
        "run_name": run_name,
        "dataset": dataset,
        "model": model,
        "epochs": epochs,
    })


@app.route("/api/training/stop", methods=["POST"])
def stop_training():
    """Stop current training."""
    with _training_lock:
        if not _training_state["active"]:
            return jsonify({"ok": False, "error": "No training in progress"})

        proc = _training_state["process"]
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        _training_state["active"] = False
        _training_state["process"] = None
        _training_state["log"].append(f"[{_ts()}] Training stopped by user")

    return jsonify({"ok": True, "message": "Training stopped"})


@app.route("/api/training/status")
def training_status():
    """Get current training status with progress."""
    with _training_lock:
        active = _training_state["active"]
        run_name = _training_state["run_name"]
        dataset = _training_state["dataset"]
        model = _training_state["model"]
        epochs = _training_state["epochs"]
        task_type = _training_state.get("task_type", "detection")
        started_at = _training_state["started_at"]
        log_lines = list(_training_state["log"][-50:])  # Last 50 lines

    # Parse progress from results.csv if training is active
    progress = {}
    if run_name:
        run_dir = os.path.join(RUNS_DIR, run_name)
        results = _read_results_csv(run_dir)
        if results:
            latest = results[-1]
            current_epoch = int(latest.get("epoch", len(results)))

            # Classification metrics have different column names
            if task_type == "classification":
                def _get_val(row, *keys):
                    for k in keys:
                        for rk in row:
                            if k in rk.strip():
                                return row[rk]
                    return None

                progress = {
                    "current_epoch": current_epoch,
                    "total_epochs": epochs,
                    "percent": round(current_epoch / max(epochs, 1) * 100, 1),
                    "metrics": {
                        "train_loss": _get_val(latest, "train/loss"),
                        "val_loss": _get_val(latest, "val/loss"),
                        "top1_acc": _get_val(latest, "top1_acc", "metrics/accuracy_top1"),
                        "top5_acc": _get_val(latest, "top5_acc", "metrics/accuracy_top5"),
                    },
                    "history": [
                        {
                            "epoch": int(r.get("epoch", i + 1)),
                            "train_loss": _get_val(r, "train/loss"),
                            "top1_acc": _get_val(r, "top1_acc", "metrics/accuracy_top1"),
                        }
                        for i, r in enumerate(results)
                    ],
                }
            else:
                progress = {
                    "current_epoch": current_epoch,
                    "total_epochs": epochs,
                    "percent": round(current_epoch / max(epochs, 1) * 100, 1),
                    "metrics": {
                        "box_loss": latest.get("train/box_loss", latest.get("         train/box_loss")),
                        "cls_loss": latest.get("train/cls_loss", latest.get("         train/cls_loss")),
                        "dfl_loss": latest.get("train/dfl_loss", latest.get("         train/dfl_loss")),
                        "mAP50": latest.get("metrics/mAP50(B)", latest.get("   metrics/mAP50(B)")),
                        "mAP50_95": latest.get("metrics/mAP50-95(B)", latest.get("   metrics/mAP50-95(B)")),
                        "precision": latest.get("metrics/precision(B)", latest.get("   metrics/precision(B)")),
                        "recall": latest.get("metrics/recall(B)", latest.get("   metrics/recall(B)")),
                    },
                    "history": [
                        {
                            "epoch": int(r.get("epoch", i + 1)),
                            "box_loss": r.get("train/box_loss", r.get("         train/box_loss")),
                            "mAP50": r.get("metrics/mAP50(B)", r.get("   metrics/mAP50(B)")),
                        }
                        for i, r in enumerate(results)
                    ],
                }

    # Check GPU info
    gpu_info = None
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            parts = result.stdout.strip().split(", ")
            if len(parts) >= 4:
                gpu_info = {
                    "name": parts[0],
                    "utilization": f"{parts[1]}%",
                    "memory_used": f"{parts[2]} MB",
                    "memory_total": f"{parts[3]} MB",
                }
    except Exception:
        pass

    return jsonify({
        "ok": True,
        "active": active,
        "dataset": dataset,
        "model": model,
        "epochs": epochs,
        "task_type": task_type,
        "started_at": started_at,
        "run_name": run_name,
        "progress": progress,
        "log": log_lines,
        "gpu": gpu_info,
    })


# ============================================================
#  Model Results
# ============================================================

@app.route("/api/training/models")
def list_models():
    """List all trained models/runs."""
    models = []
    if not os.path.isdir(RUNS_DIR):
        return jsonify({"ok": True, "models": []})

    for name in sorted(os.listdir(RUNS_DIR), reverse=True):
        run_dir = os.path.join(RUNS_DIR, name)
        if not os.path.isdir(run_dir):
            continue

        # Check for weights
        best_pt = os.path.join(run_dir, "weights", "best.pt")
        last_pt = os.path.join(run_dir, "weights", "last.pt")
        has_best = os.path.exists(best_pt)
        has_last = os.path.exists(last_pt)

        if not has_best and not has_last:
            # Check if training is still in progress (has results.csv but no weights)
            results = _read_results_csv(run_dir)
            if not results and not os.path.exists(os.path.join(run_dir, "args.yaml")):
                continue  # Empty folder

        # Read results
        results = _read_results_csv(run_dir)
        best_metrics = {}

        # Detect if classification run (check args.yaml or column names)
        is_cls = False
        args_path = os.path.join(run_dir, "args.yaml")
        if os.path.exists(args_path):
            with open(args_path, "r") as f:
                content = f.read()
                if "classify" in content or "-cls" in content:
                    is_cls = True
        if results and not is_cls:
            # Check column names
            first = results[0]
            for k in first:
                if "top1" in k or "accuracy" in k:
                    is_cls = True
                    break

        if results:
            if is_cls:
                # Classification: find best top1 accuracy
                best_acc = 0
                for r in results:
                    acc = None
                    for k in r:
                        if "top1" in k or "accuracy_top1" in k:
                            acc = r[k]
                            break
                    if acc and float(acc) > best_acc:
                        best_acc = float(acc)
                        top5 = None
                        for k in r:
                            if "top5" in k or "accuracy_top5" in k:
                                top5 = r[k]
                                break
                        best_metrics = {
                            "top1_acc": round(best_acc, 4),
                            "top5_acc": round(float(top5), 4) if top5 else None,
                            "best_epoch": int(r.get("epoch", 0)),
                        }
            else:
                # Detection: find best mAP50 epoch
                best_map = 0
                for r in results:
                    m50 = r.get("metrics/mAP50(B)", r.get("   metrics/mAP50(B)", 0))
                    if m50 and float(m50) > best_map:
                        best_map = float(m50)
                        best_metrics = {
                            "mAP50": round(float(m50), 4),
                            "mAP50_95": round(float(r.get("metrics/mAP50-95(B)", r.get("   metrics/mAP50-95(B)", 0))), 4),
                            "precision": round(float(r.get("metrics/precision(B)", r.get("   metrics/precision(B)", 0))), 4),
                            "recall": round(float(r.get("metrics/recall(B)", r.get("   metrics/recall(B)", 0))), 4),
                            "best_epoch": int(r.get("epoch", 0)),
                        }

        # File size
        model_size = "--"
        if has_best:
            size_bytes = os.path.getsize(best_pt)
            model_size = f"{size_bytes / 1024 / 1024:.1f} MB"

        # Get modification time
        try:
            mtime = max(
                os.path.getmtime(best_pt) if has_best else 0,
                os.path.getmtime(last_pt) if has_last else 0,
                os.path.getmtime(run_dir),
            )
            modified = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
        except Exception:
            modified = "--"

        models.append({
            "name": name,
            "has_best": has_best,
            "has_last": has_last,
            "model_size": model_size,
            "total_epochs": len(results),
            "best_metrics": best_metrics,
            "modified": modified,
        })

    return jsonify({"ok": True, "models": models})


@app.route("/api/training/models/<name>")
def model_detail(name):
    """Get detailed results for a specific training run."""
    run_dir = os.path.join(RUNS_DIR, name)
    if not os.path.isdir(run_dir):
        return jsonify({"ok": False, "error": f"Run '{name}' not found"}), 404

    results = _read_results_csv(run_dir)
    best_pt = os.path.join(run_dir, "weights", "best.pt")
    last_pt = os.path.join(run_dir, "weights", "last.pt")

    # Read args.yaml if available
    args = {}
    args_path = os.path.join(run_dir, "args.yaml")
    if os.path.exists(args_path):
        with open(args_path, "r") as f:
            for line in f:
                line = line.strip()
                if ":" in line:
                    key, val = line.split(":", 1)
                    args[key.strip()] = val.strip()

    # Per-epoch data for charts
    chart_data = []
    for r in results:
        chart_data.append({
            "epoch": int(r.get("epoch", len(chart_data) + 1)),
            "box_loss": _safe_float(r.get("train/box_loss", r.get("         train/box_loss"))),
            "cls_loss": _safe_float(r.get("train/cls_loss", r.get("         train/cls_loss"))),
            "val_box_loss": _safe_float(r.get("val/box_loss", r.get("           val/box_loss"))),
            "val_cls_loss": _safe_float(r.get("val/cls_loss", r.get("           val/cls_loss"))),
            "mAP50": _safe_float(r.get("metrics/mAP50(B)", r.get("   metrics/mAP50(B)"))),
            "mAP50_95": _safe_float(r.get("metrics/mAP50-95(B)", r.get("   metrics/mAP50-95(B)"))),
            "precision": _safe_float(r.get("metrics/precision(B)", r.get("   metrics/precision(B)"))),
            "recall": _safe_float(r.get("metrics/recall(B)", r.get("   metrics/recall(B)"))),
        })

    return jsonify({
        "ok": True,
        "name": name,
        "has_best": os.path.exists(best_pt),
        "has_last": os.path.exists(last_pt),
        "model_size": f"{os.path.getsize(best_pt) / 1024 / 1024:.1f} MB" if os.path.exists(best_pt) else "--",
        "total_epochs": len(results),
        "args": args,
        "chart_data": chart_data,
    })


@app.route("/api/training/models/<name>/delete", methods=["POST"])
def delete_model(name):
    """Delete a training run."""
    run_dir = os.path.join(RUNS_DIR, name)
    if not os.path.isdir(run_dir):
        return jsonify({"ok": False, "error": f"Run '{name}' not found"}), 404

    import shutil
    import stat

    def _on_rm_error(func, path, exc_info):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except Exception:
            pass

    try:
        shutil.rmtree(run_dir, onexc=_on_rm_error)
    except TypeError:
        shutil.rmtree(run_dir, onerror=lambda f, p, e: _on_rm_error(f, p, e))

    if os.path.exists(run_dir):
        return jsonify({"ok": False, "error": "Could not fully delete (files may be locked). Try again."}), 500

    return jsonify({"ok": True, "message": f"Run '{name}' deleted"})


@app.route("/api/training/health")
def health():
    """Health check endpoint."""
    return jsonify({"ok": True, "service": "dreamer-training-service"})


# ============================================================
#  Object Detection (R1 capture + YOLO analysis)
# ============================================================

# Robot configuration
ROBOTS = {
    "R1": {"host": "192.168.1.82", "user": "dream", "camera": "/dev/video0", "rotate": 180},
    "ROSMASTER": {"host": "TBD", "user": "dream", "camera": "/dev/video0", "rotate": 0},
}

# Cache YOLO model for fast inference
_yolo_model = None
_yolo_model_lock = threading.Lock()
_detection_history = []  # Store recent detections


_yolo_models_cache = {}  # path -> YOLO model

def _get_yolo_model(model_path="yolov8n.pt"):
    """Load YOLO model (cached per path for multi-model support)."""
    global _yolo_models_cache
    with _yolo_model_lock:
        if model_path not in _yolo_models_cache:
            try:
                from ultralytics import YOLO
                _yolo_models_cache[model_path] = YOLO(model_path)
                print(f"[YOLO] Loaded model: {model_path}")
            except Exception as e:
                print(f"[YOLO] Failed to load model: {e}")
                return None
        return _yolo_models_cache[model_path]


@app.route("/api/training/available-models")
def available_models():
    """List trained models available for detection."""
    models = [{"name": "yolov8n.pt (Default 80 classes)", "path": "yolov8n.pt", "type": "detection"}]
    if os.path.isdir(RUNS_DIR):
        for name in sorted(os.listdir(RUNS_DIR), reverse=True):
            best_pt = os.path.join(RUNS_DIR, name, "weights", "best.pt")
            if os.path.exists(best_pt):
                # Determine model type from args.yaml if present
                args_yaml = os.path.join(RUNS_DIR, name, "args.yaml")
                mtype = "detection"
                if os.path.exists(args_yaml):
                    try:
                        with open(args_yaml, "r") as f:
                            content = f.read()
                        if "task: classify" in content:
                            mtype = "classification"
                    except Exception:
                        pass
                models.append({"name": f"{name} (Custom)", "path": best_pt, "type": mtype})
    return jsonify({"ok": True, "models": models})


@app.route("/api/training/capture", methods=["POST"])
def capture_and_analyze():
    """Capture image from R1 via SSH, analyze with YOLO, return results."""
    data = request.json or {}
    robot_id = data.get("robot_id", "R1")
    confidence = float(data.get("confidence", 0.25))
    model_path = data.get("model", "yolov8n.pt")

    robot = ROBOTS.get(robot_id)
    if not robot:
        return jsonify({"ok": False, "error": f"Unknown robot: {robot_id}"}), 400

    host = robot["host"]
    if host == "TBD":
        return jsonify({"ok": False, "error": f"Robot {robot_id} host not configured"}), 400

    try:
        # Step 1: Capture image from robot via SSH
        import paramiko

        rotate = robot.get("rotate", 0)
        rotate_code = ""
        if rotate == 180:
            rotate_code = "f = cv2.rotate(f, cv2.ROTATE_180); "
        elif rotate == 90:
            rotate_code = "f = cv2.rotate(f, cv2.ROTATE_90_CLOCKWISE); "
        elif rotate == 270:
            rotate_code = "f = cv2.rotate(f, cv2.ROTATE_90_COUNTERCLOCKWISE); "

        capture_script = (
            'python3 -c "'
            'import cv2, sys; '
            'cap = cv2.VideoCapture(0); '
            'ret, f = cap.read(); '
            'cap.release(); '
            f'{rotate_code}'
            '_, jpg = cv2.imencode(chr(46)+chr(106)+chr(112)+chr(103), f, [cv2.IMWRITE_JPEG_QUALITY, 85]) if ret else (False, None); '
            'sys.stdout.buffer.write(jpg.tobytes()) if ret else sys.exit(1)'
            '"'
        )

        ssh_key = os.path.join(os.path.expanduser("~"), ".ssh", "id_rsa")
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=host, username=robot["user"],
            key_filename=ssh_key,
            timeout=5, banner_timeout=5, auth_timeout=5,
            allow_agent=False, look_for_keys=False,
        )
        stdin, stdout, stderr = client.exec_command(capture_script, timeout=15)
        rc = stdout.channel.recv_exit_status()
        image_bytes = stdout.read()
        err_str = stderr.read().decode("utf-8", errors="replace")
        client.close()

        if rc != 0 or len(image_bytes) < 100:
            return jsonify({"ok": False, "error": f"Capture failed: {err_str[:200]}"})

        # Step 2: Run YOLO detection
        import cv2
        import numpy as np
        import base64
        import io

        t_start = _time_ms()

        # Decode image
        nparr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None:
            return jsonify({"ok": False, "error": "Failed to decode captured image"})

        # Run YOLO — support combined mode (base + custom)
        combined = data.get("combined", False)
        custom_models = data.get("custom_models", [])

        all_detections = []

        if combined and custom_models:
            # Step A: Run base model (yolov8n.pt) for general objects
            base_model = _get_yolo_model("yolov8n.pt")
            if base_model:
                base_results = base_model(img, conf=confidence, verbose=False)
                base_result = base_results[0]
                for box in base_result.boxes:
                    cls_id = int(box.cls[0])
                    conf_val = float(box.conf[0])
                    x1, y1, x2, y2 = box.xyxy[0].tolist()
                    all_detections.append({
                        "class": base_result.names[cls_id],
                        "confidence": round(conf_val, 3),
                        "bbox": [round(x1), round(y1), round(x2), round(y2)],
                        "model": "yolov8n.pt",
                    })

            # Step B: Run each custom model (detection models only)
            for cmodel_path in custom_models:
                cmodel = _get_yolo_model(cmodel_path)
                if cmodel:
                    try:
                        c_results = cmodel(img, conf=confidence, verbose=False)
                        c_result = c_results[0]
                        if c_result.boxes is None or len(c_result.boxes) == 0:
                            continue
                        for box in c_result.boxes:
                            cls_id = int(box.cls[0])
                            conf_val = float(box.conf[0])
                            x1, y1, x2, y2 = box.xyxy[0].tolist()
                            all_detections.append({
                                "class": c_result.names[cls_id],
                                "confidence": round(conf_val, 3),
                                "bbox": [round(x1), round(y1), round(x2), round(y2)],
                                "model": os.path.basename(os.path.dirname(os.path.dirname(cmodel_path))),
                            })
                    except Exception as e:
                        print(f"[WARN] Custom model {cmodel_path} skipped: {e}")
        else:
            # Single model mode
            model = _get_yolo_model(model_path)
            if model is None:
                return jsonify({"ok": False, "error": "YOLO model not loaded"})
            results = model(img, conf=confidence, verbose=False)
            result = results[0]
            for box in result.boxes:
                cls_id = int(box.cls[0])
                conf_val = float(box.conf[0])
                x1, y1, x2, y2 = box.xyxy[0].tolist()
                all_detections.append({
                    "class": result.names[cls_id],
                    "confidence": round(conf_val, 3),
                    "bbox": [round(x1), round(y1), round(x2), round(y2)],
                    "model": os.path.basename(model_path),
                })

        t_elapsed = _time_ms() - t_start
        detections = all_detections

        # Create annotated image (draw all boxes manually for combined mode)
        annotated = img.copy()
        colors = {"yolov8n.pt": (0, 255, 0)}  # green for base
        color_idx = 0
        custom_colors = [(0, 200, 255), (255, 100, 255), (255, 255, 0), (100, 255, 100)]
        for det in detections:
            x1, y1, x2, y2 = det["bbox"]
            model_name = det.get("model", "")
            if model_name not in colors:
                colors[model_name] = custom_colors[color_idx % len(custom_colors)]
                color_idx += 1
            color = colors[model_name]
            cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
            label = f"{det['class']} {det['confidence']:.2f}"
            (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 1)
            cv2.rectangle(annotated, (x1, y1 - th - 8), (x1 + tw + 4, y1), color, -1)
            cv2.putText(annotated, label, (x1 + 2, y1 - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 1)

        _, ann_jpg = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 85])
        annotated_b64 = base64.b64encode(ann_jpg.tobytes()).decode("utf-8")

        # Also encode original
        _, orig_jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
        original_b64 = base64.b64encode(orig_jpg.tobytes()).decode("utf-8")

        # Store in history
        entry = {
            "id": str(uuid.uuid4())[:8],
            "robot_id": robot_id,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "processing_ms": t_elapsed,
            "detections": detections,
            "num_objects": len(detections),
            "image_size": {"width": img.shape[1], "height": img.shape[0]},
        }
        _detection_history.insert(0, entry)
        if len(_detection_history) > 50:
            _detection_history.pop()

        return jsonify({
            "ok": True,
            "robot_id": robot_id,
            "timestamp": entry["timestamp"],
            "processing_ms": t_elapsed,
            "detections": detections,
            "num_objects": len(detections),
            "summary": _detection_summary(detections),
            "annotated_image": annotated_b64,
            "original_image": original_b64,
            "image_size": entry["image_size"],
        })

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/training/analyze", methods=["POST"])
def analyze_upload():
    """Analyze an uploaded image with YOLO (no SSH, direct upload)."""
    if "image" not in request.files:
        return jsonify({"ok": False, "error": "No image file provided"}), 400

    import cv2
    import numpy as np
    import base64

    f = request.files["image"]
    image_bytes = f.read()
    confidence = float(request.form.get("confidence", 0.25))

    t_start = _time_ms()
    nparr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        return jsonify({"ok": False, "error": "Failed to decode image"})

    model = _get_yolo_model()
    if model is None:
        return jsonify({"ok": False, "error": "YOLO model not loaded"})

    results = model(img, conf=confidence, verbose=False)
    t_elapsed = _time_ms() - t_start

    detections = []
    result = results[0]
    for box in result.boxes:
        cls_id = int(box.cls[0])
        conf = float(box.conf[0])
        x1, y1, x2, y2 = box.xyxy[0].tolist()
        detections.append({
            "class": result.names[cls_id],
            "confidence": round(conf, 3),
            "bbox": [round(x1), round(y1), round(x2), round(y2)],
        })

    annotated = result.plot()
    _, ann_jpg = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 85])
    annotated_b64 = base64.b64encode(ann_jpg.tobytes()).decode("utf-8")

    return jsonify({
        "ok": True,
        "processing_ms": t_elapsed,
        "detections": detections,
        "num_objects": len(detections),
        "summary": _detection_summary(detections),
        "annotated_image": annotated_b64,
    })


@app.route("/api/training/save-capture", methods=["POST"])
def save_capture():
    """Save a captured image to a dataset folder for later labeling.

    Expects JSON: {"robot_id": "R1", "dataset": "red_pepper", "image": "<base64>"}
    """
    data = request.json or {}
    dataset_name = data.get("dataset", "").strip()
    image_b64 = data.get("image", "")
    robot_id = data.get("robot_id", "R1")

    if not dataset_name:
        return jsonify({"ok": False, "error": "Dataset name is required"}), 400
    if not image_b64:
        return jsonify({"ok": False, "error": "No image data"}), 400

    import base64

    # Create detection dataset folder
    dataset_name = dataset_name.replace(" ", "_")
    ds_path = _dataset_path(dataset_name)
    img_dir = os.path.join(ds_path, "train", "images")
    lbl_dir = os.path.join(ds_path, "train", "labels")
    val_img_dir = os.path.join(ds_path, "val", "images")
    val_lbl_dir = os.path.join(ds_path, "val", "labels")
    os.makedirs(img_dir, exist_ok=True)
    os.makedirs(lbl_dir, exist_ok=True)
    os.makedirs(val_img_dir, exist_ok=True)
    os.makedirs(val_lbl_dir, exist_ok=True)

    # Save image
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{robot_id}_{timestamp}.jpg"
    filepath = os.path.join(img_dir, filename)

    image_bytes = base64.b64decode(image_b64)
    with open(filepath, "wb") as f:
        f.write(image_bytes)

    # Count existing images
    existing = len([f for f in os.listdir(img_dir) if f.endswith((".jpg", ".jpeg", ".png"))])

    return jsonify({
        "ok": True,
        "filename": filename,
        "dataset": dataset_name,
        "total_images": existing,
        "message": f"Image saved ({existing} total in '{dataset_name}')",
    })


@app.route("/api/training/batch-capture", methods=["POST"])
def batch_capture():
    """Capture multiple images from robot with delay between each.

    Expects JSON: {"robot_id": "R1", "dataset": "red_pepper", "count": 10, "delay": 1}
    """
    data = request.json or {}
    robot_id = data.get("robot_id", "R1")
    dataset_name = data.get("dataset", "").strip()
    count = int(data.get("count", 5))
    delay = float(data.get("delay", 1.0))

    if not dataset_name:
        return jsonify({"ok": False, "error": "Dataset name is required"}), 400
    if count < 1 or count > 50:
        return jsonify({"ok": False, "error": "Count must be 1-50"}), 400

    robot = ROBOTS.get(robot_id)
    if not robot or robot["host"] == "TBD":
        return jsonify({"ok": False, "error": f"Robot {robot_id} not available"}), 400

    dataset_name = dataset_name.replace(" ", "_")
    ds_path = _dataset_path(dataset_name)
    img_dir = os.path.join(ds_path, "train", "images")
    lbl_dir = os.path.join(ds_path, "train", "labels")
    val_img_dir = os.path.join(ds_path, "val", "images")
    val_lbl_dir = os.path.join(ds_path, "val", "labels")
    os.makedirs(img_dir, exist_ok=True)
    os.makedirs(lbl_dir, exist_ok=True)
    os.makedirs(val_img_dir, exist_ok=True)
    os.makedirs(val_lbl_dir, exist_ok=True)

    import paramiko
    import time as _t

    ssh_key = os.path.join(os.path.expanduser("~"), ".ssh", "id_rsa")
    rotate = robot.get("rotate", 0)
    rotate_code = ""
    if rotate == 180:
        rotate_code = "f = cv2.rotate(f, cv2.ROTATE_180); "
    elif rotate == 90:
        rotate_code = "f = cv2.rotate(f, cv2.ROTATE_90_CLOCKWISE); "

    capture_script = (
        'python3 -c "'
        'import cv2, sys; '
        'cap = cv2.VideoCapture(0); '
        'ret, f = cap.read(); '
        'cap.release(); '
        f'{rotate_code}'
        '_, jpg = cv2.imencode(chr(46)+chr(106)+chr(112)+chr(103), f, [cv2.IMWRITE_JPEG_QUALITY, 85]) if ret else (False, None); '
        'sys.stdout.buffer.write(jpg.tobytes()) if ret else sys.exit(1)'
        '"'
    )

    saved = 0
    errors = []

    for i in range(count):
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                hostname=robot["host"], username=robot["user"],
                key_filename=ssh_key,
                timeout=5, banner_timeout=5, auth_timeout=5,
                allow_agent=False, look_for_keys=False,
            )
            stdin, stdout, stderr = client.exec_command(capture_script, timeout=15)
            rc = stdout.channel.recv_exit_status()
            image_bytes = stdout.read()
            client.close()

            if rc == 0 and len(image_bytes) > 100:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
                filename = f"{robot_id}_{timestamp}.jpg"
                filepath = os.path.join(img_dir, filename)
                with open(filepath, "wb") as f:
                    f.write(image_bytes)
                saved += 1
            else:
                errors.append(f"Frame {i+1}: capture failed")

        except Exception as e:
            errors.append(f"Frame {i+1}: {str(e)[:50]}")

        if i < count - 1:
            _t.sleep(delay)

    total = len([f for f in os.listdir(img_dir) if f.endswith((".jpg", ".jpeg", ".png"))])

    return jsonify({
        "ok": True,
        "saved": saved,
        "errors": errors,
        "total_images": total,
        "dataset": dataset_name,
        "message": f"Saved {saved}/{count} images ({total} total in '{dataset_name}')",
    })


@app.route("/api/training/detection-history")
def detection_history():
    """Return recent detection history."""
    return jsonify({"ok": True, "history": _detection_history})


@app.route("/api/training/robots")
def list_robots():
    """List available robots and their connection status."""
    robot_status = []
    for rid, info in ROBOTS.items():
        if info["host"] == "TBD":
            robot_status.append({"id": rid, "host": info["host"], "online": False, "status": "Not configured"})
            continue
        # Quick ping check
        try:
            result = subprocess.run(
                ["ping", "-n", "1", "-w", "1000", info["host"]],
                capture_output=True, text=True, timeout=3,
            )
            online = result.returncode == 0
        except Exception:
            online = False
        robot_status.append({"id": rid, "host": info["host"], "online": online})
    return jsonify({"ok": True, "robots": robot_status})


@app.route("/api/training/label-images/<dataset_name>")
def label_images_list(dataset_name):
    """List unlabeled images in a dataset for annotation."""
    ds_path = _dataset_path(dataset_name)
    img_dir = os.path.join(ds_path, "train", "images")
    lbl_dir = os.path.join(ds_path, "train", "labels")

    if not os.path.isdir(img_dir):
        return jsonify({"ok": False, "error": "Dataset not found"}), 404

    IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp"}
    images = []
    for f in sorted(os.listdir(img_dir)):
        if os.path.splitext(f)[1].lower() in IMG_EXTS:
            lbl_file = os.path.splitext(f)[0] + ".txt"
            has_label = os.path.exists(os.path.join(lbl_dir, lbl_file))
            images.append({"filename": f, "labeled": has_label})

    labeled_count = sum(1 for img in images if img["labeled"])
    return jsonify({
        "ok": True,
        "dataset": dataset_name,
        "images": images,
        "total": len(images),
        "labeled": labeled_count,
        "unlabeled": len(images) - labeled_count,
    })


@app.route("/api/training/label-image/<dataset_name>/<filename>")
def serve_label_image(dataset_name, filename):
    """Serve an image file for labeling."""
    from flask import send_from_directory
    img_dir = os.path.join(_dataset_path(dataset_name), "train", "images")
    return send_from_directory(img_dir, filename)


@app.route("/api/training/save-label", methods=["POST"])
def save_label():
    """Save YOLO-format label for an image.

    Expects JSON: {"dataset": "red_pepper", "filename": "img.jpg",
                   "labels": [{"class_id": 0, "cx": 0.5, "cy": 0.5, "w": 0.3, "h": 0.4}],
                   "classes": ["red_pepper"]}
    """
    data = request.json or {}
    dataset_name = data.get("dataset", "")
    filename = data.get("filename", "")
    labels = data.get("labels", [])
    classes = data.get("classes", [])

    ds_path = _dataset_path(dataset_name)
    lbl_dir = os.path.join(ds_path, "train", "labels")
    os.makedirs(lbl_dir, exist_ok=True)

    lbl_file = os.path.splitext(filename)[0] + ".txt"
    lbl_path = os.path.join(lbl_dir, lbl_file)

    lines = []
    for lbl in labels:
        lines.append(f"{lbl['class_id']} {lbl['cx']:.6f} {lbl['cy']:.6f} {lbl['w']:.6f} {lbl['h']:.6f}")

    with open(lbl_path, "w") as f:
        f.write("\n".join(lines) + "\n" if lines else "")

    # Also save/update data.yaml for detection
    yaml_path = os.path.join(ds_path, "data.yaml")
    if classes:
        _write_data_yaml(ds_path, classes, dataset_name)

    return jsonify({"ok": True, "filename": lbl_file, "num_labels": len(labels)})


@app.route("/label/<dataset_name>")
def label_tool(dataset_name):
    """Serve the built-in labeling tool page."""
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Label Tool - {dataset_name}</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:'Segoe UI',sans-serif; background:#1a1a2e; color:#eee; }}
.topbar {{ background:#16213e; padding:12px 20px; display:flex; justify-content:space-between; align-items:center; border-bottom:2px solid #0f3460; }}
.topbar h1 {{ font-size:18px; color:#e94560; }}
.topbar .info {{ color:#888; font-size:13px; }}
.main {{ display:flex; height:calc(100vh - 54px); }}
.sidebar {{ width:280px; background:#16213e; border-right:1px solid #0f3460; overflow-y:auto; padding:12px; flex-shrink:0; }}
.sidebar h3 {{ color:#4ecca3; font-size:13px; margin-bottom:8px; }}
.img-item {{ padding:8px 10px; margin:2px 0; border-radius:6px; cursor:pointer; font-size:12px; display:flex; justify-content:space-between; align-items:center; }}
.img-item:hover {{ background:#0f3460; }}
.img-item.active {{ background:#0f3460; border-left:3px solid #e94560; }}
.img-item .dot {{ width:8px; height:8px; border-radius:50%; }}
.dot.labeled {{ background:#00e676; }}
.dot.unlabeled {{ background:#e94560; }}
.canvas-area {{ flex:1; display:flex; flex-direction:column; align-items:center; justify-content:center; padding:16px; position:relative; }}
canvas {{ border:1px solid #0f3460; cursor:crosshair; max-width:100%; }}
.controls {{ background:#16213e; padding:12px 20px; display:flex; gap:12px; align-items:center; border-top:1px solid #0f3460; flex-wrap:wrap; }}
.btn {{ display:inline-block; background:#0f3460; color:#4ecca3; padding:8px 16px; border:none; border-radius:6px; font-size:13px; cursor:pointer; }}
.btn:hover {{ background:#1a4a8a; }}
.btn.primary {{ background:#e94560; color:white; }}
.btn.primary:hover {{ background:#c73652; }}
.btn.green {{ background:#00e67633; border:1px solid #00e676; color:#00e676; }}
.btn.danger {{ background:#e9456033; border:1px solid #e94560; color:#e94560; }}
.class-input {{ background:#0a1929; border:1px solid #0f3460; color:#eee; padding:6px 10px; border-radius:6px; font-size:13px; width:150px; }}
.label-list {{ margin-top:8px; }}
.label-item {{ background:#0a1929; padding:6px 10px; margin:3px 0; border-radius:4px; font-size:11px; display:flex; justify-content:space-between; align-items:center; }}
.label-item .del {{ color:#e94560; cursor:pointer; }}
.progress {{ color:#4ecca3; font-size:13px; }}
.hint {{ color:#666; font-size:11px; margin-top:8px; }}
</style>
</head>
<body>
<div class="topbar">
    <h1>Label Tool: {dataset_name}</h1>
    <div class="info"><span class="progress" id="progress">Loading...</span></div>
    <div><a href="/" class="btn" style="text-decoration:none;">Back to Training</a></div>
</div>
<div class="main">
    <div class="sidebar">
        <h3>Images</h3>
        <div id="img-list">Loading...</div>
        <div class="hint" style="margin-top:12px;">Green = labeled, Red = unlabeled</div>
        <div style="margin-top:16px;">
            <h3>Classes</h3>
            <input type="text" class="class-input" id="class-name" value="red_pepper" style="width:100%;margin-top:4px;" />
            <div class="hint">Class name for new bounding boxes</div>
        </div>
        <div style="margin-top:16px;">
            <h3>Current Labels</h3>
            <div id="label-list" class="label-list"><span style="color:#555;font-size:11px;">No labels yet</span></div>
        </div>
    </div>
    <div style="flex:1;display:flex;flex-direction:column;">
        <div class="canvas-area" id="canvas-area">
            <canvas id="canvas" width="640" height="480"></canvas>
            <div class="hint" style="margin-top:8px;">Click and drag to draw bounding box. Right-click box to delete.</div>
        </div>
        <div class="controls">
            <button class="btn green" onclick="saveLabels()">Save Labels</button>
            <button class="btn" onclick="prevImage()">Prev</button>
            <button class="btn" onclick="nextImage()">Next</button>
            <button class="btn danger" onclick="clearLabels()">Clear All</button>
            <span style="color:#888;font-size:12px;" id="img-info">--</span>
        </div>
    </div>
</div>
<script>
const DATASET = "{dataset_name}";
const API = "/api/training";
let images = [];
let currentIdx = 0;
let currentLabels = [];  // [{{class_id, class_name, cx, cy, w, h}}]
let img = new Image();
let canvas, ctx;
let drawing = false;
let startX, startY, curX, curY;
let imgW, imgH, scale, offsetX, offsetY;

function init() {{
    canvas = document.getElementById('canvas');
    ctx = canvas.getContext('2d');
    canvas.addEventListener('mousedown', onMouseDown);
    canvas.addEventListener('mousemove', onMouseMove);
    canvas.addEventListener('mouseup', onMouseUp);
    canvas.addEventListener('contextmenu', onRightClick);
    loadImages();
}}

function loadImages() {{
    fetch(API + '/label-images/' + DATASET)
        .then(r => r.json())
        .then(data => {{
            images = data.images || [];
            document.getElementById('progress').textContent =
                data.labeled + '/' + data.total + ' labeled';
            renderImageList();
            if (images.length > 0) loadImage(0);
        }});
}}

function renderImageList() {{
    document.getElementById('img-list').innerHTML = images.map((img, i) =>
        `<div class="img-item ${{i === currentIdx ? 'active' : ''}}" onclick="loadImage(${{i}})">
            <span>${{img.filename.substring(0, 25)}}</span>
            <span class="dot ${{img.labeled ? 'labeled' : 'unlabeled'}}"></span>
        </div>`
    ).join('');
}}

function loadImage(idx) {{
    currentIdx = idx;
    currentLabels = [];
    const filename = images[idx].filename;
    document.getElementById('img-info').textContent = filename + ' (' + (idx+1) + '/' + images.length + ')';

    img = new Image();
    img.onload = () => {{
        imgW = img.naturalWidth;
        imgH = img.naturalHeight;
        // Fit canvas to image aspect ratio
        const area = document.getElementById('canvas-area');
        const maxW = area.clientWidth - 32;
        const maxH = area.clientHeight - 60;
        scale = Math.min(maxW / imgW, maxH / imgH, 1);
        canvas.width = Math.floor(imgW * scale);
        canvas.height = Math.floor(imgH * scale);
        offsetX = 0; offsetY = 0;
        // Load existing labels
        loadExistingLabels(filename);
        redraw();
    }};
    img.src = API + '/label-image/' + DATASET + '/' + filename;
    renderImageList();
}}

function loadExistingLabels(filename) {{
    // Check if label file exists by trying to load it
    const lblName = filename.replace(/\\.[^.]+$/, '.txt');
    // We'll just start fresh - labels are loaded when saved
    currentLabels = [];
    updateLabelList();
}}

function redraw() {{
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(img, 0, 0, canvas.width, canvas.height);

    // Draw existing labels
    currentLabels.forEach((lbl, i) => {{
        const x = (lbl.cx - lbl.w/2) * canvas.width;
        const y = (lbl.cy - lbl.h/2) * canvas.height;
        const w = lbl.w * canvas.width;
        const h = lbl.h * canvas.height;
        ctx.strokeStyle = '#00e676';
        ctx.lineWidth = 2;
        ctx.strokeRect(x, y, w, h);
        ctx.fillStyle = 'rgba(0,230,118,0.15)';
        ctx.fillRect(x, y, w, h);
        ctx.fillStyle = '#00e676';
        ctx.font = 'bold 14px sans-serif';
        ctx.fillText(lbl.class_name, x + 4, y - 4);
    }});

    // Draw current drawing box
    if (drawing) {{
        const x = Math.min(startX, curX);
        const y = Math.min(startY, curY);
        const w = Math.abs(curX - startX);
        const h = Math.abs(curY - startY);
        ctx.strokeStyle = '#e94560';
        ctx.lineWidth = 2;
        ctx.setLineDash([5, 5]);
        ctx.strokeRect(x, y, w, h);
        ctx.setLineDash([]);
    }}
}}

function onMouseDown(e) {{
    if (e.button !== 0) return;
    const rect = canvas.getBoundingClientRect();
    startX = e.clientX - rect.left;
    startY = e.clientY - rect.top;
    drawing = true;
}}

function onMouseMove(e) {{
    if (!drawing) return;
    const rect = canvas.getBoundingClientRect();
    curX = e.clientX - rect.left;
    curY = e.clientY - rect.top;
    redraw();
}}

function onMouseUp(e) {{
    if (!drawing) return;
    drawing = false;
    const rect = canvas.getBoundingClientRect();
    curX = e.clientX - rect.left;
    curY = e.clientY - rect.top;

    const x1 = Math.min(startX, curX) / canvas.width;
    const y1 = Math.min(startY, curY) / canvas.height;
    const x2 = Math.max(startX, curX) / canvas.width;
    const y2 = Math.max(startY, curY) / canvas.height;
    const w = x2 - x1;
    const h = y2 - y1;

    if (w < 0.01 || h < 0.01) return; // Too small

    const className = document.getElementById('class-name').value.trim() || 'object';
    currentLabels.push({{
        class_id: 0,
        class_name: className,
        cx: x1 + w/2,
        cy: y1 + h/2,
        w: w,
        h: h,
    }});
    updateLabelList();
    redraw();
}}

function onRightClick(e) {{
    e.preventDefault();
    const rect = canvas.getBoundingClientRect();
    const mx = (e.clientX - rect.left) / canvas.width;
    const my = (e.clientY - rect.top) / canvas.height;

    // Find and remove clicked label
    for (let i = currentLabels.length - 1; i >= 0; i--) {{
        const lbl = currentLabels[i];
        const x1 = lbl.cx - lbl.w/2;
        const y1 = lbl.cy - lbl.h/2;
        const x2 = lbl.cx + lbl.w/2;
        const y2 = lbl.cy + lbl.h/2;
        if (mx >= x1 && mx <= x2 && my >= y1 && my <= y2) {{
            currentLabels.splice(i, 1);
            updateLabelList();
            redraw();
            return;
        }}
    }}
}}

function updateLabelList() {{
    const list = document.getElementById('label-list');
    if (currentLabels.length === 0) {{
        list.innerHTML = '<span style="color:#555;font-size:11px;">No labels - draw a box!</span>';
        return;
    }}
    list.innerHTML = currentLabels.map((lbl, i) =>
        `<div class="label-item">
            <span>${{lbl.class_name}} (${{(lbl.w*100).toFixed(0)}}%x${{(lbl.h*100).toFixed(0)}}%)</span>
            <span class="del" onclick="deleteLabel(${{i}})">&times;</span>
        </div>`
    ).join('');
}}

function deleteLabel(idx) {{
    currentLabels.splice(idx, 1);
    updateLabelList();
    redraw();
}}

function clearLabels() {{
    currentLabels = [];
    updateLabelList();
    redraw();
}}

function saveLabels() {{
    const className = document.getElementById('class-name').value.trim() || 'object';
    const filename = images[currentIdx].filename;

    fetch(API + '/save-label', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{
            dataset: DATASET,
            filename: filename,
            labels: currentLabels,
            classes: [className],
        }})
    }})
    .then(r => r.json())
    .then(data => {{
        if (data.ok) {{
            images[currentIdx].labeled = true;
            renderImageList();
            const labeled = images.filter(i => i.labeled).length;
            document.getElementById('progress').textContent = labeled + '/' + images.length + ' labeled';
            // Auto-advance to next unlabeled
            const nextUnlabeled = images.findIndex((img, i) => i > currentIdx && !img.labeled);
            if (nextUnlabeled >= 0) loadImage(nextUnlabeled);
            else if (labeled < images.length) alert('Saved! Some images still need labels.');
            else alert('All images labeled! Go back and start training.');
        }}
    }});
}}

function prevImage() {{ if (currentIdx > 0) loadImage(currentIdx - 1); }}
function nextImage() {{ if (currentIdx < images.length - 1) loadImage(currentIdx + 1); }}

document.addEventListener('keydown', e => {{
    if (e.key === 'ArrowLeft' || e.key === 'a') prevImage();
    if (e.key === 'ArrowRight' || e.key === 'd') nextImage();
    if (e.key === 's' && (e.ctrlKey || e.metaKey)) {{ e.preventDefault(); saveLabels(); }}
    if (e.key === 'Delete') clearLabels();
}});

init();
</script>
</body>
</html>'''


def _detection_summary(detections):
    """Create a human-readable summary of detections."""
    if not detections:
        return "No objects detected"
    counts = {}
    for d in detections:
        counts[d["class"]] = counts.get(d["class"], 0) + 1
    parts = [f"{count} {cls}" + ("s" if count > 1 else "") for cls, count in sorted(counts.items())]
    return "Detected: " + ", ".join(parts)


def _time_ms():
    import time as _t
    return int(_t.time() * 1000)


def _safe_float(val):
    """Convert value to float or None."""
    if val is None:
        return None
    try:
        return round(float(val), 6)
    except (ValueError, TypeError):
        return None


# ============================================================
#  Unsupervised Learning
# ============================================================

@app.route("/api/training/unsupervised", methods=["POST"])
def unsupervised_learning():
    """Run unsupervised learning tasks: clustering, anomaly detection, similarity search."""
    import base64
    import numpy as np

    content_type = request.content_type or ""

    if "multipart" in content_type:
        # FormData with query image
        config = request.form.get("config", "{}")
        import json as _json
        data = _json.loads(config)
        query_file = request.files.get("query_image")
        if query_file:
            data["_query_bytes"] = query_file.read()
    else:
        data = request.json or {}

    task = data.get("task", "cluster")
    t_start = _time_ms()

    try:
        # Collect images
        images, image_names = _collect_unsup_images(data)
        if len(images) < 2:
            return jsonify({"ok": False, "error": f"Need at least 2 images, found {len(images)}"})

        # Extract features using YOLO backbone
        backbone_path = data.get("backbone", "yolov8n.pt")
        features = _extract_features(images, backbone_path)

        if task == "cluster":
            result = _run_clustering(images, image_names, features, data)
        elif task == "anomaly":
            result = _run_anomaly_detection(images, image_names, features, data)
        elif task == "similarity":
            result = _run_similarity_search(images, image_names, features, data)
        else:
            return jsonify({"ok": False, "error": f"Unknown task: {task}"})

        result["ok"] = True
        result["processing_ms"] = _time_ms() - t_start
        return jsonify(result)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)})


def _collect_unsup_images(data):
    """Collect images from dataset, folder, or capture."""
    import cv2
    images = []
    names = []

    source = data.get("source", "dataset" if data.get("dataset") else "folder")
    dataset_name = data.get("dataset", "")
    folder_path = data.get("folder", "")

    if dataset_name:
        ds_path = os.path.join(DATASETS_DIR, dataset_name)
        # Collect from train/images, val/images, or train/A, train/B etc.
        for split in ["train", "val"]:
            split_dir = os.path.join(ds_path, split)
            if not os.path.isdir(split_dir):
                continue
            img_dir = os.path.join(split_dir, "images")
            if os.path.isdir(img_dir):
                search_dirs = [img_dir]
            else:
                # Classification structure: train/classA/, train/classB/
                search_dirs = [os.path.join(split_dir, d) for d in os.listdir(split_dir)
                               if os.path.isdir(os.path.join(split_dir, d))]
            for sdir in search_dirs:
                for fname in os.listdir(sdir):
                    if fname.lower().endswith((".jpg", ".jpeg", ".png", ".bmp")):
                        fpath = os.path.join(sdir, fname)
                        img = cv2.imread(fpath)
                        if img is not None:
                            images.append(img)
                            names.append(fname)

    elif folder_path and os.path.isdir(folder_path):
        for fname in os.listdir(folder_path):
            if fname.lower().endswith((".jpg", ".jpeg", ".png", ".bmp")):
                fpath = os.path.join(folder_path, fname)
                img = cv2.imread(fpath)
                if img is not None:
                    images.append(img)
                    names.append(fname)
        # Also check subdirectories
        for subdir in os.listdir(folder_path):
            sub_path = os.path.join(folder_path, subdir)
            if os.path.isdir(sub_path):
                for fname in os.listdir(sub_path):
                    if fname.lower().endswith((".jpg", ".jpeg", ".png", ".bmp")):
                        fpath = os.path.join(sub_path, fname)
                        img = cv2.imread(fpath)
                        if img is not None:
                            images.append(img)
                            names.append(f"{subdir}/{fname}")

    return images, names


def _extract_features(images, backbone_path="yolov8n.pt"):
    """Extract feature vectors from images using YOLO backbone."""
    import cv2
    import numpy as np

    model = _get_yolo_model(backbone_path)
    if model is None:
        raise ValueError(f"Cannot load model: {backbone_path}")

    features = []
    for img in images:
        # Resize to standard size
        resized = cv2.resize(img, (224, 224))
        # Run through model and extract features
        results = model(resized, verbose=False)
        result = results[0]

        # Extract feature vector based on model type
        if hasattr(result, 'probs') and result.probs is not None:
            # Classification model — use probability vector as features
            feat = result.probs.data.cpu().numpy().flatten()
        else:
            # Detection model — use raw image features via model embed
            # Flatten boxes info as simple feature
            # Better approach: use model's intermediate features
            try:
                embed_results = model.embed(resized, verbose=False)
                if embed_results and len(embed_results) > 0:
                    feat = embed_results[0].cpu().numpy().flatten()
                else:
                    feat = _simple_image_features(resized)
            except Exception:
                feat = _simple_image_features(resized)

        features.append(feat)

    return np.array(features)


def _simple_image_features(img):
    """Fallback: extract simple color histogram features."""
    import cv2
    import numpy as np
    features = []
    for c in range(3):
        hist = cv2.calcHist([img], [c], None, [32], [0, 256])
        features.extend(hist.flatten())
    return np.array(features)


def _make_thumbnail(img, size=150):
    """Create base64 JPEG thumbnail."""
    import cv2
    import base64
    h, w = img.shape[:2]
    scale = min(size / w, size / h)
    resized = cv2.resize(img, (int(w * scale), int(h * scale)))
    _, buf = cv2.imencode(".jpg", resized, [cv2.IMWRITE_JPEG_QUALITY, 70])
    return base64.b64encode(buf.tobytes()).decode("utf-8")


def _run_clustering(images, names, features, data):
    """K-Means clustering with visualization."""
    import numpy as np
    from sklearn.cluster import KMeans
    from sklearn.preprocessing import StandardScaler

    k = int(data.get("k", 3))
    viz_type = data.get("viz", "tsne")

    # Normalize features
    scaler = StandardScaler()
    features_norm = scaler.fit_transform(features)

    # K-Means
    kmeans = KMeans(n_clusters=k, random_state=42, n_init=10)
    labels = kmeans.fit_predict(features_norm)

    # Build cluster info
    cluster_summary = []
    clusters = []
    for i in range(k):
        mask = labels == i
        idxs = np.where(mask)[0]
        cluster_images = []
        for idx in idxs[:12]:  # Max 12 thumbnails per cluster
            cluster_images.append({
                "name": names[idx],
                "thumbnail": _make_thumbnail(images[idx]),
            })
        cluster_summary.append({"cluster": i, "count": int(mask.sum())})
        clusters.append({"cluster": i, "images": cluster_images})

    # Visualization
    viz_image = _make_scatter_plot(features_norm, labels, names, k, viz_type)

    return {
        "task": "cluster",
        "k": k,
        "num_images": len(images),
        "cluster_summary": cluster_summary,
        "clusters": clusters,
        "viz_image": viz_image,
    }


def _run_anomaly_detection(images, names, features, data):
    """Anomaly detection using feature distance from centroid."""
    import numpy as np
    from sklearn.preprocessing import StandardScaler

    # Normalize
    scaler = StandardScaler()
    features_norm = scaler.fit_transform(features)

    # Compute centroid (mean of all features = "normal")
    centroid = features_norm.mean(axis=0)

    # Compute distance from centroid for each image
    distances = np.linalg.norm(features_norm - centroid, axis=1)

    # Determine threshold
    threshold = float(data.get("threshold", 0))
    if threshold <= 0:
        # Auto: mean + 2*std
        threshold = float(distances.mean() + 2 * distances.std())

    # Classify
    anomaly_mask = distances > threshold
    normal_count = int((~anomaly_mask).sum())
    anomaly_count = int(anomaly_mask.sum())

    # Build anomaly list (sorted by distance, highest first)
    anomaly_idxs = np.where(anomaly_mask)[0]
    anomaly_idxs = anomaly_idxs[np.argsort(-distances[anomaly_idxs])]

    anomalies = []
    for idx in anomaly_idxs[:20]:  # Max 20
        anomalies.append({
            "name": names[idx],
            "error": float(distances[idx]),
            "thumbnail": _make_thumbnail(images[idx]),
        })

    # Viz: distance distribution
    viz_image = _make_anomaly_plot(distances, threshold, names)

    return {
        "task": "anomaly",
        "num_images": len(images),
        "threshold": threshold,
        "num_normal": normal_count,
        "num_anomalies": anomaly_count,
        "anomalies": anomalies,
        "viz_image": viz_image,
    }


def _run_similarity_search(images, names, features, data):
    """Find most similar images to a query."""
    import numpy as np
    import cv2
    import base64

    topn = int(data.get("topn", 5))

    # Get query image
    query_feat = None
    if "_query_bytes" in data:
        nparr = np.frombuffer(data["_query_bytes"], np.uint8)
        query_img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    elif data.get("query_base64"):
        img_bytes = base64.b64decode(data["query_base64"])
        nparr = np.frombuffer(img_bytes, np.uint8)
        query_img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    else:
        return {"ok": False, "error": "No query image provided"}

    if query_img is None:
        return {"ok": False, "error": "Failed to decode query image"}

    # Extract features from query
    backbone = data.get("backbone", "yolov8n.pt")
    query_features = _extract_features([query_img], backbone)
    query_feat = query_features[0]

    # Compute cosine similarity
    from sklearn.preprocessing import normalize
    features_norm = normalize(features)
    query_norm = normalize(query_feat.reshape(1, -1))

    similarities = (features_norm @ query_norm.T).flatten()
    top_idxs = np.argsort(-similarities)[:topn]

    results = []
    for idx in top_idxs:
        results.append({
            "name": names[idx],
            "similarity": float(similarities[idx]),
            "thumbnail": _make_thumbnail(images[idx]),
        })

    return {
        "task": "similarity",
        "num_images": len(images),
        "results": results,
    }


def _make_scatter_plot(features, labels, names, k, viz_type="tsne"):
    """Create a 2D scatter plot of clusters."""
    import numpy as np
    import base64
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        # Dimensionality reduction
        if viz_type == "tsne" and len(features) > 5:
            from sklearn.manifold import TSNE
            perplexity = min(30, len(features) - 1)
            coords = TSNE(n_components=2, perplexity=perplexity, random_state=42).fit_transform(features)
        else:
            from sklearn.decomposition import PCA
            coords = PCA(n_components=2).fit_transform(features)

        fig, ax = plt.subplots(1, 1, figsize=(8, 6))
        colors = plt.cm.Set1(np.linspace(0, 1, k))

        for i in range(k):
            mask = labels == i
            ax.scatter(coords[mask, 0], coords[mask, 1], c=[colors[i]], label=f"Cluster {i} ({mask.sum()})",
                       s=60, alpha=0.7, edgecolors="white", linewidth=0.5)

        ax.set_title(f"Image Clustering ({viz_type.upper()}, K={k})", color="white", fontsize=14)
        ax.legend(fontsize=10, facecolor="#1a1a2e", edgecolor="#0f3460", labelcolor="white")
        ax.set_facecolor("#1a1a2e")
        fig.patch.set_facecolor("#16213e")
        ax.tick_params(colors="gray")
        ax.spines["bottom"].set_color("#0f3460")
        ax.spines["left"].set_color("#0f3460")
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)

        from io import BytesIO
        buf = BytesIO()
        fig.savefig(buf, format="png", dpi=100, bbox_inches="tight", facecolor=fig.get_facecolor())
        plt.close(fig)
        return base64.b64encode(buf.getvalue()).decode("utf-8")
    except ImportError as e:
        print(f"[WARN] Visualization skipped: {e}")
        return None


def _make_anomaly_plot(distances, threshold, names):
    """Create a bar chart of reconstruction errors."""
    import numpy as np
    import base64
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(1, 1, figsize=(10, 4))
        sorted_idxs = np.argsort(distances)
        sorted_dists = distances[sorted_idxs]
        colors_list = ["#4ecca3" if d <= threshold else "#e94560" for d in sorted_dists]

        ax.bar(range(len(sorted_dists)), sorted_dists, color=colors_list, alpha=0.8)
        ax.axhline(y=threshold, color="#ffa500", linestyle="--", linewidth=2, label=f"Threshold: {threshold:.4f}")
        ax.set_xlabel("Images (sorted)", color="gray")
        ax.set_ylabel("Distance from Normal", color="gray")
        ax.set_title("Anomaly Detection — Distance Distribution", color="white", fontsize=14)
        ax.legend(fontsize=10, facecolor="#1a1a2e", edgecolor="#0f3460", labelcolor="white")
        ax.set_facecolor("#1a1a2e")
        fig.patch.set_facecolor("#16213e")
        ax.tick_params(colors="gray")
        ax.spines["bottom"].set_color("#0f3460")
        ax.spines["left"].set_color("#0f3460")
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)

        from io import BytesIO
        buf = BytesIO()
        fig.savefig(buf, format="png", dpi=100, bbox_inches="tight", facecolor=fig.get_facecolor())
        plt.close(fig)
        return base64.b64encode(buf.getvalue()).decode("utf-8")
    except ImportError as e:
        print(f"[WARN] Visualization skipped: {e}")
        return None


# ============================================================
#  Main
# ============================================================

if __name__ == "__main__":
    print()
    print("=" * 55)
    print("  AJ Robotics — Dreamer YOLO Training Service")
    print(f"  Datasets: {DATASETS_DIR}")
    print(f"  Runs:     {RUNS_DIR}")
    print(f"  Port:     {PORT}")
    print("=" * 55)
    print()
    app.run(host="0.0.0.0", port=PORT, debug=False)
