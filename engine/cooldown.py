"""engine/cooldown.py — per-threat cooldown, reads windows live from cfg."""
import time
import threading
import logging
from engine.config_manager import cfg

logger = logging.getLogger(__name__)


class CooldownManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}

    def _window(self, threat: str) -> int:
        """Always reads current cooldown from cfg — reflects UI changes instantly."""
        return int(cfg.cooldowns().get(threat, 120))

    def is_ready(self, threat: str) -> bool:
        with self._lock:
            return (time.time() - self._last.get(threat, 0)) >= self._window(threat)

    def mark_triggered(self, threat: str):
        with self._lock:
            self._last[threat] = time.time()
            logger.info(f"[cooldown] {threat} cooldown started ({self._window(threat)}s)")

    def time_remaining(self, threat: str) -> float:
        with self._lock:
            return max(0.0, self._window(threat) - (time.time() - self._last.get(threat, 0)))

    def status(self) -> dict:
        with self._lock:
            out = {}
            for t in cfg.cooldowns():
                rem = max(0.0, self._window(t) - (time.time() - self._last.get(t, 0)))
                out[t] = {"ready": rem == 0.0, "remaining": round(rem, 1)}
            return out
