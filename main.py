"""
main.py — SneakPeek entry point (FINAL VERSION — WIFI ONLY)

Architecture:
  ESP32-CAM  → WiFi stream → Python (CameraEngine)
  ESP32 DevKit → HTTP POST → Flask (/api/sensor/event)

NO SERIAL
NO COM PORT
NO DEVICE COUPLING
"""

import json
import time
import logging
import argparse
import sys
import threading
import urllib.request

from engine.config_manager import cfg
from engine.state           import SystemState
from engine.cooldown        import CooldownManager
from engine.camera          import CameraEngine
from engine.detector        import PersonDetector
from engine.recognizer      import FaceRecognizer
from engine.pose            import PoseAnalyzer
from engine.scorer          import ThreatScorer
from alert.aws_sender       import AWSSender


# ─────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────
def setup_logging(debug: bool):
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("sneakpeek.log", encoding="utf-8"),
        ]
    )


# ─────────────────────────────────────────────────────────────
# Push updates to UI
# ─────────────────────────────────────────────────────────────
def notify_ui(payload: dict):
    try:
        body = json.dumps(payload).encode()
        req  = urllib.request.Request(
            "http://localhost:5000/api/pipeline/event",
            data=body,
            headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=0.5)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="SneakPeek engine")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    setup_logging(args.debug)
    logger = logging.getLogger("main")

    # Load config
    source    = "phone"   # FORCE WIFI CAMERA MODE
    phone_url = cfg.camera().get("phone_url", "")

    logger.info("=" * 52)
    logger.info("  SneakPeek — starting")
    logger.info(f"  Source : WIFI CAMERA")
    logger.info(f"  URL    : {phone_url}")
    logger.info("=" * 52)

    # ── State + cooldown ───────────────────────────────────────
    state    = SystemState()
    cooldown = CooldownManager()

    # ── AI models ──────────────────────────────────────────────
    logger.info("Loading AI models...")
    detector      = PersonDetector()
    recognizer    = FaceRecognizer(cfg.all())
    pose_analyzer = PoseAnalyzer(cfg.all())
    scorer        = ThreatScorer()
    sender        = AWSSender()
    logger.info("Models ready.")

    # Reload embeddings on config change
    cfg.on_reload(lambda _: recognizer.reload_embeddings())

    # ── Camera Engine (WiFi stream only) ───────────────────────
    camera = CameraEngine(
        state, cooldown,
        detector, recognizer, pose_analyzer, scorer, sender
    )
    camera.start()

    # ── Flask UI ───────────────────────────────────────────────
    try:
        import os
        import sys as _sys

        ui_path = os.path.join(os.path.dirname(__file__), "sneakpeek_ui")
        if ui_path not in _sys.path:
            _sys.path.insert(0, ui_path)

        from app import app as flask_app

        flask_thread = threading.Thread(
            target=lambda: flask_app.run(
                host="0.0.0.0",
                port=5000,
                debug=False,
                threaded=True,
                use_reloader=False
            ),
            daemon=True,
            name="flask"
        )
        flask_thread.start()

        logger.info("UI server started on http://localhost:5000")

    except Exception as e:
        logger.warning(f"Flask UI not started: {e}")

    # ── Push state to UI every 0.5s ────────────────────────────
    def push_state():
        while True:
            snap = state.snapshot()
            snap["event_type"] = "state_update"
            notify_ui(snap)
            time.sleep(0.5)

    threading.Thread(
        target=push_state,
        daemon=True,
        name="ui-push"
    ).start()

    logger.info("Engine running.")
    logger.info("Open http://localhost:5000 in your browser.")
    logger.info("Press Ctrl+C to stop.\n")

    try:
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        logger.info("Stopping...")
        camera.stop()


# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    main()