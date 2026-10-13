"""
Queryset scoping.

``PermissionService.can()`` answers "may this user do X at all". This module
answers "which rows may they see". Both are required: a role check without a
queryset filter leaks data through list endpoints, search and CSV export.

Every viewset and ListView over scoped data must use one of these helpers in
``get_queryset()``. Tests in tests/test_scoping.py assert that each scoped
model has a corresponding filter here.
"""

from __future__ import annotations

from django.db.models import QuerySet

from apps.common.permissions import PermissionService
from apps.common.roles import Action, Role


def _empty(qs: QuerySet) -> QuerySet:
    return qs.none()


def visible_zones(user, qs: QuerySet) -> QuerySet:
    """Zones this user may **monitor**.

    Zones flagged ``tracking_prohibited`` - toilets, changing rooms, prayer
    rooms, medical rooms - are excluded for everyone, with no exception for
    administrators. There is no legitimate monitoring view of those rooms, so
    "can configure a zone" and "can watch a zone" are deliberately different
    questions: configuration goes through :func:`manageable_zones` and the
    admin site, monitoring comes through here.
    """
    granted = PermissionService.granted_actions(user)

    qs = qs.filter(tracking_prohibited=False)

    if Action.LIVE_VIEW_ALL in granted:
        return qs

    if Action.LIVE_VIEW_ASSIGNED in granted:
        scope = getattr(user, "scope", None)
        if scope is None:
            return _empty(qs)
        return qs.filter(pk__in=scope.zones.values_list("pk", flat=True))

    return _empty(qs)


def manageable_zones(user, qs: QuerySet) -> QuerySet:
    """Zones this user may **configure**.

    Includes prohibited zones, because somebody has to be able to mark a room
    as never-tracked in the first place. Never use this to drive a monitoring
    view or a presence query.
    """
    if Action.ZONE_MANAGE in PermissionService.granted_actions(user):
        return qs
    return _empty(qs)


def visible_cameras(user, qs: QuerySet) -> QuerySet:
    """Cameras this user may see.

    A camera in a prohibited zone is excluded for everyone here too. Such a
    camera cannot be enabled in the first place (``Camera.clean``), so this
    is belt and braces against a row that predates the flag.
    """
    granted = PermissionService.granted_actions(user)

    if Action.CAMERA_VIEW not in granted and Action.CAMERA_MANAGE not in granted:
        return _empty(qs)

    qs = qs.filter(zone__tracking_prohibited=False)

    if Action.CAMERA_MANAGE in granted:
        return qs

    if Action.LIVE_VIEW_ALL in granted:
        return qs

    scope = getattr(user, "scope", None)
    if scope is None:
        return _empty(qs)
    return qs.filter(zone__in=scope.zones.values_list("pk", flat=True))


def visible_employees(user, qs: QuerySet) -> QuerySet:
    """Employee records this user may list.

    Note the difference from history access: HR can *see* the directory for
    their departments, which is not the same as being able to replay someone's
    movements. History goes through ``PermissionService.can_view_history``.
    """
    granted = PermissionService.granted_actions(user)

    if Action.EMPLOYEE_MANAGE in granted and Action.HISTORY_ANY in granted:
        return qs

    if Action.EMPLOYEE_VIEW in granted or Action.EMPLOYEE_MANAGE in granted:
        if Action.HISTORY_ANY in granted:
            return qs
        scope = getattr(user, "scope", None)
        if scope is not None:
            dept_ids = list(scope.departments.values_list("pk", flat=True))
            if dept_ids:
                return qs.filter(department_id__in=dept_ids)
        # An HR user with no department scope set sees nobody rather than
        # everybody. Fail closed.
        return qs.filter(pk=getattr(user, "employee_id", None) or 0)

    # Everyone else: only their own record.
    return qs.filter(pk=getattr(user, "employee_id", None) or 0)


def visible_presence(user, qs: QuerySet) -> QuerySet:
    """Presence intervals this user may query.

    Includes rows unlocked by an approved historical TrackingRequest, which
    is how HR reaches a subject outside their own department - but only rows
    that overlap the request's approved data window. An approval for "last
    Tuesday" is not an approval for the subject's whole history.
    """
    from django.db.models import Q

    granted = PermissionService.granted_actions(user)

    qs = qs.filter(zone__tracking_prohibited=False)

    if Action.HISTORY_ANY in granted:
        return qs

    allowed_employee_ids: set[int] = set()

    own = getattr(user, "employee_id", None)
    if own and Action.HISTORY_OWN in granted:
        allowed_employee_ids.add(own)

    if Action.HISTORY_DEPARTMENT in granted:
        scope = getattr(user, "scope", None)
        if scope is not None:
            from apps.employees.models import Employee

            allowed_employee_ids.update(
                Employee.objects.filter(
                    department__in=scope.departments.values_list("pk", flat=True)
                ).values_list("pk", flat=True)
            )

    condition = Q(employee_id__in=allowed_employee_ids) if allowed_employee_ids else Q()
    windows = _tracking_request_windows(user)
    for employee_id, start, end in windows:
        overlap = (
            Q(employee_id=employee_id, entered_at__lt=end)
            & (Q(exited_at__gte=start) | Q(exited_at__isnull=True))
        )
        condition = condition | overlap if condition else overlap

    if not condition:
        return _empty(qs)
    return qs.filter(condition)


def _tracking_request_windows(user) -> list[tuple[int, object, object]]:
    """(employee_id, data_from, data_to) for each active historical request."""
    if not user or not user.is_authenticated:
        return []

    from django.utils import timezone

    from apps.common.permissions import models_q_requester_or_approver
    from apps.watchlist.models import TrackingRequest

    now = timezone.now()
    return list(
        TrackingRequest.objects.filter(
            status=TrackingRequest.Status.APPROVED,
            start_at__lte=now,
            end_at__gt=now,
            scope__in=[TrackingRequest.Scope.HISTORICAL, TrackingRequest.Scope.BOTH],
        )
        .filter(models_q_requester_or_approver(user))
        .values_list("employee_id", "data_from", "data_to")
    )


def visible_audit(user, qs: QuerySet) -> QuerySet:
    granted = PermissionService.granted_actions(user)
    if Action.AUDIT_READ_ALL in granted:
        return qs
    if Action.AUDIT_READ_OWN in granted:
        return qs.filter(actor=user)
    return _empty(qs)


def visible_detections(user, qs: QuerySet) -> QuerySet:
    """Raw detections, including embeddings. Developer only."""
    granted = PermissionService.granted_actions(user)
    if Action.DETECTION_VIEW_RAW in granted:
        return qs
    return _empty(qs)


class ScopedQuerySetMixin:
    """Mixin for ListView / DRF viewsets.

    Set ``scope_function`` to one of the helpers above. Forgetting it raises
    rather than silently returning everything - fail closed, loudly.
    """

    scope_function = None

    def get_queryset(self):
        qs = super().get_queryset()
        if self.scope_function is None:
            raise NotImplementedError(
                f"{type(self).__name__} must set `scope_function` "
                "(apps.common.scoping) so rows are filtered to the user's scope."
            )
        return self.scope_function(self.request.user, qs)


# Roles whose users must always have an explicit scope configured, because
# their role grants only scoped access. seed_roles warns about any such user
# left without one.
SCOPE_REQUIRED_ROLES = frozenset({Role.HR, Role.SECURITY_OPERATOR})
