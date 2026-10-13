#!/usr/bin/env python
"""
Fetch the ONNX models the vision pipeline needs.

Pulls the InsightFace ``buffalo_l`` pack and keeps only the two models this
project uses:

    det_10g.onnx     SCRFD-10GF face detector (boxes + 5 landmarks)
    w600k_r50.onnx   ArcFace R50 recogniser (512-d embeddings)

The other models in the pack (3D landmarks, gender/age, 106-point landmarks)
are deliberately discarded. This system identifies people against an enrolled
gallery; inferring attributes like age or gender from faces is outside the
stated purpose and would need its own lawful basis, so the models are not
even kept on disk.

Usage:
    docker compose --profile vision run --rm vision python scripts/download_models.py
    docker compose --profile vision run --rm vision python scripts/download_models.py --inspect
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

# Keep every script in this directory importable the same way.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PACK_URL = (
    "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip"
)

# Only these are extracted. Anything else in the archive is ignored.
WANTED = {
    "det_10g.onnx": "SCRFD-10GF face detector",
    "w600k_r50.onnx": "ArcFace R50 recogniser (512-d)",
}

MODEL_DIR = Path(os.environ.get("MODEL_DIR", "/models"))


def _progress(done: int, total: int) -> None:
    if total <= 0:
        return
    pct = done * 100 // total
    bar = "#" * (pct // 3)
    sys.stderr.write(f"\r  [{bar:<33}] {pct:3d}%  {done / 1e6:6.1f}/{total / 1e6:.1f} MB")
    sys.stderr.flush()


def download(url: str, destination: Path) -> Path:
    print(f"  downloading {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "cctv-tracking/0.1"})
    with urllib.request.urlopen(request, timeout=120) as response:
        total = int(response.headers.get("Content-Length", 0))
        done = 0
        digest = hashlib.sha256()
        with open(destination, "wb") as handle:
            while chunk := response.read(1 << 20):
                handle.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                _progress(done, total)
    sys.stderr.write("\n")
    print(f"  sha256 {digest.hexdigest()}")
    return destination


def extract(archive: Path, model_dir: Path) -> list[Path]:
    written: list[Path] = []
    with zipfile.ZipFile(archive) as zf:
        members = {Path(n).name: n for n in zf.namelist() if not n.endswith("/")}
        missing = set(WANTED) - set(members)
        if missing:
            raise SystemExit(
                f"archive does not contain {', '.join(sorted(missing))}; "
                f"it holds: {', '.join(sorted(members))}"
            )
        for name, description in WANTED.items():
            target = model_dir / name
            # Extract by reading the member explicitly rather than using
            # extractall, so a crafted archive cannot write outside MODEL_DIR.
            with zf.open(members[name]) as source, open(target, "wb") as handle:
                shutil.copyfileobj(source, handle)
            size_mb = target.stat().st_size / 1e6
            print(f"  wrote {name:<18} {size_mb:6.1f} MB  {description}")
            written.append(target)
    return written


def inspect(model_dir: Path) -> int:
    """Print each model's input/output signature.

    Worth running once after download: the wrappers in vision/detect and
    vision/embed assume these exact shapes, and a different pack revision
    would show up here rather than as silent nonsense at runtime.
    """
    try:
        import onnxruntime as ort
    except ImportError:
        print("onnxruntime is not installed; cannot inspect", file=sys.stderr)
        return 1

    for name in WANTED:
        path = model_dir / name
        if not path.exists():
            print(f"{name}: MISSING")
            continue
        session = ort.InferenceSession(
            str(path), providers=["CPUExecutionProvider"]
        )
        print(f"\n{name}")
        for inp in session.get_inputs():
            print(f"  in   {inp.name:<16} {inp.shape} {inp.type}")
        for out in session.get_outputs():
            print(f"  out  {out.name:<16} {out.shape} {out.type}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force", action="store_true", help="Re-download even if models exist."
    )
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="Print model input/output signatures and exit.",
    )
    args = parser.parse_args()

    MODEL_DIR.mkdir(parents=True, exist_ok=True)

    if args.inspect:
        return inspect(MODEL_DIR)

    present = [n for n in WANTED if (MODEL_DIR / n).exists()]
    if len(present) == len(WANTED) and not args.force:
        print(f"models already present in {MODEL_DIR}: {', '.join(present)}")
        print("pass --force to re-download")
        return 0

    print(f"fetching models into {MODEL_DIR}")
    with tempfile.TemporaryDirectory() as tmp:
        archive = download(PACK_URL, Path(tmp) / "buffalo_l.zip")
        extract(archive, MODEL_DIR)

    print("\ndone. Verify the signatures with:")
    print("  python scripts/download_models.py --inspect")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
