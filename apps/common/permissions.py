"""
PermissionService - the one place that answers "may this user do this?".

Design notes
------------
* Every gated capability goes through :meth:`PermissionService.can`. Views,
  DRF permission classes, template tags and querysets all call into here, so
  the rule exists once and is unit-testable without HTTP.
* Hiding a button is not access control. Pair every ``can()`` check with a
  queryset filter from :mod:`apps.common.scoping`.
* Checks return a :class:`Decision`, not a bare bool, so the UI can say
  *why* something is unavailable ("needs an approved tracking request")
  instead of just hiding it. ``bool(decision)`` works for the common case.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from django.apps import apps
from django.conf import settings
from django.utils import timezone

from apps.common.roles import (
    MFA_EXEMPT_ACTIONS,
    MFA_REQUIRED_ROLES,
    NEVER_ELEVATABLE,
    Action,
    Role,
    actions_for_role,
)


@dataclass(frozen=True)
class Decision:
    """Outcome of a permission check, with a human-readable reason."""

    allowed: bool
    reason: str = ""
    # Set when the capability exists but needs an approval step first, so the
    # UI can offer "request access" rather than hiding the feature.
    requires_approval: bool = False

    def __bool__(self) -> bool:
        return self.allowed


ALLOW = Decision(True)


def _deny(reason: str, *, requires_approval: bool = False) -> Decision:
    return Decision(False, reason, requires_approval)


def _is_production() -> bool:
    return getattr(settings, "ENVIRONMENT", "production") == "production"


class PermissionService:
    """Stateless; every method takes the acting user explicitly."""

    # ------------------------------------------------------------- actions

    @staticmethod
    def granted_actions(user) -> frozenset[str]:
        """All actions this user currently holds, including break-glass."""
        if not user or not user.is_authenticated or not user.is_active:
            return frozenset()

        base = actions_for_role(user.role, is_production=_is_production())

        # A Django superuser is treated as SUPER_ADMIN rather than as an
        # unconditional bypass: in a biometric system "superuser sees all"
        # must still be an auditable, enumerable set of actions.
        if user.is_superuser:
            base = base | actions_for_role(Role.SUPER_ADMIN, is_production=False)

        elevated = PermissionService._active_elevation_actions(user)
        return base | elevated

    @staticmethod
    def _active_elevation_actions(user) -> frozenset[str]:
        """Actions granted right now by an approved break-glass elevation."""
        AccessElevation = apps.get_model("accounts", "AccessElevation")
        now = timezone.now()
        rows = AccessElevation.objects.filter(
            user=user,
            status=AccessElevation.Status.ACTIVE,
            starts_at__lte=now,
            expires_at__gt=now,
        ).values_list("actions", flat=True)
        granted: set[str] = set()
        for actions in rows:
            granted.update(actions or [])
        # Authority over people is never reachable by elevation.
        return frozenset(granted) - NEVER_ELEVATABLE

    @staticmethod
    def can(user, action: str, target=None) -> Decision:
        """Primary entry point.

        ``target`` is optional and used by the capability-specific helpers
        below; plain action checks ignore it.
        """
        if not user or not user.is_authenticated:
            return _deny("not authenticated")
        if not user.is_active:
            return _deny("account disabled")

        mfa = PermissionService.check_mfa(user, action)
        if not mfa:
            return mfa

        if action not in PermissionService.granted_actions(user):
            return _deny(f"role '{user.role}' does not grant '{action}'")

        return ALLOW

    # ----------------------------------------------------------------- MFA

    @staticmethod
    def check_mfa(user, action: str = "") -> Decision:
        """Block MFA-required roles that have not enrolled a TOTP device.

        Self-service actions stay open so a locked-out admin can still reach
        the enrolment page.
        """
        if not getattr(settings, "MFA_ENFORCED", False):
            return ALLOW
        if user.role not in MFA_REQUIRED_ROLES:
            return ALLOW
        if action in MFA_EXEMPT_ACTIONS:
            return ALLOW
        if not user.has_confirmed_mfa:
            return _deny(
                "multi-factor authentication must be enrolled for this role",
                requires_approval=False,
            )
        # django_otp's middleware sets is_verified() on the request user, and
        # apps.accounts.channels_otp does the same for WebSocket connections.
        # A user object without it came from somewhere that never checked a
        # token, so it counts as unverified: fail closed.
        verified = getattr(user, "is_verified", None)
        if not callable(verified) or not verified():
            return _deny("this session is not MFA-verified")
        return ALLOW

    # ------------------------------------------------------------- history

    @staticmethod
    def can_view_history(user, employee, *, at: dt.datetime | None = None) -> Decision:
        """May ``user`` see ``employee``'s movement history?

        Resolution order - widest legitimate grant first, then the
        approval-backed path:

        1. HISTORY_ANY (Developer, Admin, Security Admin)
        2. HISTORY_OWN, when the subject is the user's own employee record
        3. HISTORY_DEPARTMENT, when the subject is inside the user's
           department scope (HR)
        4. An approved, unexpired, historical-scope TrackingRequest naming
           this subject - and then only for data inside the request's
           ``data_from``..``data_to`` window. Pass ``at`` to check a specific
           record's time; without it, the answer is whether *any* history is
           reachable, and callers must still limit rows with
           :meth:`history_windows`.
        """
        if not user or not user.is_authenticated:
            return _deny("not authenticated")
        if not user.is_active:
            return _deny("account disabled")

        mfa = PermissionService.check_mfa(user, Action.HISTORY_ANY)
        if not mfa:
            return mfa

        granted = PermissionService.granted_actions(user)

        if Action.HISTORY_ANY in granted:
            return ALLOW

        if employee is not None and getattr(user, "employee_id", None) == employee.pk:
            if Action.HISTORY_OWN in granted:
                return ALLOW

        if Action.HISTORY_DEPARTMENT in granted and employee is not None:
            if PermissionService._in_department_scope(user, employee):
                return ALLOW

        windows = PermissionService._request_windows(user, employee)
        if windows:
            if at is None or any(start <= at < end for start, end in windows):
                return ALLOW
            return _deny(
                "outside the data window your tracking request was approved for",
                requires_approval=Action.TRACKREQUEST_CREATE in granted,
            )

        if Action.TRACKREQUEST_CREATE in granted:
            return _deny(
                "outside your department - raise a tracking request for approval",
                requires_approval=True,
            )
        return _deny("not permitted to view this employee's history")

    @staticmethod
    def history_windows(user, employee) -> list[tuple[dt.datetime, dt.datetime]] | None:
        """The time windows of ``employee``'s history this user may read.

        ``None`` means unrestricted: access comes from the user's role (any,
        own, or department history), not from a tracking request. A list
        means access exists only through approved requests, and only inside
        these windows. Call after :meth:`can_view_history` has allowed access.
        """
        granted = PermissionService.granted_actions(user)
        if Action.HISTORY_ANY in granted:
            return None
        if employee is not None and getattr(user, "employee_id", None) == employee.pk:
            if Action.HISTORY_OWN in granted:
                return None
        if Action.HISTORY_DEPARTMENT in granted and employee is not None:
            if PermissionService._in_department_scope(user, employee):
                return None
        return PermissionService._request_windows(user, employee)

    @staticmethod
    def _request_windows(user, employee) -> list[tuple[dt.datetime, dt.datetime]]:
        """Data windows of this user's active historical requests for ``employee``."""
        if employee is None or not user or not user.is_authenticated:
            return []
        TrackingRequest = apps.get_model("watchlist", "TrackingRequest")
        now = timezone.now()
        return list(
            TrackingRequest.objects.filter(
                employee=employee,
                status=TrackingRequest.Status.APPROVED,
                start_at__lte=now,
                end_at__gt=now,
                scope__in=[TrackingRequest.Scope.HISTORICAL, TrackingRequest.Scope.BOTH],
            )
            .filter(models_q_requester_or_approver(user))
            .values_list("data_from", "data_to")
        )

    @staticmethod
    def can_follow_live(user, employee) -> Decision:
        """Live follow is the highest-risk capability; approval-gated for HR."""
        granted = PermissionService.granted_actions(user)

        mfa = PermissionService.check_mfa(user, Action.TRACK_FOLLOW_LIVE)
        if not mfa:
            return mfa

        if Action.TRACK_FOLLOW_LIVE in granted:
            return ALLOW

        req = PermissionService.active_tracking_request(user, employee, live=True)
        if req is not None:
            return ALLOW

        if Action.TRACKREQUEST_CREATE in granted:
            return _deny(
                "live follow needs an approved tracking request",
                requires_approval=True,
            )
        return _deny("not permitted to follow employees live")

    @staticmethod
    def active_tracking_request(user, employee, *, live: bool = False):
        """Return an approved, in-window TrackingRequest, or None.

        The requester and the approver are both allowed to view, because an
        approver must be able to see what they authorised. ``live=True``
        looks for live-follow scope; otherwise for historical scope - a
        live-only approval does not open someone's past.
        """
        if employee is None or not user or not user.is_authenticated:
            return None

        TrackingRequest = apps.get_model("watchlist", "TrackingRequest")
        now = timezone.now()
        qs = TrackingRequest.objects.filter(
            employee=employee,
            status=TrackingRequest.Status.APPROVED,
            start_at__lte=now,
            end_at__gt=now,
        ).filter(
            # Requester or approver only - an approval does not open the
            # subject's history to every holder of the same role.
            models_q_requester_or_approver(user)
        )
        if live:
            qs = qs.filter(
                scope__in=[TrackingRequest.Scope.LIVE, TrackingRequest.Scope.BOTH]
            )
        else:
            qs = qs.filter(
                scope__in=[TrackingRequest.Scope.HISTORICAL, TrackingRequest.Scope.BOTH]
            )
        return qs.order_by("-approved_at").first()

    @staticmethod
    def _in_department_scope(user, employee) -> bool:
        scope = getattr(user, "scope", None)
        if scope is None:
            return False
        if employee.department_id is None:
            return False
        # An empty department scope means "no departments", never "all".
        return scope.departments.filter(pk=employee.department_id).exists()

    # --------------------------------------------------------- media access

    @staticmethod
    def can_view_snapshot(user, detection_or_interval=None) -> Decision:
        """Snapshots are biometric evidence; operators get live-window only."""
        granted = PermissionService.granted_actions(user)
        if Action.SNAPSHOT_VIEW not in granted:
            return _deny("not permitted to view snapshots")

        mfa = PermissionService.check_mfa(user, Action.SNAPSHOT_VIEW)
        if not mfa:
            return mfa

        # Security operators monitor; they do not browse history.
        if user.role == Role.SECURITY_OPERATOR and detection_or_interval is not None:
            captured = _captured_at(detection_or_interval)
            if captured and timezone.now() - captured > dt.timedelta(minutes=15):
                return _deny("operators may only view the live window")
        return ALLOW

    @staticmethod
    def can_view_clip(user, recorded_at: dt.datetime | None = None) -> Decision:
        granted = PermissionService.granted_actions(user)
        if Action.CLIP_VIEW not in granted:
            return _deny("not permitted to view video clips")

        mfa = PermissionService.check_mfa(user, Action.CLIP_VIEW)
        if not mfa:
            return mfa

        if user.role == Role.SECURITY_OPERATOR and recorded_at is not None:
            if timezone.now() - recorded_at > dt.timedelta(hours=24):
                return _deny("operators may only replay the last 24 hours")
        return ALLOW

    # --------------------------------------------------------------- export

    @staticmethod
    def can_export(user, kind: str) -> Decision:
        """``kind`` is one of: aggregate, identity, audit.

        HR exports aggregates; an auditor exports the audit log; only an
        admin exports per-person identity data.
        """
        granted = PermissionService.granted_actions(user)
        if Action.REPORT_EXPORT not in granted:
            return _deny("not permitted to export")

        mfa = PermissionService.check_mfa(user, Action.REPORT_EXPORT)
        if not mfa:
            return mfa

        if kind == "audit":
            if Action.AUDIT_READ_ALL in granted:
                return ALLOW
            return _deny("audit exports require the Auditor role")

        if kind == "identity":
            if Action.HISTORY_ANY in granted:
                return ALLOW
            return _deny("per-employee exports require administrator access")

        if kind == "aggregate":
            if Action.REPORT_AGGREGATE_VIEW in granted:
                return ALLOW
            return _deny("not permitted to export aggregate reports")

        return _deny(f"unknown export kind '{kind}'")

    # ---------------------------------------------------------------- audit

    @staticmethod
    def can_read_audit(user, *, own_only_ok: bool = True) -> Decision:
        granted = PermissionService.granted_actions(user)
        if Action.AUDIT_READ_ALL in granted:
            return ALLOW
        if own_only_ok and Action.AUDIT_READ_OWN in granted:
            return ALLOW
        return _deny("not permitted to read the audit log")


def models_q_requester_or_approver(user):
    """Q object: tracking requests this user raised or approved."""
    from django.db.models import Q

    return Q(requested_by=user) | Q(approved_by=user)


def _captured_at(obj) -> dt.datetime | None:
    for attr in ("captured_at", "entered_at", "occurred_at"):
        value = getattr(obj, attr, None)
        if value is not None:
            return value
    return None
