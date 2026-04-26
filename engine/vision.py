"""
engine/vision.py — SneakPeek Snapshot Vision Analyser
=======================================================
This is the MISSING LINK between the camera snapshot and the threat engine.

It takes a raw numpy frame and returns a structured detection dict:
    {
        "persons":        int,       # how many people visible
        "poses":          list[str], # e.g. ["crouching", "standing"]
        "unknown_person": bool,      # True if face NOT in whitelist
        "face_ids":       list[str], # recognised face labels (empty if none)
    }

This dict is merged with sensor data in app.py and fed into scorer.py,
which then matches it against the user's custom threat conditions.

Strategy (two-tier, no paid API required):
  Tier 1 — YOLOv8n (ultralytics, runs locally, free)
    - Person detection       → fills "persons"
    - Pose estimation        → fills "poses"  (uses yolov8n-pose.pt)

  Tier 2 — face_recognition (dlib-based, runs locally, free)
    - Face detection + encoding
    - Compared against whitelist stored in engine/known_faces/
    - Unknown face → unknown_person = True

Whitelist management:
    Call VisionAnalyser.register_face(name, image_path) once per known person.
    Encodings are persisted to engine/known_faces/encodings.pkl.
    Call reload_whitelist() after adding new faces.

Install dependencies:
    pip install ultralytics face-recognition opencv-python-headless

Model files are downloaded automatically by ultralytics on first run.
"""

import logging
import os
import pickle
import threading
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

logger = logging.getLogger("sneakpeek.vision")

# ── Config ────────────────────────────────────────────────────────────────────
YOLO_POSE_MODEL   = os.environ.get("YOLO_POSE_MODEL",   "yolov8n-pose.pt")
YOLO_DETECT_MODEL = os.environ.get("YOLO_DETECT_MODEL", "yolov8n.pt")
FACE_ENCODINGS_PATH = os.path.join(
    os.path.dirname(__file__), "known_faces", "encodings.pkl"
)
FACE_TOLERANCE   = float(os.environ.get("FACE_TOLERANCE", "0.5"))  # lower = stricter

# Pose keypoint indices (COCO 17-point skeleton used by YOLOv8-pose)
# We use hip/knee/ankle y-coords to classify standing vs crouching/sitting
_KP_NOSE   = 0
_KP_LHIP   = 11
_KP_RHIP   = 12
_KP_LKNEE  = 13
_KP_RKNEE  = 14
_KP_LANK   = 15
_KP_RANK   = 16
_KP_LSHLDR = 5
_KP_RSHLDR = 6


# ── Lazy model loaders (import only if libraries present) ─────────────────────

def _load_yolo_pose():
    try:
        from ultralytics import YOLO
        model = YOLO(YOLO_POSE_MODEL)
        logger.info("YOLOv8 pose model loaded: %s", YOLO_POSE_MODEL)
        return model
    except Exception:
        logger.warning("YOLOv8 pose unavailable — pose detection disabled.")
        return None


def _load_face_recognition():
    try:
        import face_recognition
        return face_recognition
    except ImportError:
        logger.warning("face_recognition library not installed — face ID disabled.")
        return None


# ── Pose classifier from keypoints ───────────────────────────────────────────

def _classify_pose(keypoints: np.ndarray) -> str:
    """
    Given a (17, 2) or (17, 3) array of keypoints (x, y[, conf]),
    return a pose label: standing | crouching | sitting | lying | unknown.

    Logic:
      - If nose y ≈ hip y → lying (horizontal)
      - If ankle y - hip y is small relative to frame height → crouching/sitting
      - Otherwise → standing
    """
    try:
        kp = keypoints[:, :2]   # just x, y

        hip_y  = float(np.mean([kp[_KP_LHIP][1],  kp[_KP_RHIP][1]]))
        knee_y = float(np.mean([kp[_KP_LKNEE][1], kp[_KP_RKNEE][1]]))
        ank_y  = float(np.mean([kp[_KP_LANK][1],  kp[_KP_RANK][1]]))
        shl_y  = float(np.mean([kp[_KP_LSHLDR][1],kp[_KP_RSHLDR][1]]))
        nose_y = float(kp[_KP_NOSE][1])

        torso_h = abs(hip_y - shl_y)
        leg_h   = abs(ank_y - hip_y)

        if torso_h < 5 and leg_h < 5:
            return "unknown"   # keypoints not detected

        # Lying: nose is roughly at hip height (body is horizontal)
        if abs(nose_y - hip_y) < torso_h * 0.5:
            return "lying"

        # Crouching/sitting: knees significantly bent — ankle not much below hip
        if leg_h < torso_h * 0.8:
            return "crouching"

        # Standing
        return "standing"

    except Exception:
        return "unknown"


# ── Known-face whitelist ──────────────────────────────────────────────────────

class FaceWhitelist:
    """
    Stores face encodings for known people.  Thread-safe.
    """

    def __init__(self, path: str = FACE_ENCODINGS_PATH):
        self._path = path
        self._lock = threading.RLock()
        self._encodings: list[np.ndarray] = []
        self._names:     list[str]        = []
        self._load()

    def _load(self) -> None:
        try:
            if os.path.exists(self._path):
                with open(self._path, "rb") as f:
                    data = pickle.load(f)
                self._encodings = data.get("encodings", [])
                self._names     = data.get("names", [])
                logger.info("Loaded %d known face(s) from whitelist.", len(self._names))
            else:
                logger.info("No face whitelist found at %s — all faces = unknown.", self._path)
        except Exception:
            logger.exception("Could not load face whitelist.")

    def reload(self) -> None:
        with self._lock:
            self._load()

    def register(self, name: str, image_path: str) -> bool:
        """Add a face from an image file.  Returns True on success."""
        fr = _load_face_recognition()
        if fr is None:
            return False
        try:
            img = fr.load_image_file(image_path)
            encs = fr.face_encodings(img)
            if not encs:
                logger.warning("No face found in %s", image_path)
                return False
            with self._lock:
                self._encodings.append(encs[0])
                self._names.append(name)
                os.makedirs(os.path.dirname(self._path), exist_ok=True)
                with open(self._path, "wb") as f:
                    pickle.dump({"encodings": self._encodings, "names": self._names}, f)
            logger.info("Registered face: %s", name)
            return True
        except Exception:
            logger.exception("register() failed for %s", name)
            return False

    def identify(self, face_encoding: np.ndarray) -> Optional[str]:
        """
        Returns name if encoding matches whitelist, else None (= unknown).
        """
        fr = _load_face_recognition()
        if fr is None or not self._encodings:
            return None
        with self._lock:
            matches = fr.compare_faces(
                self._encodings, face_encoding, tolerance=FACE_TOLERANCE
            )
        for i, match in enumerate(matches):
            if match:
                return self._names[i]
        return None


# ── Main analyser ─────────────────────────────────────────────────────────────

class VisionAnalyser:
    """
    Analyses a camera frame and returns a detection dict that scorer.py
    can use to evaluate custom threat conditions.

    Usage (in app.py's _ai_pipeline):
        analyser = VisionAnalyser()

        def _ai_pipeline(frame):
            result = analyser.analyse(frame)
            # store result somewhere app.py can read per-request
            _latest_vision.update(result)
            return analyser.annotate(frame, result)   # returns annotated frame
    """

    def __init__(self):
        self._pose_model  = _load_yolo_pose()
        self._fr          = _load_face_recognition()
        self._whitelist   = FaceWhitelist()
        self._lock        = threading.Lock()

    def reload_whitelist(self) -> None:
        self._whitelist.reload()

    def register_face(self, name: str, image_path: str) -> bool:
        return self._whitelist.register(name, image_path)

    # ── main analysis ─────────────────────────────────────────────────────
    def analyse(self, frame: np.ndarray) -> dict:
        """
        Returns detection dict:
        {
            "persons":        int,
            "poses":          list[str],
            "unknown_person": bool,
            "face_ids":       list[str],
        }
        """
        result = {
            "persons":        0,
            "poses":          [],
            "unknown_person": False,
            "face_ids":       [],
        }

        # ── Tier 1: Person detection + pose ──────────────────────────────
        if self._pose_model is not None:
            try:
                with self._lock:
                    preds = self._pose_model(frame, verbose=False)

                for pred in preds:
                    if pred.keypoints is None:
                        continue
                    n_persons = len(pred.keypoints.data)
                    result["persons"] = n_persons

                    for kp_tensor in pred.keypoints.data:
                        kp = kp_tensor.cpu().numpy()   # (17, 3)
                        pose = _classify_pose(kp)
                        if pose != "unknown" and pose not in result["poses"]:
                            result["poses"].append(pose)

            except Exception:
                logger.exception("YOLO pose inference failed.")

        # ── Tier 2: Face identification ───────────────────────────────────
        if self._fr is not None:
            try:
                # face_recognition expects RGB
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                locations = self._fr.face_locations(rgb, model="hog")
                encodings = self._fr.face_encodings(rgb, locations)

                unknown_found = False
                for enc in encodings:
                    name = self._whitelist.identify(enc)
                    if name:
                        result["face_ids"].append(name)
                    else:
                        unknown_found = True

                result["unknown_person"] = unknown_found or (
                    len(locations) > 0 and len(result["face_ids"]) == 0
                )

            except Exception:
                logger.exception("Face recognition failed.")

        return result

    # ── annotate frame for Flask stream ───────────────────────────────────
    def annotate(self, frame: np.ndarray, result: dict) -> np.ndarray:
        """
        Draws bounding boxes and labels on a copy of the frame.
        Returns the annotated frame (does not modify original).
        """
        if self._pose_model is None:
            return frame

        try:
            with self._lock:
                preds = self._pose_model(frame, verbose=False)

            out = frame.copy()
            for pred in preds:
                if pred.boxes is None:
                    continue
                for box in pred.boxes.xyxy.cpu().numpy():
                    x1, y1, x2, y2 = map(int, box[:4])
                    color = (0, 80, 220)
                    cv2.rectangle(out, (x1, y1), (x2, y2), color, 1)

            # Overlay pose + unknown flag
            label_parts = []
            if result["poses"]:
                label_parts.append(", ".join(result["poses"]))
            if result["unknown_person"]:
                label_parts.append("UNKNOWN")
            if result["face_ids"]:
                label_parts.append(", ".join(result["face_ids"]))

            if label_parts:
                cv2.putText(
                    out, "  ".join(label_parts),
                    (10, 24), cv2.FONT_HERSHEY_SIMPLEX,
                    0.6, (0, 80, 220), 1, cv2.LINE_AA,
                )

            return out

        except Exception:
            logger.exception("annotate() failed.")
            return frame