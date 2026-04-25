"""
engine/camera.py
----------------
Handles two camera sources:

  phone mode:
    Connects to IP Webcam app MJPEG stream over WiFi.
    Grabs frames continuously in a thread.

  esp32 mode:
    Does NOT grab frames itself.
    Waits for push_esp32_frame() to be called by SerialReceiver
    each time the DevKit forwards a JPEG over USB serial.
    The DevKit fetches the JPEG from the ESP32-CAM over WiFi HTTP
    and streams it via serial — the Python side just receives it.

Both modes run the same AI pipeline and push annotated frames
to frame_queue for the Flask MJPEG stream.
"""

import cv2
import time
import queue
import threading
import logging
import numpy as np
import urllib.request
import re
from engine.config_manager import cfg

logger = logging.getLogger(__name__)

frame_queue: queue.Queue = queue.Queue(maxsize=2)

FONT  = cv2.FONT_HERSHEY_SIMPLEX
GREEN = (0, 255, 136)
RED   = (0, 50,  255)
AMBER = (0, 165, 255)
WHITE = (255, 255, 255)
BLACK = (0,   0,   0)


class CameraEngine:
    def __init__(self, state, cooldown,
                 detector, recognizer, pose_analyzer, scorer, sender):
        self._state      = state
        self._cooldown   = cooldown
        self._detector   = detector
        self._recognizer = recognizer
        self._pose       = pose_analyzer
        self._scorer     = scorer
        self._sender     = sender
        self._running    = False
        self._thread     = None

        # Frame counters
        self._frame_num  = 0   # every captured frame
        self._proc_num   = 0   # every frame that went through AI

        # Last AI results — reused on skipped frames for smooth display
        self._last_dets  = []
        self._last_faces = []
        self._last_pose  = {"contact_detected": False, "contact_iou": 0,
                            "aggression_score": 0, "pose_results": []}
        self._last_score = None
        self._last_bbox_sig = ""

    def start(self):
        self._running = True
        source = cfg.camera().get("source", "phone")

        if source == "phone":
            self._thread = threading.Thread(
                target=self._phone_loop, daemon=True, name="camera"
            )
            self._thread.start()
            logger.info("[camera] Phone/WiFi mode started")
        else:
            # ESP32 mode — frames arrive via push_esp32_frame()
            # Push an offline placeholder immediately so the UI shows something
            self._push_offline("Waiting for ESP32 frame...")
            logger.info("[camera] ESP32 mode — waiting for frames via serial")

    def stop(self):
        self._running = False

    # ── Called by SerialReceiver for each ESP32-CAM frame ─────
    def push_esp32_frame(self, jpeg_bytes: bytes):
        """
        Called from the serial receiver thread each time
        the DevKit forwards a JPEG frame over USB serial.
        Decodes and runs the full AI pipeline.
        """
        arr   = np.frombuffer(jpeg_bytes, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if frame is not None:
            self._process(frame)
        else:
            logger.warning("[camera] Failed to decode JPEG from serial")
            self._push_offline("Bad JPEG from ESP32")

    # ── Phone camera loop (IP Webcam WiFi stream) ──────────────
    def _phone_loop(self):
        cap = None
        while self._running:
            url = cfg.camera().get("phone_url", "")
            if not url or "192.168.1.x" in url:
                self._push_offline("Set phone URL in Config tab")
                time.sleep(3)
                continue

            if cap is None or not cap.isOpened():
                logger.info(f"[camera] Connecting to {url}")
                cap = cv2.VideoCapture(url)
                if not cap.isOpened():
                    logger.warning("[camera] OpenCV cannot connect — trying HTTP MJPEG fallback")
                    if cap:
                        cap.release()
                        cap = None
                    self._push_offline("Cannot connect to IP Webcam")
                    if self._http_mjpeg_loop(url):
                        continue
                    time.sleep(3)
                    continue
                logger.info("[camera] Connected")

            ret, frame = cap.read()
            if not ret or frame is None:
                logger.warning("[camera] Frame read failed — trying HTTP MJPEG fallback")
                cap.release()
                cap = None
                if self._http_mjpeg_loop(url):
                    continue
                time.sleep(1)
                continue

            self._process(frame)

        if cap:
            cap.release()

    def _http_mjpeg_loop(self, url: str) -> bool:
        """Fallback reader for raw MJPEG HTTP streams.

        Some phone camera apps expose a multipart MJPEG stream that OpenCV
        cannot open directly. This method reads the stream manually and
        extracts JPEG frames for processing.
        """
        logger.info(f"[camera] HTTP MJPEG fallback connecting to {url}")
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "multipart/x-mixed-replace,image/jpeg,*/*"
        })
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                content_type = resp.headers.get("Content-Type", "")
                logger.info(f"[camera] HTTP MJPEG content type: {content_type}")
                data = b""
                while self._running:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    data += chunk
                    start = data.find(b"\xff\xd8")
                    end = data.find(b"\xff\xd9", start + 2)
                    if start != -1 and end != -1:
                        jpeg = data[start:end + 2]
                        data = data[end + 2:]
                        frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8),
                                             cv2.IMREAD_COLOR)
                        if frame is None:
                            logger.warning("[camera] Bad JPEG from HTTP MJPEG fallback")
                            continue
                        self._process(frame)
                return True
        except Exception as exc:
            logger.warning(f"[camera] HTTP MJPEG fallback failed: {exc}")
            return False

    # ── Core per-frame AI pipeline ─────────────────────────────
    def _process(self, frame: np.ndarray):
        self._frame_num += 1
        cam_cfg  = cfg.camera()
        every_n  = int(cam_cfg.get("process_every_n_frames", 5))
        resize_w = int(cam_cfg.get("resize_width", 416))
        face_n   = int(cam_cfg.get("face_check_every_n_process", 4))
        pose_n   = int(cam_cfg.get("pose_check_every_n_process", 6))

        # For ESP32 mode every frame IS a triggered frame (no skip needed
        # because the DevKit already gates on sustained motion).
        # For phone mode, apply the frame skip.
        source = cfg.camera().get("source", "phone")
        run_ai = (source == "esp32") or (self._frame_num % every_n == 0)

        if run_ai:
            self._proc_num += 1
            self._run_ai(frame, resize_w, face_n, pose_n)
            self._state.record_frame()

        annotated = self._annotate(frame)
        self._push_frame(annotated)

    # ── AI pipeline ────────────────────────────────────────────
    def _run_ai(self, frame: np.ndarray, resize_w: int,
                face_n: int, pose_n: int):
        h, w    = frame.shape[:2]
        scale   = resize_w / w
        ai_h    = int(h * scale)
        ai_frame = cv2.resize(frame, (resize_w, ai_h))

        # 1. YOLO person detection
        self._state.update_pipeline("detecting")
        _, jpeg = cv2.imencode('.jpg', ai_frame,
                               [cv2.IMWRITE_JPEG_QUALITY, 80])
        dets = self._detector.detect(jpeg.tobytes())

        # Scale bboxes back to display frame coordinates
        sx = w / resize_w
        sy = h / ai_h
        for d in dets:
            x1, y1, x2, y2 = d["bbox"]
            d["bbox"] = [int(x1*sx), int(y1*sy),
                         int(x2*sx), int(y2*sy)]
            d["cx"]   = int(d["cx"] * sx)
            d["cy"]   = int(d["cy"] * sy)
        self._last_dets = dets

        # Update motion from YOLO detections (phone mode only)
        if cfg.camera().get("source", "phone") == "phone":
            self._state.update_motion_from_yolo(len(dets) > 0)

        if not dets:
            self._last_faces = []
            self._last_pose  = {"contact_detected": False, "contact_iou": 0,
                                 "aggression_score": 0, "pose_results": []}
            self._last_score = None
            self._state.update_pipeline("idle", score=0,
                                        person_count=0, unknown_count=0)
            return

        # 2. InsightFace — when bboxes change or every face_n cycles
        bbox_sig = str([(d["bbox"][0]//30, d["bbox"][1]//30)
                        for d in dets])
        if bbox_sig != self._last_bbox_sig or self._proc_num % face_n == 0:
            self._state.update_pipeline("recognizing",
                                        person_count=len(dets))
            self._last_faces  = self._recognizer.recognize_all(frame)
            self._last_bbox_sig = bbox_sig

        # 3. MediaPipe — only when 2+ people with possible overlap
        if len(dets) >= 2 and self._proc_num % pose_n == 0:
            self._state.update_pipeline("posing",
                                        person_count=len(dets))
            self._last_pose = self._pose.analyze(frame, dets)

        # 4. Threat scoring (reads live config every call)
        self._state.update_pipeline("scoring", person_count=len(dets))
        sr = self._scorer.score(
            self._last_faces, self._last_pose,
            len(dets), self._state
        )
        self._last_score = sr

        known   = [f for f in self._last_faces if f["known"]]
        unknown = [f for f in self._last_faces if not f["known"]]
        self._state.update_pipeline(
            "alert" if sr["is_alert"] else "idle",
            score=sr["score"],
            threats=sr["threats"],
            person_count=len(dets),
            unknown_count=len(unknown),
            known_faces=[{"name": f["name"], "bbox": f["bbox"]}
                         for f in known],
            unknown_faces=[{"bbox": f["bbox"]} for f in unknown],
        )

        # 5. Alert pipeline
        if sr["is_alert"]:
            ready = [t for t in sr["threats"]
                     if self._cooldown.is_ready(t)]
            if ready:
                sr["threats"] = ready
                _, snap = cv2.imencode(
                    '.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 90]
                )
                self._sender.send(sr, snap.tobytes())
                for t in ready:
                    self._cooldown.mark_triggered(t)

    # ── Frame annotation ───────────────────────────────────────
    def _annotate(self, frame: np.ndarray) -> np.ndarray:
        out  = frame.copy()
        h, w = out.shape[:2]

        # YOLO boxes (thin gray)
        for d in self._last_dets:
            x1, y1, x2, y2 = d["bbox"]
            cv2.rectangle(out, (x1, y1), (x2, y2), (60, 60, 60), 1)

        # Face boxes + labels
        for f in self._last_faces:
            x1, y1, x2, y2 = f["bbox"]
            color = GREEN if f["known"] else RED
            label = f"{'OK' if f['known'] else '?'} {f['name'] if f['known'] else 'UNKNOWN'}"
            cv2.rectangle(out, (x1, y1), (x2, y2), color, 2)
            (lw, lh), _ = cv2.getTextSize(label, FONT, 0.42, 1)
            ly = max(y1 - 4, lh + 6)
            cv2.rectangle(out, (x1, ly - lh - 5), (x1 + lw + 8, ly + 2),
                          color, -1)
            cv2.putText(out, label, (x1 + 4, ly - 1),
                        FONT, 0.42, BLACK, 1)

        # Aggression label
        agg = self._last_pose.get("aggression_score", 0)
        if agg > 0.5:
            cv2.putText(out, f"AGGR {agg:.2f}", (w - 160, 30),
                        FONT, 0.5, RED, 2)

        # Threat score (top-left)
        score = self._last_score["score"] if self._last_score else 0.0
        lv = ("CLEAR"  if score < .3 else "WARN"  if score < .5
              else "ALERT"  if score < .7 else "CRITICAL")
        sc = (GREEN if score < .3 else AMBER if score < .5
              else (0, 100, 255) if score < .7 else RED)
        cv2.rectangle(out, (0, 0), (200, 50), (0, 0, 0), -1)
        cv2.putText(out, f"SCORE {score:.2f}", (7, 20),
                    FONT, 0.52, sc, 2)
        cv2.putText(out, lv, (7, 40), FONT, 0.45, sc, 1)

        # Alert border flash
        if self._last_score and self._last_score.get("is_alert"):
            cv2.rectangle(out, (0, 0), (w - 1, h - 1), RED, 5)

        # Sensor strip (bottom)
        snap    = self._state.snapshot()["sensor"]
        night_s = "NIGHT" if snap.get("is_night") else "DAY"
        dur_s   = (f"MOT:{snap.get('motion_duration_s', 0):.0f}s"
                   if snap.get("motion") else "NO MOT")
        fps_s   = f"{self._state.fps}fps"
        ppm_s   = f"PPM:{snap.get('smoke_ppm', 0):.0f}"
        strip   = f" {night_s}  {dur_s}  {fps_s}  {ppm_s}"
        cv2.rectangle(out, (0, h - 22), (w, h), (0, 0, 0), -1)
        cv2.putText(out, strip, (4, h - 6), FONT, 0.34, WHITE, 1)

        return out

    # ── Push annotated frame to MJPEG queue ────────────────────
    def _push_frame(self, frame: np.ndarray):
        _, jpeg = cv2.imencode('.jpg', frame,
                               [cv2.IMWRITE_JPEG_QUALITY, 75])
        data = jpeg.tobytes()
        try:
            frame_queue.put_nowait(data)
        except queue.Full:
            try:
                frame_queue.get_nowait()
                frame_queue.put_nowait(data)
            except Exception:
                pass

    def _push_offline(self, msg: str = "OFFLINE"):
        b = np.zeros((240, 320, 3), dtype=np.uint8)
        cv2.putText(b, "CAMERA OFFLINE", (20, 100),
                    FONT, 0.65, GREEN, 2)
        cv2.putText(b, msg, (10, 130), FONT, 0.38, (80, 80, 80), 1)
        _, j = cv2.imencode('.jpg', b)
        try:
            frame_queue.put_nowait(j.tobytes())
        except queue.Full:
            pass