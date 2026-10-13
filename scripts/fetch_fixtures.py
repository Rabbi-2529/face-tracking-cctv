#!/usr/bin/env python
"""
Fetch and prepare the sample face images the test suite uses.

Two photographs of two different people, from permissively-licensed project
test data (OpenCV and scikit-image sample images). Having *real* faces in the
fixtures matters: synthetic patterns would tell you nothing about whether the
detector and embedder actually work, and the tests that assert "same person
scores far above different person" are the ones that prove the pipeline.

Each image is upscaled if needed so its face clears the 100px enrolment floor
in :mod:`vision.quality`. That is a fixture convenience, not a trick - the
source images are small, and the point here is to exercise identity logic, not
to measure accuracy. Real accuracy numbers come from your own cameras via
``scripts/camera_audit.py``.

Usage (inside the web or vision container):
    python scripts/fetch_fixtures.py
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TARGET_DIR = Path("samples/faces")
MIN_FACE_PX = 110  # a little above the 100px enrolment floor

SOURCES = {
    "face_a.jpg": (
        "https://raw.githubusercontent.com/opencv/opencv/4.x/samples/data/lena.jpg"
    ),
    "face_b.jpg": (
        "https://raw.githubusercontent.com/scikit-image/scikit-image/"
        "v0.25.0/skimage/data/astronaut.png"
    ),
}


def download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "cctv-tracking/0.1"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def prepare(name: str, url: str, *, force: bool) -> None:
    import cv2
    import numpy as np

    target = TARGET_DIR / name
    if target.exists() and not force:
        print(f"  {name}: already present")
        return

    print(f"  {name}: downloading")
    payload = download(url)
    image = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise SystemExit(f"could not decode {url}")

    try:
        from vision.detect.scrfd import SCRFDDetector

        detector = SCRFDDetector()
    except FileNotFoundError:
        print(
            f"  {name}: models absent, saving unscaled "
            "(run scripts/download_models.py first to size it correctly)"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(target), image)
        return

    faces = detector.detect(image)
    if not faces:
        raise SystemExit(f"no face detected in {url}; cannot use it as a fixture")

    width = faces[0].width
    if width < MIN_FACE_PX:
        scale = MIN_FACE_PX / width
        image = cv2.resize(
            image,
            (int(image.shape[1] * scale), int(image.shape[0] * scale)),
            interpolation=cv2.INTER_CUBIC,
        )
        confirmed = detector.detect(image)
        new_width = confirmed[0].width if confirmed else 0
        print(f"  {name}: face {width}px -> upscaled {scale:.2f}x -> {new_width}px")
    else:
        print(f"  {name}: face {width}px, no scaling needed")

    target.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(target), image, [int(cv2.IMWRITE_JPEG_QUALITY), 95])


def verify() -> int:
    """Confirm the fixtures are usable: two faces, clearly different people."""
    import cv2

    try:
        from vision.align import align_face
        from vision.detect.scrfd import SCRFDDetector
        from vision.embed.arcface import ArcFaceEmbedder, cosine_similarity
    except FileNotFoundError:
        print("\nmodels absent; skipping verification")
        return 0

    detector, embedder = SCRFDDetector(), ArcFaceEmbedder()
    embeddings = {}

    print("\nverifying fixtures")
    for name in SOURCES:
        image = cv2.imread(str(TARGET_DIR / name))
        faces = detector.detect(image)
        if not faces:
            print(f"  {name}: NO FACE - unusable")
            return 1
        face = faces[0]
        embeddings[name] = embedder.embed(align_face(image, face.landmarks))
        flag = "ok" if face.width >= 100 else "TOO SMALL for enrolment"
        print(f"  {name}: face {face.width}px, score {face.score:.3f}  [{flag}]")

    names = list(embeddings)
    similarity = cosine_similarity(embeddings[names[0]], embeddings[names[1]])
    print(f"\n  similarity between the two people: {similarity:.4f}")
    if similarity > 0.3:
        print("  WARNING: these two faces are not distinct enough to test with")
        return 1
    print("  good: clearly two different people")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="Re-download.")
    args = parser.parse_args()

    print(f"preparing sample faces in {TARGET_DIR}")
    for name, url in SOURCES.items():
        prepare(name, url, force=args.force)

    # Remove the stray PNG from an earlier manual fetch, if present.
    stray = TARGET_DIR / "face_b.png"
    if stray.exists():
        stray.unlink()

    return verify()


if __name__ == "__main__":
    raise SystemExit(main())
