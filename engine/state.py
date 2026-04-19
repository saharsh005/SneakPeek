"""
engine/state.py
---------------
Single source of truth for all live state.

update_from_serial() now reads the is_night flag directly from the
DevKit's sensor packet (the DevKit computes it from LDR reading).
This means Python doesn't need to recalculate it — the hardware
already decided based on the LDR threshold in the Arduino code.
"""

import time
import threading
import logging

logger = logging.getLogger(__name__)


class SystemState:
    def __init__(self):
        from engine.config_manager import cfg
        self._cfg  = cfg
        self._lock = threading.Lock()

        # Sensor state
        self.motion             = False
        self.smoke              = False
        self.smoke_ppm          = 0.0
        self.ldr                = 4095
        self.is_night           = False
        self.motion_duration_s  = 0.0
        self._motion_start      = None

        # Phone-mode motion frame counter
        self._motion_frame_count = 0
        self._motion_frame_min   = 3

        # Pipeline state
        self.stage         = "idle"
        self.score         = 0.0
        self.last_threats  = []
        self.person_count  = 0
        self.unknown_count = 0
        self.known_faces   = []
        self.unknown_faces = []

        # System state
        self.running     = False
        self.last_update = 0.0
        self.fps         = 0.0
        self._frame_times = []

    # ── ESP32 serial update ────────────────────────────────────
    def update_from_serial(self, packet: dict):
        """
        Called by SerialReceiver on every sensor JSON packet.
        Handles:
          motion    : int  (1 = active, 0 = not)
          smoke_ppm : float
          ldr       : int  (0-4095)
          is_night  : int  (1/0) — computed by DevKit from LDR
        """
        with self._lock:
            if "motion" in packet:
                detected = bool(packet["motion"])
                if detected and not self.motion:
                    self.motion        = True
                    self._motion_start = time.time()
                elif not detected and self.motion:
                    self.motion              = False
                    self._motion_start       = None
                    self.motion_duration_s   = 0.0
                elif detected and self._motion_start:
                    self.motion_duration_s = time.time() - self._motion_start

            if "smoke_ppm" in packet:
                self.smoke_ppm = float(packet["smoke_ppm"])
                threshold      = float(
                    self._cfg.thresholds().get("smoke_ppm_threshold", 300)
                )
                self.smoke = self.smoke_ppm >= threshold

            if "ldr" in packet:
                self.ldr = int(packet["ldr"])
                # If DevKit sends is_night flag use it directly,
                # otherwise compute from LDR value
                if "is_night" in packet:
                    self.is_night = bool(packet["is_night"])
                else:
                    night_max = int(
                        self._cfg.thresholds().get("night_ldr_max", 400)
                    )
                    self.is_night = self.ldr <= night_max

            self.running     = True
            self.last_update = time.time()

    # ── Phone mode: motion derived from YOLO ──────────────────
    def update_motion_from_yolo(self, people_detected: bool):
        with self._lock:
            min_frames = int(
                self._cfg.thresholds().get("motion_frames_min", 3)
            )
            if people_detected:
                self._motion_frame_count += 1
                if self._motion_frame_count >= min_frames:
                    if not self.motion:
                        self.motion        = True
                        self._motion_start = time.time()
                        logger.debug("[state] Motion ON (YOLO)")
                    elif self._motion_start:
                        self.motion_duration_s = (
                            time.time() - self._motion_start
                        )
            else:
                if self.motion:
                    logger.debug(
                        f"[state] Motion OFF after "
                        f"{self.motion_duration_s:.1f}s"
                    )
                self.motion              = False
                self._motion_frame_count = 0
                self._motion_start       = None
                self.motion_duration_s   = 0.0

    # ── Pipeline state update ──────────────────────────────────
    def update_pipeline(self, stage: str, score: float = 0,
                        threats: list = None, person_count: int = 0,
                        unknown_count: int = 0,
                        known_faces: list = None,
                        unknown_faces: list = None):
        with self._lock:
            self.stage         = stage
            self.score         = round(score, 3)
            self.last_threats  = threats or []
            self.person_count  = person_count
            self.unknown_count = unknown_count
            self.known_faces   = known_faces or []
            self.unknown_faces = unknown_faces or []
            self.last_update   = time.time()
            self.running       = True

    def record_frame(self):
        with self._lock:
            now = time.time()
            self._frame_times.append(now)
            self._frame_times = [t for t in self._frame_times
                                  if now - t < 10]
            if len(self._frame_times) > 1:
                span = self._frame_times[-1] - self._frame_times[0]
                self.fps = round(
                    (len(self._frame_times) - 1) / max(span, 0.001), 1
                )

    def is_sustained_motion(self, min_seconds: float = 2.0) -> bool:
        with self._lock:
            return self.motion and self.motion_duration_s >= min_seconds

    # ── Thread-safe snapshot for SSE push ─────────────────────
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "sensor": {
                    "motion":            self.motion,
                    "smoke":             self.smoke,
                    "smoke_ppm":         round(self.smoke_ppm, 1),
                    "ldr":               self.ldr,
                    "is_night":          self.is_night,
                    "motion_duration_s": round(self.motion_duration_s, 1),
                },
                "pipeline": {
                    "stage":         self.stage,
                    "score":         self.score,
                    "last_threats":  self.last_threats,
                    "person_count":  self.person_count,
                    "unknown_count": self.unknown_count,
                    "known_faces":   self.known_faces,
                    "unknown_faces": self.unknown_faces,
                    "fps":           self.fps,
                },
                "system": {
                    "running":     self.running,
                    "last_update": self.last_update,
                    "source":      self._cfg.camera().get("source", "phone"),
                },
            }
