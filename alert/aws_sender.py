"""
alert/aws_sender.py — SneakPeek AWS Alert Pipeline  (CORRECTED)
================================================================
Corrected pipeline:
  1.  Receive snapshot (JPEG bytes) + vision_result + sensor_data
  2.  Load custom_threats.json  →  evaluate EVERY enabled threat
  3.  Only if a threat matches  →  save locally  →  upload S3  →  SES email
  4.  Email contains: matched threat table, score, sensor panel,
      vision panel, embedded snapshot image (pre-signed S3 URL)

Import:
    from alert.aws_sender import AlertPipeline

Usage (from app.py alert worker):
    result = pipeline.send_if_matched(
        snapshot_jpeg = camera.snapshot_jpeg(),
        vision_result = {...},   # from your existing AI pipeline
        sensor_data   = {...},   # from /api/sensor/event
    )
"""

import json
import logging
import os
import threading
import time
import uuid
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger("sneakpeek.alert")

CUSTOM_THREATS_PATH = os.environ.get("CUSTOM_THREATS_PATH", "./custom_threats.json")
SNAPSHOT_LOCAL_DIR  = os.environ.get("SNAPSHOT_LOCAL_DIR",  "./snapshots")
MAX_RETRIES         = 3
BASE_BACKOFF        = 1.5   # seconds, doubles each retry

# ══════════════════════════════════════════════════════════════════════════════
# 1.  Custom threat evaluator
# ══════════════════════════════════════════════════════════════════════════════
def _matches(conditions: dict, vision: dict, sensors: dict) -> bool:
    """All conditions must be True (AND logic)."""
    for key, expected in conditions.items():
        if key == "motion":
            if bool(sensors.get("motion", False)) != bool(expected):
                return False
        elif key == "smoke":
            if bool(sensors.get("smoke", False)) != bool(expected):
                return False
        elif key == "night":
            if bool(sensors.get("night", False)) != bool(expected):
                return False
        elif key == "unknown_person":
            if bool(vision.get("unknown_person", False)) != bool(expected):
                return False
        elif key == "poses":
            if expected:
                if not set(vision.get("poses", [])).intersection(set(expected)):
                    return False
        elif key == "min_persons":
            if int(vision.get("persons", 0)) < int(expected):
                return False
        elif key == "min_smoke_ppm":
            if float(sensors.get("smoke_ppm", 0.0)) < float(expected):
                return False
        elif key == "max_ldr":
            if int(sensors.get("ldr", 9999)) > int(expected):
                return False
    return True


def evaluate_custom_threats(
    vision: dict,
    sensors: dict,
    threats_path: str = CUSTOM_THREATS_PATH,
) -> list:
    """Returns list of matched threat dicts, empty if none match or file missing."""
    try:
        if not os.path.exists(threats_path):
            logger.debug("custom_threats.json not found at %s", threats_path)
            return []
        with open(threats_path, "r", encoding="utf-8") as fh:
            all_threats = json.load(fh)
    except Exception:
        logger.exception("Failed to read custom_threats.json")
        return []

    matched = []
    for t in all_threats:
        if not t.get("enabled", True):
            continue
        try:
            if _matches(t.get("conditions", {}), vision, sensors):
                matched.append(t)
                logger.info("Threat matched: %s [%s]", t.get("name"), t.get("severity"))
        except Exception:
            logger.exception("Error evaluating threat '%s'", t.get("id"))
    return matched


# ══════════════════════════════════════════════════════════════════════════════
# 2.  AWS helpers
# ══════════════════════════════════════════════════════════════════════════════
def _boto_clients():
    kw = dict(
        region_name           = os.environ.get("AWS_REGION", "ap-south-1"),
        aws_access_key_id     = os.environ.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key = os.environ.get("AWS_SECRET_ACCESS_KEY"),
    )
    return boto3.client("s3", **kw), boto3.client("ses", **kw)


def _upload_s3(s3, bucket: str, key: str, data: bytes) -> str:
    """Upload and return pre-signed URL. Retries with exponential backoff."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            s3.put_object(Bucket=bucket, Key=key, Body=data, ContentType="image/jpeg")
            ttl = int(os.environ.get("AWS_SES_PRESIGN_TTL", "604800"))
            return s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": key},
                ExpiresIn=ttl,
            )
        except (BotoCoreError, ClientError) as exc:
            if attempt == MAX_RETRIES:
                raise
            wait = BASE_BACKOFF * (2 ** (attempt - 1))
            logger.warning("S3 attempt %d failed — retry in %.1fs (%s)", attempt, wait, exc)
            time.sleep(wait)


def _send_ses(ses, sender: str, recipients: list, subject: str, html: str, plain: str) -> str:
    """Send SES email. Returns MessageId."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = ses.send_email(
                Source      = sender,
                Destination = {"ToAddresses": recipients},
                Message     = {
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body": {
                        "Html": {"Data": html,  "Charset": "UTF-8"},
                        "Text": {"Data": plain, "Charset": "UTF-8"},
                    },
                },
            )
            return resp["MessageId"]
        except (BotoCoreError, ClientError) as exc:
            if attempt == MAX_RETRIES:
                raise
            wait = BASE_BACKOFF * (2 ** (attempt - 1))
            logger.warning("SES attempt %d failed — retry in %.1fs (%s)", attempt, wait, exc)
            time.sleep(wait)


def _send_smtp(subject: str, html: str, plain: str, sender: str, recipients: list[str],
               snapshot_jpeg: Optional[bytes], snapshot_name: str) -> str:
    """
    Send email via SMTP (Gmail/Brevo/Outlook/custom SMTP).
    Required env:
      SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS
    Optional:
      SMTP_USE_TLS=true|false (default true)
    """
    host = os.environ.get("SMTP_HOST", "").strip()
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "").strip()
    pwd  = os.environ.get("SMTP_PASS", "").strip()
    use_tls = os.environ.get("SMTP_USE_TLS", "true").lower() == "true"

    if not all([host, port, user, pwd]):
        raise RuntimeError("Missing SMTP config (SMTP_HOST/PORT/USER/PASS)")

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content(plain)
    msg.add_alternative(html, subtype="html")

    if snapshot_jpeg:
        msg.add_attachment(snapshot_jpeg, maintype="image", subtype="jpeg", filename=snapshot_name)

    with smtplib.SMTP(host, port, timeout=20) as server:
        if use_tls:
            server.starttls()
        server.login(user, pwd)
        server.send_message(msg)
    return f"smtp-{uuid.uuid4().hex[:12]}"


# ══════════════════════════════════════════════════════════════════════════════
# 3.  Email builder
# ══════════════════════════════════════════════════════════════════════════════
_SEV_COLOR = {"critical":"#A32D2D","high":"#854F0B","medium":"#185FA5","low":"#3B6D11"}
_SEV_BG    = {"critical":"#FCEBEB","high":"#FAEEDA","medium":"#E6F1FB","low":"#EAF3DE"}
_SEV_RANK  = {"low":1,"medium":2,"high":3,"critical":4}


def _build_email(matched: list, vision: dict, sensors: dict,
                 snapshot_url: str, timestamp: str):
    """Returns (subject, html, plain)."""
    top      = max(matched, key=lambda t: _SEV_RANK.get(t.get("severity","low"), 0))
    top_sev  = top.get("severity", "medium")
    top_name = top.get("name", "Custom Threat")
    top_msg  = top.get("message", "")

    subject  = f"[SneakPeek] {top_sev.upper()} — {top_name} at {timestamp}"

    rows = ""
    for t in matched:
        sev = t.get("severity","medium")
        conf = float(t.get("confidence", 0.0))
        rows += (
            f'<tr>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #2a2a2a;">'
            f'<span style="background:{_SEV_BG.get(sev,"#E6F1FB")};color:{_SEV_COLOR.get(sev,"#185FA5")};'
            f'padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600;">{sev.upper()}</span></td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #2a2a2a;font-weight:600;color:#e0e0e0;">{t.get("name","")}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #2a2a2a;color:#aaa;font-size:13px;">{t.get("message","")} (confidence {conf:.2f})</td>'
            f'</tr>'
        )

    motion_s  = "Yes" if sensors.get("motion") else "No"
    smoke_s   = f'{sensors.get("smoke_ppm",0.0):.0f} ppm' if sensors.get("smoke") else "Clear"
    night_s   = "Night" if sensors.get("night") else "Day"
    persons_s = str(vision.get("persons", 0))
    poses_s   = ", ".join(vision.get("poses", [])) or "—"
    unknown_s = "Yes" if vision.get("unknown_person") else "No"
    faces_s   = ", ".join(vision.get("face_ids", [])) or "—"
    uk_color  = "#ff6b6b" if vision.get("unknown_person") else "#e0e0e0"

    img_block = (
        f'<a href="{snapshot_url}">'
        f'<img src="{snapshot_url}" alt="Snapshot" style="width:100%;border-radius:6px;'
        f'border:1px solid #2a2a2a;display:block;"></a>'
        f'<div style="margin-top:8px;font-size:12px;color:#666;">'
        f'<a href="{snapshot_url}" style="color:#4a9eff;">View full-size →</a></div>'
    ) if snapshot_url else '<p style="color:#666;">No snapshot available.</p>'

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:20px;background:#0a0a0a;font-family:Arial,sans-serif;color:#e0e0e0;">
<div style="max-width:620px;margin:auto;background:#141414;border:1px solid #2a2a2a;border-radius:10px;overflow:hidden;">
  <div style="background:#1a0a0a;padding:20px 24px;border-bottom:1px solid #2a2a2a;">
    <div style="font-size:11px;color:#888;letter-spacing:2px;margin-bottom:6px;">SNEAKPEEK SECURITY ALERT</div>
    <div style="font-size:22px;font-weight:700;color:#ff4444;">{top_name}</div>
    <div style="margin-top:8px;">
      <span style="background:{_SEV_BG.get(top_sev,'#E6F1FB')};color:{_SEV_COLOR.get(top_sev,'#185FA5')};
            padding:3px 12px;border-radius:12px;font-size:12px;font-weight:600;">{top_sev.upper()}</span>
      <span style="color:#888;font-size:13px;margin-left:12px;">{timestamp}</span>
    </div>
    <div style="margin-top:10px;color:#ccc;font-size:14px;line-height:1.5;">{top_msg}</div>
  </div>
  <div style="padding:20px 24px;">
    <div style="font-size:11px;color:#888;letter-spacing:2px;margin-bottom:10px;">MATCHED THREATS</div>
    <table style="width:100%;border-collapse:collapse;font-size:13px;">
      <tr style="background:#1e1e1e;">
        <th style="padding:8px 12px;text-align:left;color:#888;font-weight:500;font-size:11px;">SEVERITY</th>
        <th style="padding:8px 12px;text-align:left;color:#888;font-weight:500;font-size:11px;">THREAT</th>
        <th style="padding:8px 12px;text-align:left;color:#888;font-weight:500;font-size:11px;">MESSAGE</th>
      </tr>{rows}
    </table>
  </div>
  <div style="padding:0 24px 20px;">
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;">
      <div style="background:#1e1e1e;border:1px solid #2a2a2a;border-radius:8px;padding:14px;">
        <div style="font-size:11px;color:#888;letter-spacing:2px;margin-bottom:10px;">SENSOR DATA</div>
        <table style="width:100%;font-size:13px;">
          <tr><td style="color:#888;padding:3px 0;">Motion</td><td style="text-align:right;color:#e0e0e0;">{motion_s}</td></tr>
          <tr><td style="color:#888;padding:3px 0;">Smoke</td><td style="text-align:right;color:#e0e0e0;">{smoke_s}</td></tr>
          <tr><td style="color:#888;padding:3px 0;">Light</td><td style="text-align:right;color:#e0e0e0;">{night_s} ({sensors.get("ldr",0)})</td></tr>
        </table>
      </div>
      <div style="background:#1e1e1e;border:1px solid #2a2a2a;border-radius:8px;padding:14px;">
        <div style="font-size:11px;color:#888;letter-spacing:2px;margin-bottom:10px;">VISION DATA</div>
        <table style="width:100%;font-size:13px;">
          <tr><td style="color:#888;padding:3px 0;">Persons</td><td style="text-align:right;color:#e0e0e0;">{persons_s}</td></tr>
          <tr><td style="color:#888;padding:3px 0;">Poses</td><td style="text-align:right;color:#e0e0e0;">{poses_s}</td></tr>
          <tr><td style="color:#888;padding:3px 0;">Unknown</td><td style="text-align:right;color:{uk_color};">{unknown_s}</td></tr>
          <tr><td style="color:#888;padding:3px 0;">Identified</td><td style="text-align:right;color:#e0e0e0;">{faces_s}</td></tr>
        </table>
      </div>
    </div>
  </div>
  <div style="padding:0 24px 24px;">
    <div style="font-size:11px;color:#888;letter-spacing:2px;margin-bottom:10px;">SNAPSHOT</div>
    {img_block}
  </div>
  <div style="padding:14px 24px;border-top:1px solid #2a2a2a;font-size:11px;color:#555;">
    Generated by SneakPeek &nbsp;·&nbsp; Do not reply to this email
  </div>
</div>
</body></html>"""

    plain = (
        f"SneakPeek Alert — {top_name} [{top_sev.upper()}]\n"
        f"Time: {timestamp}\nMessage: {top_msg}\n\n"
        f"Matched: {', '.join(t['name'] for t in matched)}\n\n"
        f"Sensors: motion={motion_s}, smoke={smoke_s}, light={night_s}\n"
        f"Vision:  persons={persons_s}, poses={poses_s}, unknown={unknown_s}, faces={faces_s}\n\n"
        f"Snapshot: {snapshot_url or 'N/A'}\n"
    )

    return subject, html, plain


# ══════════════════════════════════════════════════════════════════════════════
# 4.  Public API
# ══════════════════════════════════════════════════════════════════════════════
class AlertPipeline:
    """
    The single entry point.  Thread-safe (one alert dispatched at a time).

    result = pipeline.send_if_matched(
        snapshot_jpeg = camera.snapshot_jpeg(),
        vision_result = {
            "persons":        2,
            "poses":          ["crouching"],
            "unknown_person": True,
            "face_ids":       [],
        },
        sensor_data = {
            "motion":    True,
            "smoke":     False,
            "smoke_ppm": 0.0,
            "ldr":       180,
            "night":     True,
        },
    )
    Returns dict with keys: matched, snapshot_url, message_id, error
    """

    def __init__(self):
        self._lock = threading.Lock()

    def send_if_matched(
        self,
        snapshot_jpeg: Optional[bytes],
        vision_result: dict,
        sensor_data:   dict,
    ) -> dict:

        out = {"matched": [], "snapshot_url": None, "message_id": None, "error": None}

        # ── Step 1: evaluate custom threats ──────────────────────────────
        matched = evaluate_custom_threats(vision_result, sensor_data)
        if not matched:
            logger.info("No custom threats matched — alert suppressed.")
            return out
        out["matched"] = matched

        # ── Step 2: check env ─────────────────────────────────────────────
        bucket     = os.environ.get("AWS_S3_BUCKET", "")
        sender     = os.environ.get("AWS_SES_SENDER", "")
        recipients = [r.strip() for r in
                      os.environ.get("AWS_SES_RECIPIENT", "").split(",") if r.strip()]
        if not all([bucket, sender, recipients]):
            out["error"] = "Missing AWS config (AWS_S3_BUCKET / AWS_SES_SENDER / AWS_SES_RECIPIENT)"
            logger.error(out["error"])
            return out

        with self._lock:
            try:
                s3, ses    = _boto_clients()
                ts         = datetime.now(timezone.utc)
                ts_file    = ts.strftime("%Y%m%dT%H%M%SZ")
                ts_human   = ts.strftime("%Y-%m-%d %H:%M:%S UTC")
                filename   = f"snapshot_{ts_file}_{uuid.uuid4().hex[:8]}.jpg"
                snap_url   = ""

                # ── Step 3: save locally ──────────────────────────────────
                if snapshot_jpeg:
                    os.makedirs(SNAPSHOT_LOCAL_DIR, exist_ok=True)
                    local = os.path.join(SNAPSHOT_LOCAL_DIR, filename)
                    with open(local, "wb") as fh:
                        fh.write(snapshot_jpeg)
                    logger.info("Snapshot saved: %s", local)

                    # ── Step 4: upload S3 ─────────────────────────────────
                    prefix  = os.environ.get("AWS_S3_PREFIX", "snapshots/").rstrip("/")
                    s3_key  = f"{prefix}/{filename}"
                    snap_url = _upload_s3(s3, bucket, s3_key, snapshot_jpeg)
                    out["snapshot_url"] = snap_url
                    logger.info("Uploaded to S3: s3://%s/%s", bucket, s3_key)

                # ── Step 5: build & send email ────────────────────────────
                subject, html, plain = _build_email(
                    matched, vision_result, sensor_data, snap_url, ts_human
                )
                msg_id = _send_ses(ses, sender, recipients, subject, html, plain)
                out["message_id"] = msg_id
                logger.info("Alert email sent — id=%s threats=%s",
                            msg_id, [t["name"] for t in matched])

            except Exception as exc:
                out["error"] = str(exc)
                logger.exception("AlertPipeline.send_if_matched failed")

        return out

    def send_alert(self, payload: dict, snapshot_jpeg: Optional[bytes]) -> dict:
        """
        Backward-compatible API used by sneakpeek_ui/app.py.
        Expects payload from ThreatScorer.to_dict() + sensor context.
        """
        custom = payload.get("custom_threats", []) or []
        if not custom:
            return {"success": False, "error": "No matched custom threats", "snapshot_url": None, "message_id": None}

        matched = []
        for t in custom:
            matched.append({
                "id": t.get("id", ""),
                "name": t.get("name", "Custom Threat"),
                "severity": t.get("severity", payload.get("severity", "medium")),
                "message": t.get("message", ""),
                "confidence": float(payload.get("score", 0.0)),
            })

        provider = os.environ.get("EMAIL_PROVIDER", "aws").strip().lower()
        sender = os.environ.get("AWS_SES_SENDER", "").strip() or os.environ.get("SMTP_FROM", "").strip()
        recipients = [r.strip() for r in os.environ.get("AWS_SES_RECIPIENT", "").split(",") if r.strip()]
        if not all([sender, recipients]):
            return {"success": False, "error": "Missing sender/recipient config", "snapshot_url": None, "message_id": None}

        try:
            ts = datetime.now(timezone.utc)
            ts_file = ts.strftime("%Y%m%dT%H%M%SZ")
            ts_human = ts.strftime("%Y-%m-%d %H:%M:%S UTC")
            filename = f"snapshot_{ts_file}_{uuid.uuid4().hex[:8]}.jpg"
            snap_url = ""
            if snapshot_jpeg:
                os.makedirs(SNAPSHOT_LOCAL_DIR, exist_ok=True)
                with open(os.path.join(SNAPSHOT_LOCAL_DIR, filename), "wb") as fh:
                    fh.write(snapshot_jpeg)

            if provider == "aws":
                bucket = os.environ.get("AWS_S3_BUCKET", "").strip()
                if not bucket:
                    return {"success": False, "error": "Missing AWS_S3_BUCKET", "snapshot_url": None, "message_id": None}
                s3, ses = _boto_clients()
                if snapshot_jpeg:
                    prefix = os.environ.get("AWS_S3_PREFIX", "snapshots/").rstrip("/")
                    s3_key = f"{prefix}/{filename}"
                    snap_url = _upload_s3(s3, bucket, s3_key, snapshot_jpeg)

            subject, html, plain = _build_email(
                matched,
                {
                    "persons": payload.get("persons", 0),
                    "poses": payload.get("poses", []),
                    "unknown_person": payload.get("unknown_person", False),
                    "face_ids": payload.get("face_ids", []),
                },
                payload.get("sensor", {}),
                snap_url,
                ts_human,
            )

            if provider == "aws":
                msg_id = _send_ses(ses, sender, recipients, subject, html, plain)
            elif provider == "smtp":
                msg_id = _send_smtp(subject, html, plain, sender, recipients, snapshot_jpeg, filename)
            else:
                return {"success": False, "error": f"Unsupported EMAIL_PROVIDER: {provider}", "snapshot_url": None, "message_id": None}

            return {"success": True, "error": None, "snapshot_url": snap_url, "message_id": msg_id}
        except Exception as exc:
            logger.exception("send_alert failed")
            return {"success": False, "error": str(exc), "snapshot_url": None, "message_id": None}


# ── Backwards-compat alias so app.py import still works ──────────────────────
# app.py references AWSSender; remap it to AlertPipeline
AWSSender = AlertPipeline
