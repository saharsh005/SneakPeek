"""
sensor_context.py
-----------------
Holds the live state of all ESP32 sensors.
Updated continuously by receiver.py as serial packets arrive.
Read by scorer.py when computing threat weights.

Each sensor owns its own meaning here:
  - motion    : bool   — is something physically present and moving?
  - smoke     : bool   — has MQ-2 crossed the danger threshold?
  - smoke_ppm : float  — raw MQ-2 analogue reading (0–4095 on ESP32 ADC)
  - ldr       : int    — raw LDR reading (0=dark, 4095=bright on ESP32 ADC)
  - is_night  : bool   — derived from ldr vs config threshold (not from clock)
  - motion_duration_s : float — how many consecutive seconds motion has been active
"""

import threading
import time


class SensorContext:
    def __init__(self, config: dict):
        self._lock = threading.Lock()
        self._cfg  = config["thresholds"]

        # Live sensor values
        self.motion             = False
        self.smoke              = False
        self.smoke_ppm          = 0.0
        self.ldr                = 4095        # default: assume bright until told otherwise
        self.is_night           = False
        self.motion_duration_s  = 0.0

        self._motion_start: float | None = None

    # ------------------------------------------------------------------
    # Called by receiver.py whenever a new serial packet arrives
    # ------------------------------------------------------------------
    def update(self, packet: dict) -> None:
        """
        Expected packet keys (all optional — only update what's present):
            motion      : bool
            smoke_ppm   : float
            ldr         : int
        """
        with self._lock:
            if "motion" in packet:
                detected = bool(packet["motion"])
                if detected and not self.motion:
                    # Rising edge — record when motion started
                    self._motion_start = time.time()
                elif not detected:
                    self._motion_start = None
                    self.motion_duration_s = 0.0
                self.motion = detected

            if "smoke_ppm" in packet:
                self.smoke_ppm = float(packet["smoke_ppm"])
                self.smoke = self.smoke_ppm >= self._cfg["smoke_ppm_threshold"]

            if "ldr" in packet:
                self.ldr      = int(packet["ldr"])
                self.is_night = self.ldr <= self._cfg["night_ldr_max"]

    def tick(self) -> None:
        """Call from a background thread every second to update motion duration."""
        with self._lock:
            if self.motion and self._motion_start is not None:
                self.motion_duration_s = time.time() - self._motion_start

    # ------------------------------------------------------------------
    # Read helpers (thread-safe snapshots)
    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "motion":            self.motion,
                "smoke":             self.smoke,
                "smoke_ppm":         self.smoke_ppm,
                "ldr":               self.ldr,
                "is_night":          self.is_night,
                "motion_duration_s": self.motion_duration_s,
            }

    def is_sustained_motion(self, min_seconds: float = 2.0) -> bool:
        """
        PIR's own classification — short blips (wind, pets) are filtered here,
        not by the AI. The sensor decides what counts as a real presence event.
        """
        with self._lock:
            return self.motion and self.motion_duration_s >= min_seconds
