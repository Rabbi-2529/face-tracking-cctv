"""Build daily summaries for past days, for reports.

The nightly job only builds yesterday. Run this after first installing, after
restoring data, or to fill a gap left while the scheduler was down.
"""

from __future__ import annotations

import datetime as dt

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.tracking.tasks import build_daily_summaries


class Command(BaseCommand):
    help = "Build per-employee daily summaries for the last N complete days."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--days", type=int, default=7, help="How many days back from yesterday."
        )

    def handle(self, *args, **options) -> None:
        yesterday = timezone.localdate() - dt.timedelta(days=1)
        for offset in range(max(1, options["days"])):
            day = yesterday - dt.timedelta(days=offset)
            result = build_daily_summaries(for_date=day.isoformat())
            self.stdout.write(f"  {result['date']}: {result['built']} summar(ies)")
