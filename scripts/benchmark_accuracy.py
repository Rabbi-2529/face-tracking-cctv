#!/usr/bin/env python
"""
Measure recognition accuracy against a labelled set, before trusting the system.

Layout (one folder per person; folder names are the labels):

    <data>/gallery/<person>/*.jpg     enrolment photos, as HR would upload
    <data>/probes/<person>/*.jpg      frames or crops from YOUR cameras
    <data>/probes/_unknown/*.jpg      people who are not enrolled (optional,
                                      but without them the false-positive
                                      rate is only half measured)

Probes must come from the cameras being piloted: thresholds measured on
phone photos do not transfer to a ceiling camera at 80px.

    docker compose --profile vision run --rm vision \\
        python scripts/benchmark_accuracy.py --data /samples/benchmark

Prints TAR/FAR, recommended thresholds, the open-set result at both the
current and the recommended thresholds, and a pass/fail against the plan's
targets (>= 95% correct, <= 2% confirmed-wrong). Writes JSON with --json.

The images are biometric data. Keep the set on the server, delete it after
the pilot, and only use photos of people who have consented to the test.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def _images(folder: Path):
    return sorted(p for p in folder.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)


def load_set(root: Path, *, enrolment: bool, min_face_px: int):
    """Embed every image under root/<label>/. Returns {label: [embedding]} and skip notes."""
    import cv2

    from vision.pipeline import get_pipeline

    pipeline = get_pipeline()
    embedded: dict[str, list] = {}
    skipped: list[str] = []

    for person_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for path in _images(person_dir):
            image = cv2.imread(str(path))
            if image is None:
                skipped.append(f"{path.name}: unreadable")
                continue
            if enrolment:
                face, error = pipeline.analyse_enrollment(image)
                if error or face is None or face.embedding is None:
                    skipped.append(f"{person_dir.name}/{path.name}: {error or 'no usable face'}")
                    continue
                embedding = face.embedding
            else:
                faces = [
                    f for f in pipeline.analyse_frame(image, min_face_px=min_face_px, max_faces=3)
                    if f.usable
                ]
                if not faces:
                    skipped.append(f"{person_dir.name}/{path.name}: no face passed the live gate")
                    continue
                # The largest face is the subject of a labelled crop.
                embedding = max(faces, key=lambda f: f.face_width_px).embedding
            embedded.setdefault(person_dir.name, []).append(embedding)
    return embedded, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", required=True, type=Path, help="folder with gallery/ and probes/")
    parser.add_argument("--high", type=float, default=0.45, help="current confirmed threshold")
    parser.add_argument("--low", type=float, default=0.30, help="current probable threshold")
    parser.add_argument("--min-face-px", type=int, default=60)
    parser.add_argument("--target-far", type=float, default=0.001,
                        help="impostor acceptance allowed at the recommended confirmed threshold")
    parser.add_argument("--json", type=Path, help="write the full result here")
    args = parser.parse_args()

    from vision.benchmark import Gallery, identify, recommend, score_distributions

    gallery_dir, probe_dir = args.data / "gallery", args.data / "probes"
    if not gallery_dir.is_dir() or not probe_dir.is_dir():
        print(f"expected {gallery_dir} and {probe_dir}", file=sys.stderr)
        return 2

    templates, skipped_gallery = load_set(gallery_dir, enrolment=True, min_face_px=args.min_face_px)
    probe_sets, skipped_probes = load_set(probe_dir, enrolment=False, min_face_px=args.min_face_px)
    probes = [(label, e) for label, embeddings in probe_sets.items() for e in embeddings]

    if not templates or not probes:
        print("Nothing to measure: no usable gallery images or probes.", file=sys.stderr)
        for note in skipped_gallery + skipped_probes:
            print("  skipped", note, file=sys.stderr)
        return 2

    gallery = Gallery.build(templates)
    enrolled = [(label, e) for label, e in probes if label in templates]
    genuine, impostor = score_distributions(gallery, enrolled)
    suggestion = recommend(
        genuine, impostor, target_far=args.target_far, identities=len(templates)
    )
    current = identify(gallery, probes, high=args.high, low=args.low)
    if suggestion["sufficient"]:
        proposed = identify(gallery, probes, high=suggestion["high"], low=suggestion["low"])
    else:
        # Too little data to justify moving: report against the current values.
        proposed = current

    print(f"\nGallery: {len(templates)} people, {len(gallery.labels)} templates "
          f"({len(skipped_gallery)} images rejected by the enrolment gate)")
    print(f"Probes:  {len(probes)} usable ({len(skipped_probes)} rejected by the live gate)")
    if skipped_probes and len(skipped_probes) > len(probes):
        print("  More probes were rejected than kept: the cameras, not the thresholds, "
              "are the problem. See the camera audit.")
    print(f"Scores:  genuine median {float(_median(genuine)):.3f}, "
          f"impostor 99.9th pct {float(_quantile(impostor, 0.999)):.3f}")

    if suggestion["sufficient"]:
        print(f"\nRecommended: confirmed >= {suggestion['high']:.2f}  probable >= {suggestion['low']:.2f}"
              f"   (TAR {suggestion['tar_at_high']:.1%}, FAR {suggestion['far_at_high']:.2%})")
        if suggestion["floored"]:
            print(f"  (the data alone pointed at {suggestion['measured_high']:.2f}; "
                  "raised to the safety floor)")
    else:
        print("\nNo recommendation: not enough data to move the thresholds safely.")
        for reason in suggestion["insufficient_reasons"]:
            print(f"  - {reason}")
        print(f"  Keeping the current thresholds ({args.high:.2f} / {args.low:.2f}). "
              "Collect more people and probes from the pilot cameras.")

    reports = [("current", current)]
    if suggestion["sufficient"]:
        reports.append(("recommended", proposed))
    for title, report in reports:
        r = report
        print(f"\nAt {title} thresholds (confirmed {r.high:.2f}, probable {r.low:.2f}):")
        print(f"  enrolled probes {r.enrolled_probes}: confirmed correct {r.confirmed_correct}, "
              f"confirmed WRONG {r.confirmed_wrong}, probable {r.probable_correct + r.probable_wrong}, "
              f"missed {r.missed}")
        if r.stranger_probes:
            print(f"  strangers {r.stranger_probes}: rejected {r.stranger_rejected}, "
                  f"given a confirmed name {r.stranger_confirmed}")
        print(f"  correct {r.correct_rate:.1%} (target >= 95%), "
              f"confirmed-wrong {r.false_positive_rate:.2%} (target <= 2%)  "
              f"-> {'PASS' if r.passes else 'FAIL'}")

    if args.json:
        args.json.write_text(json.dumps({
            "recommendation": suggestion,
            "current": current.as_dict(),
            "recommended": proposed.as_dict(),
            "skipped": {"gallery": skipped_gallery, "probes": skipped_probes},
        }, indent=2))
        print(f"\nwrote {args.json}")

    # Exit 0 only for a pass that rests on enough data to mean something.
    return 0 if proposed.passes and suggestion["sufficient"] else 1


def _median(values):
    import numpy as np

    return np.median(values) if len(values) else 0.0


def _quantile(values, q):
    import numpy as np

    return np.quantile(values, q) if len(values) else 0.0


if __name__ == "__main__":
    raise SystemExit(main())
