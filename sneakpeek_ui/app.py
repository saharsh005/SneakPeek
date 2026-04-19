"""ui/app.py — SneakPeek Web UI. Run: python ui/app.py from SneakPeek root."""
import json, time, pickle, shutil, threading, subprocess, uuid, datetime, sys
from pathlib import Path
from flask import (Flask, render_template, request, jsonify,
                   Response, send_from_directory)
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024

BASE_DIR            = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

CONFIG_PATH         = BASE_DIR / "config.json"
ALERTS_PATH         = BASE_DIR / "data" / "alerts.json"
EMBEDDINGS_PATH     = BASE_DIR / "data" / "embeddings.pkl"
KNOWN_FACES_DIR     = BASE_DIR / "data" / "known_faces"
SNAPSHOTS_DIR       = BASE_DIR / "data" / "snapshots"
CUSTOM_THREATS_PATH = BASE_DIR / "data" / "custom_threats.json"

for d in [KNOWN_FACES_DIR, SNAPSHOTS_DIR, ALERTS_PATH.parent]:
    d.mkdir(parents=True, exist_ok=True)

ALLOWED_IMG = {"jpg","jpeg","png","bmp","webp"}

# _state = {
#     "sensor":   {"motion":False,"smoke":False,"smoke_ppm":0,
#                  "ldr":4095,"is_night":False,"motion_duration_s":0},
#     "pipeline": {"stage":"idle","score":0.0,"last_threats":[],
#                  "person_count":0,"unknown_count":0,"fps":0},
#     "system":   {"running":False,"last_update":0,"source":"phone"},
# }

sensor_data = {
    "pir": 0,
    "sound": 0,
    "vibration": 0,
    "distance": 0
}

_state_lock  = threading.Lock()
_sse_clients = []
_sse_lock    = threading.Lock()


# ── Helpers ───────────────────────────────────────────────────
def rc():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}

def wc(d):
    CONFIG_PATH.write_text(json.dumps(d, indent=2), encoding="utf-8")

def ra():
    try:
        return json.loads(ALERTS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []

def wa(a):
    ALERTS_PATH.write_text(json.dumps(a, indent=2), encoding="utf-8")

def rct():
    try:
        return json.loads(CUSTOM_THREATS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []

def wct(d):
    CUSTOM_THREATS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CUSTOM_THREATS_PATH.write_text(json.dumps(d, indent=2), encoding="utf-8")

def push_sse(etype, data):
    msg = f"event: {etype}\ndata: {json.dumps(data)}\n\n"
    with _sse_lock:
        dead = []
        for q in _sse_clients:
            try: q.append(msg)
            except: dead.append(q)
        for q in dead:
            _sse_clients.remove(q)

def allowed(fn):
    return "." in fn and fn.rsplit(".",1)[1].lower() in ALLOWED_IMG

def sync_threats_aws(threats: list):
    """Try to sync threats to DynamoDB via AWSSender."""
    try:
        from alert.aws_sender import AWSSender
        AWSSender().sync_threats_to_dynamo(threats)
    except Exception:
        pass


# ── Pages ──────────────────────────────────────────────────────
@app.route("/")
def index(): return render_template("index.html")

@app.route("/snapshots/<path:fn>")
def snapshot(fn): return send_from_directory(SNAPSHOTS_DIR, fn)

# ── MJPEG stream ───────────────────────────────────────────────
@app.route("/video_feed")
def video_feed():
    try:
        from engine.camera import frame_queue
    except ImportError:
        return Response(b"", mimetype="image/jpeg")

    def gen():
        import queue as qm
        blank = _blank()
        yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + blank + b"\r\n"
        while True:
            try:
                jpeg = frame_queue.get(timeout=2.0)
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
            except qm.Empty:
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + blank + b"\r\n"

    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

def _blank():
    import numpy as np, cv2
    b = np.zeros((240,320,3),dtype=np.uint8)
    cv2.putText(b,"CAMERA OFFLINE",(30,100),cv2.FONT_HERSHEY_SIMPLEX,0.65,(0,255,136),2)
    cv2.putText(b,"Set URL in Config tab",(40,130),cv2.FONT_HERSHEY_SIMPLEX,0.38,(80,80,80),1)
    _,j = cv2.imencode(".jpg",b)
    return j.tobytes()

# ── Config ─────────────────────────────────────────────────────
@app.route("/api/config", methods=["GET"])
def get_config(): return jsonify(rc())

@app.route("/api/config", methods=["POST"])
def set_config():
    """
    Write config.json. ConfigManager in engine watches this file
    and hot-reloads within 1 second — no engine restart needed.
    """
    d = request.get_json()
    if not d: return jsonify({"error":"No data"}), 400
    wc(d)
    push_sse("config_updated", {"msg":"Config saved and reloading in engine"})
    return jsonify({"ok":True, "note":"Engine will reload config within 1 second"})

# ── Alerts ─────────────────────────────────────────────────────
@app.route("/api/alerts")
def get_alerts(): return jsonify(list(reversed(ra())))

@app.route("/api/alerts/clear", methods=["POST"])
def clear_alerts():
    wa([]); push_sse("alerts_cleared",{}); return jsonify({"ok":True})

# ── Faces ──────────────────────────────────────────────────────
@app.route("/api/faces")
def get_faces():
    names = []
    if KNOWN_FACES_DIR.exists():
        for d in sorted(KNOWN_FACES_DIR.iterdir()):
            if d.is_dir():
                imgs = [f.name for f in d.iterdir() if allowed(f.name)]
                names.append({"name":d.name,"photo_count":len(imgs)})
    enrolled = []
    if EMBEDDINGS_PATH.exists():
        with open(EMBEDDINGS_PATH,"rb") as f:
            enrolled = list(pickle.load(f).keys())
    return jsonify({"identities":names,"enrolled":enrolled})

@app.route("/api/faces/upload", methods=["POST"])
def upload_face():
    name = request.form.get("name","").strip()
    if not name: return jsonify({"error":"Name required"}), 400
    if "photos" not in request.files: return jsonify({"error":"No photos"}), 400
    d = KNOWN_FACES_DIR / secure_filename(name)
    d.mkdir(parents=True, exist_ok=True)
    saved = []
    for f in request.files.getlist("photos"):
        if f and allowed(f.filename):
            fn = secure_filename(f.filename)
            f.save(d / fn); saved.append(fn)
    return jsonify({"ok":True,"saved":saved,"name":name})

@app.route("/api/faces/<n>", methods=["DELETE"])
def delete_face(n):
    p = KNOWN_FACES_DIR / secure_filename(n)
    if p.exists(): shutil.rmtree(p)
    if EMBEDDINGS_PATH.exists():
        with open(EMBEDDINGS_PATH,"rb") as f: emb = pickle.load(f)
        if n in emb:
            del emb[n]
            with open(EMBEDDINGS_PATH,"wb") as f: pickle.dump(emb,f)
    return jsonify({"ok":True})

@app.route("/api/faces/enroll", methods=["POST"])
def enroll():
    s = BASE_DIR / "setup" / "enroll.py"
    if not s.exists(): return jsonify({"error":"enroll.py not found"}), 404
    try:
        r = subprocess.run(["python", str(s)], cwd=str(BASE_DIR),
                           capture_output=True, text=True, timeout=180)
        push_sse("enrollment_done",{"ok":r.returncode==0})
        return jsonify({"ok":r.returncode==0,"output":r.stdout[-2000:],
                        "error":r.stderr[-500:] if r.returncode!=0 else ""})
    except subprocess.TimeoutExpired:
        return jsonify({"error":"Timed out"}), 500

# ── Custom threats ──────────────────────────────────────────────
@app.route("/api/custom_threats")
def get_ct(): return jsonify(rct())

@app.route("/api/custom_threats", methods=["POST"])
def add_ct():
    d = request.get_json()
    if not d or not d.get("description","").strip():
        return jsonify({"error":"Description required"}), 400
    threats = rct()
    t = {"id":"ct_"+uuid.uuid4().hex[:8],
         "description":d["description"].strip(),
         "severity":d.get("severity","medium"),
         "enabled":True,
         "created_at":datetime.datetime.utcnow().isoformat()+"Z"}
    threats.append(t); wct(threats)
    # Sync to DynamoDB if AWS configured
    threading.Thread(target=sync_threats_aws, args=(threats,), daemon=True).start()
    return jsonify({"ok":True,"threat":t})

@app.route("/api/custom_threats/<tid>", methods=["PATCH"])
def update_ct(tid):
    d = request.get_json() or {}
    threats = rct()
    for t in threats:
        if t["id"] == tid:
            for k in ["enabled","description","severity"]:
                if k in d: t[k] = d[k]
            break
    wct(threats)
    threading.Thread(target=sync_threats_aws, args=(threats,), daemon=True).start()
    return jsonify({"ok":True})

@app.route("/api/custom_threats/<tid>", methods=["DELETE"])
def delete_ct(tid):
    threats = [t for t in rct() if t["id"] != tid]
    wct(threats)
    threading.Thread(target=sync_threats_aws, args=(threats,), daemon=True).start()
    return jsonify({"ok":True})

# ── Pipeline event (from engine) ────────────────────────────────
@app.route("/api/pipeline/event", methods=["POST"])
def pipeline_event():
    d     = request.get_json() or {}
    etype = d.get("event_type","state_update")
    with _state_lock:
        if "sensor"   in d: _state["sensor"].update(d["sensor"])
        if "pipeline" in d: _state["pipeline"].update(d["pipeline"])
        if "system"   in d: _state["system"].update(d["system"])
        _state["system"]["last_update"] = time.time()
        _state["system"]["running"]     = True
    if etype == "alert":
        alert = {"id":int(time.time()*1000),
                 "timestamp":d.get("timestamp",""),
                 "threats":d.get("threats",[]),
                 "reasons":d.get("reasons",[]),
                 "score":d.get("score",0),
                 "snapshot":d.get("snapshot",""),
                 "sensor":d.get("sensor",{})}
        alerts = ra(); alerts.append(alert); wa(alerts[-200:])
        push_sse("new_alert",alert)
    with _state_lock:
        push_sse("state_update",dict(_state))
    return jsonify({"ok":True})

@app.route('/')
def home():
    return "Server is running"

from flask import request, jsonify

@app.route('/api/sensor/event', methods=['POST'])
def sensor_event():
    global sensor_data

    data = request.json
    print("Incoming sensor data:", data)  # DEBUG

    # Update values properly (IMPORTANT)
    sensor_data["pir"] = data.get("pir", sensor_data["pir"])
    sensor_data["sound"] = data.get("sound", sensor_data["sound"])
    sensor_data["vibration"] = data.get("vibration", sensor_data["vibration"])
    sensor_data["distance"] = data.get("distance", sensor_data["distance"])

    return jsonify({"status": "updated"})

@app.route("/api/camera/event", methods=["POST"])
def camera_event():
    d = request.get_json() or {}

    with _state_lock:
        _state["pipeline"].update({
            "person_count": d.get("person_count", 0),
            "unknown_count": d.get("unknown_count", 0),
            "last_threats": d.get("threats", []),
            "score": d.get("score", 0)
        })
        _state["system"]["last_update"] = time.time()

    push_sse("camera_update", _state["pipeline"])
    return jsonify({"ok": True})

@app.route('/api/state')
def get_state():
    return jsonify(sensor_data)

@app.route("/api/cooldown")
def get_cooldown():
    """Return cooldown status — engine updates this via state push."""
    with _state_lock:
        return jsonify(_state.get("cooldown", {}))

# ── SSE ────────────────────────────────────────────────────────
@app.route("/stream")
def stream():
    q = []
    with _sse_lock: _sse_clients.append(q)
    def gen():
        with _state_lock:
            yield f"event: state_update\ndata: {json.dumps(_state)}\n\n"
        try:
            while True:
                if q: yield q.pop(0)
                else:
                    yield ": ping\n\n"; time.sleep(0.8)
        except GeneratorExit: pass
        finally:
            with _sse_lock:
                if q in _sse_clients: _sse_clients.remove(q)
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})


def start_ui_server(host: str = "0.0.0.0", port: int = 5000):
    def run():
        print(f"SneakPeek UI  →  http://{host}:{port}")
        app.run(host=host, port=port, debug=False,
                threaded=True, use_reloader=False)

    thread = threading.Thread(target=run, daemon=True, name="ui-server")
    thread.start()
    return thread


if __name__ == "__main__":
    print("SneakPeek UI  →  http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)