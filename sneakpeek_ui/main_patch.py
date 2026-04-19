"""
PATCH for main.py
-----------------
Add these changes to wire ThreatMatcher into the existing on_frame pipeline.
The matcher runs AFTER scoring confirms a threat, and BEFORE the email is sent.
It replaces the old hardcoded threat profile check with Claude-powered matching.
"""

# ── 1. Add import at top of main.py ──────────────────────────────────
from engine.threat_matcher import ThreatMatcher

# ── 2. In main(), initialise matcher alongside other components ───────
# Add after:  sender = AWSSender(config)
matcher = ThreatMatcher(config)

# ── 3. Update make_on_frame() signature ──────────────────────────────
# Change:
#   def make_on_frame(ctx, cooldown, detector, recognizer, pose_analyzer, scorer, sender):
# To:
#   def make_on_frame(ctx, cooldown, detector, recognizer, pose_analyzer, scorer, sender, matcher):

# ── 4. Replace Step 6 in on_frame() with this ────────────────────────
# (replaces the block that sends to AWS)

"""
        # ── Step 6: Custom threat matching (Claude API) ───────────────
        matched_threats = matcher.match(score_result)

        if not matched_threats:
            logger.info("No custom threats matched — no alert sent")
            return

        # Filter to threats not on cooldown
        ready = [
            m for m in matched_threats
            if m["id"] != "smoke_or_gas" and cooldown.is_ready(m["id"])
        ]
        if not ready:
            logger.info("All matched threats on cooldown — suppressed")
            return

        # ── Step 7: Send to AWS ───────────────────────────────────────
        score_result["matched_custom_threats"] = ready
        score_result["threats"] = [m["description"][:60] for m in ready]
        score_result["reasons"] = [
            f"[{m['severity'].upper()}] {m['reason']}" for m in ready
        ]

        success = sender.send(score_result, jpeg_bytes)
        if success:
            for m in ready:
                cooldown.mark_triggered(m["id"])
            # Notify UI
            try:
                import requests
                requests.post("http://localhost:5000/api/pipeline/event", json={
                    "event_type": "alert",
                    "sensor":     score_result["sensor_context"],
                    "pipeline":   {
                        "stage":        "alert",
                        "score":        score_result["score"],
                        "last_threats": [m["description"][:40] for m in ready],
                        "person_count": score_result["person_count"],
                        "unknown_count":score_result["unknown_count"],
                    },
                    **score_result,
                }, timeout=1)
            except Exception:
                pass
"""

# ── 5. Add claude_api_key to config.json ─────────────────────────────
# In config.json, add at the top level:
# "claude_api_key": "sk-ant-..."
# (or set it via the UI → Threat Profile → Claude API Key field)
