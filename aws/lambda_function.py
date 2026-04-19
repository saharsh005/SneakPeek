"""
aws/lambda_function.py
-----------------------
AWS Lambda function triggered by API Gateway.

Two routes handled:
  POST /alert   — receives snapshot + detection data, matches threats, sends email
  POST /threats — syncs custom threats from laptop to DynamoDB

Environment variables required (set in Lambda console):
  DYNAMODB_TABLE  — e.g. "sneakpeek-threats"
  S3_BUCKET       — e.g. "sneakpeek-snapshots"
  SES_FROM_EMAIL  — verified SES sender email
  ANTHROPIC_KEY   — Claude API key for threat matching

IAM permissions needed:
  - dynamodb:GetItem, PutItem, Scan (on DYNAMODB_TABLE)
  - s3:PutObject, GetObject (on S3_BUCKET)
  - ses:SendEmail
"""

import json, os, base64, datetime, urllib.request
import boto3

dynamodb = boto3.resource("dynamodb")
s3       = boto3.client("s3")
ses      = boto3.client("ses", region_name=os.environ.get("AWS_REGION","ap-south-1"))

TABLE_NAME   = os.environ.get("DYNAMODB_TABLE", "sneakpeek-threats")
BUCKET       = os.environ.get("S3_BUCKET",      "sneakpeek-snapshots")
FROM_EMAIL   = os.environ.get("SES_FROM_EMAIL", "")
CLAUDE_KEY   = os.environ.get("ANTHROPIC_KEY",  "")
CLAUDE_MODEL = "claude-sonnet-4-20250514"


def lambda_handler(event, context):
    path   = event.get("path", "/alert")
    body   = json.loads(event.get("body", "{}"))
    method = event.get("httpMethod", "POST")

    if path.endswith("/threats"):
        return handle_sync_threats(body)
    else:
        return handle_alert(body)


# ── /alert — main threat matching and email flow ──────────────
def handle_alert(body: dict) -> dict:
    score    = body.get("score", 0)
    threats  = body.get("threats", [])
    reasons  = body.get("reasons", [])
    sensor   = body.get("sensor_context", {})
    email    = body.get("alert_email", "")
    snap_b64 = body.get("snapshot_b64", "")
    ts       = body.get("timestamp", datetime.datetime.utcnow().isoformat()+"Z")

    # 1. Upload snapshot to S3
    snapshot_url = ""
    if snap_b64:
        snapshot_url = upload_snapshot(snap_b64, ts)

    # 2. Load user's custom threats from DynamoDB
    custom_threats = load_custom_threats()

    if not custom_threats:
        # No custom threats defined — fall back to raw AI threat labels
        if threats and email:
            send_email(email, score, threats, reasons, sensor, snapshot_url)
        return ok({"matched": threats, "email_sent": bool(threats and email)})

    # 3. Build detection summary for Claude
    detection_summary = build_detection_summary(body)

    # 4. Ask Claude to match
    matched = match_with_claude(detection_summary, custom_threats)

    # 5. Send email for matched threats
    email_sent = False
    if matched and email:
        send_threat_email(email, matched, score, reasons, sensor, snapshot_url)
        email_sent = True

    return ok({"matched": [m["id"] for m in matched], "email_sent": email_sent})


# ── /threats — sync custom threats from laptop ────────────────
def handle_sync_threats(body: dict) -> dict:
    threats = body.get("threats", [])
    table   = dynamodb.Table(TABLE_NAME)
    for t in threats:
        table.put_item(Item={
            "threat_id":   t["id"],
            "description": t["description"],
            "severity":    t.get("severity", "medium"),
            "enabled":     t.get("enabled", True),
            "created_at":  t.get("created_at", ""),
        })
    return ok({"synced": len(threats)})


# ── DynamoDB helpers ───────────────────────────────────────────
def load_custom_threats() -> list:
    try:
        table = dynamodb.Table(TABLE_NAME)
        resp  = table.scan(
            FilterExpression=boto3.dynamodb.conditions.Attr("enabled").eq(True)
        )
        return resp.get("Items", [])
    except Exception as e:
        print(f"[dynamo] Error loading threats: {e}")
        return []


# ── S3 snapshot upload ─────────────────────────────────────────
def upload_snapshot(b64_data: str, timestamp: str) -> str:
    try:
        safe = timestamp.replace(":", "-").replace(".", "-")
        key  = f"snapshots/snap_{safe}.jpg"
        s3.put_object(
            Bucket=BUCKET, Key=key,
            Body=base64.b64decode(b64_data),
            ContentType="image/jpeg"
        )
        url = f"https://{BUCKET}.s3.amazonaws.com/{key}"
        print(f"[s3] Uploaded {key}")
        return url
    except Exception as e:
        print(f"[s3] Upload error: {e}")
        return ""


# ── Claude threat matching ─────────────────────────────────────
def build_detection_summary(body: dict) -> str:
    sensor = body.get("sensor_context", {})
    return "\n".join([
        f"Threat score: {body.get('score',0):.2f}",
        f"Persons detected: {body.get('person_count',0)}",
        f"Unknown persons: {body.get('unknown_count',0)}",
        f"AI labels: {', '.join(body.get('threats',[])) or 'none'}",
        f"Reasons: {' | '.join(body.get('reasons',[])) or 'none'}",
        f"Time context: {'night' if sensor.get('is_night') else 'day'}",
        f"Smoke detected: {'yes, ppm='+str(sensor.get('smoke_ppm',0)) if sensor.get('smoke') else 'no'}",
        f"Motion duration: {sensor.get('motion_duration_s',0):.0f}s",
    ])


def match_with_claude(detection_summary: str, threats: list) -> list:
    if not CLAUDE_KEY:
        # No Claude key — return all enabled threats as matched (fallback)
        return [{"id":t["threat_id"],"description":t["description"],
                 "severity":t.get("severity","medium"),"reason":"Keyword match fallback"} for t in threats]

    threats_text = "\n".join([
        f'- ID:{t["threat_id"]} | Severity:{t.get("severity","medium")} | "{t["description"]}"'
        for t in threats
    ])
    prompt = f"""You are a security system threat classifier.

Current AI detection from a CCTV camera:
{detection_summary}

User's custom threat alerts:
{threats_text}

For each custom threat, decide if the current detection matches it.
Respond ONLY with valid JSON, no markdown, no explanation:
[{{"id":"threat_id","matched":true,"confidence":0.9,"reason":"one sentence"}}]"""

    try:
        body = json.dumps({
            "model":      CLAUDE_MODEL,
            "max_tokens": 800,
            "messages":   [{"role":"user","content":prompt}]
        }).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=body,
            headers={
                "Content-Type":      "application/json",
                "x-api-key":         CLAUDE_KEY,
                "anthropic-version": "2023-06-01",
            }
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read())
        raw  = resp["content"][0]["text"].strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"): raw = raw[4:]
        results = json.loads(raw)

        id_map  = {t["threat_id"]: t for t in threats}
        matched = []
        for r in results:
            if r.get("matched") and r.get("confidence", 0) >= 0.5:
                t = id_map.get(r["id"], {})
                matched.append({
                    "id":          r["id"],
                    "description": t.get("description",""),
                    "severity":    t.get("severity","medium"),
                    "reason":      r.get("reason",""),
                    "confidence":  r.get("confidence",0),
                })
        return matched
    except Exception as e:
        print(f"[claude] Error: {e}")
        return []


# ── SES email ──────────────────────────────────────────────────
def send_threat_email(to_email: str, matched: list,
                      score: float, reasons: list,
                      sensor: dict, snapshot_url: str):
    severity_order = {"critical":0,"high":1,"medium":2,"low":3}
    top = sorted(matched, key=lambda x: severity_order.get(x.get("severity","medium"),2))
    if not top: return

    subject = f"[SneakPeek] {top[0]['severity'].upper()} ALERT — {top[0]['description'][:60]}"

    matched_html = "".join([f"""
    <tr>
      <td style="padding:8px 0;border-bottom:1px solid #1a1a1a">
        <strong style="color:#00ff88">{m['description']}</strong><br>
        <small style="color:#888">Severity: {m['severity'].upper()} | {m['reason']}</small>
      </td>
    </tr>""" for m in matched])

    snap_html = f'<img src="{snapshot_url}" style="max-width:480px;border:1px solid #333;border-radius:4px">' if snapshot_url else "<p style='color:#666'>No snapshot available</p>"

    html = f"""<!DOCTYPE html>
<html><body style="background:#080808;color:#c0c0c0;font-family:monospace;padding:24px;max-width:600px">
  <div style="border:1px solid #1e1e1e;border-left:4px solid #ff2244;border-radius:4px;padding:20px">
    <h2 style="color:#ff2244;margin:0 0 16px;letter-spacing:2px">SNEAKPEEK SECURITY ALERT</h2>
    <p style="color:#888;margin:0 0 16px;font-size:12px">Score: <strong style="color:#ffaa00">{score:.2f}</strong> | {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}</p>
    <h3 style="color:#00ff88;font-size:11px;letter-spacing:2px;margin:0 0 8px">MATCHED THREATS</h3>
    <table style="width:100%">{matched_html}</table>
    <h3 style="color:#00ff88;font-size:11px;letter-spacing:2px;margin:16px 0 8px">AI DETECTION REASONS</h3>
    <ul style="margin:0;padding-left:20px;color:#888">{''.join(f'<li>{r}</li>' for r in reasons)}</ul>
    <h3 style="color:#00ff88;font-size:11px;letter-spacing:2px;margin:16px 0 8px">SENSOR CONTEXT</h3>
    <p style="color:#888;font-size:11px">
      Time: {'NIGHT' if sensor.get('is_night') else 'DAY'} |
      Smoke: {'YES ('+str(sensor.get('smoke_ppm',0))+'ppm)' if sensor.get('smoke') else 'No'} |
      Motion: {sensor.get('motion_duration_s',0):.0f}s
    </p>
    <h3 style="color:#00ff88;font-size:11px;letter-spacing:2px;margin:16px 0 8px">SNAPSHOT</h3>
    {snap_html}
  </div>
</body></html>"""

    try:
        ses.send_email(
            Source=FROM_EMAIL,
            Destination={"ToAddresses":[to_email]},
            Message={
                "Subject":{"Data":subject},
                "Body":{"Html":{"Data":html}}
            }
        )
        print(f"[ses] Email sent to {to_email}")
    except Exception as e:
        print(f"[ses] Email error: {e}")


def send_email(to_email, score, threats, reasons, sensor, snapshot_url):
    """Fallback email when no custom threats defined."""
    send_threat_email(to_email,
        [{"description":t,"severity":"medium","reason":"AI detection"} for t in threats],
        score, reasons, sensor, snapshot_url)


def ok(body: dict) -> dict:
    return {"statusCode":200,"headers":{"Content-Type":"application/json"},"body":json.dumps(body)}
