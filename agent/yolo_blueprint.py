"""YOLO Chart Analyzer Blueprint - runs locally on CashCow."""

import glob
import json
import os
import re
import subprocess

from flask import Blueprint, Response, jsonify, render_template, request

yolo_bp = Blueprint("yolo", __name__)

_YOLO_DIR = "/home/dongchul/yolo_chart"
_YOLO_ACTIVATE = "source /home/dongchul/yolo_venv/bin/activate"


@yolo_bp.route("/yolo")
def yolo_page():
    return render_template("yolo_analyzer.html")


@yolo_bp.route("/api/yolo/status")
def yolo_status():
    return jsonify({"online": os.path.isfile(f"{_YOLO_DIR}/models/model.pt")})


@yolo_bp.route("/api/yolo/history")
def yolo_history():
    try:
        dates = []
        for path in sorted(glob.glob(f"{_YOLO_DIR}/results/*/summary.json"), reverse=True):
            date_str = os.path.basename(os.path.dirname(path))
            with open(path) as f:
                s = json.load(f)
            dates.append({
                "date": date_str,
                "patterns_found": s.get("tickers_with_patterns", 0),
            })
        return jsonify({"dates": dates})
    except Exception as e:
        return jsonify({"dates": [], "error": str(e)})


@yolo_bp.route("/api/yolo/watchlist", methods=["GET", "POST"])
def yolo_watchlist():
    watchlist_path = f"{_YOLO_DIR}/watchlist.txt"
    if request.method == "GET":
        try:
            with open(watchlist_path) as f:
                text = f.read()
            tickers = [
                line.strip() for line in text.split("\n")
                if line.strip() and not line.strip().startswith("#")
            ]
            return jsonify({"tickers": tickers})
        except Exception as e:
            return jsonify({"tickers": [], "error": str(e)})
    else:
        tickers = request.json.get("tickers", [])
        content = "# YOLO Chart Analyzer Watchlist\n" + "\n".join(tickers) + "\n"
        try:
            with open(watchlist_path, "w") as f:
                f.write(content)
            return jsonify({"status": "ok"})
        except Exception as e:
            return jsonify({"status": "error", "error": str(e)})


@yolo_bp.route("/api/yolo/run", methods=["POST"])
def yolo_run():
    data = request.json or {}
    tickers_input = data.get("tickers", "watchlist")
    days = data.get("days", 120)
    confidence = data.get("confidence", 0.3)

    try:
        base = f"{_YOLO_ACTIVATE} && cd {_YOLO_DIR}"

        if tickers_input.lower() == "watchlist":
            cmd = f"{base} && python run_nightly.py --days {int(days)} --confidence {float(confidence)}"
        else:
            ticker_list = [t.strip().upper() for t in tickers_input.split(",") if t.strip()]
            ticker_str = "\n".join(ticker_list)
            cmd = (
                f"echo '{ticker_str}' > /tmp/yolo_custom_watchlist.txt && "
                f"{base} && python run_nightly.py --watchlist /tmp/yolo_custom_watchlist.txt "
                f"--days {int(days)} --confidence {float(confidence)}"
            )

        result = subprocess.run(
            ["bash", "-c", cmd], capture_output=True, text=True, timeout=300
        )
        out, err = result.stdout, result.stderr

        date_match = re.search(r"(\d{4}-\d{2}-\d{2})", out)
        date_str = date_match.group(1) if date_match else ""

        summary = None
        if date_str:
            try:
                with open(f"{_YOLO_DIR}/results/{date_str}/summary.json") as f:
                    summary = json.load(f)
            except Exception:
                pass

        return jsonify({
            "status": "ok",
            "date": date_str,
            "log": out + ("\n" + err if err.strip() else ""),
            "summary": summary,
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)})


@yolo_bp.route("/api/yolo/results/<date>")
def yolo_results(date):
    try:
        with open(f"{_YOLO_DIR}/results/{date}/summary.json") as f:
            summary = json.load(f)
        return jsonify({"summary": summary})
    except Exception as e:
        return jsonify({"summary": None, "error": str(e)})


@yolo_bp.route("/api/yolo/image/<date>/<filename>")
def yolo_image(date, filename):
    try:
        filepath = f"{_YOLO_DIR}/results/{date}/{filename}"
        with open(filepath, "rb") as f:
            data = f.read()
        content_type = "image/jpeg" if filename.endswith(".jpg") else "image/png"
        return Response(data, content_type=content_type)
    except Exception as e:
        return Response(f"Image not found: {e}", status=404)
