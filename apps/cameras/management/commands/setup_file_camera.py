"""
Set up a video-file camera for end-to-end testing of the vision pipeline.

Enrols a face for one employee, points a camera at a video file, and
optionally disables the other cameras so the test runs clean. Lets the whole
Phase 2 path - detect, align, embed, track, vote, match, publish, aggregate -
be verified with no camera hardware.

    docker compose exec web python manage.py setup_file_camera \\
        --employee EMP001 --face samples/faces/face_a.jpg \\
        --video /samples/walk.mp4 --exclusive

Note the two different paths: ``--face`` is read by this command inside the
web container, while ``--video`` is opened later by a vision worker, which
mounts ./samples at /samples.
"""

from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.cameras.models import Camera
from apps.employees.enrollment import enroll_image, gallery_coverage
from apps.employees.models import Employee
from apps.org.models import Zone


class Command(BaseCommand):
    help = "Create a video-file camera and enrol a face, for pipeline testing."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--employee", default="EMP001")
        parser.add_argument("--face", default="samples/faces/face_a.jpg")
        parser.add_argument(
            "--video",
            default="/samples/walk.mp4",
            help="Path as the VISION container sees it (./samples is /samples).",
        )
        parser.add_argument("--zone", default="reception")
        parser.add_argument("--code", default="cam-test-file")
        parser.add_argument("--sample-fps", type=float, default=4.0)
        parser.add_argument(
            "--exclusive",
            action="store_true",
            help="Disable every other camera, so only this one is ingested.",
        )
        parser.add_argument(
            "--skip-enroll",
            action="store_true",
            help="Leave the gallery alone; only set up the camera.",
        )

    @transaction.atomic
    def handle(self, *args, **options) -> None:
        employee = Employee.objects.alive().filter(
            employee_code=options["employee"]
        ).first()
        if employee is None:
            raise CommandError(
                f"No employee {options['employee']}. Run `manage.py seed_demo` first."
            )

        zone = Zone.objects.alive().filter(code=options["zone"]).first()
        if zone is None:
            raise CommandError(f"No zone with code {options['zone']}.")
        if zone.tracking_prohibited:
            raise CommandError(
                f"Zone {zone.code} has tracking prohibited; cameras cannot be "
                "enabled there."
            )

        # --- enrol a face so there is something to match against ----------
        if not options["skip_enroll"]:
            face_path = Path(options["face"])
            if not face_path.exists():
                raise CommandError(
                    f"{face_path} not found. Fetch the sample faces with "
                    "`make fixtures`."
                )

            result = enroll_image(
                employee,
                face_path.read_bytes(),
                filename=face_path.name,
                enrolled_by=None,
            )
            if result.accepted:
                self.stdout.write(self.style.SUCCESS(f"  enrolled: {result.message}"))
            else:
                self.stdout.write(self.style.WARNING(f"  not enrolled: {result.message}"))
                if result.quality:
                    self.stdout.write(f"  quality: {result.quality}")

            coverage = gallery_coverage(employee)
            self.stdout.write(
                f"  gallery for {employee.employee_code}: "
                f"{coverage['active_count']} active image(s), "
                f"matchable={coverage['is_processable']}"
            )
            if not coverage["is_processable"]:
                self.stdout.write(
                    self.style.WARNING(
                        "  this employee is NOT matchable yet - check consent and "
                        "that at least one enrolment is active"
                    )
                )

        # --- point a camera at the video ----------------------------------
        if options["exclusive"]:
            disabled = (
                Camera.objects.alive()
                .exclude(code=options["code"])
                .filter(is_enabled=True)
                .update(is_enabled=False)
            )
            self.stdout.write(f"  disabled {disabled} other camera(s)")

        camera, created = Camera.objects.update_or_create(
            code=options["code"],
            defaults={
                "name": "Test file camera",
                "zone": zone,
                "vendor": Camera.Vendor.FILE,
                "purpose": Camera.Purpose.IDENTITY,
                # OpenCV opens a plain path exactly as it opens an RTSP URL.
                "rtsp_url": options["video"],
                "sample_fps": options["sample_fps"],
                "is_enabled": True,
                "measured_face_width_px": 73,
                "calibration_notes": (
                    "Synthetic test fixture, not a real camera. A panned studio "
                    "portrait is far kinder than a real doorway - do not read "
                    "accuracy from it."
                ),
            },
        )

        self.stdout.write(
            self.style.SUCCESS(
                f"  camera {camera.code} {'created' if created else 'updated'}: "
                f"{options['video']} -> zone {zone.code} at "
                f"{options['sample_fps']}fps"
            )
        )
        self.stdout.write(
            "\nStart the real (non-mock) vision worker:\n"
            "  MOCK_VISION=0 docker compose --profile vision up -d "
            "--force-recreate vision\n"
            "Then watch it work:\n"
            "  docker compose logs -f vision aggregator"
        )
