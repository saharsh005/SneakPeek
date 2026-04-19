"""
engine/threat_matcher.py
------------------------
Matches AI detection results against the user's custom threat descriptions
using the Claude API as an intelligent matching layer.

Instead of hardcoded threat types, the user writes plain English like:
  "alert me if strangers are near the entrance at night"
  "notify if more than 2 unknown people gather at the door"
  "warn me if anyone is being physically aggressive"

The matcher sends the AI detection payload + all custom threats to Claude,
which returns a structured JSON decision on which threats matched and why.

Fallback: if Claude API is unavailable, keyword matching is used instead.

Custom threats are stored in data/custom_threats.json:
[
  {
    "id": "ct_001",
    "description": "alert if unknown person near door at night",
    "severity": "high",
    "enabled": true,
    "created_at": "2024-01-01T00:00:00Z"
  },
  ...
]
"""

import json
import logging
import urllib.request
import urllib.error
from pathlib import Path

logger = logging.getLogger(__name__)

THREATS_PATH = Path("data/custom_threats.json")
CLAUDE_API   = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = "claude-sonnet-4-20250514"

# Keywords that map to AI detection labels — used as fallback
KEYWORD_MAP = {
    "unknown":     ["unknown_person"],
    "stranger":    ["unknown_person"],
    "intruder":    ["unknown_person"],
    "crowd":       ["crowd_at_night"],
    "group":       ["crowd_at_night"],
    "gather":      ["crowd_at_night"],
    "fight":       ["aggressive_gesture", "physical_contact"],
    "aggress":     ["aggressive_gesture"],
    "attack":      ["aggressive_gesture", "physical_contact"],
    "contact":     ["physical_contact"],
    "touch":       ["physical_contact"],
    "smoke":       ["smoke_or_gas"],
    "fire":        ["smoke_or_gas"],
    "gas":         ["smoke_or_gas"],
    "night":       ["crowd_at_night", "motion_at_night"],
    "dark":        ["crowd_at_night", "motion_at_night"],
    "motion":      ["motion_at_night"],
}


class ThreatMatcher:
    def __init__(self, config: dict):
        self._api_key = config.get("claude_api_key", "")
        self._enabled = bool(self._api_key)
        if not self._enabled:
            logger.warning(
                "[matcher] No claude_api_key in config — "
                "falling back to keyword matching"
            )

    # ── Public API ─────────────────────────────────────────────
    def match(self, score_result: dict) -> list[dict]:
        """
        Compare AI detection result against all enabled custom threats.

        Parameters
        ----------
        score_result : output from ThreatScorer.score()

        Returns
        -------
        List of matched custom threats:
            [{"id": "ct_001", "description": "...", "severity": "high",
              "reason": "why it matched", "confidence": 0.9}, ...]
        """
        threats = self._load_threats()
        enabled = [t for t in threats if t.get("enabled", True)]
        if not enabled:
            logger.debug("[matcher] No enabled custom threats")
            return []

        if self._enabled:
            return self._match_claude(score_result, enabled)
        else:
            return self._match_keywords(score_result, enabled)

    # ── Claude matching ────────────────────────────────────────
    def _match_claude(self, score_result: dict, threats: list) -> list[dict]:
        """Use Claude API to intelligently match detections to custom threats."""

        detection_summary = self._build_detection_summary(score_result)
        threats_text = "\n".join([
            f'- ID: {t["id"]} | Severity: {t["severity"]} | '
            f'Description: "{t["description"]}"'
            for t in threats
        ])

        prompt = f"""You are a security system threat matcher. 
        
Current detection from AI security camera:
{detection_summary}

User's custom threat alerts:
{threats_text}

For each custom threat, decide if the current detection matches it.
A match means the detection is consistent with what the user described.
Be reasonably inclusive — if there's a plausible match, include it.

Respond ONLY with a JSON array. No explanation, no markdown, just JSON:
[
  {{
    "id": "threat_id",
    "matched": true or false,
    "confidence": 0.0-1.0,
    "reason": "one sentence why it matched or didn't"
  }}
]"""

        try:
            body = json.dumps({
                "model":      CLAUDE_MODEL,
                "max_tokens": 1000,
                "messages":   [{"role": "user", "content": prompt}]
            }).encode()

            req = urllib.request.Request(
                CLAUDE_API,
                data=body,
                headers={
                    "Content-Type":      "application/json",
                    "x-api-key":         self._api_key,
                    "anthropic-version": "2023-06-01",
                }
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data     = json.loads(resp.read())
                raw_text = data["content"][0]["text"].strip()

            # Strip markdown fences if present
            if raw_text.startswith("```"):
                raw_text = raw_text.split("```")[1]
                if raw_text.startswith("json"):
                    raw_text = raw_text[4:]

            results  = json.loads(raw_text)
            matched  = []
            id_map   = {t["id"]: t for t in threats}

            for r in results:
                if r.get("matched") and r.get("confidence", 0) >= 0.5:
                    t = id_map.get(r["id"], {})
                    matched.append({
                        "id":          r["id"],
                        "description": t.get("description", ""),
                        "severity":    t.get("severity", "medium"),
                        "reason":      r.get("reason", ""),
                        "confidence":  r.get("confidence", 0),
                    })

            logger.info(f"[matcher] Claude matched {len(matched)}/{len(threats)} threats")
            return matched

        except Exception as e:
            logger.error(f"[matcher] Claude API error: {e} — falling back to keywords")
            return self._match_keywords(score_result, threats)

    # ── Keyword fallback ───────────────────────────────────────
    def _match_keywords(self, score_result: dict, threats: list) -> list[dict]:
        """Simple keyword matching — no API required."""
        ai_labels = set(score_result.get("threats", []))
        matched   = []

        for t in threats:
            desc  = t["description"].lower()
            words = desc.replace(",", " ").replace(".", " ").split()
            hit_labels = set()

            for word in words:
                for kw, labels in KEYWORD_MAP.items():
                    if kw in word:
                        hit_labels.update(labels)

            overlap = hit_labels & ai_labels
            if overlap:
                matched.append({
                    "id":          t["id"],
                    "description": t["description"],
                    "severity":    t.get("severity", "medium"),
                    "reason":      f"Keywords matched: {', '.join(overlap)}",
                    "confidence":  0.7,
                })

        logger.info(f"[matcher] Keyword matched {len(matched)}/{len(threats)} threats")
        return matched

    # ── Detection summary builder ──────────────────────────────
    @staticmethod
    def _build_detection_summary(score_result: dict) -> str:
        ctx = score_result.get("sensor_context", {})
        lines = [
            f"Threat score: {score_result.get('score', 0):.2f}",
            f"Persons detected: {score_result.get('person_count', 0)}",
            f"Unknown persons: {score_result.get('unknown_count', 0)}",
            f"Known persons: {score_result.get('known_count', 0)}",
            f"AI threat labels: {', '.join(score_result.get('threats', [])) or 'none'}",
            f"Reasons: {' | '.join(score_result.get('reasons', [])) or 'none'}",
            f"Time of day: {'night' if ctx.get('is_night') else 'day'}",
            f"Smoke detected: {'yes' if ctx.get('smoke') else 'no'}",
            f"Smoke PPM: {ctx.get('smoke_ppm', 0):.0f}",
            f"Motion duration: {ctx.get('motion_duration_s', 0):.1f} seconds",
        ]
        return "\n".join(lines)

    # ── Storage helpers ────────────────────────────────────────
    @staticmethod
    def _load_threats() -> list:
        if not THREATS_PATH.exists():
            return []
        try:
            return json.loads(THREATS_PATH.read_text(encoding="utf-8"))
        except Exception:
            return []

    @staticmethod
    def save_threats(threats: list) -> None:
        THREATS_PATH.parent.mkdir(parents=True, exist_ok=True)
        THREATS_PATH.write_text(
            json.dumps(threats, indent=2), encoding="utf-8"
        )

    @staticmethod
    def load_threats() -> list:
        return ThreatMatcher._load_threats()
