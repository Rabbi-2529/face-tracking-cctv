"""
Role-aware views.

Every view here does two things, and both are required:

1. asks :class:`PermissionService` whether the action is allowed at all, and
2. filters its queryset through :mod:`apps.common.scoping`.

A role check without a queryset filter leaks rows through list pages and
search. A queryset filter without a role check lets someone reach a page they
should not see at all.

Identity-bearing reads also write an audit row. A denied attempt is audited
too - a run of denials is exactly the signal worth having.
"""

from __future__ import annotations

import datetime as dt

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.generic import ListView, TemplateView

from apps.common.audit import record
from apps.common.permissions import PermissionService
from apps.common.roles import Action
from apps.common.scoping import (
    visible_audit,
    visible_cameras,
    visible_employees,
    visible_presence,
    visible_zones,
)
from apps.employees.models import Employee
from apps.org.models import Zone
from apps.tracking.models import MovementEvent, PresenceInterval


class ActionRequiredMixin(LoginRequiredMixin):
    """Gate a view on a single action from the matrix."""

    required_action: str | None = None

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return super().dispatch(request, *args, **kwargs)

        if self.required_action:
            decision = PermissionService.can(request.user, self.required_action)
            if not decision:
                record(
                    f"view.denied:{self.required_action}",
                    reason=decision.reason,
                    success=False,
                )
                raise PermissionDenied(decision.reason)
        return super().dispatch(request, *args, **kwargs)


class DashboardView(LoginRequiredMixin, TemplateView):
    """Landing page. Shows each role only the panels it can act on."""

    template_name = "dashboard/index.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        granted = PermissionService.granted_actions(user)
        now = timezone.now()

        ctx["now"] = now

        if Action.LIVE_VIEW_ALL in granted or Action.LIVE_VIEW_ASSIGNED in granted:
            zones = visible_zones(user, Zone.objects.alive())
            ctx["zone_count"] = zones.count()
            ctx["people_present"] = (
                visible_presence(
                    user, PresenceInterval.objects.filter(exited_at__isnull=True)
                )
                .values("employee_id")
                .distinct()
                .count()
            )

        if Action.CAMERA_VIEW in granted or Action.CAMERA_MANAGE in granted:
            from apps.cameras.models import Camera

            cameras = visible_cameras(user, Camera.objects.alive())
            ctx["camera_total"] = cameras.count()
            ctx["camera_offline"] = cameras.filter(
                status=Camera.Status.OFFLINE
            ).count()
            ctx["camera_not_identity_capable"] = cameras.filter(
                purpose=Camera.Purpose.IDENTITY
            ).filter(
                Q(measured_face_width_px__lt=60) | Q(measured_face_width_px__isnull=True)
            ).count()

        if Action.EMPLOYEE_VIEW in granted or Action.EMPLOYEE_MANAGE in granted:
            employees = visible_employees(user, Employee.objects.alive())
            ctx["employee_count"] = employees.count()
            # Enrolled but unable to be matched: the number worth watching,
            # because it silently limits coverage.
            ctx["employees_not_processable"] = sum(
                1 for e in employees.prefetch_related("enrollments", "consent_records")
                if not e.is_processable()
            )

        if Action.TRACKREQUEST_APPROVE in granted:
            from apps.watchlist.models import TrackingRequest

            ctx["pending_requests"] = TrackingRequest.objects.filter(
                status=TrackingRequest.Status.PENDING
            ).count()

        if Action.TRACKREQUEST_CREATE in granted:
            from apps.watchlist.models import TrackingRequest

            ctx["my_active_requests"] = TrackingRequest.objects.filter(
                requested_by=user,
                status=TrackingRequest.Status.APPROVED,
                end_at__gt=now,
            ).count()

        if Action.ALERT_ACK in granted:
            from apps.alerts.models import Alert
            from apps.alerts.views import visible_alerts

            ctx["open_alerts"] = visible_alerts(
                user, Alert.objects.filter(status=Alert.Status.OPEN)
            ).count()

        if Action.USER_MANAGE in granted:
            from apps.accounts.models import AccessElevation

            ctx["pending_elevations"] = AccessElevation.objects.filter(
                status=AccessElevation.Status.PENDING
            ).count()

        if Action.HISTORY_ANY in granted or Action.HISTORY_DEPARTMENT in granted:
            ctx["implausible_pending"] = MovementEvent.objects.filter(
                is_plausible=False,
                review_status=MovementEvent.ReviewStatus.PENDING,
            ).count()

        return ctx


class LiveBoardView(ActionRequiredMixin, TemplateView):
    """Live occupancy. Real updates arrive over the /ws/live/ socket."""

    template_name = "tracking/live_board.html"

    def dispatch(self, request, *args, **kwargs):
        # Either flavour of live view is acceptable here; the socket and the
        # queryset both scope down to what the user may actually see.
        if request.user.is_authenticated:
            granted = PermissionService.granted_actions(request.user)
            if not ({Action.LIVE_VIEW_ALL, Action.LIVE_VIEW_ASSIGNED} & granted):
                record("view.denied:live", success=False)
                raise PermissionDenied("You do not have access to the live board.")
        return super(ActionRequiredMixin, self).dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        zones = (
            visible_zones(self.request.user, Zone.objects.alive())
            .select_related("floor", "floor__building")
            .annotate(
                occupancy=Count(
                    "presence_intervals",
                    filter=Q(presence_intervals__exited_at__isnull=True),
                    distinct=True,
                )
            )
            .order_by("floor__building__name", "floor__level", "name")
        )
        zones = list(zones)
        ctx["zones"] = zones
        ctx["floors"] = _floors_with_zones(zones)
        ctx["total_present"] = sum(z.occupancy for z in zones)
        record("live.view", metadata={"zones": len(zones)})
        return ctx


class ZoneSearchView(ActionRequiredMixin, TemplateView):
    """Zone-first search: "who was in this zone during this window".

    Preferred over person-first search for most investigations. It answers
    the operational question without opening anybody's whole day, and needs
    no tracking request.
    """

    template_name = "tracking/zone_search.html"
    required_action = Action.HISTORY_OWN  # refined below

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        ctx["zones"] = visible_zones(user, Zone.objects.alive()).order_by("name")

        zone_code = self.request.GET.get("zone")
        start_raw = self.request.GET.get("start")
        end_raw = self.request.GET.get("end")

        if not (zone_code and start_raw and end_raw):
            return ctx

        zone = visible_zones(user, Zone.objects.alive()).filter(code=zone_code).first()
        if zone is None:
            raise Http404("Unknown or inaccessible zone")

        start = _parse_local(start_raw)
        end = _parse_local(end_raw)
        if start is None or end is None or end <= start:
            messages.error(self.request, "Give a valid start and end time.")
            return ctx

        # Overlap, not containment: someone who was already in the room when
        # the window opened still counts.
        intervals = (
            visible_presence(
                user,
                PresenceInterval.objects.filter(
                    zone=zone,
                    entered_at__lt=end,
                ).filter(Q(exited_at__gt=start) | Q(exited_at__isnull=True)),
            )
            .select_related("employee", "zone")
            .order_by("entered_at")
        )

        ctx.update(
            {
                "selected_zone": zone,
                "start": start,
                "end": end,
                "results": intervals,
                "result_count": intervals.count(),
            }
        )
        record(
            "zone.search",
            object_type="org.Zone",
            object_id=zone.pk,
            reason=f"{start.isoformat()} .. {end.isoformat()}",
            metadata={"zone": zone.code, "results": intervals.count()},
        )
        return ctx


class EmployeeTimelineView(LoginRequiredMixin, TemplateView):
    """One person's day: zones, transitions, dwell times, confidence.

    This is the view the whole access design is built around, so it checks
    :meth:`PermissionService.can_view_history` specifically rather than a
    blanket action, and offers the tracking-request path when the answer is
    "not without approval".
    """

    template_name = "tracking/employee_timeline.html"

    def get(self, request, *args, **kwargs):
        employee = get_object_or_404(
            Employee.objects.alive().select_related("department"),
            uuid=kwargs["employee_uuid"],
        )

        decision = PermissionService.can_view_history(request.user, employee)
        if not decision:
            record(
                "history.denied",
                target_employee=employee,
                reason=decision.reason,
                success=False,
            )
            if decision.requires_approval:
                messages.warning(
                    request,
                    f"{decision.reason}. Raise a tracking request to proceed.",
                )
                return redirect(
                    reverse("watchlist:create") + f"?employee={employee.uuid}"
                )
            raise PermissionDenied(decision.reason)

        # None: role-based access, the whole retained history. A list: access
        # through tracking requests, limited to their approved data windows.
        windows = PermissionService.history_windows(request.user, employee)

        day = _parse_date(request.GET.get("date"))
        if day is None:
            day = timezone.localdate()
            if windows and not any(_overlaps_day(w, day) for w in windows):
                # Open on the most recent approved day, not on an empty today.
                day = timezone.localtime(max(w[1] for w in windows) - dt.timedelta(seconds=1)).date()
        tz = timezone.get_current_timezone()
        day_start = timezone.make_aware(dt.datetime.combine(day, dt.time.min), tz)
        day_end = day_start + dt.timedelta(days=1)

        interval_qs = PresenceInterval.objects.filter(
            employee=employee,
            entered_at__gte=day_start,
            entered_at__lt=day_end,
            zone__tracking_prohibited=False,
        )
        movement_qs = MovementEvent.objects.filter(
            employee=employee, occurred_at__gte=day_start, occurred_at__lt=day_end
        )
        if windows is not None:
            interval_qs = interval_qs.filter(_interval_window_q(windows))
            movement_qs = movement_qs.filter(_instant_window_q(windows, "occurred_at"))

        intervals = list(
            interval_qs.select_related("zone", "zone__floor", "zone__floor__building")
            .prefetch_related("corrections")
            .order_by("entered_at")
        )
        for interval in intervals:
            # A reported mis-identification stays visible on the entry it is
            # about: "disputed" while open, "not this person" once accepted.
            states = {c.status for c in interval.corrections.all()}
            interval.correction_state = (
                "corrected" if "accepted" in states else "disputed" if "open" in states else ""
            )
        movements = movement_qs.select_related("from_zone", "to_zone").order_by("occurred_at")

        record(
            "history.view",
            target_employee=employee,
            object_type="employees.Employee",
            object_id=employee.pk,
            reason=f"timeline for {day.isoformat()}",
            metadata={
                "intervals": len(intervals),
                "via_tracking_request": windows is not None,
            },
        )

        return render(
            request,
            self.template_name,
            {
                "employee": employee,
                "day": day,
                "prev_day": day - dt.timedelta(days=1),
                "next_day": day + dt.timedelta(days=1),
                "intervals": intervals,
                "movements": movements,
                "total_seconds": sum(i.duration_seconds or 0 for i in intervals),
                # Surfaced deliberately: a timeline built largely from
                # "probable" matches should not read as established fact.
                "low_confidence_count": sum(
                    1 for i in intervals if i.confidence != "confirmed"
                ),
                "implausible_count": sum(1 for m in movements if not m.is_plausible),
                "can_follow_live": bool(
                    PermissionService.can_follow_live(request.user, employee)
                ),
                # Thumbnails are fetched one by one on request, each through
                # the audited endpoint; this only decides whether to offer them.
                "can_view_snapshots": bool(
                    PermissionService.can_view_snapshot(request.user)
                ),
                "movement_map": _movement_map(intervals),
                "access_windows": windows,
                "day_outside_windows": windows is not None
                and not any(_overlaps_day(w, day) for w in windows),
            },
        )


def _overlaps_day(window, day) -> bool:
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(dt.datetime.combine(day, dt.time.min), tz)
    return window[0] < start + dt.timedelta(days=1) and window[1] > start


def _interval_window_q(windows) -> Q:
    """Intervals overlapping any of the approved data windows."""
    condition = Q(pk__in=[])
    for start, end in windows:
        condition |= Q(entered_at__lt=end) & (
            Q(exited_at__gte=start) | Q(exited_at__isnull=True)
        )
    return condition


def _instant_window_q(windows, field: str) -> Q:
    """Rows whose ``field`` timestamp falls inside any approved data window."""
    condition = Q(pk__in=[])
    for start, end in windows:
        condition |= Q(**{f"{field}__gte": start, f"{field}__lt": end})
    return condition


class EmployeeListView(ActionRequiredMixin, ListView):
    template_name = "tracking/employee_list.html"
    context_object_name = "employees"
    paginate_by = 50
    required_action = Action.EMPLOYEE_VIEW

    def get_queryset(self):
        qs = (
            visible_employees(self.request.user, Employee.objects.alive())
            .select_related("department", "designation")
            .prefetch_related("enrollments", "consent_records")
            .order_by("full_name")
        )
        query = self.request.GET.get("q", "").strip()
        if query:
            qs = qs.filter(
                Q(full_name__icontains=query) | Q(employee_code__icontains=query)
            )
            record("employee.search", reason=query, metadata={"q": query})
        return qs


class AuditLogView(ActionRequiredMixin, ListView):
    """Audit viewer. Auditors see everything; everyone else sees their own."""

    template_name = "tracking/audit_log.html"
    context_object_name = "entries"
    paginate_by = 100
    required_action = Action.AUDIT_READ_OWN

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            decision = PermissionService.can_read_audit(request.user)
            if not decision:
                raise PermissionDenied(decision.reason)
        return super(ActionRequiredMixin, self).dispatch(request, *args, **kwargs)

    def get_queryset(self):
        from apps.accounts.models import AuditLog

        qs = visible_audit(
            self.request.user, AuditLog.objects.select_related("actor", "target_employee")
        )
        action = self.request.GET.get("action", "").strip()
        if action:
            qs = qs.filter(action__startswith=action)
        if self.request.GET.get("failures_only"):
            qs = qs.filter(success=False)
        return qs.order_by("-at")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["scope_is_all"] = bool(
            PermissionService.can(self.request.user, Action.AUDIT_READ_ALL)
        )
        return ctx


def _authorise_snapshot(request, detection_id: int):
    """Shared gate for both snapshot endpoints.

    Two checks, not one: the viewer must be allowed to see imagery *at all*
    (role, and the operator live-window rule), and must be allowed to see
    *this person's* history. A snapshot is an identification, so the second
    check is what stops it becoming a way around the tracking-request gate.
    """
    if not request.user.is_authenticated:
        raise PermissionDenied("not authenticated")

    from apps.tracking.models import Detection

    detection = get_object_or_404(
        Detection.objects.select_related("matched_employee", "camera"), pk=detection_id
    )

    decision = PermissionService.can_view_snapshot(request.user, detection)
    if not decision:
        record(
            "snapshot.denied",
            target_employee=detection.matched_employee,
            reason=decision.reason,
            success=False,
        )
        raise PermissionDenied(decision.reason)

    if detection.matched_employee is not None:
        history = PermissionService.can_view_history(
            request.user, detection.matched_employee, at=detection.captured_at
        )
        if not history:
            record(
                "snapshot.denied",
                target_employee=detection.matched_employee,
                reason=history.reason,
                success=False,
            )
            raise PermissionDenied(history.reason)

    if not detection.snapshot_key:
        raise Http404("No snapshot stored for this detection")

    return detection


def snapshot_url(request, detection_id: int) -> JsonResponse:
    """Hand back the protected endpoint for one snapshot.

    Deliberately not a signed object-storage URL. A signed URL leaves the
    system and can be pasted into a chat; this returns a path that re-checks
    permission and writes an audit row on every fetch.
    """
    detection = _authorise_snapshot(request, detection_id)
    return JsonResponse(
        {
            "url": reverse("tracking:snapshot_file", args=[detection.pk]),
            "camera": detection.camera.code,
            "captured_at": detection.captured_at.isoformat(),
        }
    )


def snapshot_file(request, detection_id: int):
    """Stream one snapshot after re-checking permission.

    Works the same whether the file sits on a local volume or in a private
    bucket, which keeps one audit point for all imagery.
    """
    detection = _authorise_snapshot(request, detection_id)
    return _stream_snapshot(
        detection.snapshot_key,
        filename=f"snapshot-{detection.pk}.jpg",
        target_employee=detection.matched_employee,
        object_type="tracking.Detection",
        object_id=detection.pk,
        metadata={
            "camera": detection.camera.code,
            "captured_at": detection.captured_at.isoformat(),
        },
    )


def interval_snapshot(request, interval_id: int, which: str):
    """Evidence for one timeline entry: its first or last snapshot.

    Same two checks as a detection snapshot - may this viewer see imagery at
    all, and may they see this person's history - plus the prohibited-zone
    rule, which no permission overrides.
    """
    if not request.user.is_authenticated:
        raise PermissionDenied("not authenticated")
    if which not in ("first", "last"):
        raise Http404("Unknown snapshot")

    interval = get_object_or_404(
        PresenceInterval.objects.select_related("employee", "zone"), pk=interval_id
    )
    if interval.zone.tracking_prohibited:
        raise Http404("No snapshot")

    for decision in (
        PermissionService.can_view_snapshot(request.user, interval),
        PermissionService.can_view_history(
            request.user,
            interval.employee,
            # The time this particular image was taken, so a request's data
            # window is checked against the evidence itself.
            at=interval.entered_at if which == "first" else interval.last_seen_at,
        ),
    ):
        if not decision:
            record(
                "snapshot.denied",
                target_employee=interval.employee,
                reason=decision.reason,
                success=False,
            )
            raise PermissionDenied(decision.reason)

    key = interval.first_snapshot_key if which == "first" else interval.last_snapshot_key
    if not key:
        raise Http404("No snapshot stored for this entry")

    return _stream_snapshot(
        key,
        filename=f"interval-{interval.pk}-{which}.jpg",
        target_employee=interval.employee,
        object_type="tracking.PresenceInterval",
        object_id=interval.pk,
        metadata={"zone": interval.zone.code, "which": which},
    )


def cluster_snapshot(request, cluster_uuid):
    """Representative image of an unknown-face cluster, for the review queue.

    No named person is involved, so the history check does not apply; the
    gate is snapshot permission plus access to the review queue itself.
    """
    if not request.user.is_authenticated:
        raise PermissionDenied("not authenticated")

    from apps.tracking.models import UnknownFaceCluster

    cluster = get_object_or_404(UnknownFaceCluster, uuid=cluster_uuid)
    for decision in (
        PermissionService.can_view_snapshot(request.user),
        PermissionService.can(request.user, Action.HISTORY_ANY),
    ):
        if not decision:
            record("snapshot.denied", reason=decision.reason, success=False)
            raise PermissionDenied(decision.reason)

    if not cluster.representative_snapshot_key:
        raise Http404("No snapshot stored for this cluster")

    return _stream_snapshot(
        cluster.representative_snapshot_key,
        filename=f"unknown-{str(cluster.uuid)[:8]}.jpg",
        target_employee=None,
        object_type="tracking.UnknownFaceCluster",
        object_id=cluster.pk,
        metadata={"cluster": str(cluster.uuid)},
    )


def _stream_snapshot(key: str, *, filename: str, **audit) -> FileResponse:
    """Open, audit and stream one snapshot. Callers have already authorised."""
    from django.core.files.storage import default_storage

    try:
        handle = default_storage.open(key, "rb")
    except FileNotFoundError as exc:
        # The retention job has already purged it. Say so plainly rather than
        # returning a 500.
        raise Http404("This snapshot has been deleted under the retention policy") from exc

    record("snapshot.view", **audit)

    response = FileResponse(handle, content_type="image/jpeg")
    # Never cached, never stored by an intermediary, never a referrer leak.
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    response["X-Content-Type-Options"] = "nosniff"
    response["Referrer-Policy"] = "no-referrer"
    return response


def report_correction(request, employee_uuid):
    """Record a reported mis-identification.

    This is the rectification right that privacy law requires, and it is also
    the only source of labelled errors you will ever get. Those labels are
    what threshold tuning should be driven by, so the queue is treated as an
    accuracy signal rather than a complaints box.

    Anyone who can view the timeline can report against it - including the
    subject looking at their own record.
    """
    if request.method != "POST":
        raise Http404("POST only")

    employee = get_object_or_404(Employee.objects.alive(), uuid=employee_uuid)

    decision = PermissionService.can_view_history(request.user, employee)
    if not decision:
        record(
            "correction.denied",
            target_employee=employee,
            reason=decision.reason,
            success=False,
        )
        raise PermissionDenied(decision.reason)

    reason = (request.POST.get("reason") or "").strip()
    if len(reason) < 5:
        messages.error(request, "Say what is wrong so it can be investigated.")
        return redirect(
            reverse("tracking:employee_timeline", args=[employee.uuid])
        )

    from apps.employees.models import IdentityCorrection

    # Optional: the specific timeline entry being disputed. It must belong to
    # this employee - a report cannot be pinned to someone else's record.
    interval = None
    interval_id = (request.POST.get("interval") or "").strip()
    if interval_id.isdigit():
        candidates = PresenceInterval.objects.filter(pk=int(interval_id), employee=employee)
        windows = PermissionService.history_windows(request.user, employee)
        if windows is not None:
            candidates = candidates.filter(_interval_window_q(windows))
        interval = candidates.first()

    correction = IdentityCorrection.objects.create(
        presence_interval=interval,
        was_employee=employee,
        reported_by=request.user,
        reason=reason[:2000],
    )

    record(
        "correction.reported",
        target_employee=employee,
        object_type="employees.IdentityCorrection",
        object_id=correction.pk,
        reason=reason[:500],
        metadata={"interval": interval.pk if interval else None},
    )
    messages.success(
        request,
        "Reported. This is treated as a correction request and feeds back into "
        "tuning, so thank you - wrong matches are hard to find without it.",
    )
    return redirect(reverse("tracking:employee_timeline", args=[employee.uuid]))


# --------------------------------------------------------------- helpers


def _floors_with_zones(zones) -> list[dict]:
    """Group already-scoped zones by floor, with a plan drawing where one exists.

    The drawing only ever contains the zones passed in, so a floor plan can
    never show a room the viewer is not allowed to monitor.
    """
    from apps.org.plans import floor_drawing, has_plan

    floors: dict[int, dict] = {}
    for zone in zones:
        entry = floors.setdefault(zone.floor_id, {"floor": zone.floor, "zones": []})
        entry["zones"].append(zone)
    for entry in floors.values():
        entry["drawing"] = (
            floor_drawing(entry["floor"], entry["zones"]) if has_plan(entry["floor"]) else None
        )
    return list(floors.values())


def _movement_map(intervals) -> list[dict]:
    """The day's path, floor by floor, for the movement map.

    Each stop is numbered by its position in the whole day, so a path that
    leaves one floor and returns to it later still reads in order. Only
    floors with a plan image are drawn; stops in zones without an outline
    are skipped on the drawing but still listed on the timeline.
    """
    from apps.org.plans import centroid, floor_drawing, has_plan, valid_polygon

    floors: dict[int, dict] = {}
    for number, interval in enumerate(intervals, start=1):
        floor = interval.zone.floor
        if not has_plan(floor):
            continue
        polygon = valid_polygon(interval.zone.plan_polygon)
        if polygon is None:
            continue
        entry = floors.setdefault(floor.pk, {"floor": floor, "stops": []})
        x, y = centroid(polygon)
        entry["stops"].append(
            {
                "n": number,
                "x": x,
                "y": y,
                "zone": interval.zone.name,
                "at": timezone.localtime(interval.entered_at).strftime("%H:%M"),
                "confidence": interval.confidence,
            }
        )

    result = []
    for entry in floors.values():
        floor = entry["floor"]
        # Outline every trackable room on the floor for orientation. This is
        # site layout, not anyone's data; never-tracked rooms stay off it.
        zones = floor.zones.alive().filter(tracking_prohibited=False)
        drawing = floor_drawing(floor, zones)
        drawing["stops"] = entry["stops"]
        drawing["path"] = " ".join(f"{s['x']},{s['y']}" for s in entry["stops"])
        drawing["dom_id"] = f"map-{floor.pk}"
        drawing["data_id"] = f"map-{floor.pk}-stops"
        result.append(drawing)
    return result


def _parse_local(raw: str):
    """Parse an HTML datetime-local value into an aware datetime."""
    from django.utils.dateparse import parse_datetime

    value = parse_datetime(raw)
    if value is None:
        return None
    if timezone.is_naive(value):
        value = timezone.make_aware(value, timezone.get_current_timezone())
    return value


def _parse_date(raw: str | None):
    if not raw:
        return None
    try:
        return dt.date.fromisoformat(raw)
    except ValueError:
        return None
