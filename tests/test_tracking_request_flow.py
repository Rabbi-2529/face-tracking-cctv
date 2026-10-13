"""
The tracking-request workflow, through the views.

Covers a bug worth a permanent test: the request form originally restricted
its subject list to employees the requester could already see, which made the
gate useless - HR could only request access to people whose history it could
already read. The subject list must span the organisation; the *approval* is
what gates the history.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.watchlist.models import TrackingRequest

pytestmark = pytest.mark.django_db


def _request_payload(employee, **overrides) -> dict:
    now = timezone.localtime()
    payload = {
        "employee": employee.pk,
        "purpose": TrackingRequest.Purpose.THEFT_INVESTIGATION,
        "reason": (
            "Stock discrepancy reported in the Operations store room on "
            "7 October; need to establish who entered."
        ),
        "scope": TrackingRequest.Scope.BOTH,
        "data_from": (now - dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M"),
        "data_to": now.strftime("%Y-%m-%dT%H:%M"),
        "window_hours": 48,
    }
    payload.update(overrides)
    return payload


def test_hr_can_name_a_subject_outside_its_directory_scope(client, users, employees):
    """The regression test for the form-scope bug.

    The Operations employee is outside HR's department scope, which is
    exactly the case the request mechanism exists for.
    """
    client.force_login(users["hr"])
    response = client.post(
        reverse("watchlist:create"), _request_payload(employees["ops"])
    )

    assert response.status_code == 302, (
        "form rejected a subject outside the requester's scope; the subject "
        "list must span the organisation"
    )
    created = TrackingRequest.objects.get(employee=employees["ops"])
    assert created.status == TrackingRequest.Status.PENDING
    assert created.requested_by == users["hr"]


def test_a_thin_reason_is_rejected(client, users, employees):
    client.force_login(users["hr"])
    response = client.post(
        reverse("watchlist:create"),
        _request_payload(employees["ops"], reason="because"),
    )
    assert response.status_code == 200  # re-rendered with errors
    assert not TrackingRequest.objects.exists()


def test_suppressing_notice_requires_a_written_reason(client, users, employees):
    client.force_login(users["hr"])
    response = client.post(
        reverse("watchlist:create"),
        _request_payload(
            employees["ops"], notification_suppressed=True, suppression_reason=""
        ),
    )
    assert response.status_code == 200
    assert not TrackingRequest.objects.exists()


def test_pending_request_grants_nothing(client, users, employees):
    client.force_login(users["hr"])
    client.post(reverse("watchlist:create"), _request_payload(employees["ops"]))

    response = client.get(
        reverse("tracking:employee_timeline", args=[employees["ops"].uuid])
    )
    # Redirected back to the request form rather than shown the timeline.
    assert response.status_code == 302


def test_requester_cannot_approve_through_the_view(client, users, employees):
    client.force_login(users["hr"])
    client.post(reverse("watchlist:create"), _request_payload(employees["ops"]))
    tracking_request = TrackingRequest.objects.get(employee=employees["ops"])

    response = client.post(
        reverse("watchlist:decide", args=[tracking_request.uuid]),
        {"decision": "approve", "window_hours": 48},
    )
    # HR holds no approve action at all, so this is refused outright.
    assert response.status_code == 403
    tracking_request.refresh_from_db()
    assert tracking_request.status == TrackingRequest.Status.PENDING


def test_approval_then_revocation_opens_and_closes_access(client, users, employees):
    client.force_login(users["hr"])
    client.post(reverse("watchlist:create"), _request_payload(employees["ops"]))
    tracking_request = TrackingRequest.objects.get(employee=employees["ops"])
    timeline = reverse("tracking:employee_timeline", args=[employees["ops"].uuid])

    # Approve as the administrator.
    client.force_login(users["super_admin"])
    response = client.post(
        reverse("watchlist:decide", args=[tracking_request.uuid]),
        {"decision": "approve", "window_hours": 48, "note": "Approved."},
    )
    assert response.status_code == 302
    tracking_request.refresh_from_db()
    assert tracking_request.status == TrackingRequest.Status.APPROVED
    assert tracking_request.is_active

    # HR can now read the subject's timeline.
    client.force_login(users["hr"])
    assert client.get(timeline).status_code == 200

    # Revoke, and access closes again immediately.
    client.force_login(users["super_admin"])
    client.post(
        reverse("watchlist:decide", args=[tracking_request.uuid]),
        {"decision": "revoke", "note": "Resolved without CCTV."},
    )
    client.force_login(users["hr"])
    assert client.get(timeline).status_code == 302


def test_expiry_task_closes_lapsed_requests_and_sessions(users, employees):
    from apps.watchlist.models import TrackSession
    from apps.watchlist.tasks import expire_tracking_requests

    now = timezone.now()
    tracking_request = TrackingRequest.objects.create(
        employee=employees["ops"],
        requested_by=users["hr"],
        purpose=TrackingRequest.Purpose.SECURITY_INCIDENT,
        reason="Badge-in anomaly reported by the night guard on 2 October.",
        scope=TrackingRequest.Scope.LIVE,
        data_from=now - dt.timedelta(days=2),
        data_to=now,
        status=TrackingRequest.Status.APPROVED,
        approved_by=users["super_admin"],
        approved_at=now - dt.timedelta(hours=3),
        start_at=now - dt.timedelta(hours=3),
        end_at=now - dt.timedelta(hours=1),  # already lapsed
    )
    session = TrackSession.objects.create(
        request=tracking_request, opened_by=users["hr"]
    )

    result = expire_tracking_requests()

    tracking_request.refresh_from_db()
    session.refresh_from_db()
    assert tracking_request.status == TrackingRequest.Status.EXPIRED
    assert session.ended_at is not None, "a live session outlived its authorisation"
    assert result["expired"] == 1


def test_audit_rows_are_written_for_grant_and_denial(client, users, employees):
    from apps.accounts.models import AuditLog

    timeline = reverse("tracking:employee_timeline", args=[employees["ops"].uuid])

    client.force_login(users["hr"])
    client.get(timeline)  # denied
    client.get(reverse("tracking:employee_timeline", args=[employees["eng"].uuid]))

    actions = set(AuditLog.objects.values_list("action", flat=True))
    assert "history.denied" in actions, "a refused identity read was not audited"
    assert "history.view" in actions, "a permitted identity read was not audited"

    denial = AuditLog.objects.filter(action="history.denied").first()
    assert denial.success is False
    assert denial.target_employee_code == employees["ops"].employee_code
    assert denial.reason
