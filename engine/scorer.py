"""
engine/scorer.py — SneakPeek Threat Scoring Engine
====================================================
Responsibilities:
  1. Score a detection event (pose, motion, person identity, smoke, LDR)
     using a base ruleset.
  2. Load and evaluate user-defined custom threats from custom_threats.json.
  3. Return a unified ThreatResult that the alert pipeline consumes.

custom_threats.json schema
--------------------------
[
  {
    "id":          "threat_001",
    "name":        "Night Intruder",
    "enabled":     true,
    "severity":    "high",            // low | medium | high | critical
    "conditions": {                   // ALL conditions must match (AND logic)
      "motion":         true,         // bool — require motion?
      "unknown_person": true,         // bool — person NOT in whitelist?
      "night":          true,         // bool — LDR indicates darkness?
      "smoke":          false,        // bool — smoke detected?
      "poses":          ["standing"]  // list[str] | null — any of these poses?
    },
    "message":  "Unrecognised person detected at night with motion."
  }
]

Detection event dict schema (produced by the AI pipeline and passed to score())
--------------------------------------------------------------------------------
{
  "motion":         bool,
  "smoke":          bool,
  "night":          bool,             // LDR below threshold
  "unknown_person": bool,             // face not in whitelist
  "persons":        int,              // count of detected persons
  "poses":          list[str],        // e.g. ["crouching", "standing"]
  "smoke_ppm":      float,
  "ldr":            int,
  "raw_score":      float             // base AI confidence 0–1
}
"""

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("sneakpeek.scorer")

CUSTOM_THREATS_PATH = os.environ.get(
    "CUSTOM_THREATS_PATH",
    os.path.join(os.path.dirname(__file__), "..", "custom_threats.json"),
)

# Severity ordering for comparison
_SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}


# ──────────────────────────────────────────────────────────────────────────────
# Data structures
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class MatchedThreat:
    id:       str
    name:     str
    severity: str
    message:  str


@dataclass
class ThreatResult:
    """Unified output of the scoring pipeline."""
    score:            float               # 0.0–1.0 normalised threat level
    severity:         str                 # low | medium | high | critical | none
    alert:            bool                # True → trigger alert pipeline
    base_reasons:     list[str] = field(default_factory=list)   # built-in flags
    custom_threats:   list[MatchedThreat] = field(default_factory=list)
    top_threat_name:  Optional[str] = None
    top_threat_msg:   Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "score":          round(self.score, 3),
            "severity":       self.severity,
            "alert":          self.alert,
            "base_reasons":   self.base_reasons,
            "custom_threats": [
                {"id": t.id, "name": t.name,
                 "severity": t.severity, "message": t.message}
                for t in self.custom_threats
            ],
            "top_threat_name": self.top_threat_name,
            "top_threat_msg":  self.top_threat_msg,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Custom Threat Loader  (hot-reloadable)
# ──────────────────────────────────────────────────────────────────────────────
class CustomThreatLoader:
    """
    Loads custom_threats.json from disk.  Thread-safe.
    Call reload() after the user edits the file from the UI.
    """

    def __init__(self, path: str = CUSTOM_THREATS_PATH):
        self._path   = path
        self._lock   = threading.RLock()
        self._threats: list[dict] = []
        self.reload()

    def reload(self) -> None:
        """Re-read the JSON file.  Silently skips if file is absent or malformed."""
        try:
            if not os.path.exists(self._path):
                logger.info("custom_threats.json not found at %s — using empty list.", self._path)
                with self._lock:
                    self._threats = []
                return
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, list):
                raise ValueError("Root element must be a JSON array.")
            with self._lock:
                self._threats = [t for t in data if t.get("enabled", True)]
            logger.info("Loaded %d custom threat(s) from %s", len(self._threats), self._path)
        except Exception:
            logger.exception("Failed to load custom_threats.json — keeping previous list.")

    def get_threats(self) -> list[dict]:
        with self._lock:
            return list(self._threats)   # shallow copy — dicts are treated as read-only


# ──────────────────────────────────────────────────────────────────────────────
# Condition Evaluator
# ──────────────────────────────────────────────────────────────────────────────
def _eval_conditions(conditions: dict[str, Any], event: dict[str, Any]) -> bool:
    """
    Returns True only if every condition key in `conditions` matches the event.

    Supported condition keys:
      motion         (bool)  — event["motion"]
      smoke          (bool)  — event["smoke"]
      night          (bool)  — event["night"]
      unknown_person (bool)  — event["unknown_person"]
      poses          (list)  — event["poses"] has ANY overlap with the list
      min_persons    (int)   — event["persons"] >= value
      min_smoke_ppm  (float) — event["smoke_ppm"] >= value
      max_ldr        (int)   — event["ldr"] <= value
    """
    for key, expected in conditions.items():
        if key == "motion":
            if bool(event.get("motion", False)) != bool(expected):
                return False
        elif key == "smoke":
            if bool(event.get("smoke", False)) != bool(expected):
                return False
        elif key == "night":
            if bool(event.get("night", False)) != bool(expected):
                return False
        elif key == "unknown_person":
            if bool(event.get("unknown_person", False)) != bool(expected):
                return False
        elif key == "poses":
            if expected:
                event_poses = set(event.get("poses", []))
                if not event_poses.intersection(set(expected)):
                    return False
        elif key == "min_persons":
            if int(event.get("persons", 0)) < int(expected):
                return False
        elif key == "min_smoke_ppm":
            if float(event.get("smoke_ppm", 0.0)) < float(expected):
                return False
        elif key == "max_ldr":
            if int(event.get("ldr", 9999)) > int(expected):
                return False
        else:
            logger.debug("Unknown condition key '%s' — skipping.", key)

    return True


# ──────────────────────────────────────────────────────────────────────────────
# Built-in Base Scorer
# ──────────────────────────────────────────────────────────────────────────────
class _BaseScorer:
    """
    Simple rule-based scorer that converts raw sensor + AI flags into a
    normalised threat score and list of reasons.
    """

    # Weights for each signal (sum to sensible total)
    _W_MOTION         = 0.20
    _W_UNKNOWN_PERSON = 0.30
    _W_SMOKE          = 0.25
    _W_NIGHT_MOTION   = 0.15   # bonus: motion at night
    _W_MULTI_PERSON   = 0.10   # bonus: multiple people

    _ALERT_THRESHOLD  = 0.35   # score above this → trigger alert

    def score(self, event: dict[str, Any]) -> tuple[float, str, list[str]]:
        """Returns (raw_score 0-1, severity_str, reasons_list)."""
        s       = 0.0
        reasons = []

        if event.get("motion"):
            s += self._W_MOTION
            reasons.append("Motion detected")

        if event.get("unknown_person"):
            s += self._W_UNKNOWN_PERSON
            reasons.append("Unknown person in frame")

        if event.get("smoke"):
            s += self._W_SMOKE
            reasons.append(f"Smoke detected ({event.get('smoke_ppm', 0):.0f} ppm)")

        if event.get("motion") and event.get("night"):
            s += self._W_NIGHT_MOTION
            reasons.append("Motion at night")

        persons = int(event.get("persons", 0))
        if persons > 1:
            s += self._W_MULTI_PERSON
            reasons.append(f"{persons} persons detected")

        # Clamp to [0, 1]
        s = min(s, 1.0)

        # Severity banding
        if s >= 0.75:
            severity = "critical"
        elif s >= 0.5:
            severity = "high"
        elif s >= 0.35:
            severity = "medium"
        elif s > 0.0:
            severity = "low"
        else:
            severity = "none"

        return s, severity, reasons


# ──────────────────────────────────────────────────────────────────────────────
# Public API — ThreatScorer
# ──────────────────────────────────────────────────────────────────────────────
class ThreatScorer:
    """
    Unified scorer:  base rules  +  custom threats.

    Usage:
        scorer  = ThreatScorer()          # loads custom_threats.json once
        result  = scorer.score(event)     # ThreatResult
        payload = result.to_dict()        # JSON-serialisable
    """

    def __init__(self, threats_path: str = CUSTOM_THREATS_PATH):
        self._base   = _BaseScorer()
        self._loader = CustomThreatLoader(threats_path)

    def reload_custom_threats(self) -> None:
        """Call after the user updates custom_threats.json via the UI."""
        self._loader.reload()

    # ── main entry point ────────────────────────────────────────────────────
    def score(self, event: dict[str, Any]) -> ThreatResult:
        # 1. Base score
        base_score, base_severity, base_reasons = self._base.score(event)

        # 2. Custom threats
        matched: list[MatchedThreat] = []
        for threat_def in self._loader.get_threats():
            try:
                conditions = threat_def.get("conditions", {})
                if _eval_conditions(conditions, event):
                    matched.append(MatchedThreat(
                        id       = threat_def.get("id", "?"),
                        name     = threat_def.get("name", "Custom Threat"),
                        severity = threat_def.get("severity", "medium"),
                        message  = threat_def.get("message", "Custom threat triggered."),
                    ))
                    logger.info("Custom threat matched: %s", threat_def.get("name"))
            except Exception:
                logger.exception("Error evaluating custom threat '%s'", threat_def.get("id"))

        # 3. Merge severity — take the highest across base + custom
        final_severity = base_severity
        for m in matched:
            if _SEVERITY_RANK.get(m.severity, 0) > _SEVERITY_RANK.get(final_severity, 0):
                final_severity = m.severity

        # 4. Boost score if a high custom threat fired but base was low
        final_score = base_score
        if matched:
            custom_boost = _SEVERITY_RANK.get(final_severity, 0) / 4.0
            final_score = max(final_score, custom_boost)

        # 5. Alert decision
        alert = (
            final_score >= self._base._ALERT_THRESHOLD
            or final_severity in ("high", "critical")
            or bool(matched)
        )

        # 6. Top threat for display
        top = matched[0] if matched else None

        return ThreatResult(
            score           = final_score,
            severity        = final_severity,
            alert           = alert,
            base_reasons    = base_reasons,
            custom_threats  = matched,
            top_threat_name = top.name    if top else None,
            top_threat_msg  = top.message if top else None,
        )


# ──────────────────────────────────────────────────────────────────────────────
# CLI smoke-test
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    scorer = ThreatScorer()

    test_event = {
        "motion":         True,
        "smoke":          False,
        "night":          True,
        "unknown_person": True,
        "persons":        1,
        "poses":          ["standing"],
        "smoke_ppm":      0.0,
        "ldr":            200,
        "raw_score":      0.6,
    }

    result = scorer.score(test_event)
    import json as _json
    print(_json.dumps(result.to_dict(), indent=2))