"""
Roles and actions - the single source of truth for who may do what.

Nothing else in the codebase should hard-code a role name. Views, serializers
and querysets ask ``PermissionService.can(user, Action.X, target)``; this
module decides. The matrix below is the executable form of the role table in
CCTV_TRACKING_PLAN.md section 7, and tests/test_permission_matrix.py asserts
the two stay in agreement.
"""

from __future__ import annotations

from django.db import models


class Role(models.TextChoices):
    """A user's primary role. One per user, by design.

    Splitting technical power (DEVELOPER) from people power (SUPER_ADMIN) is
    deliberate: nobody should be able to both change how matching works and
    authorise who gets followed.
    """

    DEVELOPER = "developer", "Developer / Engineer"
    SUPER_ADMIN = "super_admin", "Super Admin"
    SECURITY_ADMIN = "security_admin", "Security Admin"
    HR = "hr", "HR"
    SECURITY_OPERATOR = "security_operator", "Security Operator"
    AUDITOR = "auditor", "Auditor"
    EMPLOYEE = "employee", "Employee (self-service)"


class Action:
    """Every gated capability in the system.

    String constants rather than Django model permissions because several of
    these are not CRUD on a single model (``TRACK_FOLLOW_LIVE``,
    ``HISTORY_DEPARTMENT``) and we want one uniform check.
    """

    # --- system / technical ------------------------------------------------
    CAMERA_MANAGE = "camera.manage"
    CAMERA_VIEW = "camera.view"
    PIPELINE_CONFIGURE = "pipeline.configure"
    PIPELINE_VIEW = "pipeline.view"
    DETECTION_VIEW_RAW = "detection.view_raw"
    SYSTEM_LOGS_VIEW = "system.logs_view"
    ZONE_MANAGE = "zone.manage"

    # --- identity ---------------------------------------------------------
    EMPLOYEE_MANAGE = "employee.manage"
    EMPLOYEE_VIEW = "employee.view"
    ENROLLMENT_MANAGE = "enrollment.manage"
    CONSENT_MANAGE = "consent.manage"
    CONSENT_VIEW = "consent.view"

    # --- tracking ---------------------------------------------------------
    LIVE_VIEW_ALL = "live.view_all"
    LIVE_VIEW_ASSIGNED = "live.view_assigned"
    TRACK_FOLLOW_LIVE = "track.follow_live"
    HISTORY_ANY = "history.any"
    HISTORY_DEPARTMENT = "history.department"
    HISTORY_OWN = "history.own"
    SNAPSHOT_VIEW = "snapshot.view"
    CLIP_VIEW = "clip.view"
    TRACKREQUEST_CREATE = "trackrequest.create"
    TRACKREQUEST_APPROVE = "trackrequest.approve"

    # --- alerts / reports -------------------------------------------------
    ALERTRULE_MANAGE = "alertrule.manage"
    ALERT_ACK = "alert.ack"
    REPORT_AGGREGATE_VIEW = "report.aggregate_view"
    REPORT_EXPORT = "report.export"

    # --- governance -------------------------------------------------------
    AUDIT_READ_ALL = "audit.read_all"
    AUDIT_READ_OWN = "audit.read_own"
    USER_MANAGE = "user.manage"
    RETENTION_MANAGE = "retention.manage"
    RETENTION_VIEW = "retention.view"
    PURGE_EXECUTE = "purge.execute"

    @classmethod
    def all(cls) -> frozenset[str]:
        return frozenset(
            value
            for name, value in vars(cls).items()
            if name.isupper() and isinstance(value, str)
        )


A = Action

# ---------------------------------------------------------------------------
# The matrix.
#
# Read this as the authoritative answer to "what can this role do?". Three
# capabilities are deliberately absent from DEVELOPER - approving a tracking
# request, managing users, and executing a purge - so that deep technical
# access can never be self-escalated into authority over people.
# ---------------------------------------------------------------------------

ROLE_ACTIONS: dict[str, frozenset[str]] = {
    Role.DEVELOPER: frozenset(
        {
            A.CAMERA_MANAGE,
            A.CAMERA_VIEW,
            A.PIPELINE_CONFIGURE,
            A.PIPELINE_VIEW,
            A.DETECTION_VIEW_RAW,
            A.SYSTEM_LOGS_VIEW,
            A.ZONE_MANAGE,
            A.EMPLOYEE_VIEW,
            A.LIVE_VIEW_ALL,
            A.HISTORY_ANY,
            A.HISTORY_OWN,
            A.SNAPSHOT_VIEW,
            A.CLIP_VIEW,
            A.ALERTRULE_MANAGE,
            A.ALERT_ACK,
            A.REPORT_AGGREGATE_VIEW,
            A.REPORT_EXPORT,
            A.AUDIT_READ_OWN,
            A.RETENTION_VIEW,
        }
    ),
    Role.SUPER_ADMIN: frozenset(
        Action.all()
        # Pipeline internals and raw biometric vectors stay with Developer.
        # An admin who needs a threshold changed asks for a change, which
        # leaves a record.
        - {A.PIPELINE_CONFIGURE, A.DETECTION_VIEW_RAW, A.SYSTEM_LOGS_VIEW}
    ),
    Role.SECURITY_ADMIN: frozenset(
        {
            A.CAMERA_MANAGE,
            A.CAMERA_VIEW,
            A.PIPELINE_VIEW,
            A.ZONE_MANAGE,
            A.EMPLOYEE_VIEW,
            A.LIVE_VIEW_ALL,
            A.TRACK_FOLLOW_LIVE,
            A.HISTORY_ANY,
            A.HISTORY_OWN,
            A.SNAPSHOT_VIEW,
            A.CLIP_VIEW,
            A.TRACKREQUEST_CREATE,
            A.TRACKREQUEST_APPROVE,
            A.ALERTRULE_MANAGE,
            A.ALERT_ACK,
            A.REPORT_AGGREGATE_VIEW,
            A.REPORT_EXPORT,
            A.AUDIT_READ_OWN,
            A.RETENTION_VIEW,
        }
    ),
    Role.HR: frozenset(
        {
            A.EMPLOYEE_MANAGE,
            A.EMPLOYEE_VIEW,
            A.ENROLLMENT_MANAGE,
            A.CONSENT_MANAGE,
            A.CONSENT_VIEW,
            # Own department only. Anything wider needs an approved
            # TrackingRequest - see PermissionService.can_view_history.
            A.HISTORY_DEPARTMENT,
            A.HISTORY_OWN,
            A.TRACKREQUEST_CREATE,
            A.REPORT_AGGREGATE_VIEW,
            A.REPORT_EXPORT,
            A.AUDIT_READ_OWN,
        }
    ),
    Role.SECURITY_OPERATOR: frozenset(
        {
            A.CAMERA_VIEW,
            A.LIVE_VIEW_ASSIGNED,
            A.SNAPSHOT_VIEW,
            A.CLIP_VIEW,
            A.ALERT_ACK,
            A.HISTORY_OWN,
            A.AUDIT_READ_OWN,
        }
    ),
    Role.AUDITOR: frozenset(
        {
            A.AUDIT_READ_ALL,
            A.CONSENT_VIEW,
            A.RETENTION_VIEW,
            A.REPORT_EXPORT,  # audit log exports only; see can_export()
            A.HISTORY_OWN,
        }
    ),
    Role.EMPLOYEE: frozenset(
        {
            A.HISTORY_OWN,
            A.AUDIT_READ_OWN,
            A.CONSENT_VIEW,  # own record only
        }
    ),
}

# ---------------------------------------------------------------------------
# Environment-conditional grants.
#
# A developer debugging the pipeline needs to see how matching behaves, not
# whose face it is. Outside production they get the full set; in production
# these three require a time-boxed, reasoned AccessElevation (break-glass).
# ---------------------------------------------------------------------------

DEVELOPER_NONPROD_EXTRA: frozenset[str] = frozenset(
    {
        A.EMPLOYEE_MANAGE,
        A.ENROLLMENT_MANAGE,
        A.TRACK_FOLLOW_LIVE,
    }
)

# Actions that can never be granted by break-glass elevation, because they
# are authority over people rather than access to data.
NEVER_ELEVATABLE: frozenset[str] = frozenset(
    {
        A.TRACKREQUEST_APPROVE,
        A.USER_MANAGE,
        A.PURGE_EXECUTE,
        A.RETENTION_MANAGE,
    }
)

# Roles that must have TOTP enrolled before any gated action is allowed.
# Mirrors settings.MFA_ENFORCED_ROLES; kept here so the matrix is readable
# in one place.
MFA_REQUIRED_ROLES: frozenset[str] = frozenset(
    {Role.DEVELOPER, Role.SUPER_ADMIN, Role.SECURITY_ADMIN, Role.HR}
)

# Actions that are safe without MFA even for an MFA-required role, so that a
# locked-out admin can still reach the pages needed to enrol a token.
MFA_EXEMPT_ACTIONS: frozenset[str] = frozenset({A.HISTORY_OWN, A.AUDIT_READ_OWN})


def actions_for_role(role: str, *, is_production: bool = True) -> frozenset[str]:
    """Base action set for a role, before scoping and elevation."""
    granted = ROLE_ACTIONS.get(role, frozenset())
    if role == Role.DEVELOPER and not is_production:
        granted = granted | DEVELOPER_NONPROD_EXTRA
    return granted
