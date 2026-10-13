#!/usr/bin/env python
"""
Copy the page libraries into static/vendor/ so the server needs no internet.

The interface loads jQuery, DataTables, Select2, flatpickr and a QR-code
library from public CDNs by default. A production server on an isolated
network (as a camera system should be) cannot reach them, and some
organisations do not allow third-party script hosts at all. This downloads
the exact versions the templates pin, records a SHA-256 for each file, and
leaves them in static/vendor/ to be committed and served locally.

    python scripts/vendor_assets.py          # download / refresh
    python scripts/vendor_assets.py --check  # verify files against the manifest

Then set ASSETS_LOCAL=1 and run collectstatic (the migrate service does).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "static" / "vendor"
MANIFEST = VENDOR / "MANIFEST.json"

# Must match the versions in templates/base.html and accounts/mfa_setup.html.
ASSETS = {
    "jquery/jquery.min.js": "https://cdnjs.cloudflare.com/ajax/libs/jquery/3.7.1/jquery.min.js",
    "datatables/dataTables.min.js": "https://cdn.datatables.net/2.1.8/js/dataTables.min.js",
    "datatables/dataTables.dataTables.min.css": "https://cdn.datatables.net/2.1.8/css/dataTables.dataTables.min.css",
    "select2/select2.min.js": "https://cdnjs.cloudflare.com/ajax/libs/select2/4.1.0-rc.0/js/select2.min.js",
    "select2/select2.min.css": "https://cdnjs.cloudflare.com/ajax/libs/select2/4.1.0-rc.0/css/select2.min.css",
    "flatpickr/flatpickr.min.js": "https://cdnjs.cloudflare.com/ajax/libs/flatpickr/4.6.13/flatpickr.min.js",
    "flatpickr/flatpickr.min.css": "https://cdnjs.cloudflare.com/ajax/libs/flatpickr/4.6.13/flatpickr.min.css",
    "qrcodejs/qrcode.min.js": "https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js",
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def download() -> int:
    manifest = {}
    for relative, url in ASSETS.items():
        request = urllib.request.Request(url, headers={"User-Agent": "cctv-tracking/vendor-assets"})
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read()
        if len(data) < 500:
            print(f"suspiciously small response for {url}", file=sys.stderr)
            return 1
        target = VENDOR / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        manifest[relative] = {"url": url, "sha256": sha256(data), "bytes": len(data)}
        print(f"  {relative:<45} {len(data):>8} bytes")
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {MANIFEST.relative_to(ROOT)}")
    return 0


def check() -> int:
    if not MANIFEST.exists():
        print("no manifest; run without --check first", file=sys.stderr)
        return 1
    manifest = json.loads(MANIFEST.read_text())
    bad = 0
    for relative, entry in manifest.items():
        path = VENDOR / relative
        if not path.exists() or sha256(path.read_bytes()) != entry["sha256"]:
            print(f"MISMATCH {relative}")
            bad += 1
    print("all files match the manifest" if not bad else f"{bad} file(s) differ")
    return 1 if bad else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="verify against the manifest, download nothing")
    args = parser.parse_args()
    return check() if args.check else download()


if __name__ == "__main__":
    raise SystemExit(main())
