"""
Read-mostly API for the dashboards.

Each viewset pairs an action gate (``required_action`` + ``HasAction``) with
a scoping filter in ``get_queryset``. Both, always - see the module docstring
in apps/common/scoping.py for why one without the other is not enough.
"""

from __future__ import annotations

import datetime as dt

from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.api.permissions import HasAction, HasWriteAction
from apps.api.serializers import (
    CameraSerializer,
    EmployeeSerializer,
    MovementEventSerializer,
    PresenceIntervalSerializer,
    TrackingRequestSerializer,
    ZoneSerializer,
)
from apps.cameras.models import Camera
from apps.common.audit import record
from apps.common.permissions import PermissionService
from apps.common.roles import Action
from apps.common.scoping import (
    visible_cameras,
    visible_employees,
    visible_presence,
    visible_zones,
)
from apps.employees.models import Employee
from apps.org.models import Zone
from apps.tracking.models import MovementEvent, PresenceInterval
from apps.watchlist.models import TrackingRequest


class ZoneViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = ZoneSerializer
    permission_classes = [HasAction]
    required_action = Action.LIVE_VIEW_ASSIGNED
    lookup_field = "code"

    def get_queryset(self):
        return (
            visible_zones(self.request.user, Zone.objects.alive())
            .select_related("floor", "floor__building")
            .annotate(
                occupancy=Count(
                    "presence_intervals",
                    filter=Q(presence_intervals__exited_at__isnull=True),
                    distinct=True,
                )
            )
            .order_by("floor__level", "name")
        )

    @action(detail=False, methods=["get"], url_path="occupancy")
    def occupancy(self, request):
        """Current occupancy per zone - what the live board polls as a
        fallback when the WebSocket is unavailable."""
        data = self.get_serializer(self.get_queryset(), many=True).data
        return Response({"at": timezone.now().isoformat(), "zones": data})


class CameraViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = CameraSerializer
    permission_classes = [HasAction]
    required_action = Action.CAMERA_VIEW
    lookup_field = "code"
    filterset_fields = ["status", "purpose", "is_enabled"]

    def get_queryset(self):
        return visible_cameras(
            self.request.user, Camera.objects.alive()
        ).select_related("zone")

    @action(detail=False, methods=["get"], url_path="health")
    def health(self, request):
        """Per-camera health summary, plus the honest capability count.

        ``identity_cameras_below_spec`` is the number marked for identity
        work whose measured face width cannot support it - the single most
        useful number for explaining disappointing accuracy.
        """
        qs = self.get_queryset()
        return Response(
            {
                "total": qs.count(),
                "online": qs.filter(status=Camera.Status.ONLINE).count(),
                "degraded": qs.filter(status=Camera.Status.DEGRADED).count(),
                "offline": qs.filter(status=Camera.Status.OFFLINE).count(),
                "identity_cameras": qs.filter(
                    purpose=Camera.Purpose.IDENTITY
                ).count(),
                "identity_cameras_below_spec": qs.filter(
                    purpose=Camera.Purpose.IDENTITY
                )
                .filter(
                    Q(measured_face_width_px__lt=60)
                    | Q(measured_face_width_px__isnull=True)
                )
                .count(),
            }
        )


class EmployeeViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = EmployeeSerializer
    permission_classes = [HasAction]
    required_action = Action.EMPLOYEE_VIEW
    lookup_field = "uuid"
    throttle_scope = "identity_lookup"
    filterset_fields = ["status", "is_trackable"]

    def get_queryset(self):
        return visible_employees(
            self.request.user, Employee.objects.alive()
        ).select_related("department", "designation").annotate(
            enrollment_count=Count(
                "enrollments", filter=Q(enrollments__is_active=True), distinct=True
            )
        )

    @action(detail=True, methods=["get"], url_path="timeline")
    def timeline(self, request, uuid=None):
        """One day of presence intervals for one person.

        The only endpoint in the API that goes through
        ``can_view_history``, which is where the tracking-request gate bites.
        """
        employee = self.get_object()

        decision = PermissionService.can_view_history(request.user, employee)
        if not decision:
            record(
                "api.history_denied",
                target_employee=employee,
                reason=decision.reason,
                success=False,
            )
            raise ValidationError(
                {
                    "detail": decision.reason,
                    "requires_approval": decision.requires_approval,
                }
            )

        day_raw = request.query_params.get("date")
        try:
            day = dt.date.fromisoformat(day_raw) if day_raw else timezone.localdate()
        except ValueError as exc:
            raise ValidationError({"date": "Use YYYY-MM-DD."}) from exc

        tz = timezone.get_current_timezone()
        start = timezone.make_aware(dt.datetime.combine(day, dt.time.min), tz)
        end = start + dt.timedelta(days=1)

        intervals = PresenceInterval.objects.filter(
            employee=employee,
            entered_at__gte=start,
            entered_at__lt=end,
            zone__tracking_prohibited=False,
        )
        movements = MovementEvent.objects.filter(
            employee=employee, occurred_at__gte=start, occurred_at__lt=end
        )
        # Access through a tracking request is limited to its data window.
        windows = PermissionService.history_windows(request.user, employee)
        if windows is not None:
            from apps.tracking.views import _instant_window_q, _interval_window_q

            intervals = intervals.filter(_interval_window_q(windows))
            movements = movements.filter(_instant_window_q(windows, "occurred_at"))
        intervals = intervals.select_related("zone").order_by("entered_at")
        movements = movements.select_related("from_zone", "to_zone").order_by("occurred_at")

        record(
            "api.history_view",
            target_employee=employee,
            reason=f"timeline {day.isoformat()}",
            metadata={"intervals": intervals.count()},
        )

        return Response(
            {
                "employee": EmployeeSerializer(employee).data,
                "date": day.isoformat(),
                "intervals": PresenceIntervalSerializer(intervals, many=True).data,
                "movements": MovementEventSerializer(movements, many=True).data,
                "caveats": {
                    "low_confidence_intervals": intervals.exclude(
                        confidence="confirmed"
                    ).count(),
                    "implausible_movements": movements.filter(
                        is_plausible=False
                    ).count(),
                },
            }
        )


class PresenceViewSet(viewsets.ReadOnlyModelViewSet):
    """Zone-first queries. Scoped, so it cannot be used to sweep the org."""

    serializer_class = PresenceIntervalSerializer
    permission_classes = [HasAction]
    required_action = Action.HISTORY_OWN
    throttle_scope = "identity_lookup"
    filterset_fields = ["zone__code", "confidence"]

    def get_queryset(self):
        qs = visible_presence(
            self.request.user,
            PresenceInterval.objects.select_related("employee", "zone"),
        )

        since = self.request.query_params.get("since")
        until = self.request.query_params.get("until")
        if since:
            qs = qs.filter(entered_at__gte=since)
        if until:
            qs = qs.filter(entered_at__lt=until)
        if self.request.query_params.get("open_only"):
            qs = qs.filter(exited_at__isnull=True)
        return qs.order_by("-entered_at")


class TrackingRequestViewSet(viewsets.ModelViewSet):
    """Raise and read tracking requests. Approval is a UI/POST action only.

    Deliberately not approvable through this API: approving is an authority
    decision that should happen in a reviewed interface showing the reason,
    not as a scriptable call.
    """

    serializer_class = TrackingRequestSerializer
    permission_classes = [HasWriteAction]
    required_action = Action.TRACKREQUEST_CREATE
    required_write_action = Action.TRACKREQUEST_CREATE
    lookup_field = "uuid"
    # Lets the schema generator derive the model and the `uuid` path type
    # without executing get_queryset(), which needs a real request.user.
    # Empty by design: the live queryset below is always scoped.
    queryset = TrackingRequest.objects.none()
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        user = self.request.user
        qs = TrackingRequest.objects.select_related(
            "employee", "requested_by", "approved_by"
        )
        if PermissionService.can(user, Action.TRACKREQUEST_APPROVE):
            return qs.order_by("-requested_at")
        return qs.filter(Q(requested_by=user) | Q(approved_by=user)).order_by(
            "-requested_at"
        )

    def perform_create(self, serializer):
        employee = serializer.validated_data["employee"]
        # Any active employee may be named - reaching someone outside the
        # requester's directory scope is the point of the mechanism. What
        # the approval gates is access to the history, not knowledge that
        # the person exists.
        if employee.deleted_at is not None or employee.status != Employee.Status.ACTIVE:
            raise ValidationError(
                {"employee": "Tracking requests may only name an active employee."}
            )

        instance = serializer.save(
            requested_by=self.request.user, status=TrackingRequest.Status.PENDING
        )
        record(
            "trackrequest.created",
            target_employee=instance.employee,
            object_type="watchlist.TrackingRequest",
            object_id=instance.pk,
            reason=instance.reason[:1000],
            metadata={"via": "api", "purpose": instance.purpose},
        )
