"""
tests/simulate.py
-----------------
Simulates the full AI pipeline using local test images.
Use this to develop and test the engine without any ESP32 hardware.

Usage:
    python tests/simulate.py --image path/to/test.jpg [--night] [--smoke]

Options:
    --image   Path to JPEG/PNG to test (required)
    --night   Simulate night-time LDR reading (LDR=100)
    --smoke   Simulate smoke detection (ppm=500)
    --port    COM port to skip (default: simulation mode, no serial)
"""

import sys
import json
import logging
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("simulate")


def run(image_path: str, night: bool, smoke: bool):
    from engine.sensor_context import SensorContext
    from engine.cooldown        import CooldownManager
    from engine.detector        import PersonDetector
    from engine.recognizer      import FaceRecognizer
    from engine.pose            import PoseAnalyzer
    from engine.scorer          import ThreatScorer
    from alert.aws_sender       import AWSSender

    with open("config.json") as f:
        config = json.load(f)

    # ── Build sensor context with simulated readings ──────────────────
    ctx = SensorContext(config)
    ctx.update({
        "motion":     1,
        "smoke_ppm":  500.0 if smoke else 50.0,
        "ldr":        100   if night  else 3000,
    })
    ctx.tick()
    # Simulate 3 seconds of sustained motion
    import time
    ctx._motion_start = time.time() - 3
    ctx.tick()

    logger.info(f"Sensor context: {ctx.snapshot()}")

    # ── Load image ────────────────────────────────────────────────────
    img_path = Path(image_path)
    if not img_path.exists():
        logger.error(f"Image not found: {image_path}")
        sys.exit(1)

    with open(img_path, "rb") as f:
        jpeg_bytes = f.read()

    logger.info(f"Loaded image: {img_path.name} ({len(jpeg_bytes)} bytes)")

    # ── Run pipeline ──────────────────────────────────────────────────
    logger.info("─" * 50)
    logger.info("Step 1 — Person detection (YOLOv8)")
    detector   = PersonDetector()
    detections = detector.detect(jpeg_bytes)

    if not detections:
        logger.info("No people detected — pipeline stops here. No alert.")
        return

    logger.info(f"Found {len(detections)} person(s)")

    logger.info("─" * 50)
    logger.info("Step 2 — Face recognition (InsightFace)")
    recognizer   = FaceRecognizer(config)
    frame        = PersonDetector.get_frame(jpeg_bytes)
    face_results = recognizer.recognize_all(frame)
    for fr in face_results:
        status = "KNOWN" if fr["known"] else "UNKNOWN"
        logger.info(f"  {status}: {fr['name']} (similarity={fr['similarity']})")

    logger.info("─" * 50)
    logger.info("Step 3 — Pose analysis (MediaPipe)")
    pose_analyzer = PoseAnalyzer(config)
    pose_result   = pose_analyzer.analyze(frame, detections)
    logger.info(f"  Contact detected : {pose_result['contact_detected']} (iou={pose_result['contact_iou']})")
    logger.info(f"  Aggression score : {pose_result['aggression_score']}")

    logger.info("─" * 50)
    logger.info("Step 4 — Threat scoring")
    scorer       = ThreatScorer(config, ctx)
    score_result = scorer.score(face_results, pose_result, len(detections))
    logger.info(f"  Final score : {score_result['score']}")
    logger.info(f"  Is alert    : {score_result['is_alert']}")
    logger.info(f"  Threats     : {score_result['threats']}")
    for r in score_result['reasons']:
        logger.info(f"  Reason      : {r}")

    logger.info("─" * 50)
    if score_result["is_alert"]:
        logger.info("Step 5 — Sending to AWS (stub)")
        sender = AWSSender(config)
        sender.send(score_result, jpeg_bytes)
    else:
        logger.info("Step 5 — Score below threshold. No alert sent.")

    logger.info("─" * 50)
    logger.info("Simulation complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SneakPeek AI pipeline simulator")
    parser.add_argument("--image", required=True, help="Path to test image")
    parser.add_argument("--night", action="store_true", help="Simulate night-time")
    parser.add_argument("--smoke", action="store_true", help="Simulate smoke detection")
    args = parser.parse_args()

    run(args.image, args.night, args.smoke)
