#!/usr/bin/env python
"""
Build a synthetic "walk past the camera" video from a still portrait.

Lets the whole Phase 2 pipeline be exercised end to end - detect, align,
embed, track, vote, match, publish, aggregate - with no camera hardware and
no real person's footage. It is a test fixture, not a substitute for the
camera audit: a clean studio portrait panned across a frame is far kinder
than a real doorway, so treat any accuracy it shows as an upper bound.

Usage (inside the web or vision container):
    python scripts/make_test_video.py \\
        --face samples/faces/face_a.jpg \\
        --out samples/walk.mp4 --seconds 20
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402
import numpy as np  # noqa: E402


def build(
    face_path: Path,
    out_path: Path,
    *,
    seconds: int,
    fps: int,
    width: int,
    height: int,
    face_height: int,
) -> None:
    portrait = cv2.imread(str(face_path))
    if portrait is None:
        raise SystemExit(f"could not read {face_path}")

    scale = face_height / portrait.shape[0]
    face = cv2.resize(
        portrait,
        (int(portrait.shape[1] * scale), face_height),
        interpolation=cv2.INTER_AREA,
    )
    fh, fw = face.shape[:2]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(
        str(out_path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height)
    )
    if not writer.isOpened():
        raise SystemExit("could not open the video writer; is ffmpeg available?")

    total = seconds * fps
    rng = np.random.default_rng(42)

    # A plain mid-grey background with light noise, so the detector has a
    # realistic amount of texture to reject rather than a flat field.
    for index in range(total):
        frame = np.full((height, width, 3), 70, dtype=np.uint8)
        frame += rng.integers(0, 14, frame.shape, dtype=np.uint8)

        # Pan left to right and back, so the subject leaves and re-enters.
        progress = index / max(1, total - 1)
        sweep = abs((progress * 2.0) - 1.0)  # 1 -> 0 -> 1
        x = int((1.0 - sweep) * (width - fw))
        y = int((height - fh) / 2 + np.sin(progress * 8.0) * 18)

        x = max(0, min(width - fw, x))
        y = max(0, min(height - fh, y))
        frame[y : y + fh, x : x + fw] = face

        writer.write(frame)

    writer.release()
    size_mb = out_path.stat().st_size / 1e6
    print(
        f"wrote {out_path} ({total} frames, {seconds}s @ {fps}fps, "
        f"{face_height}px face, {size_mb:.1f} MB)"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--face", default="samples/faces/face_a.jpg")
    parser.add_argument("--out", default="samples/walk.mp4")
    parser.add_argument("--seconds", type=int, default=20)
    parser.add_argument("--fps", type=int, default=15)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument(
        "--face-height",
        type=int,
        default=260,
        help="Pixel height of the pasted portrait. Lower it to simulate a "
        "camera that is further away.",
    )
    args = parser.parse_args()

    build(
        Path(args.face),
        Path(args.out),
        seconds=args.seconds,
        fps=args.fps,
        width=args.width,
        height=args.height,
        face_height=args.face_height,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
