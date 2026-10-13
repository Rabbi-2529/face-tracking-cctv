"""
Phase 5: governance and hardening.

MFA verification at sign-in, tracking-request data windows, live-follow
termination, the alert engine and inbox, the remaining retention jobs, and
the self-service additions.
"""

from __future__ import annotations

import datetime as dt
import io
from zoneinfo import ZoneInfo

import pytest
from django.core.files.base import ContentFile
from django.urls import reverse
from django.utils import timezone

from apps.common.permissions import PermissionService
from apps.common.roles import Action

PASSWORD = "TestPassw0rd!123"
IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture(autouse=True)
def _isolation(settings, tmp_path, monkeypatch):
    """Private media, no channel layer, and a fresh alert-rule cache."""
    from apps.alerts.engine import clear_rule_cache

    settings.STORAGES = {
        **settings.STORAGES,
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": str(tmp_path), "base_url": None},
        },
    }
    monkeypatch.setattr("channels.layers.get_channel_layer", lambda *a, **k: None)
    clear_rule_cache()
    yield
    clear_rule_cache()


def _audit(action: str):
    from apps.accounts.models import AuditLog

    return AuditLog.objects.filter(action=action)


# ======================================================================= MFA


def _totp(user):
    from django_otp.plugins.otp_totp.models import TOTPDevice

    return TOTPDevice.objects.create(user=user, name="phone", confirmed=True)


def _token(device) -> str:
    from django_otp.oath import TOTP

    totp = TOTP(device.bin_key, device.step, device.t0, device.digits, device.drift)
    return f"{totp.token():0{device.digits}d}"


def _sign_in(client, user):
    return client.post(
        reverse("accounts:login"), {"username": user.email, "password": PASSWORD}
    )


def test_unverified_user_object_is_refused_when_mfa_is_enforced(users, settings):
    """A user object that never went through OTP middleware - a socket, a
    script - has no is_verified() and must count as unverified."""
    settings.MFA_ENFORCED = True
    admin = users["super_admin"]
    _totp(admin)

    assert not PermissionService.can(admin, Action.USER_MANAGE)
    admin.is_verified = lambda: True
    assert PermissionService.can(admin, Action.USER_MANAGE)


def test_sign_in_requires_the_second_factor(client, users, settings):
    settings.MFA_ENFORCED = True
    admin = users["super_admin"]
    device = _totp(admin)

    response = _sign_in(client, admin)
    assert response.status_code == 302
    assert response["Location"].startswith(reverse("accounts:mfa_verify"))

    # Password alone does not open gated pages.
    assert client.get(reverse("useradmin:list")).status_code == 403

    response = client.post(reverse("accounts:mfa_verify"), {"token": _token(device)})
    assert response.status_code == 302
    assert client.get(reverse("useradmin:list")).status_code == 200
    assert _audit("auth.mfa_verified").exists()


def test_a_wrong_code_is_refused_and_audited(client, users, settings):
    settings.MFA_ENFORCED = True
    admin = users["super_admin"]
    _totp(admin)
    _sign_in(client, admin)

    client.post(reverse("accounts:mfa_verify"), {"token": "000000"})
    assert _audit("auth.mfa_failed").filter(success=False).exists()
    assert client.get(reverse("useradmin:list")).status_code == 403


def test_backup_code_works_exactly_once(client, users, settings):
    from apps.accounts.views import _issue_backup_codes

    settings.MFA_ENFORCED = True
    admin = users["super_admin"]
    _totp(admin)
    code = _issue_backup_codes(admin)[0]

    _sign_in(client, admin)
    client.post(reverse("accounts:mfa_verify"), {"token": code})
    assert client.get(reverse("useradmin:list")).status_code == 200

    client.logout()
    _sign_in(client, admin)
    client.post(reverse("accounts:mfa_verify"), {"token": code})
    assert client.get(reverse("useradmin:list")).status_code == 403


def test_enrolling_issues_backup_codes_and_verifies_the_session(client, users, settings):
    from django_otp.plugins.otp_static.models import StaticToken
    from django_otp.plugins.otp_totp.models import TOTPDevice

    settings.MFA_ENFORCED = True
    hr = users["hr"]
    client.force_login(hr)
    client.get(reverse("accounts:mfa_setup"))
    device = TOTPDevice.objects.get(user=hr, confirmed=False)

    response = client.post(reverse("accounts:mfa_setup"), {"token": _token(device)})
    assert response.status_code == 200
    assert len(response.context["codes"]) == 10
    assert StaticToken.objects.filter(device__user=hr).count() == 10
    # Verified by the enrolment itself: gated pages open straight away.
    assert client.get(reverse("tracking:employee_list")).status_code == 200


def test_admin_can_reset_someone_elses_authenticator_only(client, users):
    from django_otp.plugins.otp_totp.models import TOTPDevice

    admin, hr = users["super_admin"], users["hr"]
    _totp(hr)
    _totp(admin)
    client.force_login(admin)

    client.post(reverse("useradmin:mfa_reset", args=[hr.uuid]), {"reason": "phone lost, ID checked"})
    assert not TOTPDevice.objects.filter(user=hr).exists()
    assert _audit("user.mfa_reset").exists()

    client.post(reverse("useradmin:mfa_reset", args=[admin.uuid]), {"reason": "my own phone"})
    assert TOTPDevice.objects.filter(user=admin).exists()


def test_socket_verification_follows_the_session(users):
    from django_otp import DEVICE_ID_SESSION_KEY

    from apps.accounts.channels_otp import device_for_session

    admin, hr = users["super_admin"], users["hr"]
    device = _totp(admin)
    session = {DEVICE_ID_SESSION_KEY: device.persistent_id}

    assert device_for_session(admin, session) == device
    # Another user's device in the session verifies nobody.
    assert device_for_session(hr, session) is None
    assert device_for_session(admin, {}) is None


# ============================================================== data windows


def _window_request(users, employees, *, scope="historical", days=(3, 2)):
    from apps.watchlist.models import TrackingRequest

    now = timezone.now()
    tr = TrackingRequest.objects.create(
        employee=employees["ops"],
        requested_by=users["hr"],
        purpose=TrackingRequest.Purpose.THEFT_INVESTIGATION,
        reason="A laptop went missing from the stores room on that day.",
        scope=scope,
        data_from=now - dt.timedelta(days=days[0]),
        data_to=now - dt.timedelta(days=days[1]),
    )
    tr.approve(users["super_admin"], window_hours=4)
    return tr


def _interval(employee, zone, at, **extra):
    from apps.tracking.models import PresenceInterval

    return PresenceInterval.objects.create(
        employee=employee,
        zone=zone,
        entered_at=at,
        last_seen_at=at + dt.timedelta(minutes=5),
        exited_at=at + dt.timedelta(minutes=5),
        confidence="confirmed",
        **extra,
    )


def test_request_access_is_limited_to_its_data_window(users, employees, org, presence):
    from apps.common.scoping import visible_presence
    from apps.tracking.models import PresenceInterval

    hr, ops = users["hr"], employees["ops"]
    tr = _window_request(users, employees)
    inside = _interval(ops, org["office"], tr.data_from + dt.timedelta(hours=2))

    assert PermissionService.can_view_history(hr, ops)
    assert PermissionService.can_view_history(hr, ops, at=inside.entered_at)
    outside = PermissionService.can_view_history(hr, ops, at=timezone.now())
    assert not outside and "data window" in outside.reason
    assert PermissionService.history_windows(hr, ops) == [(tr.data_from, tr.data_to)]

    rows = visible_presence(hr, PresenceInterval.objects.filter(employee=ops))
    # The presence fixture's rows are from the last hour: outside the window.
    assert list(rows) == [inside]


def test_live_only_request_opens_no_history(users, employees):
    hr, ops = users["hr"], employees["ops"]
    _window_request(users, employees, scope="live")

    assert not PermissionService.can_view_history(hr, ops)
    assert PermissionService.can_follow_live(hr, ops)


def test_timeline_shows_only_the_approved_window(client, users, employees, org):
    hr, ops = users["hr"], employees["ops"]
    tr = _window_request(users, employees)
    inside = _interval(ops, org["office"], tr.data_from + dt.timedelta(hours=2))
    _interval(ops, org["reception"], tr.data_from - dt.timedelta(hours=6))

    client.force_login(hr)
    url = reverse("tracking:employee_timeline", args=[ops.uuid])
    response = client.get(url + f"?date={timezone.localtime(inside.entered_at).date()}")
    assert [i.pk for i in response.context["intervals"]] == [inside.pk]
    assert response.context["access_windows"]

    # Without a date it opens on the approved window, not on an empty today.
    response = client.get(url)
    assert response.context["day"] <= timezone.localtime(tr.data_to).date()


def test_evidence_outside_the_window_is_refused(client, users, employees, org, settings):
    from apps.accounts.models import AccessElevation

    hr, ops = users["hr"], employees["ops"]
    tr = _window_request(users, employees)
    outside = _interval(ops, org["office"], timezone.now() - dt.timedelta(hours=1))
    outside.first_snapshot_key = "snapshots/x.jpg"
    outside.save()

    # Give HR snapshot access for this test, so only the window decides.
    now = timezone.now()
    AccessElevation.objects.create(
        user=hr,
        actions=[Action.SNAPSHOT_VIEW],
        reason="test",
        status=AccessElevation.Status.ACTIVE,
        starts_at=now - dt.timedelta(minutes=1),
        expires_at=now + dt.timedelta(hours=1),
    )
    assert tr.is_active
    client.force_login(hr)
    response = client.get(reverse("tracking:interval_snapshot", args=[outside.pk, "first"]))
    assert response.status_code == 403


# =============================================================== live follow


def test_revoking_disconnects_open_sessions(users, employees, monkeypatch):
    from apps.watchlist.models import TrackSession

    tr = _window_request(users, employees, scope="live")
    session = TrackSession.objects.create(
        request=tr, opened_by=users["hr"], channel_name="specific.abc!123"
    )

    sent: list[tuple[str, dict]] = []

    class _Layer:
        async def send(self, channel, message):
            sent.append((channel, message))

    monkeypatch.setattr("channels.layers.get_channel_layer", lambda *a, **k: _Layer())

    tr.revoke(users["super_admin"], note="investigation closed")

    assert sent == [("specific.abc!123", {"type": "session.terminate"})]
    session.refresh_from_db()
    assert session.ended_at is not None


def test_live_follow_page_shows_nothing_from_before_the_approval(
    client, users, employees, org
):
    from apps.tracking.models import MovementEvent

    ops = employees["ops"]
    before = timezone.now() - dt.timedelta(minutes=30)
    MovementEvent.objects.create(employee=ops, to_zone=org["office"], occurred_at=before)
    tr = _window_request(users, employees, scope="live")

    client.force_login(users["hr"])
    response = client.get(reverse("watchlist:follow", args=[tr.uuid]))
    assert response.status_code == 200
    assert response.context["movements"] == []
    assert response.context["current"] is None


# ==================================================================== alerts


@pytest.fixture
def server_room(org):
    """The office, made restricted, with a camera."""
    from apps.cameras.models import Camera

    office = org["office"]
    office.is_restricted = True
    office.save(update_fields=["is_restricted"])
    camera = Camera.objects.create(
        code="cam-office",
        name="office door",
        zone=office,
        purpose=Camera.Purpose.IDENTITY,
        rtsp_url="rtsp://u:p@10.0.0.1/office",
        measured_face_width_px=110,
    )
    return {"zone": office, "camera": camera}


def _rule(rule_type, **fields):
    from apps.alerts.models import AlertRule

    m2m = {k: fields.pop(k) for k in list(fields) if k.startswith(("authorised_", "applies_"))}
    rule = AlertRule.objects.create(name=rule_type, rule_type=rule_type, **fields)
    for name, values in m2m.items():
        getattr(rule, name).set(values)
    return rule


def _alerts():
    from apps.alerts.models import Alert

    return Alert.objects.all()


def test_unauthorised_entry_to_a_restricted_zone_alerts(employees, org, server_room):
    from apps.alerts.engine import on_confirmed_presence

    _rule("restricted_zone", authorised_departments=[org["engineering"]])
    now = timezone.now()

    on_confirmed_presence(_interval(employees["eng"], server_room["zone"], now))
    assert not _alerts().exists(), "Engineering is authorised"

    on_confirmed_presence(_interval(employees["ops"], server_room["zone"], now))
    alert = _alerts().get()
    assert alert.employee == employees["ops"]
    assert "Operations" not in alert.message and employees["ops"].full_name not in alert.message


def test_repeat_triggers_fold_into_one_alert(employees, server_room):
    from apps.alerts.engine import on_confirmed_presence

    _rule("restricted_zone")
    now = timezone.now()
    on_confirmed_presence(_interval(employees["ops"], server_room["zone"], now))
    on_confirmed_presence(
        _interval(employees["ops"], server_room["zone"], now + dt.timedelta(minutes=5))
    )

    alert = _alerts().get()
    assert alert.occurrences == 2


def test_probable_presence_alerts_only_once_confirmed(employees, server_room):
    """Through the aggregator: a probable sighting raises nothing; the same
    interval upgraded to confirmed does."""
    from apps.tracking.aggregator import DetectionAggregator

    _rule("restricted_zone")
    camera = server_room["camera"]
    now = timezone.now()

    def event(confidence, at):
        return {
            "v": 1, "camera_code": camera.code, "captured_at": at.isoformat(),
            "track_id": "t", "employee_code": employees["ops"].employee_code,
            "match_score": 0.4, "confidence": confidence, "snapshot_key": "",
        }

    aggregator = DetectionAggregator()
    aggregator.handle(event("probable", now))
    assert not _alerts().exists()

    aggregator.handle(event("confirmed", now + dt.timedelta(seconds=10)))
    assert _alerts().count() == 1


@pytest.mark.parametrize(
    ("local", "expected"),
    [
        (dt.datetime(2026, 10, 14, 23, 0), True),   # Wednesday night
        (dt.datetime(2026, 10, 14, 6, 30), True),   # before 07:00
        (dt.datetime(2026, 10, 14, 12, 0), False),  # Wednesday noon
        (dt.datetime(2026, 10, 17, 12, 0), True),   # Saturday
    ],
)
def test_after_hours_window(local, expected):
    from apps.alerts.models import AlertRule

    rule = AlertRule(
        rule_type="after_hours",
        after_hours_start=dt.time(20, 0),
        after_hours_end=dt.time(7, 0),
        weekends_count=True,
    )
    assert rule.in_after_hours(local) is expected


def test_after_hours_presence_alerts_in_local_time(employees, org):
    from apps.alerts.engine import on_confirmed_presence

    _rule(
        "after_hours",
        after_hours_start=dt.time(20, 0),
        after_hours_end=dt.time(7, 0),
        weekends_count=False,
    )
    noon = dt.datetime(2026, 10, 14, 12, 0, tzinfo=IST)
    night = dt.datetime(2026, 10, 14, 23, 0, tzinfo=IST)

    on_confirmed_presence(_interval(employees["eng"], org["reception"], noon))
    assert not _alerts().exists()
    on_confirmed_presence(_interval(employees["eng"], org["reception"], night))
    assert _alerts().get().rule_type == "after_hours"


def test_unknown_face_in_a_restricted_zone_alerts(server_room):
    import numpy as np

    from apps.tracking.aggregator import DetectionAggregator

    _rule("unknown_face")
    vector = np.random.default_rng(1).normal(size=512)
    DetectionAggregator().handle(
        {
            "v": 1, "camera_code": server_room["camera"].code,
            "captured_at": timezone.now().isoformat(), "track_id": "u1",
            "employee_code": None, "confidence": "unknown", "snapshot_key": "",
            "embedding": (vector / np.linalg.norm(vector)).tolist(),
        }
    )
    alert = _alerts().get()
    assert alert.unknown_cluster is not None
    assert alert.employee is None


def test_camera_offline_alerts(server_room):
    from apps.cameras.tasks import check_camera_health

    _rule("camera_down")
    check_camera_health()
    alert = _alerts().get()
    assert alert.camera == server_room["camera"]


def _alert_in(zone, employee=None):
    from apps.alerts.models import Alert

    now = timezone.now()
    return Alert.objects.create(
        rule_type="restricted_zone",
        severity="critical",
        zone=zone,
        employee=employee,
        triggered_at=now,
        last_seen_at=now,
        dedupe_key=f"t{zone.pk}",
        message=f"Entry to {zone.name} by someone not authorised for it",
    )


def test_inbox_is_scoped_and_names_are_for_administrators(client, users, employees, org):
    _alert_in(org["reception"], employees["ops"])
    _alert_in(org["office"], employees["ops"])

    client.force_login(users["operator"])
    response = client.get(reverse("alerts:inbox"))
    assert [a.zone for a in response.context["alerts"]] == [org["reception"]]
    assert employees["ops"].full_name not in response.content.decode()

    client.force_login(users["super_admin"])
    response = client.get(reverse("alerts:inbox"))
    assert len(response.context["alerts"]) == 2
    assert employees["ops"].full_name in response.content.decode()

    client.force_login(users["hr"])
    assert client.get(reverse("alerts:inbox")).status_code == 403


def test_alert_handling_is_audited(client, users, org):
    alert = _alert_in(org["reception"])
    url = reverse("alerts:action", args=[alert.uuid])
    client.force_login(users["operator"])

    client.post(url, {"action": "acknowledge"})
    alert.refresh_from_db()
    assert alert.status == "acknowledged" and alert.acknowledged_by == users["operator"]

    client.post(url, {"action": "resolve"})
    alert.refresh_from_db()
    assert alert.status == "acknowledged", "resolving needs a note"

    client.post(url, {"action": "resolve", "note": "visitor escorted out"})
    alert.refresh_from_db()
    assert alert.status == "resolved"
    assert _audit("alert.resolved").exists()

    # Outside the operator's zones, the alert does not exist for them.
    other = _alert_in(org["office"])
    response = client.post(reverse("alerts:action", args=[other.uuid]), {"action": "acknowledge"})
    assert response.status_code == 404


def test_only_rule_managers_configure_alerts(client, users, org):
    from apps.alerts.models import AlertRule

    client.force_login(users["operator"])
    assert client.get(reverse("alerts:rules")).status_code == 403

    client.force_login(users["super_admin"])
    response = client.post(
        reverse("alerts:rule_create"),
        {
            "name": "Office after hours",
            "rule_type": "after_hours",
            "severity": "warning",
            "is_enabled": "on",
            "zone": org["office"].pk,
            "after_hours_start": "21:00",
            "after_hours_end": "06:00",
        },
    )
    assert response.status_code == 302
    assert AlertRule.objects.get().created_by == users["super_admin"]
    assert _audit("alertrule.created").exists()


# ================================================================= retention


def test_retention_covers_every_derived_table(db, settings, users, employees, org):
    from apps.accounts.models import LoginEvent
    from apps.alerts.models import Alert
    from apps.cameras.models import Camera, CameraHealth
    from apps.tracking.models import DailySummary, MovementEvent
    from apps.tracking.tasks import enforce_retention

    now = timezone.now()
    old = now - dt.timedelta(days=settings.RETENTION["audit_days"] + 1)
    camera = Camera.objects.create(
        code="c1", name="c1", zone=org["office"], rtsp_url="rtsp://x/1"
    )
    for at in (old, now):
        MovementEvent.objects.create(employee=employees["eng"], to_zone=org["office"], occurred_at=at)
        DailySummary.objects.create(employee=employees["eng"], date=at.date())
        Alert.objects.create(
            rule_type="camera_down", severity="warning", zone=org["office"],
            triggered_at=at, last_seen_at=at, dedupe_key=f"k{at.timestamp()}", message="m",
        )
        CameraHealth.objects.filter(
            pk=CameraHealth.objects.create(camera=camera, status="online").pk
        ).update(at=at)
        LoginEvent.objects.filter(
            pk=LoginEvent.objects.create(email_attempted="a@b.c").pk
        ).update(at=at)

    results = enforce_retention()

    for key in (
        "movements_deleted",
        "daily_summaries_deleted",
        "alerts_deleted",
        "camera_health_deleted",
        "login_events_deleted",
    ):
        assert results[key] == 1, key
    assert MovementEvent.objects.count() == 1
    assert LoginEvent.objects.count() == 1


# ============================================================== self-service


def test_developer_can_request_break_glass(client, users):
    from apps.accounts.models import AccessElevation

    client.force_login(users["developer"])
    response = client.post(
        reverse("accounts:elevation_request"),
        {
            "actions": [Action.ENROLLMENT_MANAGE, Action.USER_MANAGE],
            "reason": "OPS-412: enrolment conflict for one employee needs inspecting.",
        },
    )
    assert response.status_code == 302
    elevation = AccessElevation.objects.get()
    # Authority over people is silently dropped, never granted.
    assert elevation.actions == [Action.ENROLLMENT_MANAGE]
    assert elevation.status == AccessElevation.Status.PENDING
    assert _audit("elevation.requested").exists()


def test_only_developers_have_break_glass(client, users):
    client.force_login(users["hr"])
    response = client.get(reverse("accounts:elevation_request"))
    assert response.status_code == 302


def test_people_see_their_own_enrolled_images_and_nobody_elses(client, users, employees):
    from PIL import Image

    enrollment = employees["eng"].enrollments.get()
    buffer = io.BytesIO()
    Image.new("RGB", (20, 20)).save(buffer, format="JPEG")
    enrollment.image.save("me.jpg", ContentFile(buffer.getvalue()), save=True)
    url = reverse("accounts:own_enrollment_image", args=[enrollment.uuid])

    client.force_login(users["employee"])
    assert client.get(url).status_code == 200
    assert _audit("enrollment.self_view").exists()

    client.force_login(users["super_admin"])
    assert client.get(url).status_code == 403
