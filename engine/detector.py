"""
detector.py
-----------
YOLOv8 person detection.

Responsibility:
  - Decode JPEG bytes into a frame
  - Run YOLOv8 nano (fast, CPU-friendly on Windows)
  - Return only person detections with their bounding boxes + confidence

YOLOv8 is used here — not InsightFace/MediaPipe — because we need a first
pass to know IF people are in the frame and WHERE they are before running
the heavier face recognition and pose models on those regions.

This is the gatekeeper: if YOLO finds no people, the AI pipeline stops here.
"""

import cv2
import numpy as np
import logging
from ultralytics import YOLO

logger = logging.getLogger(__name__)

PERSON_CLASS_ID = 0  # COCO class 0 = person


class PersonDetector:
    def __init__(self, confidence: float = 0.45):
        """
        Parameters
        ----------
        confidence : minimum YOLO confidence to count as a detection
        """
        logger.info("[detector] Loading YOLOv8n — first run will download weights (~6MB)")
        self._model = YOLO("yolov8n.pt")  # nano — fastest, good enough for surveillance
        self._conf  = confidence
        logger.info("[detector] YOLOv8n ready")

    def detect(self, jpeg_bytes: bytes) -> list[dict]:
        """
        Parameters
        ----------
        jpeg_bytes : raw JPEG from ESP32-CAM

        Returns
        -------
        List of detections, each:
            {
                "bbox"  : [x1, y1, x2, y2],   # pixel coords
                "conf"  : float,               # 0.0 – 1.0
                "cx"    : int,                 # centre x
                "cy"    : int,                 # centre y
            }
        Empty list = no people found, pipeline should stop.
        """
        frame = self._decode(jpeg_bytes)
        if frame is None:
            return []

        results = self._model(
            frame,
            classes=[PERSON_CLASS_ID],
            conf=self._conf,
            verbose=False
        )[0]

        detections = []
        for box in results.boxes:
            x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
            conf = float(box.conf[0])
            detections.append({
                "bbox": [x1, y1, x2, y2],
                "conf": conf,
                "cx":   (x1 + x2) // 2,
                "cy":   (y1 + y2) // 2,
            })

        logger.debug(f"[detector] {len(detections)} person(s) found")
        return detections

    # ------------------------------------------------------------------
    def _decode(self, jpeg_bytes: bytes) -> np.ndarray | None:
        arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if frame is None:
            logger.warning("[detector] Failed to decode JPEG")
        return frame

    @staticmethod
    def get_frame(jpeg_bytes: bytes) -> np.ndarray | None:
        """Convenience — decode JPEG to BGR numpy array without running YOLO."""
        arr   = np.frombuffer(jpeg_bytes, dtype=np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR)
