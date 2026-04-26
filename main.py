"""
main.py — SneakPeek Entry Point
================================
Run this from the project root:

    python main.py                    # default port 5000
    PORT=8080 python main.py          # custom port

What it does:
  1. Loads .env
  2. Validates required environment variables (warns, does not crash)
  3. Starts the Flask app with all background threads via ui/app.py
  4. Handles SIGINT / SIGTERM for graceful shutdown
     (stops CameraManager threads cleanly before exit)

Project layout expected:
    SneakPeek/
    ├── main.py                ← this file
    ├── .env
    ├── custom_threats.json
    ├── engine/
    │   ├── camera.py
    │   ├── scorer.py
    │   └── vision.py          (optional, or use your own)
    ├── ui/
    │   ├── app.py
    │   ├── static/
    │   └── templates/
    ├── alert/
    │   └── aws_sender.py
    └── snapshots/             (auto-created)
"""

import logging
import os
import signal
import sys

# ── 1. Load .env before any other import reads os.environ ─────────────────────
from dotenv import load_dotenv
load_dotenv()

# ──────────────────────────────────────────────────────────────────────────────
# Logging — set up early so every module's logger inherits the format
# ──────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s [%(threadName)s] %(name)s — %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),           # console
        logging.FileHandler("sneakpeek.log", "a"),   # rolling log in project root
    ],
)
logger = logging.getLogger("sneakpeek.main")

# ──────────────────────────────────────────────────────────────────────────────
# 2. Validate environment (warn only — system still runs without AWS keys,
#    it just won't send emails until you add them)
# ──────────────────────────────────────────────────────────────────────────────
REQUIRED_ENV = {
    "CAM_STREAM_URL":       "ESP32-CAM MJPEG stream URL (e.g. http://192.168.1.100/stream)",
    "AWS_ACCESS_KEY_ID":    "AWS access key for S3 + SES",
    "AWS_SECRET_ACCESS_KEY":"AWS secret key",
    "AWS_REGION":           "AWS region (e.g. ap-south-1)",
    "AWS_S3_BUCKET":        "S3 bucket name for snapshots",
    "AWS_SES_SENDER":       "Verified SES sender email",
    "AWS_SES_RECIPIENT":    "Alert recipient email(s), comma-separated",
}

missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
if missing:
    logger.warning("=" * 60)
    logger.warning("Missing environment variables (set in .env or shell):")
    for k in missing:
        logger.warning("  %-30s  %s", k, REQUIRED_ENV[k])
    logger.warning("Camera stream and alerts may not work until these are set.")
    logger.warning("=" * 60)
else:
    logger.info("All required environment variables loaded.")

# ──────────────────────────────────────────────────────────────────────────────
# 3. Ensure output directories exist
# ──────────────────────────────────────────────────────────────────────────────
for d in [
    os.environ.get("SNAPSHOT_LOCAL_DIR", "./snapshots"),
    "engine/known_faces",     # face-recognition whitelist
]:
    os.makedirs(d, exist_ok=True)

# Ensure custom_threats.json exists (empty list if missing)
threats_path = os.environ.get("CUSTOM_THREATS_PATH", "./custom_threats.json")
if not os.path.exists(threats_path):
    import json
    with open(threats_path, "w") as fh:
        json.dump([], fh)
    logger.info("Created empty custom_threats.json at %s", threats_path)

# ──────────────────────────────────────────────────────────────────────────────
# 4. Import Flask app and subsystems
#    (done AFTER .env is loaded so env vars are available at import time)
# ──────────────────────────────────────────────────────────────────────────────
from sneakpeek_ui.app import app, camera, start_background_services

# ──────────────────────────────────────────────────────────────────────────────
# 5. Graceful shutdown handler
# ──────────────────────────────────────────────────────────────────────────────
def _shutdown(signum, frame):
    sig_name = signal.Signals(signum).name
    logger.info("Received %s — shutting down SneakPeek…", sig_name)

    try:
        camera.stop()
        logger.info("Camera threads stopped.")
    except Exception:
        logger.exception("Error stopping camera.")

    logger.info("Goodbye.")
    sys.exit(0)


signal.signal(signal.SIGINT,  _shutdown)
signal.signal(signal.SIGTERM, _shutdown)

# ──────────────────────────────────────────────────────────────────────────────
# 6. Start background threads then Flask
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    host = os.environ.get("HOST", "0.0.0.0")

    logger.info("Starting SneakPeek Security System")
    logger.info("  Camera URL : %s", os.environ.get("CAM_STREAM_URL", "(not set)"))
    logger.info("  Flask      : http://%s:%d", host, port)
    logger.info("  Threats    : %s", threats_path)
    logger.info("  Snapshots  : %s", os.environ.get("SNAPSHOT_LOCAL_DIR", "./snapshots"))

    start_background_services()

    # debug=False is mandatory — debug mode launches a second process
    # which breaks all daemon threads and the signal handlers.
    app.run(
        host    = host,
        port    = port,
        debug   = False,
        threaded= True,     # handle multiple requests concurrently (SSE + video_feed)
        use_reloader = False,
    )