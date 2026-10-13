"""
Aggregate reports and exports.

Built on :class:`tracking.DailySummary`, which the nightly job writes for the
previous day, so reports never scan the raw tables and never include today.

Two rules shape everything here:

* **Purpose limitation.** These are headcount and space-use reports. There is
  no "time at desk", no per-person ranking and no productivity measure, and
  the per-person export is an identity export that only administrators get.
* **Small numbers identify people.** "1 person in Finance on Saturday" names
  that person to anyone who knows the team. For viewers without
  organisation-wide history access, any count below ``SMALL_CELL`` is shown
  as "<3" and its hours are withheld, on screen and in the CSV alike.

Every view and every export is audited, and exports are rate-limited per user.
"""

from __future__ import annotations

import csv
import datetime as dt
from collections import defaultdict

from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse, StreamingHttpResponse
from django.utils import timezone
from django.views.generic import TemplateView, View

from apps.common.audit import record
from apps.common.permissions import PermissionService
from apps.common.roles import Action
from apps.common.scoping import visible_audit, visible_employees
from apps.employees.models import Employee
from apps.org.models import Department, Zone
from apps.tracking.models import DailySummary
from apps.tracking.views import ActionRequiredMixin

SMALL_CELL = 3
SUPPRESSED = f"<{SMALL_CELL}"
MAX_RANGE_DAYS = 92
EXPORTS_PER_HOUR = 10


# ------------------------------------------------------------------ the data


def _range(request) -> tuple[dt.date, dt.date]:
    """Requested date range, defaulting to the last seven complete days."""
    yesterday = timezone.localdate() - dt.timedelta(days=1)

    def parse(name: str, default: dt.date) -> dt.date:
        try:
            return dt.date.fromisoformat(request.GET.get(name, ""))
        except ValueError:
            return default

    end = min(parse("end", yesterday), yesterday)
    start = parse("start", end - dt.timedelta(days=6))
    if start > end:
        start, end = end, start
    if (end - start).days >= MAX_RANGE_DAYS:
        start = end - dt.timedelta(days=MAX_RANGE_DAYS - 1)
    return start, end


def _summaries(user, start: dt.date, end: dt.date, department_id: str = ""):
    employees = visible_employees(user, Employee.objects.all())
    qs = DailySummary.objects.filter(
        date__gte=start, date__lte=end, employee__in=employees
    ).select_related("employee", "employee__department")
    if department_id.isdigit():
        qs = qs.filter(employee__department_id=int(department_id))
    return qs


def build_report(user, start: dt.date, end: dt.date, department_id: str = "") -> dict:
    """Headcount by day and department, and space use by zone."""
    suppress = Action.HISTORY_ANY not in PermissionService.granted_actions(user)
    summaries = list(_summaries(user, start, end, department_id))

    # Headcount: one summary row is one person present on one day.
    days = [start + dt.timedelta(days=n) for n in range((end - start).days + 1)]
    departments: dict[str, str] = {}
    headcount: dict[tuple[dt.date, str], int] = defaultdict(int)
    totals: dict[dt.date, int] = defaultdict(int)
    for row in summaries:
        dept = row.employee.department
        key = dept.code if dept else "-"
        departments[key] = dept.name if dept else "No department"
        headcount[(row.date, key)] += 1
        totals[row.date] += 1

    dept_keys = sorted(departments, key=lambda k: departments[k])

    def cell(value: int):
        if suppress and 0 < value < SMALL_CELL:
            return SUPPRESSED
        return value

    headcount_rows = [
        {
            "date": day,
            "cells": [cell(headcount.get((day, k), 0)) for k in dept_keys],
            "total": cell(totals.get(day, 0)),
        }
        for day in days
    ]

    # Space use: person-hours and distinct people per zone over the range.
    seconds: dict[str, int] = defaultdict(int)
    people: dict[str, set[int]] = defaultdict(set)
    for row in summaries:
        for code, secs in (row.dwell_by_zone or {}).items():
            seconds[code] += int(secs or 0)
            people[code].add(row.employee_id)

    zones = {
        z.code: z
        for z in Zone.objects.filter(code__in=seconds.keys(), tracking_prohibited=False)
    }
    zone_rows = []
    for code, zone in zones.items():
        distinct = len(people[code])
        hidden = suppress and distinct < SMALL_CELL
        zone_rows.append(
            {
                "zone": zone,
                "people": SUPPRESSED if hidden else distinct,
                "hours": None if hidden else round(seconds[code] / 3600, 1),
                "sort": seconds[code],
            }
        )
    zone_rows.sort(key=lambda r: r["sort"], reverse=True)

    low_confidence = (
        round(sum(r.low_confidence_ratio for r in summaries) / len(summaries), 3)
        if summaries
        else None
    )

    return {
        "start": start,
        "end": end,
        "suppressed": suppress,
        "dept_keys": dept_keys,
        "dept_names": [departments[k] for k in dept_keys],
        "headcount_rows": headcount_rows,
        "zone_rows": zone_rows,
        "person_days": len(summaries),
        "days_with_data": len({r.date for r in summaries}),
        "day_count": len(days),
        "low_confidence_ratio": low_confidence,
        "restricted_entries": sum(r.restricted_zone_entries for r in summaries),
    }


# ----------------------------------------------------------------- the pages


class ReportView(ActionRequiredMixin, TemplateView):
    template_name = "tracking/reports.html"
    required_action = Action.REPORT_AGGREGATE_VIEW

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        start, end = _range(self.request)
        department_id = self.request.GET.get("department", "")

        ctx["report"] = build_report(user, start, end, department_id)
        ctx["department_id"] = department_id
        ctx["departments"] = _visible_departments(user)
        ctx["can_export_aggregate"] = bool(PermissionService.can_export(user, "aggregate"))
        ctx["can_export_identity"] = bool(PermissionService.can_export(user, "identity"))
        ctx["query"] = self.request.GET.urlencode()

        record(
            "report.view",
            reason=f"{start.isoformat()} .. {end.isoformat()}",
            metadata={"department": department_id or None},
        )
        return ctx


def _visible_departments(user):
    ids = (
        visible_employees(user, Employee.objects.all())
        .exclude(department__isnull=True)
        .values_list("department_id", flat=True)
        .distinct()
    )
    return Department.objects.filter(pk__in=ids).order_by("name")


# ------------------------------------------------------------------- exports


def _safe(value):
    """Neutralise spreadsheet formula injection in text cells.

    A name or reason beginning with "=" is executed by Excel on open. Numbers
    are left alone so they stay numeric.
    """
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


class _Echo:
    def write(self, value):
        return value


def _csv_response(filename: str, header: list[str], rows) -> StreamingHttpResponse:
    writer = csv.writer(_Echo())

    def stream():
        yield writer.writerow(header)
        for row in rows:
            yield writer.writerow([_safe(v) for v in row])

    response = StreamingHttpResponse(stream(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["Cache-Control"] = "no-store, private"
    return response


def _check_export(request, kind: str) -> None:
    """Permission and rate limit for one export, both audited on refusal."""
    decision = PermissionService.can_export(request.user, kind)
    if not decision:
        record(f"export.denied:{kind}", reason=decision.reason, success=False)
        raise PermissionDenied(decision.reason)

    key = f"export-rate:{request.user.pk}"
    cache.add(key, 0, timeout=3600)
    try:
        used = cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=3600)
        used = 1
    if used > EXPORTS_PER_HOUR:
        record(
            f"export.denied:{kind}",
            reason=f"rate limit of {EXPORTS_PER_HOUR} exports per hour",
            success=False,
        )
        raise _RateLimited()


class _RateLimited(Exception):
    pass


class ReportExportView(ActionRequiredMixin, View):
    """CSV of a report. ``kind`` is headcount, zones or people."""

    required_action = Action.REPORT_EXPORT

    def get(self, request, kind):
        if kind not in ("headcount", "zones", "people"):
            raise PermissionDenied("Unknown export.")
        export_kind = "identity" if kind == "people" else "aggregate"
        try:
            _check_export(request, export_kind)
        except _RateLimited:
            return HttpResponse("Export limit reached. Try again later.", status=429)

        start, end = _range(request)
        department_id = request.GET.get("department", "")
        filename = f"{kind}-{start.isoformat()}-{end.isoformat()}.csv"

        record(
            "report.export",
            reason=f"{kind} {start.isoformat()} .. {end.isoformat()}",
            metadata={"kind": export_kind, "report": kind, "department": department_id or None},
        )

        if kind == "people":
            summaries = _summaries(request.user, start, end, department_id).order_by(
                "date", "employee__employee_code"
            )
            header = [
                "date", "employee_code", "full_name", "department", "first_seen",
                "last_seen", "present_hours", "zones_visited", "movements",
                "restricted_zone_entries", "low_confidence_ratio",
            ]
            rows = (
                [
                    s.date.isoformat(),
                    s.employee.employee_code,
                    s.employee.full_name,
                    s.employee.department.name if s.employee.department else "",
                    timezone.localtime(s.first_seen_at).isoformat() if s.first_seen_at else "",
                    timezone.localtime(s.last_seen_at).isoformat() if s.last_seen_at else "",
                    round(s.present_seconds / 3600, 2),
                    s.total_zones_visited,
                    s.total_movements,
                    s.restricted_zone_entries,
                    s.low_confidence_ratio,
                ]
                for s in summaries.iterator()
            )
            return _csv_response(filename, header, rows)

        report = build_report(request.user, start, end, department_id)
        if kind == "headcount":
            header = ["date", *report["dept_names"], "total"]
            rows = (
                [r["date"].isoformat(), *r["cells"], r["total"]]
                for r in report["headcount_rows"]
            )
        else:
            header = ["zone_code", "zone", "distinct_people", "person_hours"]
            rows = (
                [r["zone"].code, r["zone"].name, r["people"], "" if r["hours"] is None else r["hours"]]
                for r in report["zone_rows"]
            )
        return _csv_response(filename, header, rows)


class AuditExportView(ActionRequiredMixin, View):
    """The audit log as CSV - the Auditor's export, with the page's filters."""

    required_action = Action.AUDIT_READ_ALL

    def get(self, request):
        try:
            _check_export(request, "audit")
        except _RateLimited:
            return HttpResponse("Export limit reached. Try again later.", status=429)

        from apps.accounts.models import AuditLog

        qs = visible_audit(request.user, AuditLog.objects.select_related("actor"))
        action = request.GET.get("action", "").strip()
        if action:
            qs = qs.filter(action__startswith=action)
        if request.GET.get("failures_only"):
            qs = qs.filter(success=False)
        qs = qs.order_by("-at")

        record(
            "audit.export",
            reason=f"filter action={action or '*'}",
            metadata={"failures_only": bool(request.GET.get("failures_only"))},
        )
        header = [
            "at", "actor", "action", "subject_code", "object_type", "object_id",
            "reason", "success", "ip_address", "request_path",
        ]
        rows = (
            [
                timezone.localtime(e.at).isoformat(),
                e.actor.email if e.actor else "system",
                e.action,
                e.target_employee_code,
                e.object_type,
                e.object_id,
                e.reason,
                "yes" if e.success else "no",
                e.ip_address or "",
                e.request_path,
            ]
            for e in qs.iterator(chunk_size=1000)
        )
        stamp = timezone.localtime().strftime("%Y%m%d-%H%M")
        return _csv_response(f"audit-{stamp}.csv", header, rows)
