"""Dual-authorisation purge: two Super Admins, exact scope, audit survives."""

from __future__ import annotations

import datetime as dt

import pytest
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.urls import reverse
from django.utils import timezone


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.STORAGES = {
        **settings.STORAGES,
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": str(tmp_path), "base_url": None},
        },
    }


@pytest.fixture
def second_admin(db):
    from apps.accounts.models import User

    return User.objects.create_user(
        email="admin2@test.local", password="TestPassw0rd!123", full_name="Second Admin", role="super_admin"
    )


@pytest.fixture
def history(org, employees):
    """Presence and detections for both employees, today and 10 days ago."""
    from django.core.files.storage import default_storage

    from apps.cameras.models import Camera
    from apps.tracking.models import Detection, MovementEvent, PresenceInterval

    camera = Camera.objects.create(code="cam-o", name="o", zone=org["office"], rtsp_url="rtsp://x/1")
    rows = {}
    now = timezone.now()
    for key, employee in employees.items():
        for label, at in (("today", now - dt.timedelta(hours=1)), ("old", now - dt.timedelta(days=10))):
            snap = default_storage.save(f"snapshots/{key}-{label}.jpg", ContentFile(b"jpg"))
            rows[(key, label, "interval")] = PresenceInterval.objects.create(
                employee=employee, zone=org["office"], entered_at=at, last_seen_at=at,
                exited_at=at, first_snapshot_key=snap,
            )
            rows[(key, label, "detection")] = Detection.objects.create(
                camera=camera, captured_at=at, matched_employee=employee, snapshot_key=snap,
                confidence="confirmed", match_score=0.8,
            )
            MovementEvent.objects.create(employee=employee, to_zone=org["office"], occurred_at=at)
    return rows


def _request(admin, employee=None, days_back=None, kinds=("movement", "detections")):
    from apps.tracking import purge

    today = timezone.localdate()
    return purge.create(
        admin,
        employee=employee,
        date_from=today - dt.timedelta(days=days_back) if days_back is not None else None,
        date_to=today if days_back is not None else None,
        kinds=list(kinds),
        reason="Erasure request from the employee, ticket DP-42.",
    )


# ---------------------------------------------------------------- access


def test_only_super_admins_reach_purge(client, users):
    for key in ("hr", "developer", "security_admin", "auditor"):
        client.force_login(users[key])
        assert client.get(reverse("tracking:purge_list")).status_code == 403
    client.force_login(users["super_admin"])
    assert client.get(reverse("tracking:purge_list")).status_code == 200


def test_break_glass_can_never_grant_purge(users):
    from apps.accounts.models import AccessElevation
    from apps.common.permissions import PermissionService
    from apps.common.roles import Action

    now = timezone.now()
    AccessElevation.objects.create(
        user=users["developer"], actions=[Action.PURGE_EXECUTE], reason="x" * 30,
        status=AccessElevation.Status.ACTIVE, starts_at=now - dt.timedelta(minutes=1),
        expires_at=now + dt.timedelta(hours=1),
    )
    assert not PermissionService.can(users["developer"], Action.PURGE_EXECUTE)


# ------------------------------------------------------------ validation


@pytest.mark.parametrize(
    ("employee", "dates", "kinds", "reason"),
    [
        (None, False, ["movement"], "A good long reason for the purge."),       # everyone, all time
        (None, True, ["face_templates"], "A good long reason for the purge."),  # templates need a person
        ("eng", False, [], "A good long reason for the purge."),                # nothing chosen
        ("eng", False, ["movement"], "short"),                                  # thin reason
    ],
)
def test_invalid_requests_are_refused(users, employees, employee, dates, kinds, reason):
    from apps.tracking import purge

    today = timezone.localdate()
    with pytest.raises(ValidationError):
        purge.create(
            users["super_admin"],
            employee=employees[employee] if employee else None,
            date_from=today - dt.timedelta(days=1) if dates else None,
            date_to=today if dates else None,
            kinds=kinds,
            reason=reason,
        )


# ------------------------------------------------------------- the rule


def test_requester_cannot_approve_their_own_purge(users, employees, history):
    from apps.tracking import purge
    from apps.tracking.models import PresenceInterval

    request = _request(users["super_admin"], employees["eng"])
    with pytest.raises(ValidationError):
        purge.approve_and_execute(request, users["super_admin"])
    assert PresenceInterval.objects.filter(employee=employees["eng"]).count() == 2


def test_approved_purge_deletes_exactly_the_scope(
    users, second_admin, employees, history, django_capture_on_commit_callbacks
):
    from django.core.files.storage import default_storage

    from apps.accounts.models import AuditLog
    from apps.tracking import purge
    from apps.tracking.models import Detection, MovementEvent, PresenceInterval

    eng, ops = employees["eng"], employees["ops"]
    request = _request(users["super_admin"], eng, days_back=2)
    assert request.preview["presence intervals"] == 1
    audit_before = AuditLog.objects.count()

    with django_capture_on_commit_callbacks(execute=True):
        done = purge.approve_and_execute(request, second_admin, "Confirmed with the DPO.")

    assert done.status == "executed"
    assert done.result["presence intervals"] == 1 and done.result["detections"] == 1
    # Today's data for eng is gone; eng's older data and all of ops's remain.
    assert PresenceInterval.objects.filter(employee=eng).count() == 1
    assert PresenceInterval.objects.filter(employee=ops).count() == 2
    assert Detection.objects.filter(matched_employee=eng).count() == 1
    assert MovementEvent.objects.filter(employee=ops).count() == 2
    # The image of the purged detection is gone; the others are not.
    assert not default_storage.exists("snapshots/eng-today.jpg")
    assert default_storage.exists("snapshots/eng-old.jpg")
    assert default_storage.exists("snapshots/ops-today.jpg")
    done.refresh_from_db()
    assert done.result["image files"] >= 1
    # The audit log only grows.
    assert AuditLog.objects.count() > audit_before
    assert AuditLog.objects.filter(action="purge.executed").exists()


def test_snapshots_only_keeps_the_records(users, second_admin, employees, history, django_capture_on_commit_callbacks):
    from django.core.files.storage import default_storage

    from apps.tracking import purge
    from apps.tracking.models import Detection, PresenceInterval

    request = _request(users["super_admin"], employees["ops"], kinds=["snapshots"])
    with django_capture_on_commit_callbacks(execute=True):
        purge.approve_and_execute(request, second_admin)

    assert Detection.objects.filter(matched_employee=employees["ops"], snapshot_key="").count() == 2
    assert PresenceInterval.objects.filter(employee=employees["ops"]).count() == 2
    assert not PresenceInterval.objects.filter(employee=employees["ops"]).exclude(first_snapshot_key="").exists()
    assert not default_storage.exists("snapshots/ops-old.jpg")


def test_face_templates_are_purged_for_the_person(users, second_admin, employees):
    from apps.employees.models import FaceEnrollment
    from apps.tracking import purge

    request = _request(users["super_admin"], employees["eng"], kinds=["face_templates"])
    purge.approve_and_execute(request, second_admin)
    assert not FaceEnrollment.objects.filter(employee=employees["eng"]).exists()
    assert FaceEnrollment.objects.filter(employee=employees["ops"]).exists()


def test_reject_needs_a_note_and_cancel_is_for_the_requester(users, second_admin, employees):
    from apps.tracking import purge

    request = _request(users["super_admin"], employees["eng"])
    with pytest.raises(ValidationError):
        purge.reject(request, second_admin, "")
    with pytest.raises(ValidationError):
        purge.cancel(request, second_admin)
    purge.reject(request, second_admin, "Not an erasure case.")
    request.refresh_from_db()
    assert request.status == "rejected"
    with pytest.raises(ValidationError):
        purge.approve_and_execute(request, second_admin)


def test_undecided_requests_expire(users, second_admin, employees):
    from apps.tracking import purge
    from apps.tracking.models import PurgeRequest

    request = _request(users["super_admin"], employees["eng"])
    PurgeRequest.objects.filter(pk=request.pk).update(created_at=timezone.now() - dt.timedelta(days=8))
    with pytest.raises(ValidationError):
        purge.approve_and_execute(request, second_admin)
    request.refresh_from_db()
    assert request.status == "expired"


# ----------------------------------------------------------------- views


def test_request_and_approve_through_the_screens(client, users, second_admin, employees, history):
    from apps.tracking.models import PresenceInterval, PurgeRequest

    client.force_login(users["super_admin"])
    form = {
        "employee": employees["eng"].pk, "date_from": "", "date_to": "",
        "kinds": ["movement"], "reason": "Erasure request from the employee, ticket DP-42.",
    }
    preview = client.post(reverse("tracking:purge_create"), {**form, "action": "preview"})
    assert preview.context["preview"]["presence intervals"] == 2
    assert not PurgeRequest.objects.exists()

    client.post(reverse("tracking:purge_create"), {**form, "action": "submit"})
    request = PurgeRequest.objects.get()
    url = reverse("tracking:purge_detail", args=[request.uuid])

    client.force_login(second_admin)
    client.post(url, {"action": "approve", "confirm": "yes"})
    request.refresh_from_db()
    assert request.status == "pending", "approval needs DELETE typed"

    client.post(url, {"action": "approve", "confirm": "DELETE"})
    request.refresh_from_db()
    assert request.status == "executed"
    assert not PresenceInterval.objects.filter(employee=employees["eng"]).exists()
