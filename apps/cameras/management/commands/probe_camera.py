"""
Find the working RTSP path for a camera you have the IP and login for.

Vendors use different stream paths, firmware revisions change them, and the
error you get for a wrong path looks identical to the error for wrong
credentials. This tries the known paths in turn and tells you which one
opened, so you can stop guessing.

Only probes a host you name, with credentials you supply - it is for
commissioning your own cameras, not for discovering devices.

    docker compose exec web python manage.py probe_camera \\
        --host 192.168.1.64 --username cctv_ai --password 'secret'

    # try a specific vendor only, and report face sizes too
    docker compose exec web python manage.py probe_camera \\
        --host 192.168.1.64 --username cctv_ai --password 'secret' \\
        --vendor dahua --measure
"""

from __future__ import annotations

import os
import statistics
import time
from urllib.parse import quote

from django.core.management.base import BaseCommand

# Force TCP and a socket timeout: over UDP a wrong path or a firewalled host
# blocks for minutes instead of failing.
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|stimeout;4000000"
)

# (vendor, label, path template). Ordered roughly by how common they are.
CANDIDATES = [
    ("dahua", "Dahua / CP Plus main stream", "/cam/realmonitor?channel={ch}&subtype=0"),
    ("dahua", "Dahua / CP Plus sub stream", "/cam/realmonitor?channel={ch}&subtype=1"),
    ("hikvision", "Hikvision main stream", "/Streaming/Channels/{ch}01"),
    ("hikvision", "Hikvision sub stream", "/Streaming/Channels/{ch}02"),
    ("hikvision", "Hikvision older firmware", "/h264/ch{ch}/main/av_stream"),
    ("axis", "Axis", "/axis-media/media.amp"),
    ("onvif", "ONVIF generic profile 1", "/onvif1"),
    ("onvif", "ONVIF generic stream 1", "/stream1"),
    ("onvif", "Generic live stream", "/live/ch0{ch}"),
    ("uniview", "Uniview", "/media/video{ch}"),
]


class Command(BaseCommand):
    help = "Try the known RTSP paths for a camera and report which one works."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--host", required=True, help="Camera or NVR IP address.")
        parser.add_argument("--port", type=int, default=554)
        parser.add_argument("--username", default="")
        parser.add_argument("--password", default="")
        parser.add_argument(
            "--channel",
            type=int,
            default=1,
            help="1 for a camera addressed directly; the channel number via an NVR.",
        )
        parser.add_argument(
            "--vendor",
            default="",
            help="Limit to one vendor: dahua, hikvision, axis, onvif, uniview.",
        )
        parser.add_argument(
            "--measure",
            action="store_true",
            help="On a working path, sample frames and measure face sizes.",
        )
        parser.add_argument("--timeout", type=float, default=6.0)

    def handle(self, *args, **options) -> None:
        import cv2

        host = options["host"]
        port = options["port"]
        channel = options["channel"]
        username = options["username"]
        password = options["password"]
        vendor = options["vendor"].strip().lower()

        credentials = ""
        if username:
            # Percent-encode: camera passwords routinely contain @ : / #,
            # any of which silently breaks the URL otherwise.
            credentials = f"{quote(username, safe='')}:{quote(password, safe='')}@"

        candidates = [c for c in CANDIDATES if not vendor or c[0] == vendor]
        if not candidates:
            self.stderr.write(self.style.ERROR(f"Unknown vendor '{vendor}'."))
            return

        self.stdout.write(
            f"Probing {host}:{port} channel {channel} "
            f"({len(candidates)} path(s), {options['timeout']:.0f}s each)\n"
        )
        if not username:
            self.stdout.write(
                self.style.WARNING(
                    "  No credentials given. Most cameras will refuse the stream.\n"
                )
            )

        working: list[tuple[str, str, str]] = []

        for vendor_key, label, template in candidates:
            path = template.format(ch=channel)
            url = f"rtsp://{credentials}{host}:{port}{path}"
            display = f"rtsp://{host}:{port}{path}"

            started = time.monotonic()
            capture = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)

            opened = capture.isOpened()
            ok, frame = capture.read() if opened else (False, None)
            elapsed = time.monotonic() - started

            if ok and frame is not None:
                resolution = f"{frame.shape[1]}x{frame.shape[0]}"
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  WORKS  {label:<32} {resolution:<11} {elapsed:4.1f}s"
                    )
                )
                self.stdout.write(f"         {display}")
                working.append((vendor_key, label, display))

                if options["measure"]:
                    self._measure(capture, frame)
            else:
                reason = "no frame" if opened else "refused"
                self.stdout.write(f"  --     {label:<32} {reason:<11} {elapsed:4.1f}s")

            capture.release()

        self._summarise(working, host, channel, bool(username))

    def _measure(self, capture, first_frame) -> None:
        """Sample frames on a working path and report face sizes."""
        try:
            from vision.detect.scrfd import SCRFDDetector

            detector = SCRFDDetector()
        except FileNotFoundError:
            self.stdout.write(
                "         (face measurement needs the models: "
                "python scripts/download_models.py)"
            )
            return

        widths: list[int] = []
        frames = 0
        deadline = time.monotonic() + 8

        frame = first_frame
        while time.monotonic() < deadline and frames < 24:
            frames += 1
            for face in detector.detect(frame, max_faces=3):
                widths.append(face.width)
            ok, frame = capture.read()
            if not ok or frame is None:
                break

        if not widths:
            self.stdout.write(
                "         no face seen while sampling - stand in front of it "
                "and probe again"
            )
            return

        median = statistics.median(widths)
        if median >= 100:
            verdict = self.style.SUCCESS("good for identity")
        elif median >= 60:
            verdict = self.style.WARNING("marginal for identity")
        else:
            verdict = self.style.ERROR("presence only, cannot name anyone")
        self.stdout.write(
            f"         {len(widths)} face(s) over {frames} frames, "
            f"median {median:.0f}px - {verdict}"
        )

    def _summarise(self, working, host: str, channel: int, had_credentials: bool) -> None:
        self.stdout.write("")
        if not working:
            self.stdout.write(self.style.ERROR("No path worked. Check, in order:"))
            if not had_credentials:
                self.stdout.write("  1. Supply --username and --password")
            self.stdout.write(
                f"  - can this host reach the camera at all?  "
                f"try: docker compose exec web python -c "
                f"\"import socket;print(socket.create_connection(('{host}',554),3))\""
            )
            self.stdout.write("  - is RTSP enabled on the camera, on port 554?")
            self.stdout.write("  - are the credentials right, and not locked out?")
            self.stdout.write(
                "  - via an NVR, is --channel correct and is the NVR's "
                "concurrent-stream limit already reached?"
            )
            self.stdout.write(
                "  - set the camera to H.264 rather than H.265 if it offers both"
            )
            return

        self.stdout.write(
            self.style.SUCCESS(f"{len(working)} working path(s) found.")
        )
        main = next(
            (w for w in working if "main" in w[1].lower()), working[0]
        )
        self.stdout.write(
            f"\nUse the MAIN stream for an identity camera: {main[1]}"
        )
        self.stdout.write(
            "Add it in the panel at /cameras/new/ - choose "
            f"'{main[0]}' as the vendor and let the form build the URL, "
            f"with host {host} and channel {channel}."
        )
