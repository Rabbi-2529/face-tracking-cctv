#!/usr/bin/env python
"""
Build a synthetic labelled set for exercising the accuracy benchmark.

From each fixture portrait in samples/faces/ it makes:

* gallery/<person>/  enrolment-style variants (small turns, lighting shifts)
* probes/<person>/   CCTV-style degradations: the face shrunk to 60-120 px,
                     blurred, JPEG-compressed, darkened or washed out,
                     slightly rotated, set in a larger frame

This proves the benchmark runs end to end and shows how the pipeline holds
up as images degrade. It is NOT an accuracy measurement: two studio
portraits are far easier than real people at a real doorway, and with only
two identities the false-positive side is barely tested. The real number
comes from probes captured by the pilot cameras.

    docker compose run --rm web python scripts/make_synthetic_benchmark.py \\
        --faces samples/faces --out samples/benchmark
"""

from __future__ import annotations

import argparse
import random
import shutil
from pathlib import Path

import cv2
import numpy as np

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}


def rotate(image: np.ndarray, degrees: float) -> np.ndarray:
    h, w = image.shape[:2]
    matrix = cv2.getRotationMatrix2D((w / 2, h / 2), degrees, 1.0)
    return cv2.warpAffine(image, matrix, (w, h), borderMode=cv2.BORDER_REFLECT)


def light(image: np.ndarray, gain: float, bias: float) -> np.ndarray:
    return np.clip(image.astype(np.float32) * gain + bias, 0, 255).astype(np.uint8)


def jpeg(image: np.ndarray, quality: int) -> np.ndarray:
    ok, buf = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    return cv2.imdecode(buf, cv2.IMREAD_COLOR) if ok else image


def measured_face_width(image: np.ndarray) -> int:
    """Width of the largest face in the portrait, from OpenCV's bundled detector."""
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    grey = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    faces = cascade.detectMultiScale(grey, scaleFactor=1.1, minNeighbors=5, minSize=(40, 40))
    if len(faces) == 0:
        return int(image.shape[1] * 0.5)
    return int(max(w for (_x, _y, w, _h) in faces))


def place_in_frame(face: np.ndarray, face_px: int, rng: random.Random, source_face_px: int) -> np.ndarray:
    """Scale the portrait so the face is about face_px wide, on a 1280x720 frame."""
    h, w = face.shape[:2]
    scale = face_px / max(1, source_face_px)
    small = cv2.resize(face, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
    frame = np.full((720, 1280, 3), rng.randint(70, 140), dtype=np.uint8)
    sh, sw = small.shape[:2]
    y = rng.randint(0, max(0, 720 - sh))
    x = rng.randint(0, max(0, 1280 - sw))
    frame[y:y + sh, x:x + sw] = small[: 720 - y, : 1280 - x]
    return frame


def gallery_variants(face: np.ndarray) -> list[np.ndarray]:
    return [
        face,
        rotate(face, -6),
        rotate(face, 6),
        light(face, 1.1, 8),
        light(face, 0.9, -6),
    ]


def probe_variants(face: np.ndarray, rng: random.Random, count: int) -> list[np.ndarray]:
    source_px = measured_face_width(face)
    probes = []
    for _ in range(count):
        image = rotate(face, rng.uniform(-10, 10))
        image = light(image, rng.uniform(0.65, 1.25), rng.uniform(-25, 20))
        image = place_in_frame(image, rng.randint(60, 120), rng, source_px)
        if rng.random() < 0.6:
            k = rng.choice([3, 5])
            image = cv2.GaussianBlur(image, (k, k), 0)
        image = jpeg(image, rng.randint(35, 80))
        probes.append(image)
    return probes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--faces", type=Path, default=Path("samples/faces"))
    parser.add_argument("--out", type=Path, default=Path("samples/benchmark"))
    parser.add_argument("--probes", type=int, default=25, help="probes per person")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    portraits = sorted(p for p in args.faces.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
    if len(portraits) < 2:
        print("Need at least two portraits in", args.faces)
        return 2

    if args.out.exists():
        shutil.rmtree(args.out)
    for path in portraits:
        person = path.stem
        face = cv2.imread(str(path))
        if face is None:
            print("skipping unreadable", path)
            continue
        for kind, images in (
            ("gallery", gallery_variants(face)),
            ("probes", probe_variants(face, rng, args.probes)),
        ):
            folder = args.out / kind / person
            folder.mkdir(parents=True, exist_ok=True)
            for i, image in enumerate(images):
                cv2.imwrite(str(folder / f"{person}_{i:02d}.jpg"), image)
        print(f"{person}: {len(gallery_variants(face))} gallery, {args.probes} probes")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
