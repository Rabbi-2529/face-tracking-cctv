"""Delete the synthetic detections written by scripts/load_test.py."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.tracking.models import Detection

TRACK_PREFIX = "loadtest:"


class Command(BaseCommand):
    help = "Delete detections created by the load test (track ids starting 'loadtest:')."

    def handle(self, *args, **options) -> None:
        deleted, _ = Detection.objects.filter(track_id__startswith=TRACK_PREFIX).delete()
        self.stdout.write(self.style.SUCCESS(f"deleted {deleted} load-test detection(s)"))
