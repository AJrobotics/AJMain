"""Vision pipeline configuration — shared by server and client."""

# Vision server (runs on Christy)
VISION_SERVER_HOST = "0.0.0.0"
VISION_SERVER_PORT = 5100

# --- AI Backend ---
# Options: "yolo" (free, local), "gemini" (free, cloud), "claude" (paid, cloud)
VISION_BACKEND = "yolo"

# YOLO settings (FREE, local, no internet needed)
YOLO_MODEL = "yolov8n.pt"       # nano: fast, ~6MB  (options: yolov8n, yolov8s, yolov8m)
YOLO_CONFIDENCE = 0.25           # minimum confidence threshold (lower = detect more)

# Gemini settings (FREE - 15 req/min, requires API key)
GEMINI_MODEL = "gemini-2.0-flash"
GEMINI_MAX_TOKENS = 1024

# Claude settings (paid - fallback)
CLAUDE_MODEL = "claude-sonnet-4-20250514"
CLAUDE_MAX_TOKENS = 1024

# Default analysis prompt (for Gemini/Claude only)
DEFAULT_PROMPT = (
    "Describe what you see in this image. "
    "Identify objects, obstacles, people, or notable features. "
    "Be concise and practical for robot navigation."
)

# Image settings
JPEG_QUALITY = 75
MAX_IMAGE_SIZE = (1280, 960)    # resize before sending to API
THUMBNAIL_SIZE = (320, 240)     # for dashboard display

# Storage on Christy
IMAGE_STORE_DIR = "/home/ajrobotics/vision/images"
MAX_HISTORY_PER_ROBOT = 200

# Known robots with cameras
CAMERA_ROBOTS = {
    "R1": {"host": "192.168.1.82", "user": "dream", "camera": "/dev/video1", "rotate": 180},
    "ROSMASTER": {"host": "TBD", "user": "TBD", "camera": "/dev/video0", "rotate": 0},
}

# Christy connection (for client -> server)
CHRISTY_VISION_URL = "http://192.168.1.94:5100"
