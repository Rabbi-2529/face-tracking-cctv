"""Pilot readiness checklist in the terminal. Exits 1 if anything fails."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.tracking.pilot import FAIL, PASS, WARN, pilot_readiness

MARKS = {PASS: "PASS", WARN: "WARN", FAIL: "FAIL"}


class Command(BaseCommand):
    help = "Check the pilot's 'before day 1' list against the database."

    def handle(self, *args, **options) -> None:
        result = pilot_readiness()
        area = None
        for check in result["checks"]:
            if check.area != area:
                area = check.area
                self.stdout.write(f"\n{area}")
            style = {PASS: self.style.SUCCESS, WARN: self.style.WARNING, FAIL: self.style.ERROR}[check.status]
            self.stdout.write(f"  {style(MARKS[check.status])}  {check.name}: {check.detail}")
            if check.fix:
                self.stdout.write(f"        -> {check.fix}")

        counts = result["counts"]
        self.stdout.write(
            f"\n{counts[PASS]} pass, {counts[WARN]} warn, {counts[FAIL]} fail - "
            + ("ready to start the pilot." if result["ready"] else "not ready: fix the failures first.")
        )
        if not result["ready"]:
            raise SystemExit(1)
