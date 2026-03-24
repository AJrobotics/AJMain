"""Detect chart patterns using foduucom YOLOv8 model."""

import json
import os
from pathlib import Path

import cv2
from ultralytics import YOLO

# foduucom model classes
PATTERN_CLASSES = {
    0: "Head and Shoulders Bottom",
    1: "Head and Shoulders Top",
    2: "M_Head (Double Top)",
    3: "StockLine (Trend Line)",
    4: "Triangle",
    5: "W_Bottom (Double Bottom)",
}

DEFAULT_MODEL_PATH = os.path.join(os.path.dirname(__file__), "models", "model.pt")


class PatternDetector:
    def __init__(self, model_path: str = DEFAULT_MODEL_PATH, confidence: float = 0.3):
        self.model = YOLO(model_path)
        self.confidence = confidence
        print(f"Model loaded: {model_path} (conf={confidence})")

    def detect(self, image_path: str) -> list[dict]:
        """Run pattern detection on a single chart image."""
        results = self.model(image_path, conf=self.confidence, verbose=False)
        detections = []

        for result in results:
            boxes = result.boxes
            if boxes is None or len(boxes) == 0:
                continue

            for box in boxes:
                cls_id = int(box.cls[0])
                conf = float(box.conf[0])
                x1, y1, x2, y2 = box.xyxy[0].tolist()

                detections.append({
                    "class_id": cls_id,
                    "class_name": PATTERN_CLASSES.get(cls_id, f"class_{cls_id}"),
                    "confidence": round(conf, 4),
                    "bbox": [round(v, 1) for v in [x1, y1, x2, y2]],
                })

        return detections

    def detect_and_save(
        self, image_path: str, output_dir: str, ticker: str
    ) -> dict:
        """Detect patterns and save annotated image + JSON result."""
        os.makedirs(output_dir, exist_ok=True)
        detections = self.detect(image_path)

        # Save annotated image
        img = cv2.imread(image_path)
        for det in detections:
            x1, y1, x2, y2 = [int(v) for v in det["bbox"]]
            label = f'{det["class_name"]} {det["confidence"]:.2f}'
            color = (0, 255, 0) if "Bottom" in det["class_name"] else (0, 0, 255)
            cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
            cv2.putText(
                img, label, (x1, y1 - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2,
            )

        annotated_path = os.path.join(output_dir, f"{ticker}_detected.jpg")
        cv2.imwrite(annotated_path, img)

        # Save JSON result
        result = {
            "ticker": ticker,
            "source_image": image_path,
            "annotated_image": annotated_path,
            "num_detections": len(detections),
            "detections": detections,
        }
        json_path = os.path.join(output_dir, f"{ticker}.json")
        with open(json_path, "w") as f:
            json.dump(result, f, indent=2)

        return result


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python pattern_detector.py <image_path> [output_dir]")
        sys.exit(1)

    img = sys.argv[1]
    outdir = sys.argv[2] if len(sys.argv) > 2 else "results_temp"
    ticker = Path(img).stem

    detector = PatternDetector()
    result = detector.detect_and_save(img, outdir, ticker)
    print(f"\nDetections for {ticker}: {result['num_detections']}")
    for d in result["detections"]:
        print(f"  - {d['class_name']} (conf: {d['confidence']})")
