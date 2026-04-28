"""
sneakpeek_ui/app.py — SneakPeek Flask Backend (COMPLETE + FULLY INTEGRATED)
=============================================================================
Run ONLY via:  python main.py  (from project root D:\SneakPeek\)
NEVER run this file directly — it cannot reach the camera alone.

All routes:
  GET  /                      → index.html (the UI)
  GET  /video_feed            → MJPEG camera stream
  GET  /api/sse               → Server-Sent Events (live updates)
  GET  /api/state             → full current state
  GET  /api/alerts            → alert history (data/alerts.json)
  POST /api/alerts/clear      → clear alert history
  GET  /api/faces             → enrolled face names
  POST /api/sensor/event      → ESP32 DevKit sensor data
  POST /api/threats/reload    → save + reload custom_threats.json
  GET  /api/camera/status     → {online: bool}
  GET  /api/config            → read config.json
  POST /api/config            → save config.json
  GET  /health                → health check

Vision pipeline (real AI — uses your existing engine modules):
  - engine/detector.py  → YOLO person detection
  - engine/recognizer.py → face recognition
  - engine/pose.py       → pose analysis
  These run inside _ai_pipeline() on every camera frame (Thread 2).
  Results are stored in _latest_vision and merged with sensor data
  on every /api/sensor/event call before scoring.
"""

import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import cv2
import numpy as np
from flask import Flask, Response, jsonify, render_template, request, send_from_directory, stream_with_context
from werkzeug.utils import secure_filename

from dotenv import load_dotenv
load_dotenv()

# ── engine + alert imports ────────────────────────────────────────────────────
from engine.camera    import CameraManager, CameraConfig, mjpeg_generator
from engine.config_manager import cfg
from engine.receiver  import SerialReceiver
from engine.scorer    import ThreatScorer
from alert.aws_sender import AWSSender

logger = logging.getLogger("sneakpeek.app")

# ── Paths ─────────────────────────────────────────────────────────────────────
_HERE           = os.path.dirname(os.path.abspath(__file__))
_ROOT           = os.path.dirname(_HERE)
DATA_DIR        = os.environ.get("DATA_DIR",            os.path.join(os.path.dirname(_HERE), "data"))
ALERTS_FILE     = os.path.join(DATA_DIR, "alerts.json")
KNOWN_FACES_DIR = os.path.join(DATA_DIR, "known_faces")
CONFIG_FILE     = os.environ.get("CONFIG_FILE",         os.path.join(os.path.dirname(_HERE), "config.json"))
THREATS_PATH    = os.environ.get("CUSTOM_THREATS_PATH", os.path.join(os.path.dirname(_HERE), "data", "custom_threats.json"))
SNAPSHOT_DIR    = os.environ.get("SNAPSHOT_LOCAL_DIR", os.path.join(os.path.dirname(_HERE), "snapshots"))

if not os.path.isabs(SNAPSHOT_DIR):
    SNAPSHOT_DIR = os.path.abspath(os.path.join(_ROOT, SNAPSHOT_DIR))

os.makedirs(DATA_DIR,        exist_ok=True)
os.makedirs(KNOWN_FACES_DIR, exist_ok=True)
os.makedirs(SNAPSHOT_DIR,    exist_ok=True)
_ENROLL_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}

# Bootstrap runtime AWS recipient from config.json on startup.
try:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as _cf:
            _sync_boot_cfg = json.load(_cf)
        # helper defined below; call via deferred local function once available
except Exception:
    _sync_boot_cfg = {}

# ── Flask ─────────────────────────────────────────────────────────────────────
app = Flask(__name__,
            static_folder  = os.path.join(_HERE, "static"),
            template_folder= os.path.join(_HERE, "templates"))

# ── Shared state ──────────────────────────────────────────────────────────────
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
    "camera":        {"online": False},
    "last_alert_ts": None,
}
_last_sensor_ts = 0.0
_last_sensor_log_ts = 0.0

# Latest vision result — written by _ai_pipeline every frame
# read by sensor_event on every sensor POST
_latest_vision: dict = {
    "persons": 0, "poses": [], "unknown_person": False, "face_ids": [],
}
_vision_lock = threading.Lock()
_last_ai_state_push = 0.0

ALERT_COOLDOWN_S = int(os.environ.get("ALERT_COOLDOWN_S", "60"))

# ── SSE ───────────────────────────────────────────────────────────────────────
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


# ── Alerts log ────────────────────────────────────────────────────────────────
_alerts_lock = threading.Lock()


def _load_alerts() -> list:
    try:
        if os.path.exists(ALERTS_FILE):
            with open(ALERTS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        logger.exception("Failed to load alerts.json")
    return []


def _append_alert(entry: dict) -> None:
    with _alerts_lock:
        alerts = _load_alerts()
        alerts.insert(0, entry)
        alerts = alerts[:200]
        try:
            with open(ALERTS_FILE, "w", encoding="utf-8") as f:
                json.dump(alerts, f, indent=2)
        except Exception:
            logger.exception("Failed to save alerts.json")


def _save_local_snapshot(jpeg: bytes | None) -> str | None:
    if not jpeg:
        return None
    try:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        fname = f"snapshot_{ts}_{uuid.uuid4().hex[:8]}.jpg"
        path = os.path.join(SNAPSHOT_DIR, fname)
        with open(path, "wb") as f:
            f.write(jpeg)
        return fname
    except Exception:
        logger.exception("Failed to save local snapshot")
        return None


def _sync_runtime_aws_recipients_from_config(conf: dict[str, Any]) -> None:
    """
    Keep AWS email recipients in sync with UI config without requiring restart.
    Expects config schema:
      {"email_provider": "smtp|aws", "aws": {"alert_email": "..."}}
    """
    try:
        provider = (conf.get("email_provider") or conf.get("aws", {}).get("email_provider") or "").strip().lower()
        if provider in {"smtp", "aws"}:
            os.environ["EMAIL_PROVIDER"] = provider
            logger.info("Runtime email provider updated from config: %s", provider)

        smtp_cfg = conf.get("smtp", {}) if isinstance(conf, dict) else {}
        for env_key, cfg_key in [
            ("SMTP_FROM", "from"),
            ("SMTP_HOST", "host"),
            ("SMTP_PORT", "port"),
            ("SMTP_USER", "user"),
            ("SMTP_PASS", "pass"),
            ("SMTP_USE_TLS", "use_tls"),
        ]:
            if cfg_key in smtp_cfg and smtp_cfg[cfg_key] not in [None, ""]:
                os.environ[env_key] = str(smtp_cfg[cfg_key])

        aws_cfg = conf.get("aws", {}) if isinstance(conf, dict) else {}
        alert_email = (aws_cfg.get("alert_email") or "").strip()
        if alert_email:
            os.environ["AWS_SES_RECIPIENT"] = alert_email
            logger.info("Runtime AWS recipient updated from config: %s", alert_email)
    except Exception:
        logger.exception("Failed to sync runtime AWS recipient from config")


if isinstance(globals().get("_sync_boot_cfg", None), dict):
    _sync_runtime_aws_recipients_from_config(globals().get("_sync_boot_cfg", {}))


# ── AI pipeline ───────────────────────────────────────────────────────────────
# Try to import your existing engine modules. Falls back gracefully if missing.
try:
    from engine.detector   import PersonDetector
    from engine.recognizer import FaceRecognizer
    from engine.pose       import PoseAnalyzer

    _detector   = PersonDetector(confidence=float(cfg.thresholds().get("yolo_confidence", 0.45)))
    _recognizer = FaceRecognizer(cfg.all())
    _pose       = PoseAnalyzer(cfg.all())
    _AI_AVAILABLE = True
    logger.info("AI pipeline loaded: detector + recognizer + pose")
except Exception as _e:
    _AI_AVAILABLE = False
    logger.warning("AI pipeline not fully available (%s) — vision disabled, camera stream still works.", _e)


def _ai_pipeline(frame):
    """
    Called by FrameProcessor (Thread 2) for every camera frame.

    If your original detector/recognizer/pose modules are present,
    they run here. Results are stored in _latest_vision so that
    sensor_event can merge them when scoring threats.

    Returns an annotated frame for the /video_feed stream.
    """
    global _latest_vision, _last_ai_state_push

    if not _AI_AVAILABLE:
        return frame   # passthrough — stream works, AI disabled

    try:
        # ── Run your original AI code ─────────────────────────────────────
        # Adjust these calls to match your actual module APIs.
        # The typical pattern from your original codebase:

        ok, enc = cv2.imencode(".jpg", frame)
        if not ok:
            return frame
        det_result = _detector.detect(enc.tobytes())
        rec_result = _recognizer.recognize_all(frame)
        pose_result = _pose.analyze(frame, det_result)
        unknown_person = any(not f.get("known", False) for f in rec_result)
        face_ids = [f.get("name") for f in rec_result if f.get("known")]
        poses = []
        if pose_result.get("aggression_score", 0.0) >= float(cfg.thresholds().get("aggression_score_min", 0.6)):
            poses.append("aggressive")
        if pose_result.get("contact_detected"):
            poses.append("contact")

        vision = {
            "persons":        int(len(det_result)),
            "poses":          poses,
            "unknown_person": bool(unknown_person),
            "face_ids":       face_ids,
        }

        with _vision_lock:
            _latest_vision = vision

        # Draw person boxes from YOLO
        for det in det_result:
            x1, y1, x2, y2 = det.get("bbox", [0, 0, 0, 0])
            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 180, 255), 2)
            cv2.putText(
                frame,
                f"PERSON {det.get('conf', 0.0):.2f}",
                (x1, max(18, y1 - 8)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                (0, 180, 255),
                2,
                cv2.LINE_AA,
            )

        # Draw face boxes and KNOWN/UNKNOWN labels
        for face in rec_result:
            fx1, fy1, fx2, fy2 = face.get("bbox", [0, 0, 0, 0])
            known = bool(face.get("known", False))
            name = face.get("name", "unknown")
            sim = float(face.get("similarity", 0.0))
            color = (50, 210, 90) if known else (60, 60, 240)
            label = f"KNOWN: {name} ({sim:.2f})" if known else f"UNKNOWN ({sim:.2f})"
            cv2.rectangle(frame, (fx1, fy1), (fx2, fy2), color, 2)
            cv2.putText(
                frame,
                label,
                (fx1, max(18, fy1 - 8)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                color,
                2,
                cv2.LINE_AA,
            )
        now = time.time()
        if now - _last_ai_state_push >= 1.0:
            push_sse("state_update", _legacy_state_snapshot())
            _last_ai_state_push = now

        return frame

    except Exception:
        logger.exception("AI pipeline error — using raw frame.")
        return frame


# ── Camera + scorer + AWS ─────────────────────────────────────────────────────
_CAM_URL = os.environ.get("CAM_STREAM_URL", "http://192.168.1.100/stream")

_cam_cfg                  = CameraConfig()
_cam_cfg.stream_url       = _CAM_URL
_cam_cfg.target_fps       = 8
_cam_cfg.read_timeout_s   = 30.0
_cam_cfg.online_timeout_s = 15.0

camera  = CameraManager(stream_url=_CAM_URL, process_fn=_ai_pipeline, cfg=_cam_cfg)
scorer  = ThreatScorer()
aws     = AWSSender()


# ── Background threads ────────────────────────────────────────────────────────
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
_serial_rx: Any = None


def _alert_worker():
    while True:
        try:
            payload, snap = _alert_queue.get(timeout=5)
        except queue.Empty:
            continue
        try:
            ts = datetime.now(timezone.utc).isoformat()
            local_snapshot = _save_local_snapshot(snap)
            sensor = dict(payload.get("sensor", {}))
            _append_alert({
                "timestamp": ts,
                "score": float(payload.get("score", 0.0)),
                "severity": payload.get("severity", "medium"),
                "threats": [t.get("name", "") for t in payload.get("custom_threats", [])] or payload.get("base_reasons", []),
                "reasons": payload.get("base_reasons", []),
                "snapshot": local_snapshot,
                "sensor": {
                    **sensor,
                    "is_night": sensor.get("night", sensor.get("is_night", False)),
                    "motion_duration_s": sensor.get("motion_duration_s", 0.0),
                },
                "email_sent": False,
            })

            result = aws.send_alert(payload, snap)
            push_sse("alert_sent", {**result, "timestamp": ts})
            if result.get("success"):
                logger.info("Alert sent — MessageId: %s", result.get("message_id"))
        except Exception:
            logger.exception("Alert worker error.")
        finally:
            _alert_queue.task_done()


def _try_dispatch_alert(threat_dict: dict, sensor_dict: dict) -> None:
    with _state_lock:
        last_ts = _state.get("last_alert_ts")
        now     = time.time()
        if last_ts and (now - last_ts) < ALERT_COOLDOWN_S:
            logger.debug("Alert cooldown active.")
            return
        _state["last_alert_ts"] = now

    payload = {**threat_dict, "sensor": sensor_dict}
    snap = camera.snapshot_jpeg()
    if snap is None:
        raw = camera.get_raw_frame()
        if raw is not None:
            ok, enc = cv2.imencode(".jpg", raw, [cv2.IMWRITE_JPEG_QUALITY, 90])
            if ok:
                snap = enc.tobytes()
    try:
        _alert_queue.put_nowait((payload, snap))
    except queue.Full:
        logger.warning("Alert queue full — dropping.")


def _legacy_state_snapshot() -> dict[str, Any]:
    with _state_lock:
        s = dict(_state["sensor"])
        t = dict(_state["threat"])
        c = dict(_state["camera"])
    with _vision_lock:
        v = dict(_latest_vision)
    return {
        "sensor": {
            "motion": s.get("motion", False),
            "smoke": s.get("smoke", False),
            "smoke_ppm": s.get("smoke_ppm", 0.0),
            "ldr": s.get("ldr", 4095),
            "is_night": s.get("night", False),
            "motion_duration_s": s.get("motion_duration_s", 0.0),
        },
        "pipeline": {
            "stage": "alert" if t.get("alert") else "scoring",
            "score": t.get("score", 0.0),
            "last_threats": [ct.get("name", "") for ct in t.get("custom_threats", [])] or t.get("base_reasons", []),
            "person_count": v.get("persons", 0),
            "unknown_count": 1 if v.get("unknown_person", False) else 0,
            "fps": 0,
        },
        "system": {
            "running": True,
            "last_update": time.time(),
        },
        "camera": {"online": c.get("online", False)},
    }


def _process_sensor_payload(data: dict[str, Any]) -> tuple[dict[str, Any], float]:
    global _last_sensor_ts, _last_sensor_log_ts
    motion = bool(data.get("motion", 0))
    smoke_ppm = float(data.get("smoke_ppm", 0.0))
    ldr = int(data.get("ldr", 4095))
    night_max = int(cfg.thresholds().get("night_ldr_max", 400))
    smoke_thr = float(cfg.thresholds().get("smoke_ppm_threshold", 300.0))
    night = ldr < night_max
    smoke = smoke_ppm >= smoke_thr
    now = time.time()
    if motion:
        motion_dur = float(_state["sensor"].get("motion_duration_s", 0.0)) + max(0.0, now - _last_sensor_ts) if _last_sensor_ts else 0.0
    else:
        motion_dur = 0.0
    _last_sensor_ts = now

    sensor_dict = {
        "motion": motion, "smoke_ppm": round(smoke_ppm, 1),
        "ldr": ldr, "night": night, "smoke": smoke, "motion_duration_s": round(motion_dur, 1),
    }
    with _state_lock:
        _state["sensor"].update(sensor_dict)
        _state["sensor"]["last_seen"] = now
    push_sse("sensor_update", sensor_dict)
    if now - _last_sensor_log_ts >= 5.0:
        logger.info("[sensor] motion=%s smoke_ppm=%.1f ldr=%d", motion, smoke_ppm, ldr)
        _last_sensor_log_ts = now

    with _vision_lock:
        vision = dict(_latest_vision)
    detection_event = {
        **sensor_dict,
        "unknown_person": vision.get("unknown_person", False),
        "persons": vision.get("persons", 0),
        "poses": vision.get("poses", []),
        "face_ids": vision.get("face_ids", []),
        "raw_score": 0.0,
    }
    threat = scorer.score(detection_event)
    threat_dict = threat.to_dict()
    with _state_lock:
        _state["threat"].update(threat_dict)
    push_sse("threat_update", threat_dict)
    push_sse("state_update", _legacy_state_snapshot())
    if threat.alert:
        _try_dispatch_alert(threat_dict, sensor_dict)
    return threat_dict, threat.score


class _SerialStateAdapter:
    def update_from_serial(self, packet: dict):
        if packet.get("type") == "sensor" or all(k in packet for k in ("motion", "smoke_ppm", "ldr")):
            _process_sensor_payload(packet)


def _serial_on_frame(_jpeg_bytes: bytes) -> None:
    return


def start_background_services():
    """Called by main.py after import."""
    global _serial_rx
    camera.start()
    _serial_rx = SerialReceiver(_SerialStateAdapter(), _serial_on_frame)
    _serial_rx.start()
    threading.Thread(target=_camera_health_monitor,
                     name="CameraHealth", daemon=True).start()
    threading.Thread(target=_alert_worker,
                     name="AlertWorker",  daemon=True).start()
    logger.info("Background services started.")


# ══════════════════════════════════════════════════════════════════════════════
# Routes
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    """Serve the main UI page."""
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
        yield f"event: init\ndata: {json.dumps(_legacy_state_snapshot())}\n\n"
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
        mimetype  = "text/event-stream",
        headers   = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/sensor/event", methods=["POST"])
def sensor_event():
    """
    Receives JSON from ESP32 DevKit:
      {"motion": 1, "smoke_ppm": 145.2, "ldr": 3100}

    Flow:
      1. Parse + update _state["sensor"] + SSE push
      2. Merge with latest vision result from _ai_pipeline
      3. Score with ThreatScorer (base rules + custom threats)
      4. SSE push threat_update
      5. If alert → dispatch to alert worker (AWS S3 + SES)
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400

    threat_dict, threat_score = _process_sensor_payload(data)
    return jsonify({"status": "ok", "threat_score": threat_score, "threat": threat_dict}), 200


@app.route("/api/state")
def get_state():
    return jsonify(_legacy_state_snapshot())


@app.route("/api/alerts")
def get_alerts():
    return jsonify(_load_alerts())


@app.route("/api/alerts/clear", methods=["POST"])
def clear_alerts():
    with _alerts_lock:
        try:
            with open(ALERTS_FILE, "w", encoding="utf-8") as f:
                json.dump([], f)
        except Exception:
            logger.exception("Failed to clear alerts.json")
    return jsonify({"status": "cleared"})


@app.route("/snapshots/<path:filename>")
def snapshot_file(filename: str):
    return send_from_directory(SNAPSHOT_DIR, filename)


@app.route("/api/faces")
def get_faces():
    """Returns enrolled face names (each subdir of data/known_faces/ = one person)."""
    try:
        if not os.path.exists(KNOWN_FACES_DIR):
            return jsonify([])
        names = [
            d for d in os.listdir(KNOWN_FACES_DIR)
            if os.path.isdir(os.path.join(KNOWN_FACES_DIR, d))
        ]
        return jsonify(sorted(names))
    except Exception:
        logger.exception("Failed to list known faces")
        return jsonify([])


@app.route("/api/faces/upload", methods=["POST"])
def upload_faces():
    name = (request.form.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "Missing name"}), 400
    safe_name = secure_filename(name)
    if not safe_name:
        return jsonify({"ok": False, "error": "Invalid name"}), 400

    files = request.files.getlist("photos")
    if not files:
        return jsonify({"ok": False, "error": "No photos uploaded"}), 400

    person_dir = os.path.join(KNOWN_FACES_DIR, safe_name)
    os.makedirs(person_dir, exist_ok=True)
    saved = []
    skipped = []

    for f in files:
        orig = f.filename or ""
        ext = os.path.splitext(orig)[1].lower()
        if ext not in _ENROLL_EXTS:
            skipped.append(orig or "(unnamed)")
            continue
        base = secure_filename(os.path.splitext(orig)[0]) or "photo"
        fname = f"{base}_{uuid.uuid4().hex[:8]}{ext}"
        path = os.path.join(person_dir, fname)
        f.save(path)
        saved.append(fname)

    if not saved:
        return jsonify({"ok": False, "error": "No valid image files"}), 400

    return jsonify({"ok": True, "name": safe_name, "saved": saved, "skipped": skipped})


@app.route("/api/faces/<name>", methods=["DELETE"])
def delete_face(name: str):
    safe_name = secure_filename(name)
    if not safe_name:
        return jsonify({"ok": False, "error": "Invalid name"}), 400
    person_dir = os.path.join(KNOWN_FACES_DIR, safe_name)
    if not os.path.isdir(person_dir):
        return jsonify({"ok": False, "error": "Not found"}), 404
    try:
        shutil.rmtree(person_dir)
        return jsonify({"ok": True})
    except Exception:
        logger.exception("Failed deleting face folder")
        return jsonify({"ok": False, "error": "Delete failed"}), 500


@app.route("/api/faces/enroll", methods=["POST"])
def run_enrollment():
    enroll_script = os.path.join(_ROOT, "setup", "enroll.py")
    if not os.path.exists(enroll_script):
        return jsonify({"ok": False, "error": "setup/enroll.py not found"}), 404
    try:
        proc = subprocess.run(
            [sys.executable, enroll_script],
            cwd=_ROOT,
            capture_output=True,
            text=True,
            timeout=180,
        )
        output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        ok = proc.returncode == 0
        if ok:
            try:
                if _AI_AVAILABLE and "_recognizer" in globals():
                    _recognizer.reload_embeddings()
            except Exception:
                logger.exception("Failed to reload embeddings after enroll")
        return jsonify({"ok": ok, "output": output, "returncode": proc.returncode})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "Enrollment timed out"}), 504
    except Exception:
        logger.exception("Enrollment failed")
        return jsonify({"ok": False, "error": "Enrollment failed"}), 500


@app.route("/api/custom_threats", methods=["GET"])
def get_custom_threats():
    try:
        if os.path.exists(THREATS_PATH):
            with open(THREATS_PATH, "r", encoding="utf-8") as f:
                return jsonify(json.load(f))
    except Exception:
        logger.exception("Failed reading custom threats")
    return jsonify([])


@app.route("/api/custom_threats", methods=["POST"])
def add_custom_threat():
    data = request.get_json(silent=True) or {}
    desc = (data.get("description") or "").strip()
    if not desc:
        return jsonify({"ok": False, "error": "description required"}), 400
    item = {
        "id": f"ct_{int(time.time() * 1000)}",
        "description": desc,
        "severity": data.get("severity", "medium"),
        "enabled": True,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    arr = []
    if os.path.exists(THREATS_PATH):
        with open(THREATS_PATH, "r", encoding="utf-8") as f:
            arr = json.load(f)
    arr.append(item)
    with open(THREATS_PATH, "w", encoding="utf-8") as f:
        json.dump(arr, f, indent=2)
    scorer.reload_custom_threats()
    return jsonify({"ok": True, "threat": item})


@app.route("/api/custom_threats/<threat_id>", methods=["PATCH"])
def patch_custom_threat(threat_id: str):
    data = request.get_json(silent=True) or {}
    arr = []
    if os.path.exists(THREATS_PATH):
        with open(THREATS_PATH, "r", encoding="utf-8") as f:
            arr = json.load(f)
    changed = False
    for t in arr:
        if t.get("id") == threat_id:
            for k in ("description", "severity", "enabled"):
                if k in data:
                    t[k] = data[k]
            changed = True
            break
    if not changed:
        return jsonify({"ok": False, "error": "not found"}), 404
    with open(THREATS_PATH, "w", encoding="utf-8") as f:
        json.dump(arr, f, indent=2)
    scorer.reload_custom_threats()
    return jsonify({"ok": True})


@app.route("/api/custom_threats/<threat_id>", methods=["DELETE"])
def delete_custom_threat(threat_id: str):
    arr = []
    if os.path.exists(THREATS_PATH):
        with open(THREATS_PATH, "r", encoding="utf-8") as f:
            arr = json.load(f)
    arr = [t for t in arr if t.get("id") != threat_id]
    with open(THREATS_PATH, "w", encoding="utf-8") as f:
        json.dump(arr, f, indent=2)
    scorer.reload_custom_threats()
    return jsonify({"ok": True})


@app.route("/api/threats/reload", methods=["POST"])
def reload_threats():
    data = request.get_json(silent=True) or {}
    if "threats" in data:
        try:
            os.makedirs(os.path.dirname(THREATS_PATH), exist_ok=True)
            with open(THREATS_PATH, "w", encoding="utf-8") as f:
                json.dump(data["threats"], f, indent=2)
            logger.info("custom_threats.json saved (%d threats)", len(data["threats"]))
        except Exception:
            return jsonify({"status": "error", "message": "Could not save file"}), 500
    scorer.reload_custom_threats()
    count = len(scorer._loader.get_threats())
    return jsonify({"status": "reloaded", "threat_count": count})


@app.route("/api/config", methods=["GET"])
def get_config():
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return jsonify(json.load(f))
    except Exception:
        logger.exception("Failed to read config.json")
    return jsonify({})


@app.route("/api/config", methods=["POST"])
def save_config():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        _sync_runtime_aws_recipients_from_config(data)
        return jsonify({"status": "saved"})
    except Exception:
        return jsonify({"error": "Write failed"}), 500


@app.route("/api/camera/status")
def camera_status():
    return jsonify({"online": camera.is_online})


@app.route("/health")
def health():
    face_count = 0
    if os.path.exists(KNOWN_FACES_DIR):
        face_count = len([
            d for d in os.listdir(KNOWN_FACES_DIR)
            if os.path.isdir(os.path.join(KNOWN_FACES_DIR, d))
        ])
    with _state_lock:
        return jsonify({
            "status":         "ok",
            "camera_online":  _state["camera"]["online"],
            "sse_clients":    len(_sse_clients),
            "enrolled_faces": face_count,
            "ai_available":   _AI_AVAILABLE,
        })


@app.route("/api/debug/live")
def debug_live():
    with _state_lock:
        sensor = dict(_state.get("sensor", {}))
        threat = dict(_state.get("threat", {}))
    with _vision_lock:
        vision = dict(_latest_vision)
    return jsonify({
        "camera_online": camera.is_online,
        "sensor": sensor,
        "vision": vision,
        "threat": {
            "score": threat.get("score", 0.0),
            "severity": threat.get("severity", "none"),
            "alert": threat.get("alert", False),
        },
        "server_time": time.time(),
    })


if __name__ == "__main__":
    print("\n⚠  Do not run sneakpeek_ui/app.py directly.")
    print("   Run from the project root:  python main.py\n")
