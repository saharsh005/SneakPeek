"""
ui/app.py — SneakPeek Flask Backend  (FINAL)
=============================================
Fixes applied vs original:
  [FIX 1] Camera stream uses threaded CameraManager — no blocking/FB-OVF.
  [FIX 2] /api/sensor/event updates _state["sensor"] and SSE-pushes in real time.
  [FIX 3] Custom threats are evaluated before sending any alert.
  [FIX 4] Alert passes BOTH vision_result + sensor_data to AlertPipeline
           so custom threat conditions can match properly.

AI integration:
  Your existing AI pipeline (YOLO, pose, face-rec) runs inside _ai_pipeline().
  It must store results into _latest_vision so sensor events can pick them up.
"""

import json
import logging
import os
import queue
import threading
import time
from typing import Any
from flask import render_template
from flask import Flask, Response, jsonify, request, stream_with_context

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv()  # reads SneakPeek/.env

from engine.camera    import CameraManager, CameraConfig, mjpeg_generator
from engine.scorer    import ThreatScorer
from alert.aws_sender import AlertPipeline   # corrected pipeline

# ──────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s [%(threadName)s] %(name)s — %(levelname)s: %(message)s",
)
logger = logging.getLogger("sneakpeek.app")

# ──────────────────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder="static", template_folder="templates")

# ──────────────────────────────────────────────────────────────────────────────
# Shared state
# ──────────────────────────────────────────────────────────────────────────────
_state_lock = threading.RLock()
_state: dict[str, Any] = {
    "sensor": {
        "motion": False, "smoke_ppm": 0.0,
        "ldr": 4095,     "night": False, "smoke": False,
    },
    "threat": {
        "score": 0.0, "severity": "none", "alert": False,
        "base_reasons": [], "custom_threats": [],
        "top_threat_name": None, "top_threat_msg": None,
    },
    "camera":       {"online": False},
    "last_alert_ts": None,
}

# Latest vision result — written by _ai_pipeline, read by sensor_event
_latest_vision: dict = {
    "persons":        0,
    "poses":          [],
    "unknown_person": False,
    "face_ids":       [],
}
_vision_lock = threading.Lock()

ALERT_COOLDOWN_S = int(os.environ.get("ALERT_COOLDOWN_S", "60"))

# ──────────────────────────────────────────────────────────────────────────────
# SSE broadcast
# ──────────────────────────────────────────────────────────────────────────────
_sse_clients: list[queue.Queue] = []
_sse_lock    = threading.Lock()


def push_sse(event_type: str, data: dict) -> None:
    msg  = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
    dead = []
    with _sse_lock:
        for q in _sse_clients:
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _sse_clients.remove(q)


# ──────────────────────────────────────────────────────────────────────────────
# AI pipeline callback (plugs into CameraManager)
# ──────────────────────────────────────────────────────────────────────────────
def _ai_pipeline(frame):
    """
    Called by CameraManager for every processed frame.
    Replace the body with your actual YOLO + pose + face-rec code.
    Must write results into _latest_vision and return an annotated frame.
    """
    # ── YOUR EXISTING AI CODE GOES HERE ──────────────────────────────────────
    # Example (replace with real calls):
    #
    #   result = analyser.analyse(frame)          # VisionAnalyser from vision.py
    #   annotated = result.pop("annotated_frame") # your annotated frame
    #   with _vision_lock:
    #       _latest_vision.update(result)
    #   return annotated
    #
    # For now we return the frame unchanged and leave _latest_vision at defaults.
    return frame


# ──────────────────────────────────────────────────────────────────────────────
# Subsystem init
# ──────────────────────────────────────────────────────────────────────────────
_CAM_URL = os.environ.get("CAM_STREAM_URL", "http://192.168.1.100/stream")
_cam_cfg             = CameraConfig()
_cam_cfg.stream_url  = _CAM_URL
_cam_cfg.target_fps  = 8

camera  = CameraManager(stream_url=_CAM_URL, process_fn=_ai_pipeline, cfg=_cam_cfg)
scorer  = ThreatScorer()
pipeline = AlertPipeline()   # evaluate custom threats → S3 → SES


# ──────────────────────────────────────────────────────────────────────────────
# Background threads
# ──────────────────────────────────────────────────────────────────────────────
def _camera_health_monitor():
    prev = None
    while True:
        online = camera.is_online
        with _state_lock:
            _state["camera"]["online"] = online
        if online != prev:
            push_sse("camera_status", {"online": online})
            logger.info("Camera: %s", "ONLINE" if online else "OFFLINE")
            prev = online
        time.sleep(3)


_alert_queue: queue.Queue = queue.Queue(maxsize=5)


def _alert_worker():
    """
    Dequeues (vision_result, sensor_data, snapshot_jpeg) tuples.
    Calls AlertPipeline which: evaluates custom threats → S3 → SES.
    Only sends email if a custom threat matches.
    """
    while True:
        try:
            vision, sensors, snap = _alert_queue.get(timeout=5)
        except queue.Empty:
            continue
        try:
            result = pipeline.send_if_matched(
                snapshot_jpeg = snap,
                vision_result = vision,
                sensor_data   = sensors,
            )
            push_sse("alert_sent", {
                "matched":      [t["name"] for t in result["matched"]],
                "snapshot_url": result["snapshot_url"],
                "message_id":   result["message_id"],
                "error":        result["error"],
            })
            if result["matched"]:
                logger.info("Alert sent — threats: %s", [t["name"] for t in result["matched"]])
            else:
                logger.info("Alert suppressed — no custom threats matched.")
        except Exception:
            logger.exception("Alert worker error.")
        finally:
            _alert_queue.task_done()


def _try_dispatch_alert(vision: dict, sensors: dict) -> None:
    """Enqueue alert if cooldown has elapsed."""
    with _state_lock:
        last_ts = _state.get("last_alert_ts")
        now     = time.time()
        if last_ts and (now - last_ts) < ALERT_COOLDOWN_S:
            logger.debug("Alert cooldown active — skipping.")
            return
        _state["last_alert_ts"] = now

    snap = camera.snapshot_jpeg()
    try:
        _alert_queue.put_nowait((vision, sensors, snap))
    except queue.Full:
        logger.warning("Alert queue full — dropping.")


# ──────────────────────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/")
def home():
    return render_template("index.html")

@app.route("/video_feed")
def video_feed():
    return Response(
        stream_with_context(mjpeg_generator(camera)),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@app.route("/api/sse")
def sse_stream():
    client_q: queue.Queue = queue.Queue(maxsize=50)
    with _sse_lock:
        _sse_clients.append(client_q)
    logger.info("SSE client connected (total: %d)", len(_sse_clients))

    def _generate():
        with _state_lock:
            snap = json.loads(json.dumps(_state))
        yield f"event: init\ndata: {json.dumps(snap)}\n\n"
        try:
            while True:
                try:
                    yield client_q.get(timeout=25)
                except queue.Empty:
                    yield ": ping\n\n"
        except GeneratorExit:
            pass
        finally:
            with _sse_lock:
                try:
                    _sse_clients.remove(client_q)
                except ValueError:
                    pass
            logger.info("SSE client disconnected (total: %d)", len(_sse_clients))

    return Response(
        stream_with_context(_generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/sensor/event", methods=["POST"])
def sensor_event():
    """
    Receives JSON from ESP32 DevKit:
      { "motion": 1, "smoke_ppm": 145.2, "ldr": 3100 }

    Flow:
      1. Parse + update _state["sensor"]
      2. Push SSE sensor_update
      3. Merge with latest vision result
      4. Score with ThreatScorer (base rules)
      5. If threat.alert → dispatch alert worker
         (alert worker evaluates custom threats before sending email)
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400

    motion    = bool(data.get("motion", 0))
    smoke_ppm = float(data.get("smoke_ppm", 0.0))
    ldr       = int(data.get("ldr", 4095))
    night     = ldr < 400
    smoke     = smoke_ppm >= 300.0

    sensor_dict = {
        "motion": motion, "smoke_ppm": round(smoke_ppm, 1),
        "ldr": ldr, "night": night, "smoke": smoke,
    }

    with _state_lock:
        _state["sensor"].update(sensor_dict)
    push_sse("sensor_update", sensor_dict)

    # Grab latest vision result (written by _ai_pipeline on every frame)
    with _vision_lock:
        vision = dict(_latest_vision)

    # Build unified detection event for scorer
    detection_event = {
        **sensor_dict,
        "unknown_person": vision.get("unknown_person", False),
        "persons":        vision.get("persons", 0),
        "poses":          vision.get("poses", []),
        "raw_score":      0.0,
    }

    threat      = scorer.score(detection_event)
    threat_dict = threat.to_dict()

    with _state_lock:
        _state["threat"].update(threat_dict)
    push_sse("threat_update", threat_dict)

    # Dispatch alert (custom threat check happens inside the worker)
    if threat.alert:
        _try_dispatch_alert(vision, sensor_dict)

    return jsonify({"status": "ok", "threat_score": threat.score}), 200


@app.route("/api/state")
def get_state():
    with _state_lock:
        return jsonify(_state)


@app.route("/api/threats/reload", methods=["POST"])
def reload_threats():
    """Called from UI threat builder after saving custom_threats.json."""
    data = request.get_json(silent=True) or {}

    # If UI sent the threat list directly, persist it to disk first
    if "threats" in data:
        threats_path = os.environ.get("CUSTOM_THREATS_PATH", "./custom_threats.json")
        try:
            with open(threats_path, "w", encoding="utf-8") as fh:
                json.dump(data["threats"], fh, indent=2)
            logger.info("custom_threats.json saved (%d threats)", len(data["threats"]))
        except Exception:
            logger.exception("Could not save custom_threats.json")
            return jsonify({"status": "error", "message": "Could not save file"}), 500

    scorer.reload_custom_threats()
    count = len(scorer._loader.get_threats())
    return jsonify({"status": "reloaded", "threat_count": count})


@app.route("/api/camera/status")
def camera_status():
    return jsonify({"online": camera.is_online})


@app.route("/health")
def health():
    with _state_lock:
        return jsonify({
            "status":        "ok",
            "camera_online": _state["camera"]["online"],
            "sse_clients":   len(_sse_clients),
        })


# ──────────────────────────────────────────────────────────────────────────────
# Startup helper (called from main.py — NOT from __main__ guard)
# ──────────────────────────────────────────────────────────────────────────────
def start_background_services():
    camera.start()
    threading.Thread(target=_camera_health_monitor, name="CameraHealth", daemon=True).start()
    threading.Thread(target=_alert_worker,          name="AlertWorker",  daemon=True).start()
    logger.info("Background services started.")


# Allow running app.py directly for quick testing
if __name__ == "__main__":
    start_background_services()
    app.run(
        host    = "0.0.0.0",
        port    = int(os.environ.get("PORT", 5000)),
        debug   = False,
        threaded= True,
    )