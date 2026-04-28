"""
main.py — SneakPeek Entry Point
================================
Place this file at:  D:\SneakPeek\main.py

Run ONLY this file:
    python main.py

NEVER run sneakpeek_ui/app.py directly — it cannot reach the camera
because main.py already holds the stream connection, and ESP32-CAM
only supports ONE client at a time.

What this does:
  1. Loads .env from project root
  2. Validates env vars (warns, does not crash)
  3. Creates output directories
  4. Imports Flask app from sneakpeek_ui/app.py
  5. Starts all background threads (camera, alert worker, health monitor)
  6. Runs Flask
  7. Handles CTRL+C cleanly (stops camera threads)
"""

import logging
import os
import signal
import sys
import json

# ── 1. Load .env FIRST before any other import reads os.environ ───────────────
from dotenv import load_dotenv
load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s [%(threadName)s] %(name)s — %(levelname)s: %(message)s",
    handlers= [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("sneakpeek.log", "a"),
    ],
)
logger = logging.getLogger("sneakpeek.main")

# ── 2. Validate env vars ───────────────────────────────────────────────────────
REQUIRED = {
    "CAM_STREAM_URL":        "ESP32-CAM stream  e.g. http://10.224.162.150/stream",
    "AWS_ACCESS_KEY_ID":     "AWS access key",
    "AWS_SECRET_ACCESS_KEY": "AWS secret key",
    "AWS_REGION":            "AWS region  e.g. ap-south-1",
    "AWS_S3_BUCKET":         "S3 bucket name",
    "AWS_SES_SENDER":        "Verified SES sender email",
    "AWS_SES_RECIPIENT":     "Alert recipient email(s)",
}
missing = [k for k in REQUIRED if not os.environ.get(k)]
if missing:
    logger.warning("=" * 60)
    logger.warning("Missing env vars (set in .env):")
    for k in missing:
        logger.warning("  %-30s %s", k, REQUIRED[k])
    logger.warning("=" * 60)
else:
    logger.info("All required environment variables loaded.")

# ── 3. Create directories ─────────────────────────────────────────────────────
for d in [
    os.environ.get("SNAPSHOT_LOCAL_DIR", "./snapshots"),
    "./data",
    "./data/known_faces",
]:
    os.makedirs(d, exist_ok=True)

# Ensure custom_threats.json exists
threats_path = os.environ.get("CUSTOM_THREATS_PATH", "./data/custom_threats.json")
if not os.path.exists(threats_path):
    with open(threats_path, "w") as f:
        json.dump([], f)
    logger.info("Created empty custom_threats.json at %s", threats_path)

# ── 4. Import from the REAL app module: sneakpeek_ui/app.py ───────────────────
# Add project root to path so all engine/ alert/ imports resolve correctly
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sneakpeek_ui.app import app, camera, start_background_services

# ── 5. Graceful shutdown ───────────────────────────────────────────────────────
def _shutdown(signum, frame):
    logger.info("Received %s — shutting down SneakPeek…", signal.Signals(signum).name)
    try:
        camera.stop()
        logger.info("Camera threads stopped.")
    except Exception:
        logger.exception("Error stopping camera.")
    logger.info("Goodbye.")
    sys.exit(0)

signal.signal(signal.SIGINT,  _shutdown)
signal.signal(signal.SIGTERM, _shutdown)

# ── 6. Start ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    host = os.environ.get("HOST", "0.0.0.0")

    logger.info("Starting SneakPeek Security System")
    logger.info("  Camera URL : %s", os.environ.get("CAM_STREAM_URL", "(not set)"))
    logger.info("  Flask      : http://%s:%d", host, port)
    logger.info("  Threats    : %s", threats_path)
    logger.info("  Snapshots  : %s", os.environ.get("SNAPSHOT_LOCAL_DIR", "./snapshots"))

    start_background_services()

    app.run(
        host        = host,
        port        = port,
        debug       = False,
        threaded    = True,
        use_reloader= False,
    )