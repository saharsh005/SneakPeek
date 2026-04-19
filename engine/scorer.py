"""
engine/scorer.py
----------------
Reads weights, thresholds, and multipliers from ConfigManager on EVERY call.
So when the UI changes weights or thresholds, the next frame uses the new values
instantly — no engine restart needed.
"""
import logging
from engine.config_manager import cfg

logger = logging.getLogger(__name__)


class ThreatScorer:
    def __init__(self):
        pass  # No local config copy — reads live from cfg every call

    def score(self, face_results: list, pose_result: dict,
              person_count: int, state) -> dict:

        # Read fresh every call — UI changes take effect immediately
        weights    = cfg.weights()
        thresholds = cfg.thresholds()
        profile    = cfg.threat_profile()
        night_mul  = cfg.night_multiplier()

        snap      = state.snapshot()["sensor"]
        is_night  = snap["is_night"]
        smoke     = snap["smoke"]
        smoke_ppm = snap["smoke_ppm"]

        threats = []
        reasons = []
        raw     = 0.0

        unknown_count = sum(1 for f in face_results if not f["known"])
        known_count   = sum(1 for f in face_results if f["known"])

        # ── 1. Unknown person ──────────────────────────────────
        if unknown_count > 0:
            w    = weights.get("unknown_person", 0.35)
            raw += w * min(1.0, unknown_count / 2)
            if profile.get("unknown_person", {}).get("enabled"):
                threats.append("unknown_person")
                reasons.append(f"{unknown_count} unknown person(s) detected")

        # ── 2. Crowd ───────────────────────────────────────────
        crowd_min = int(thresholds.get("crowd_count_min", 3))
        if person_count >= crowd_min:
            raw += weights.get("crowd_factor", 0.20)
            if is_night and profile.get("crowd_at_night", {}).get("enabled"):
                threats.append("crowd_at_night")
                reasons.append(f"{person_count} people at night")

        # ── 3. Physical contact ────────────────────────────────
        if pose_result.get("contact_detected"):
            iou  = pose_result.get("contact_iou", 0)
            min_iou = float(thresholds.get("contact_overlap_min", 0.15))
            if iou >= min_iou:
                raw += weights.get("physical_contact", 0.25) * iou
                if profile.get("physical_contact", {}).get("enabled"):
                    threats.append("physical_contact")
                    reasons.append(f"Physical contact (iou={iou:.2f})")

        # ── 4. Aggression ──────────────────────────────────────
        agg      = pose_result.get("aggression_score", 0)
        agg_min  = float(thresholds.get("aggression_score_min", 0.60))
        if agg >= agg_min:
            raw += weights.get("aggression", 0.20) * agg
            if profile.get("aggressive_gesture", {}).get("enabled"):
                threats.append("aggressive_gesture")
                reasons.append(f"Aggressive posture (score={agg:.2f})")

        # ── 5. Night multiplier (LDR sensor) ───────────────────
        if is_night:
            raw *= night_mul
            if (person_count > 0 and
                    profile.get("motion_at_night", {}).get("enabled") and
                    "motion_at_night" not in threats):
                threats.append("motion_at_night")
                reasons.append("Motion with people detected at night")

        # ── 6. Smoke — autonomous bypass ──────────────────────
        smoke_threshold = float(thresholds.get("smoke_ppm_threshold", 300))
        smoke_alert = False
        if smoke and smoke_ppm >= smoke_threshold:
            if profile.get("smoke_or_gas", {}).get("enabled"):
                threats.append("smoke_or_gas")
                reasons.append(f"Smoke/gas (ppm={smoke_ppm:.0f})")
                smoke_alert = True

        final    = min(1.0, raw)
        min_score = float(thresholds.get("threat_score_min", 0.50))
        is_alert  = smoke_alert or (final >= min_score and len(threats) > 0)

        logger.debug(
            f"[scorer] {final:.3f} | alert={is_alert} | "
            f"threats={threats} | night={is_night}"
        )

        return {
            "score":          round(final, 3),
            "threats":        list(set(threats)),
            "reasons":        reasons,
            "is_alert":       is_alert,
            "person_count":   person_count,
            "unknown_count":  unknown_count,
            "known_count":    known_count,
            "sensor_context": snap,
        }
