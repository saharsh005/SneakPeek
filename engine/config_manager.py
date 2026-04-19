"""
engine/config_manager.py
-------------------------
Watches config.json for changes and reloads automatically.
All engine components read from this instead of holding their own copy.

When UI saves config:
  1. Flask writes config.json
  2. ConfigManager detects file change (mtime)
  3. Reloads into memory
  4. All engine components reading via .get() see new values instantly
  5. Flask pushes SSE "config_updated" event to browser

Usage in engine:
    from engine.config_manager import cfg
    threshold = cfg.get("thresholds", {}).get("threat_score_min", 0.5)
    weights   = cfg.weights()   # shortcut
"""

import json
import time
import threading
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

CONFIG_PATH = Path("config.json")


class ConfigManager:
    def __init__(self):
        self._lock    = threading.RLock()
        self._data    = {}
        self._mtime   = 0.0
        self._callbacks: list = []
        self._load()
        # Start file watcher
        t = threading.Thread(target=self._watch, daemon=True, name="cfg-watch")
        t.start()

    # ── Public read API ────────────────────────────────────────
    def get(self, key: str, default=None):
        with self._lock:
            return self._data.get(key, default)

    def all(self) -> dict:
        with self._lock:
            return dict(self._data)

    def thresholds(self) -> dict:
        return self.get("thresholds", {})

    def weights(self) -> dict:
        return self.get("weights", {})

    def cooldowns(self) -> dict:
        return self.get("cooldown_seconds", {})

    def night_multiplier(self) -> float:
        return float(self.get("night_multiplier", 1.8))

    def threat_profile(self) -> dict:
        return self.get("threat_profile", {})

    def camera(self) -> dict:
        return self.get("camera", {})

    def aws(self) -> dict:
        return self.get("aws", {})

    def on_reload(self, fn):
        """Register a callback that fires when config is reloaded."""
        self._callbacks.append(fn)

    # ── Write (called by Flask when UI saves) ──────────────────
    def save(self, data: dict) -> bool:
        try:
            with self._lock:
                CONFIG_PATH.write_text(
                    json.dumps(data, indent=2), encoding="utf-8"
                )
                self._data  = data
                self._mtime = CONFIG_PATH.stat().st_mtime
            logger.info("[cfg] Config saved and reloaded")
            self._fire_callbacks()
            return True
        except Exception as e:
            logger.error(f"[cfg] Save error: {e}")
            return False

    # ── Internal ───────────────────────────────────────────────
    def _load(self):
        try:
            if CONFIG_PATH.exists():
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                with self._lock:
                    self._data  = data
                    self._mtime = CONFIG_PATH.stat().st_mtime
                logger.debug("[cfg] Config loaded")
        except Exception as e:
            logger.error(f"[cfg] Load error: {e}")

    def _watch(self):
        while True:
            try:
                if CONFIG_PATH.exists():
                    mtime = CONFIG_PATH.stat().st_mtime
                    if mtime > self._mtime + 0.1:
                        self._load()
                        self._fire_callbacks()
                        logger.info("[cfg] Config reloaded from disk")
            except Exception:
                pass
            time.sleep(1)

    def _fire_callbacks(self):
        for fn in self._callbacks:
            try:
                fn(self._data)
            except Exception as e:
                logger.error(f"[cfg] Callback error: {e}")


# Singleton — import this everywhere
cfg = ConfigManager()
