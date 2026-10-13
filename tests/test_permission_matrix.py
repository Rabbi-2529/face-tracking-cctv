"""
Executable form of the role matrix in CCTV_TRACKING_PLAN.md section 7.

These tests are the point of the access design. If one of them fails, either
the matrix changed on purpose - and the plan document needs the same change -
or a permission regression has just been caught before it shipped.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.utils import timezone

from apps.common.permissions import PermissionService
from apps.common.roles import Action, Role, actions_for_role

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# The matrix, as data. (role, action, expected) - production environment.
# ---------------------------------------------------------------------------
MATRIX = [
    # Developer: deep technical access, no authority over people.
    (Role.DEVELOPER, Action.PIPELINE_CONFIGURE, True),
    (Role.DEVELOPER, Action.DETECTION_VIEW_RAW, True),
    (Role.DEVELOPER, Action.CAMERA_MANAGE, True),
    (Role.DEVELOPER, Action.HISTORY_ANY, True),
    (Role.DEVELOPER, Action.TRACKREQUEST_APPROVE, False),
    (Role.DEVELOPER, Action.USER_MANAGE, False),
    (Role.DEVELOPER, Action.PURGE_EXECUTE, False),
    (Role.DEVELOPER, Action.CONSENT_MANAGE, False),
    # In production a Developer cannot manage employees or follow people live.
    (Role.DEVELOPER, Action.EMPLOYEE_MANAGE, False),
    (Role.DEVELOPER, Action.TRACK_FOLLOW_LIVE, False),
    # Super Admin: full operational authority, but not pipeline internals or
    # raw biometric vectors.
    (Role.SUPER_ADMIN, Action.USER_MANAGE, True),
    (Role.SUPER_ADMIN, Action.TRACKREQUEST_APPROVE, True),
    (Role.SUPER_ADMIN, Action.PURGE_EXECUTE, True),
    (Role.SUPER_ADMIN, Action.HISTORY_ANY, True),
    (Role.SUPER_ADMIN, Action.PIPELINE_CONFIGURE, False),
    (Role.SUPER_ADMIN, Action.DETECTION_VIEW_RAW, False),
    # Security Admin: operational, but cannot manage users or retention.
    (Role.SECURITY_ADMIN, Action.TRACKREQUEST_APPROVE, True),
    (Role.SECURITY_ADMIN, Action.TRACK_FOLLOW_LIVE, True),
    (Role.SECURITY_ADMIN, Action.USER_MANAGE, False),
    (Role.SECURITY_ADMIN, Action.RETENTION_MANAGE, False),
    (Role.SECURITY_ADMIN, Action.DETECTION_VIEW_RAW, False),
    # HR: people administration, not surveillance.
    (Role.HR, Action.EMPLOYEE_MANAGE, True),
    (Role.HR, Action.ENROLLMENT_MANAGE, True),
    (Role.HR, Action.CONSENT_MANAGE, True),
    (Role.HR, Action.HISTORY_DEPARTMENT, True),
    (Role.HR, Action.TRACKREQUEST_CREATE, True),
    (Role.HR, Action.HISTORY_ANY, False),
    (Role.HR, Action.TRACK_FOLLOW_LIVE, False),
    (Role.HR, Action.TRACKREQUEST_APPROVE, False),
    (Role.HR, Action.CAMERA_MANAGE, False),
    (Role.HR, Action.CLIP_VIEW, False),
    # Security Operator: monitors now, does not browse history.
    (Role.SECURITY_OPERATOR, Action.LIVE_VIEW_ASSIGNED, True),
    (Role.SECURITY_OPERATOR, Action.ALERT_ACK, True),
    (Role.SECURITY_OPERATOR, Action.LIVE_VIEW_ALL, False),
    (Role.SECURITY_OPERATOR, Action.HISTORY_ANY, False),
    (Role.SECURITY_OPERATOR, Action.EMPLOYEE_VIEW, False),
    (Role.SECURITY_OPERATOR, Action.REPORT_EXPORT, False),
    # Auditor: reads the record, sees no footage.
    (Role.AUDITOR, Action.AUDIT_READ_ALL, True),
    (Role.AUDITOR, Action.CONSENT_VIEW, True),
    (Role.AUDITOR, Action.HISTORY_ANY, False),
    (Role.AUDITOR, Action.SNAPSHOT_VIEW, False),
    (Role.AUDITOR, Action.LIVE_VIEW_ALL, False),
    # Employee: own data only.
    (Role.EMPLOYEE, Action.HISTORY_OWN, True),
    (Role.EMPLOYEE, Action.AUDIT_READ_OWN, True),
    (Role.EMPLOYEE, Action.HISTORY_ANY, False),
    (Role.EMPLOYEE, Action.HISTORY_DEPARTMENT, False),
    (Role.EMPLOYEE, Action.EMPLOYEE_VIEW, False),
    (Role.EMPLOYEE, Action.SNAPSHOT_VIEW, False),
]


@pytest.mark.parametrize("role,action,expected", MATRIX)
def test_role_grants_match_the_documented_matrix(users, role, action, expected):
    user = next(u for u in users.values() if u.role == role)
    assert bool(PermissionService.can(user, action)) is expected, (
        f"{role} / {action}: expected {expected}. If this change is intended, "
        "update CCTV_TRACKING_PLAN.md section 7 as well."
    )


def test_no_role_holds_every_action():
    """No single role should be able to do everything.

    Separation of duties is the whole point: the role that can change how
    matching works must not also be the role that authorises who gets
    followed.
    """
    everything = Action.all()
    for role in Role.values:
        granted = actions_for_role(role, is_production=True)
        assert granted != everything, f"{role} holds every action"


def test_developer_cannot_self_escalate_to_authority_over_people(users):
    """The three capabilities a Developer must never reach, even unscoped."""
    developer = users["developer"]
    for action in (
        Action.TRACKREQUEST_APPROVE,
        Action.USER_MANAGE,
        Action.PURGE_EXECUTE,
        Action.RETENTION_MANAGE,
    ):
        assert not PermissionService.can(developer, action)


def test_developer_gets_identity_access_outside_production(users, settings):
    """Pseudonymised in production, full access in dev - and the reverse is
    the bug this guards against."""
    developer = users["developer"]

    settings.ENVIRONMENT = "production"
    assert not PermissionService.can(developer, Action.EMPLOYEE_MANAGE)

    settings.ENVIRONMENT = "development"
    assert PermissionService.can(developer, Action.EMPLOYEE_MANAGE)
    assert PermissionService.can(developer, Action.TRACK_FOLLOW_LIVE)
    # Still no authority over people, even in development.
    assert not PermissionService.can(developer, Action.TRACKREQUEST_APPROVE)


# ---------------------------------------------------------------------------
# History access: the resolution order that the whole design rests on.
# ---------------------------------------------------------------------------


def test_hr_sees_own_department_history(users, employees):
    assert PermissionService.can_view_history(users["hr"], employees["eng"])


def test_hr_is_blocked_outside_its_department_but_offered_the_request_path(
    users, employees
):
    decision = PermissionService.can_view_history(users["hr"], employees["ops"])
    assert not decision
    # The UI must be able to offer "request access" rather than just hiding it.
    assert decision.requires_approval
    assert "tracking request" in decision.reason


def test_unscoped_hr_sees_nothing_rather_than_everything(users, employees):
    """Fail closed. An unset scope is the most likely misconfiguration."""
    decision = PermissionService.can_view_history(users["hr_unscoped"], employees["eng"])
    assert not decision


def test_employee_sees_own_history_only(users, employees):
    staff = users["employee"]
    assert PermissionService.can_view_history(staff, employees["eng"])
    assert not PermissionService.can_view_history(staff, employees["ops"])


def test_approved_tracking_request_unlocks_one_subject_for_the_requester(
    users, employees
):
    from apps.watchlist.models import TrackingRequest

    hr = users["hr"]
    now = timezone.now()

    request = TrackingRequest.objects.create(
        employee=employees["ops"],
        requested_by=hr,
        purpose=TrackingRequest.Purpose.THEFT_INVESTIGATION,
        reason="Stock discrepancy in the Operations store on 3 October.",
        scope=TrackingRequest.Scope.BOTH,
        data_from=now - dt.timedelta(days=7),
        data_to=now,
    )
    # Pending is not enough.
    assert not PermissionService.can_view_history(hr, employees["ops"])

    request.approve(users["super_admin"], window_hours=24)
    assert PermissionService.can_view_history(hr, employees["ops"])
    assert PermissionService.can_follow_live(hr, employees["ops"])

    # It does not open the subject to other holders of the same role.
    assert not PermissionService.can_view_history(
        users["hr_unscoped"], employees["ops"]
    )


def test_expired_request_stops_granting_access(users, employees):
    from apps.watchlist.models import TrackingRequest

    hr = users["hr"]
    now = timezone.now()
    request = TrackingRequest.objects.create(
        employee=employees["ops"],
        requested_by=hr,
        purpose=TrackingRequest.Purpose.SECURITY_INCIDENT,
        reason="Badge-in anomaly reported by the night guard on 2 October.",
        scope=TrackingRequest.Scope.HISTORICAL,
        data_from=now - dt.timedelta(days=2),
        data_to=now,
    )
    request.approve(users["super_admin"], window_hours=1)

    # Wind the window into the past: the authorisation must stop working on
    # its own, with nobody revoking it.
    request.start_at = now - dt.timedelta(hours=3)
    request.end_at = now - dt.timedelta(hours=2)
    request.save(update_fields=["start_at", "end_at"])

    assert not PermissionService.can_view_history(hr, employees["ops"])


def test_requester_cannot_approve_their_own_request(users, employees):
    from django.core.exceptions import ValidationError

    from apps.watchlist.models import TrackingRequest

    admin = users["super_admin"]
    now = timezone.now()
    request = TrackingRequest.objects.create(
        employee=employees["ops"],
        requested_by=admin,
        purpose=TrackingRequest.Purpose.AUDIT,
        reason="Quarterly access review for the Operations department.",
        scope=TrackingRequest.Scope.HISTORICAL,
        data_from=now - dt.timedelta(days=1),
        data_to=now,
    )
    with pytest.raises(ValidationError):
        request.approve(admin)


def test_revoking_a_request_ends_access_immediately(users, employees):
    from apps.watchlist.models import TrackingRequest

    hr = users["hr"]
    now = timezone.now()
    request = TrackingRequest.objects.create(
        employee=employees["ops"],
        requested_by=hr,
        purpose=TrackingRequest.Purpose.HR_GRIEVANCE,
        reason="Grievance filed 1 October naming the Operations store room.",
        scope=TrackingRequest.Scope.BOTH,
        data_from=now - dt.timedelta(days=3),
        data_to=now,
    )
    request.approve(users["super_admin"], window_hours=48)
    assert PermissionService.can_view_history(hr, employees["ops"])

    request.revoke(users["super_admin"], note="Resolved without CCTV.")
    assert not PermissionService.can_view_history(hr, employees["ops"])


# ---------------------------------------------------------------------------
# Break-glass elevation
# ---------------------------------------------------------------------------


def test_elevation_grants_a_capability_temporarily(users):
    from apps.accounts.models import AccessElevation

    developer = users["developer"]
    assert not PermissionService.can(developer, Action.EMPLOYEE_MANAGE)

    now = timezone.now()
    AccessElevation.objects.create(
        user=developer,
        actions=[Action.EMPLOYEE_MANAGE],
        reason="Debugging duplicate enrolment reported in ticket OPS-412.",
        status=AccessElevation.Status.ACTIVE,
        approved_by=users["super_admin"],
        approved_at=now,
        starts_at=now - dt.timedelta(minutes=1),
        expires_at=now + dt.timedelta(minutes=30),
    )
    assert PermissionService.can(developer, Action.EMPLOYEE_MANAGE)


def test_elevation_cannot_grant_authority_over_people(users):
    from apps.accounts.models import AccessElevation

    developer = users["developer"]
    now = timezone.now()
    AccessElevation.objects.create(
        user=developer,
        actions=[Action.TRACKREQUEST_APPROVE, Action.USER_MANAGE, Action.PURGE_EXECUTE],
        reason="Attempting to self-escalate; this must not work.",
        status=AccessElevation.Status.ACTIVE,
        approved_by=users["super_admin"],
        approved_at=now,
        starts_at=now - dt.timedelta(minutes=1),
        expires_at=now + dt.timedelta(minutes=30),
    )
    assert not PermissionService.can(developer, Action.TRACKREQUEST_APPROVE)
    assert not PermissionService.can(developer, Action.USER_MANAGE)
    assert not PermissionService.can(developer, Action.PURGE_EXECUTE)


def test_expired_elevation_grants_nothing(users):
    from apps.accounts.models import AccessElevation

    developer = users["developer"]
    now = timezone.now()
    AccessElevation.objects.create(
        user=developer,
        actions=[Action.EMPLOYEE_MANAGE],
        reason="Window has already passed.",
        status=AccessElevation.Status.ACTIVE,
        approved_by=users["super_admin"],
        starts_at=now - dt.timedelta(hours=3),
        expires_at=now - dt.timedelta(hours=2),
    )
    assert not PermissionService.can(developer, Action.EMPLOYEE_MANAGE)


# ---------------------------------------------------------------------------
# MFA and account state
# ---------------------------------------------------------------------------


def test_mfa_required_roles_are_blocked_until_enrolled(users, settings):
    settings.MFA_ENFORCED = True
    hr = users["hr"]

    assert not PermissionService.can(hr, Action.EMPLOYEE_MANAGE)
    # Self-service stays reachable so a locked-out user can enrol a token.
    assert PermissionService.can(hr, Action.HISTORY_OWN)


def test_operator_role_does_not_require_mfa(users, settings):
    settings.MFA_ENFORCED = True
    assert PermissionService.can(users["operator"], Action.LIVE_VIEW_ASSIGNED)


def test_deactivated_account_loses_everything(users):
    admin = users["super_admin"]
    admin.is_active = False
    admin.save(update_fields=["is_active"])
    assert not PermissionService.can(admin, Action.USER_MANAGE)
    assert PermissionService.granted_actions(admin) == frozenset()


def test_anonymous_user_gets_nothing():
    from django.contrib.auth.models import AnonymousUser

    anon = AnonymousUser()
    assert PermissionService.granted_actions(anon) == frozenset()
    assert not PermissionService.can(anon, Action.HISTORY_OWN)


# ---------------------------------------------------------------------------
# Export and media rules
# ---------------------------------------------------------------------------


def test_export_kinds_are_separated_by_role(users):
    assert PermissionService.can_export(users["hr"], "aggregate")
    assert not PermissionService.can_export(users["hr"], "identity")
    assert not PermissionService.can_export(users["hr"], "audit")

    assert PermissionService.can_export(users["auditor"], "audit")
    assert not PermissionService.can_export(users["auditor"], "identity")

    assert PermissionService.can_export(users["super_admin"], "identity")

    assert not PermissionService.can_export(users["operator"], "aggregate")


def test_operator_clip_access_is_limited_to_24_hours(users):
    operator = users["operator"]
    recent = timezone.now() - dt.timedelta(hours=2)
    old = timezone.now() - dt.timedelta(days=3)

    assert PermissionService.can_view_clip(operator, recent)
    assert not PermissionService.can_view_clip(operator, old)
    # An admin is not restricted in the same way.
    assert PermissionService.can_view_clip(users["super_admin"], old)
