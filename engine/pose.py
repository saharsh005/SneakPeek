import cv2
import numpy as np
import logging

import mediapipe as mp
from mediapipe.tasks.python.vision import PoseLandmarker, PoseLandmarkerOptions
from mediapipe.tasks.python import BaseOptions
from mediapipe.tasks.python.vision import RunningMode

logger = logging.getLogger(__name__)


class PoseAnalyzer:
    def __init__(self, config: dict):
        self._contact_thresh = config["thresholds"]["contact_overlap_min"]
        self._aggression_thresh = config["thresholds"]["aggression_score_min"]

        # ✅ New MediaPipe API (WORKS with 0.10.x)
        options = PoseLandmarkerOptions(
            base_options=BaseOptions(model_asset_path="pose_landmarker.task"),
            running_mode=RunningMode.IMAGE,
            num_poses=1
        )

        self._pose = PoseLandmarker.create_from_options(options)

        self._prev_keypoints = {}

        logger.info("[pose] MediaPipe Tasks Pose ready")

    # ------------------------------------------------------------------
    def analyze(self, frame: np.ndarray, person_detections: list[dict]) -> dict:

        contact_iou = self._max_pairwise_iou(person_detections)
        contact_detected = contact_iou >= self._contact_thresh

        pose_results = []
        aggression_max = 0.0

        for idx, det in enumerate(person_detections):
            crop, _ = self._crop_person(frame, det["bbox"])
            if crop is None:
                continue

            kp = self._get_keypoints(crop)
            if kp is None:
                continue

            prev_kp = self._prev_keypoints.get(idx)
            score = self._aggression_score(kp, prev_kp)

            self._prev_keypoints[idx] = kp

            aggression_max = max(aggression_max, score)

            pose_results.append({
                "bbox": det["bbox"],
                "aggression_score": round(score, 3),
                "arms_raised": self._arms_raised(kp),
                "torso_lean": self._torso_lean(kp),
            })

        return {
            "contact_detected": contact_detected,
            "contact_iou": round(contact_iou, 3),
            "aggression_score": round(aggression_max, 3),
            "pose_results": pose_results,
        }

    # ------------------------------------------------------------------
    def _get_keypoints(self, crop: np.ndarray):
        rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)

        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        result = self._pose.detect(mp_image)

        if not result.pose_landmarks:
            return None

        lm = result.pose_landmarks[0]

        def get(i):
            return (lm[i].x, lm[i].y, lm[i].visibility)

        joints = {
            "l_shoulder": get(11),
            "r_shoulder": get(12),
            "l_elbow": get(13),
            "r_elbow": get(14),
            "l_wrist": get(15),
            "r_wrist": get(16),
            "l_hip": get(23),
            "r_hip": get(24),
        }

        return joints

    # ------------------------------------------------------------------
    def _aggression_score(self, kp: dict, prev_kp: dict = None) -> float:
        arms = self._arms_raised(kp)
        lean = self._torso_lean(kp)

        velocity = self._hand_velocity(kp, prev_kp) if prev_kp else 0.0
        distance = self._wrist_distance(kp)

        score = (arms * 0.3) + (lean * 0.3) + (velocity * 0.4)

        # ✅ suppress handshake-like behavior
        if distance < 0.05 and velocity < 0.1:
            score *= 0.3

        return min(1.0, score)

    # ------------------------------------------------------------------
    def _arms_raised(self, kp: dict) -> float:
        l = kp["l_wrist"][1] < kp["l_shoulder"][1]
        r = kp["r_wrist"][1] < kp["r_shoulder"][1]
        return 1.0 if (l or r) else 0.0

    def _torso_lean(self, kp: dict) -> float:
        try:
            sh_x = (kp["l_shoulder"][0] + kp["r_shoulder"][0]) / 2
            hp_x = (kp["l_hip"][0] + kp["r_hip"][0]) / 2
            sw = abs(kp["l_shoulder"][0] - kp["r_shoulder"][0])
            if sw < 0.01:
                return 0.0
            return min(1.0, abs(sh_x - hp_x) / sw)
        except:
            return 0.0

    def _wrist_distance(self, kp: dict) -> float:
        lx, ly, _ = kp["l_wrist"]
        rx, ry, _ = kp["r_wrist"]
        return np.sqrt((lx - rx)**2 + (ly - ry)**2)

    def _hand_velocity(self, kp: dict, prev_kp: dict) -> float:
        try:
            lx1, ly1, _ = kp["l_wrist"]
            lx0, ly0, _ = prev_kp["l_wrist"]

            rx1, ry1, _ = kp["r_wrist"]
            rx0, ry0, _ = prev_kp["r_wrist"]

            v = np.sqrt((lx1 - lx0)**2 + (ly1 - ly0)**2) + \
                np.sqrt((rx1 - rx0)**2 + (ry1 - ry0)**2)

            return min(1.0, v)
        except:
            return 0.0

    # ------------------------------------------------------------------
    @staticmethod
    def _max_pairwise_iou(detections):
        if len(detections) < 2:
            return 0.0
        max_iou = 0.0
        for i in range(len(detections)):
            for j in range(i + 1, len(detections)):
                max_iou = max(max_iou,
                    PoseAnalyzer._iou(detections[i]["bbox"], detections[j]["bbox"]))
        return max_iou

    @staticmethod
    def _iou(a, b):
        ax1, ay1, ax2, ay2 = a
        bx1, by1, bx2, by2 = b
        ix1, iy1 = max(ax1, bx1), max(ay1, by1)
        ix2, iy2 = min(ax2, bx2), min(ay2, by2)
        inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
        if inter == 0:
            return 0.0
        area_a = (ax2 - ax1) * (ay2 - ay1)
        area_b = (bx2 - bx1) * (by2 - by1)
        return inter / (area_a + area_b - inter)

    def _crop_person(self, frame, bbox):
        x1, y1, x2, y2 = bbox
        h, w = frame.shape[:2]
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(w, x2), min(h, y2)
        if x2 <= x1 or y2 <= y1:
            return None, None
        return frame[y1:y2, x1:x2].copy(), (x1, y1)
