"""Nightly pipeline: generate charts -> detect patterns -> save results."""

import json
import os
import shutil
import sys
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from chart_generator import generate_charts
from pattern_detector import PatternDetector


def run_nightly(
    watchlist_path: str = None,
    base_output_dir: str = None,
    model_path: str = None,
    confidence: float = 0.3,
    days: int = 120,
):
    """Run the full nightly analysis pipeline."""
    if watchlist_path is None:
        watchlist_path = os.path.join(SCRIPT_DIR, "watchlist.txt")
    if base_output_dir is None:
        base_output_dir = os.path.join(SCRIPT_DIR, "results")
    if model_path is None:
        model_path = os.path.join(SCRIPT_DIR, "models", "model.pt")

    today = datetime.now().strftime("%Y-%m-%d")
    result_dir = os.path.join(base_output_dir, today)
    chart_dir = os.path.join(result_dir, "charts")

    print(f"=== YOLO Chart Pattern Analysis - {today} ===")
    print(f"Watchlist: {watchlist_path}")
    print(f"Output: {result_dir}")
    print()

    # Step 1: Generate chart images
    print("[Step 1/3] Generating chart images...")
    chart_results = generate_charts(watchlist_path, chart_dir, days=days)
    print()

    # Step 2: Run pattern detection
    print("[Step 2/3] Running YOLO pattern detection...")
    detector = PatternDetector(model_path=model_path, confidence=confidence)

    all_results = []
    for cr in chart_results:
        if cr["status"] != "ok":
            all_results.append({
                "ticker": cr["ticker"],
                "status": cr["status"],
                "detections": [],
            })
            continue

        result = detector.detect_and_save(cr["path"], result_dir, cr["ticker"])
        all_results.append({
            "ticker": cr["ticker"],
            "status": "ok",
            "num_detections": result["num_detections"],
            "detections": result["detections"],
        })

        if result["num_detections"] > 0:
            print(f"  ** {cr['ticker']}: {result['num_detections']} pattern(s) found!")
        else:
            print(f"  {cr['ticker']}: no patterns")

    print()

    # Step 3: Generate summary
    print("[Step 3/3] Generating summary...")
    patterns_found = [r for r in all_results if r.get("num_detections", 0) > 0]
    summary = {
        "date": today,
        "total_tickers": len(all_results),
        "tickers_with_patterns": len(patterns_found),
        "tickers_analyzed": sum(1 for r in all_results if r["status"] == "ok"),
        "highlights": [],
        "all_results": all_results,
    }

    for r in patterns_found:
        for d in r["detections"]:
            summary["highlights"].append({
                "ticker": r["ticker"],
                "pattern": d["class_name"],
                "confidence": d["confidence"],
            })

    # Sort highlights by confidence
    summary["highlights"].sort(key=lambda x: x["confidence"], reverse=True)

    summary_path = os.path.join(result_dir, "summary.json")
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)

    # Clean up temp chart dir
    shutil.rmtree(chart_dir, ignore_errors=True)

    # Print summary
    print(f"\n{'='*50}")
    print(f"SUMMARY - {today}")
    print(f"{'='*50}")
    print(f"Tickers analyzed: {summary['tickers_analyzed']}/{summary['total_tickers']}")
    print(f"Patterns found:   {summary['tickers_with_patterns']} tickers")

    if summary["highlights"]:
        print(f"\nHighlights:")
        for h in summary["highlights"]:
            print(f"  {h['ticker']:6s} | {h['pattern']:30s} | conf: {h['confidence']:.2f}")
    else:
        print("\nNo patterns detected today.")

    print(f"\nResults saved to: {result_dir}")
    return summary


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="YOLO Chart Pattern Nightly Analysis")
    parser.add_argument("--watchlist", default=None, help="Path to watchlist.txt")
    parser.add_argument("--output", default=None, help="Base output directory")
    parser.add_argument("--model", default=None, help="Path to YOLO model weights")
    parser.add_argument("--confidence", type=float, default=0.3, help="Detection confidence threshold")
    parser.add_argument("--days", type=int, default=120, help="Number of trading days for chart")
    args = parser.parse_args()

    run_nightly(
        watchlist_path=args.watchlist,
        base_output_dir=args.output,
        model_path=args.model,
        confidence=args.confidence,
        days=args.days,
    )
