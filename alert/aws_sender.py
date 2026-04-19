"""
alert/aws_sender.py
-------------------
Sends confirmed threats to AWS. Two modes:

STUB mode (no API Gateway URL set):
  - Saves snapshot to data/snapshots/ locally
  - Notifies UI via localhost
  - Logs what would be sent

LIVE mode (API Gateway URL configured):
  - Uploads JPEG to S3
  - POSTs detection payload to API Gateway
  - Lambda reads custom threats from DynamoDB
  - Lambda calls Claude to match threats
  - Lambda sends SES email if matched

Custom threats are stored BOTH locally (data/custom_threats.json)
AND synced to DynamoDB when AWS is configured.
The Lambda function reads from DynamoDB — not from local file.
"""

import json, logging, datetime, base64, urllib.request, urllib.error
from pathlib import Path
from engine.config_manager import cfg

logger = logging.getLogger(__name__)

SNAPSHOTS_DIR       = Path("data/snapshots")
CUSTOM_THREATS_PATH = Path("data/custom_threats.json")
SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)


class AWSSender:
    def __init__(self):
        pass  # All config read live from cfg

    def send(self, score_result: dict, jpeg_bytes: bytes) -> bool:
        aws      = cfg.aws()
        api_url  = aws.get("api_gateway_url", "")
        is_live  = bool(api_url and api_url.startswith("http"))

        timestamp = datetime.datetime.utcnow().isoformat() + "Z"
        filename  = self._save_snapshot(jpeg_bytes, timestamp)
        payload   = self._build_payload(score_result, timestamp, filename)

        if is_live:
            ok = self._send_live(payload, jpeg_bytes, aws)
        else:
            self._log_stub(payload)
            ok = True

        # Always notify UI (so alert appears in the log regardless)
        self._notify_ui(payload, filename)
        return ok

    # ── Sync custom threats to DynamoDB ───────────────────────
    def sync_threats_to_dynamo(self, threats: list) -> bool:
        """
        Called when user saves custom threats from UI.
        Pushes each threat to DynamoDB so Lambda can read them.
        """
        aws = cfg.aws()
        api = aws.get("api_gateway_url", "")
        if not api:
            logger.debug("[aws] Skipping DynamoDB sync — no API URL configured")
            return False
        try:
            body = json.dumps({
                "action": "sync_threats",
                "threats": threats,
                "user_id": "default"
            }).encode()
            req = urllib.request.Request(
                api.rstrip("/") + "/threats",
                data=body,
                headers={"Content-Type": "application/json"}
            )
            urllib.request.urlopen(req, timeout=5)
            logger.info(f"[aws] Synced {len(threats)} custom threats to DynamoDB")
            return True
        except Exception as e:
            logger.error(f"[aws] DynamoDB sync error: {e}")
            return False

    # ── Live send ──────────────────────────────────────────────
    def _send_live(self, payload: dict, jpeg_bytes: bytes, aws: dict) -> bool:
        try:
            payload["snapshot_b64"] = base64.b64encode(jpeg_bytes).decode()
            body = json.dumps(payload).encode()
            req  = urllib.request.Request(
                aws["api_gateway_url"].rstrip("/") + "/alert",
                data=body,
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                resp = json.loads(r.read())
            logger.info(f"[aws] Sent alert — response: {resp}")
            return True
        except Exception as e:
            logger.error(f"[aws] Send failed: {e}")
            return False

    # ── Payload builder ────────────────────────────────────────
    def _build_payload(self, sr: dict, timestamp: str, filename: str) -> dict:
        custom_threats = []
        if CUSTOM_THREATS_PATH.exists():
            try:
                custom_threats = [
                    t for t in json.loads(
                        CUSTOM_THREATS_PATH.read_text(encoding="utf-8")
                    ) if t.get("enabled", True)
                ]
            except Exception:
                pass
        return {
            "timestamp":        timestamp,
            "score":            sr["score"],
            "threats":          sr["threats"],
            "reasons":          sr["reasons"],
            "person_count":     sr["person_count"],
            "unknown_count":    sr["unknown_count"],
            "sensor_context":   sr.get("sensor_context", {}),
            "custom_threats":   custom_threats,
            "alert_email":      cfg.aws().get("alert_email", ""),
            "snapshot_filename":filename,
        }

    def _save_snapshot(self, jpeg_bytes: bytes, timestamp: str) -> str:
        safe = timestamp.replace(":", "-").replace(".", "-")
        fn   = f"snap_{safe}.jpg"
        try:
            (SNAPSHOTS_DIR / fn).write_bytes(jpeg_bytes)
        except Exception as e:
            logger.error(f"[aws] Snapshot save error: {e}"); fn = ""
        return fn

    def _log_stub(self, p: dict):
        logger.warning("=" * 55)
        logger.warning("[aws] ALERT (stub — not sent to AWS)")
        logger.warning(f"  Score   : {p['score']}")
        logger.warning(f"  Threats : {p['threats']}")
        logger.warning(f"  Reasons : {p['reasons']}")
        logger.warning(f"  Night   : {p['sensor_context'].get('is_night')}")
        logger.warning(f"  Threats to match: {len(p['custom_threats'])}")
        logger.warning("=" * 55)

    def _notify_ui(self, payload: dict, snapshot_filename: str):
        try:
            body = json.dumps({
                "event_type": "alert",
                "timestamp":  payload["timestamp"],
                "threats":    payload["threats"],
                "reasons":    payload["reasons"],
                "score":      payload["score"],
                "snapshot":   snapshot_filename,
                "sensor":     payload["sensor_context"],
            }).encode()
            req = urllib.request.Request(
                "http://localhost:5000/api/pipeline/event",
                data=body,
                headers={"Content-Type": "application/json"}
            )
            urllib.request.urlopen(req, timeout=1)
        except Exception:
            pass
