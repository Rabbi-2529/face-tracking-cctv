"""Pilot readiness checklist in the terminal. Exits 1 if anything fails."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.tracking.pilot import FAIL, PASS, WARN, pilot_readiness

MARKS = {PASS: "PASS", WARN: "WARN", FAIL: "FAIL"}


def print_report(command, result: dict, *, ready: str, not_ready: str) -> None:
    """Shared terminal layout for the readiness and go-live checks."""
    styles = {PASS: command.style.SUCCESS, WARN: command.style.WARNING, FAIL: command.style.ERROR}
    area = None
    for check in result["checks"]:
        if check.area != area:
            area = check.area
            command.stdout.write(f"\n{area}")
        command.stdout.write(f"  {styles[check.status](MARKS[check.status])}  {check.name}: {check.detail}")
        if check.fix:
            command.stdout.write(f"        -> {check.fix}")
    counts = result["counts"]
    command.stdout.write(
        f"\n{counts[PASS]} pass, {counts[WARN]} warn, {counts[FAIL]} fail - "
        + (ready if result["ready"] else not_ready)
    )


class Command(BaseCommand):
    help = "Check the pilot's 'before day 1' list against the database."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--floor",
            help="Limit site and camera checks to one floor (name or id), for floor-by-floor rollout.",
        )

    def handle(self, *args, **options) -> None:
        floor = None
        if options.get("floor"):
            from apps.org.models import Floor

            value = options["floor"]
            floor = (
                Floor.objects.filter(pk=int(value)).first() if value.isdigit()
                else Floor.objects.filter(name__iexact=value).first()
            )
            if floor is None:
                self.stderr.write(self.style.ERROR(f"No floor '{value}'."))
                raise SystemExit(2)
            self.stdout.write(f"Readiness for {floor}")

        result = pilot_readiness(floor=floor)
        print_report(self, result, ready="ready to start.", not_ready="not ready: fix the failures first.")
        if not result["ready"]:
            raise SystemExit(1)
