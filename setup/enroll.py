"""
setup/enroll.py
---------------
One-time face enrollment — run this before starting the monitor.

Usage:
    python setup/enroll.py

What it does:
  1. Scans data/known_faces/ for subfolders (each subfolder = one person)
  2. Runs InsightFace on every image in each subfolder
  3. Averages the embeddings for each person (more photos = more robust)
  4. Saves the result to data/embeddings.pkl

Folder structure expected:
    data/known_faces/
        Alice/
            alice1.jpg
            alice2.jpg
        Bob/
            bob.png

After running, start main.py and everyone not in known_faces will be
classified as unknown.
"""

import sys
import pickle
import logging
import numpy as np
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger(__name__)

KNOWN_FACES_DIR = Path("data/known_faces")
EMBEDDINGS_PATH = Path("data/embeddings.pkl")
IMG_EXTS        = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def enroll():
    try:
        from insightface.app import FaceAnalysis
    except ImportError:
        logger.error("insightface not installed. Run: pip install insightface onnxruntime")
        sys.exit(1)

    if not KNOWN_FACES_DIR.exists():
        logger.error(f"Directory not found: {KNOWN_FACES_DIR}")
        logger.error("Create it and add subfolders named after each person.")
        sys.exit(1)

    app = FaceAnalysis(name="buffalo_sc", providers=["CPUExecutionProvider"])
    app.prepare(ctx_id=0, det_size=(320, 320))

    embeddings: dict[str, np.ndarray] = {}
    people = sorted([d for d in KNOWN_FACES_DIR.iterdir() if d.is_dir()])

    if not people:
        logger.error("No person subfolders found in data/known_faces/")
        sys.exit(1)

    for person_dir in people:
        name   = person_dir.name
        images = [f for f in person_dir.iterdir() if f.suffix.lower() in IMG_EXTS]

        if not images:
            logger.warning(f"  [{name}] No images found — skipping")
            continue

        logger.info(f"  Enrolling: {name} ({len(images)} image(s))")
        person_embs = []

        for img_path in images:
            import cv2
            frame = cv2.imread(str(img_path))
            if frame is None:
                logger.warning(f"    Could not read {img_path.name} — skipping")
                continue

            faces = app.get(frame)
            if not faces:
                logger.warning(f"    No face detected in {img_path.name} — skipping")
                continue
            if len(faces) > 1:
                logger.warning(f"    Multiple faces in {img_path.name} — using largest")

            # Use face with largest bounding box
            face = max(faces, key=lambda f: (f.bbox[2]-f.bbox[0]) * (f.bbox[3]-f.bbox[1]))
            person_embs.append(face.embedding)
            logger.info(f"    + {img_path.name}")

        if not person_embs:
            logger.warning(f"  [{name}] No usable embeddings — skipping")
            continue

        # Average all embeddings for this person (improves robustness)
        avg_emb = np.mean(person_embs, axis=0)
        avg_emb = avg_emb / np.linalg.norm(avg_emb)  # L2 normalise
        embeddings[name] = avg_emb
        logger.info(f"  [{name}] Enrolled with {len(person_embs)} embedding(s)")

    if not embeddings:
        logger.error("No embeddings generated. Check your images.")
        sys.exit(1)

    EMBEDDINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(EMBEDDINGS_PATH, "wb") as f:
        pickle.dump(embeddings, f)

    logger.info(f"\nSaved {len(embeddings)} identity(ies) to {EMBEDDINGS_PATH}")
    logger.info("Known faces: " + ", ".join(embeddings.keys()))
    logger.info("\nReady. Run: python main.py")


if __name__ == "__main__":
    enroll()
