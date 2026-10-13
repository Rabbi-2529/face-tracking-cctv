#!/usr/bin/env python
"""
Camera viability audit - run this FIRST, before building anything else.

It connects to each camera, samples frames for a while, finds faces, and
measures how many pixels wide they actually are. That single number decides
whether face recognition is possible on that camera:

    >= 100 px   good - reliable identification
    60 - 99 px  marginal - works, with errors; reposition if you can
    < 60 px     presence only - this camera cannot name anyone
    no faces    unusable for people, or nobody walked past during the sample

If most cameras come back under 60 px, the fix is camera placement, not model
tuning, and the project plan needs to change before any GPU is bought. That
is exactly the finding you want in week one rather than month four.

Face detection here uses OpenCV's bundled Haar cascade, not the production
SCRFD model. Haar is weaker and will slightly *under*-count faces, which is
the right direction for a go/no-go check: a camera that passes this will do
better in production, never worse. It also means this script needs no model
downloads and no GPU.

Usage
-----
Audit every enabled camera in the database and record the results:

    docker compose --profile vision run --rm vision \\
        python scripts/camera_audit.py --seconds 120 --save

Audit one URL without touching the database (useful during installation,
while an engineer is physically aiming the camera):

    docker compose --profile vision run --rm vision \\
        python scripts/camera_audit.py \\
        --url "rtsp://user:pass@192.168.1.64:554/cam/realmonitor?channel=1&subtype=0"
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import statistics
import sys
import time
from pathlib import Path

# Running a file inside scripts/ puts scripts/ on sys.path, not the project
# root, so `import vision` would fail. Fix it before any project import.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Force TCP: over UDP a flaky camera blocks forever instead of erroring.
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|stimeout;5000000"
)

import cv2  # noqa: E402
import numpy as np  # noqa: E402

GOOD_PX = 100
MARGINAL_PX = 60


def verdict_for(median_px: float | None, faces: int) -> str:
    if not faces or median_px is None:
        return "unusable"
    if median_px >= GOOD_PX:
        return "identity_ok"
    if median_px >= MARGINAL_PX:
        return "marginal"
    return "presence_only"


def audit_stream(url: str, label: str, seconds: int, sample_fps: float) -> dict:
    """Sample one stream and measure the faces it delivers."""
    cascade = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    )
    if cascade.empty():
        raise RuntimeError("Could not load the bundled Haar cascade from OpenCV.")

    capture = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    result = {
        "label": label,
        "frames_sampled": 0,
        "faces_detected": 0,
        "face_widths": [],
        "sharpness": [],
        "brightness": [],
        "resolution": None,
        "error": "",
    }

    if not capture.isOpened():
        result["error"] = "could not open stream (check URL, credentials, port 554)"
        return result

    interval = 1.0 / max(sample_fps, 0.5)
    deadline = time.monotonic() + seconds
    next_sample = 0.0
    consecutive_failures = 0

    while time.monotonic() < deadline:
        ok, frame = capture.read()
        if not ok or frame is None:
            consecutive_failures += 1
            if consecutive_failures > 100:
                result["error"] = "stream stopped delivering frames"
                break
            time.sleep(0.05)
            continue
        consecutive_failures = 0

        now = time.monotonic()
        if now < next_sample:
            continue
        next_sample = now + interval

        if result["resolution"] is None:
            result["resolution"] = f"{frame.shape[1]}x{frame.shape[0]}"

        grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        result["frames_sampled"] += 1
        result["brightness"].append(float(np.mean(grey)))

        faces = cascade.detectMultiScale(
            grey, scaleFactor=1.1, minNeighbors=5, minSize=(24, 24)
        )
        for (x, y, w, h) in faces:
            result["faces_detected"] += 1
            result["face_widths"].append(int(w))
            crop = grey[y : y + h, x : x + w]
            if crop.size:
                # Variance of Laplacian: low means blurred, which also hurts
                # recognition even when the face is big enough.
                result["sharpness"].append(
                    float(cv2.Laplacian(crop, cv2.CV_64F).var())
                )

    capture.release()
    return result


def summarise(raw: dict) -> dict:
    widths = raw["face_widths"]
    median = statistics.median(widths) if widths else None
    p90 = (
        float(np.percentile(widths, 90)) if len(widths) >= 5 else (median or None)
    )
    return {
        "label": raw["label"],
        "resolution": raw["resolution"],
        "frames_sampled": raw["frames_sampled"],
        "faces_detected": raw["faces_detected"],
        "median_face_width_px": round(median, 1) if median else None,
        "p90_face_width_px": round(p90, 1) if p90 else None,
        "mean_sharpness": round(statistics.fmean(raw["sharpness"]), 1)
        if raw["sharpness"]
        else None,
        "mean_brightness": round(statistics.fmean(raw["brightness"]), 1)
        if raw["brightness"]
        else None,
        "verdict": verdict_for(median, raw["faces_detected"]),
        "error": raw["error"],
    }


def advice_for(row: dict) -> str:
    if row["error"]:
        return row["error"]
    if row["verdict"] == "unusable":
        return (
            "no faces seen - either nobody passed during the sample, or the "
            "camera sees only backs and tops of heads"
        )
    if row["verdict"] == "presence_only":
        return "faces far too small: move the camera to a doorway at face height"
    if row["verdict"] == "marginal":
        return "will work with errors: lower the mount or reduce the tilt if possible"
    brightness = row["mean_brightness"]
    if brightness is not None and (brightness < 50 or brightness > 205):
        return "face size is fine, but exposure is poor - check for backlight"
    sharpness = row["mean_sharpness"]
    if sharpness is not None and sharpness < 50:
        return "face size is fine, but images are soft - check focus"
    return "good for identity"


def print_table(rows: list[dict]) -> None:
    header = (
        f"{'camera':<22}{'res':<12}{'frames':>7}{'faces':>7}"
        f"{'median px':>11}{'p90 px':>8}  {'verdict':<15}advice"
    )
    print()
    print(header)
    print("-" * len(header))
    for row in rows:
        print(
            f"{row['label'][:21]:<22}"
            f"{(row['resolution'] or '-'):<12}"
            f"{row['frames_sampled']:>7}"
            f"{row['faces_detected']:>7}"
            f"{(row['median_face_width_px'] or 0):>11}"
            f"{(row['p90_face_width_px'] or 0):>8}  "
            f"{row['verdict']:<15}"
            f"{advice_for(row)}"
        )

    counts: dict[str, int] = {}
    for row in rows:
        counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1

    print()
    print("Summary:", ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))

    usable = counts.get("identity_ok", 0) + counts.get("marginal", 0)
    if rows and usable == 0:
        print()
        print("STOP. No camera in this estate can identify a face.")
        print("Re-plan the camera layer before writing any recognition code:")
        print("  - add a camera at each doorway you care about")
        print("  - mount at 1.8-2.2 m, facing people as they walk toward it")
        print("  - keep the downward tilt under 15-20 degrees")
    elif usable < len(rows) / 2:
        print()
        print(
            f"WARNING: only {usable} of {len(rows)} cameras can identify faces. "
            "Expect poor coverage until more doorway cameras are added."
        )


def load_cameras_from_db() -> list[tuple[str, str, int]]:
    """(code, rtsp_url, camera_id) for every enabled, eligible camera."""
    from vision.gallery import load_cameras

    return [(c.code, c.rtsp_url, c.id) for c in load_cameras()]


def save_results(results: list[tuple[int, dict]]) -> None:
    """Write CameraAuditResult rows and update each camera's measured width.

    Done with plain SQL so this script keeps working without Django loaded.
    """
    import psycopg

    from vision.config import settings

    with psycopg.connect(settings.database_url) as conn, conn.cursor() as cur:
        for camera_id, row in results:
            cur.execute(
                """
                INSERT INTO cameras_cameraauditresult
                    (camera_id, ran_at, frames_sampled, faces_detected,
                     median_face_width_px, p90_face_width_px, mean_sharpness,
                     mean_brightness, verdict, notes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    camera_id,
                    dt.datetime.now(dt.UTC),
                    row["frames_sampled"],
                    row["faces_detected"],
                    row["median_face_width_px"],
                    row["p90_face_width_px"],
                    row["mean_sharpness"],
                    row["mean_brightness"],
                    row["verdict"],
                    advice_for(row),
                ),
            )
            if row["median_face_width_px"]:
                cur.execute(
                    "UPDATE cameras_camera SET measured_face_width_px = %s "
                    "WHERE id = %s",
                    (int(row["median_face_width_px"]), camera_id),
                )
        conn.commit()
    print(f"\nSaved {len(results)} audit result(s) to the database.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url", help="Audit a single RTSP URL instead of the camera database."
    )
    parser.add_argument(
        "--seconds",
        type=int,
        default=60,
        help="Sample duration per camera. Use at least 120s during working "
        "hours so real people walk past (default: 60).",
    )
    parser.add_argument("--sample-fps", type=float, default=2.0)
    parser.add_argument(
        "--save",
        action="store_true",
        help="Record results and update each camera's measured face width.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    args = parser.parse_args()

    if args.url:
        targets = [("ad-hoc", args.url, None)]
    else:
        try:
            targets = load_cameras_from_db()
        except Exception as exc:
            print(f"Could not load cameras: {exc}", file=sys.stderr)
            print("Pass --url to audit a single stream instead.", file=sys.stderr)
            return 2

    if not targets:
        print("No enabled cameras found.", file=sys.stderr)
        return 1

    print(
        f"Auditing {len(targets)} camera(s) for {args.seconds}s each "
        f"(~{len(targets) * args.seconds // 60} min total)."
    )
    print("Run this during working hours - it needs real people walking past.")

    rows: list[dict] = []
    to_save: list[tuple[int, dict]] = []

    for code, url, camera_id in targets:
        print(f"\n  {code} ... ", end="", flush=True)
        summary = summarise(audit_stream(url, code, args.seconds, args.sample_fps))
        print(summary["verdict"])
        rows.append(summary)
        if camera_id is not None:
            to_save.append((camera_id, summary))

    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print_table(rows)

    if args.save and to_save:
        save_results(to_save)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
