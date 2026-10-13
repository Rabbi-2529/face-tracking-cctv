"""Production go-live audit. Exits 1 if anything fails."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.tracking.golive import golive_readiness
from apps.tracking.management.commands.pilot_check import print_report


class Command(BaseCommand):
    help = "Check that this installation is safe to use for real: settings, accounts, backups, jobs."

    def handle(self, *args, **options) -> None:
        result = golive_readiness()
        print_report(self, result, ready="ready to go live.", not_ready="NOT ready to go live: fix the failures first.")
        if not result["ready"]:
            raise SystemExit(1)
