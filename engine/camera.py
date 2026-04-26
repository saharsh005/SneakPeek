"""
engine/camera.py — SneakPeek Threaded Camera System (STABLE)
=============================================================
Uses raw HTTP requests to read MJPEG — more reliable than cv2.VideoCapture
for ESP32-CAM streams which use non-standard MJPEG framing.

Fixes vs previous version:
  - Switched from cv2.VideoCapture (FFMPEG) to requests streaming
    → eliminates "Read timed out" from urllib3 (was using wrong timeout)
  - read_timeout_s raised to 30s (ESP32-CAM can stall between frames)
  - is_online timeout loosened to 15s (camera can be slow but alive)
  - Brightness boost via cv2 applied on every raw frame
  - Offline placeholder shows "Camera Offline" in red clearly
"""

import cv2
import time
import logging
import threading
import numpy as np
import requests
import struct
from typing import Optional, Callable

logger = logging.getLogger("sneakpeek.camera")

# ─────────────────────────────────────────────────────────────────────────────
class CameraConfig:
    stream_url:      str   = "http://192.168.1.100/stream"
    reconnect_base_s: float = 2.0     # wait before first reconnect
    reconnect_max_s:  float = 30.0    # max reconnect wait
    read_timeout_s:   float = 30.0    # seconds with no frame before reconnect
    online_timeout_s: float = 15.0    # seconds with no frame before "offline"
    target_fps:       int   = 8       # AI processor FPS cap
    jpeg_quality:     int   = 85      # re-encode quality when serving UI
    brightness_alpha: float = 1.4     # contrast multiplier (1.0=none, 1.4=+40%)
    brightness_beta:  int   = 20      # brightness additive (0=none, 20=+20)
    http_timeout:     int   = 10      # TCP connect+read timeout for requests


# ─────────────────────────────────────────────────────────────────────────────
class _FrameSlot:
    """Lock-protected single-frame store — always returns newest frame."""
    def __init__(self):
        self._lock  = threading.Lock()
        self._frame: Optional[np.ndarray] = None
        self._ts:    float = 0.0

    def put(self, frame: np.ndarray) -> None:
        with self._lock:
            self._frame = frame
            self._ts    = time.monotonic()

    def get(self) -> tuple:
        with self._lock:
            return self._frame, self._ts

    def age(self) -> float:
        with self._lock:
            return float("inf") if self._ts == 0.0 else time.monotonic() - self._ts


# ─────────────────────────────────────────────────────────────────────────────
# MJPEG parser — extracts JPEG frames from a raw HTTP chunked stream
# ─────────────────────────────────────────────────────────────────────────────
_SOI = b'\xff\xd8'   # JPEG start-of-image marker
_EOI = b'\xff\xd9'   # JPEG end-of-image marker


def _iter_mjpeg_frames(stream_response):
    """
    Generator that yields raw JPEG bytes from a requests streaming response.
    Handles both:
      - multipart/x-mixed-replace (standard MJPEG with --boundary headers)
      - Raw JPEG streams (just SOI...EOI pairs)
    """
    buf = b''
    for chunk in stream_response.iter_content(chunk_size=4096):
        if not chunk:
            continue
        buf += chunk
        # Find complete JPEG frames by SOI/EOI markers
        while True:
            start = buf.find(_SOI)
            if start == -1:
                buf = b''
                break
            end = buf.find(_EOI, start + 2)
            if end == -1:
                # Incomplete frame — keep buffering
                buf = buf[start:]
                break
            jpeg_bytes = buf[start: end + 2]
            buf = buf[end + 2:]
            if len(jpeg_bytes) > 1000:   # sanity check — skip corrupt tiny frames
                yield jpeg_bytes


# ─────────────────────────────────────────────────────────────────────────────
# Thread 1 — Frame Reader
# ─────────────────────────────────────────────────────────────────────────────
class FrameReader(threading.Thread):
    def __init__(self, url: str, raw_slot: _FrameSlot, cfg: CameraConfig):
        super().__init__(name="FrameReader", daemon=True)
        self.url       = url
        self.slot      = raw_slot
        self.cfg       = cfg
        self._stop_ev  = threading.Event()
        self.connected = False

    def stop(self) -> None:
        self._stop_ev.set()

    def _boost(self, frame: np.ndarray) -> np.ndarray:
        """Apply brightness + contrast boost."""
        if self.cfg.brightness_alpha == 1.0 and self.cfg.brightness_beta == 0:
            return frame
        return cv2.convertScaleAbs(
            frame,
            alpha=self.cfg.brightness_alpha,
            beta=self.cfg.brightness_beta,
        )

    def run(self) -> None:
        backoff = self.cfg.reconnect_base_s

        while not self._stop_ev.is_set():
            logger.info("Connecting to camera: %s", self.url)
            try:
                resp = requests.get(
                    self.url,
                    stream  = True,
                    timeout = self.cfg.http_timeout,
                    headers = {"Connection": "keep-alive"},
                )
                resp.raise_for_status()
                self.connected = True
                logger.info("MJPEG stream connected.")
                backoff = self.cfg.reconnect_base_s   # reset backoff

                last_frame_ts = time.monotonic()

                for jpeg_bytes in _iter_mjpeg_frames(resp):
                    if self._stop_ev.is_set():
                        break

                    # Check read timeout (stream alive but no frames)
                    now = time.monotonic()
                    if now - last_frame_ts > self.cfg.read_timeout_s:
                        logger.warning("Frame timeout — reconnecting.")
                        break
                    last_frame_ts = now

                    # Decode JPEG → numpy frame
                    arr   = np.frombuffer(jpeg_bytes, dtype=np.uint8)
                    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                    if frame is None:
                        continue

                    # Apply brightness/contrast boost
                    frame = self._boost(frame)
                    self.slot.put(frame)

                resp.close()

            except requests.exceptions.Timeout:
                logger.warning("Stream error: connect/read timeout")
            except requests.exceptions.ConnectionError as e:
                logger.warning("Stream error: %s", e)
            except Exception as e:
                logger.warning("Stream error: %s", e)

            self.connected = False

            if not self._stop_ev.is_set():
                logger.info("Reconnecting in %.1f s...", backoff)
                self._stop_ev.wait(backoff)
                backoff = min(backoff * 2, self.cfg.reconnect_max_s)

        logger.info("FrameReader stopped.")


# ─────────────────────────────────────────────────────────────────────────────
# Thread 2 — Frame Processor (AI pipeline)
# ─────────────────────────────────────────────────────────────────────────────
class FrameProcessor(threading.Thread):
    def __init__(self, raw_slot: _FrameSlot, processed_slot: _FrameSlot,
                 process_fn: Optional[Callable], cfg: CameraConfig):
        super().__init__(name="FrameProcessor", daemon=True)
        self.raw      = raw_slot
        self.proc     = processed_slot
        self.process  = process_fn or (lambda f: f)
        self.cfg      = cfg
        self._stop_ev = threading.Event()
        self._interval = 1.0 / max(cfg.target_fps, 1)

    def stop(self) -> None:
        self._stop_ev.set()

    def run(self) -> None:
        last_ts = -1.0
        while not self._stop_ev.is_set():
            t0 = time.monotonic()
            frame, ts = self.raw.get()

            if frame is None or ts == last_ts:
                time.sleep(0.01)
                continue

            last_ts = ts
            try:
                result = self.process(frame)
                if result is not None:
                    self.proc.put(result)
            except Exception:
                logger.exception("AI pipeline error — using raw frame.")
                self.proc.put(frame)

            elapsed = time.monotonic() - t0
            sleep_t = self._interval - elapsed
            if sleep_t > 0:
                time.sleep(sleep_t)

        logger.info("FrameProcessor stopped.")


# ─────────────────────────────────────────────────────────────────────────────
# Public API — CameraManager
# ─────────────────────────────────────────────────────────────────────────────
class CameraManager:
    def __init__(self, stream_url: str,
                 process_fn: Optional[Callable] = None,
                 cfg: Optional[CameraConfig] = None):
        self._cfg            = cfg or CameraConfig()
        self._cfg.stream_url = stream_url
        self._raw_slot       = _FrameSlot()
        self._proc_slot      = _FrameSlot()
        self._reader         = FrameReader(stream_url, self._raw_slot, self._cfg)
        self._processor      = FrameProcessor(self._raw_slot, self._proc_slot,
                                               process_fn, self._cfg)

    def start(self) -> None:
        logger.info("CameraManager starting threads.")
        self._reader.start()
        self._processor.start()

    def stop(self) -> None:
        logger.info("CameraManager stopping threads.")
        self._reader.stop()
        self._processor.stop()
        self._reader.join(timeout=5)
        self._processor.join(timeout=5)
        logger.info("CameraManager stopped.")

    @property
    def is_online(self) -> bool:
        """True if a frame arrived within online_timeout_s."""
        return (
            self._reader.connected
            and self._raw_slot.age() < self._cfg.online_timeout_s
        )

    def get_raw_frame(self) -> Optional[np.ndarray]:
        frame, _ = self._raw_slot.get()
        return frame

    def get_processed_frame(self) -> Optional[np.ndarray]:
        frame, _ = self._proc_slot.get()
        return frame

    def get_jpeg_bytes(self, quality: Optional[int] = None) -> Optional[bytes]:
        frame = self.get_processed_frame()
        if frame is None:
            return None
        q  = quality or self._cfg.jpeg_quality
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, q])
        return buf.tobytes() if ok else None

    def snapshot_jpeg(self, quality: int = 92) -> Optional[bytes]:
        return self.get_jpeg_bytes(quality=quality)


# ─────────────────────────────────────────────────────────────────────────────
# Flask MJPEG generator
# ─────────────────────────────────────────────────────────────────────────────
_OFFLINE_JPEG: Optional[bytes] = None

def _make_offline_frame() -> bytes:
    global _OFFLINE_JPEG
    if _OFFLINE_JPEG is None:
        img = np.zeros((240, 320, 3), dtype=np.uint8)
        img[:] = (30, 30, 30)
        cv2.putText(img, "Camera Offline", (50, 110),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (60, 60, 220), 2)
        cv2.putText(img, "Reconnecting...", (65, 140),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (120, 120, 120), 1)
        _, buf = cv2.imencode(".jpg", img)
        _OFFLINE_JPEG = buf.tobytes()
    return _OFFLINE_JPEG


def mjpeg_generator(camera: CameraManager):
    """Flask generator for /video_feed."""
    boundary = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
    while True:
        jpeg = camera.get_jpeg_bytes()
        if jpeg is None:
            jpeg = _make_offline_frame()
        yield boundary + jpeg + b"\r\n"
        time.sleep(1 / 15)   # cap browser stream at 15 fps