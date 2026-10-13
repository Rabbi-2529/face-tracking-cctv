"""
Queryset scoping.

A role check without a queryset filter leaks rows through list pages, search
and CSV export. These tests cover the half of access control that is easy to
forget because the UI still looks correct when it is missing.
"""

from __future__ import annotations

import pytest

from apps.common.scoping import (
    visible_cameras,
    visible_detections,
    visible_employees,
    visible_presence,
    visible_zones,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def cameras(db, org):
    from apps.cameras.models import Camera

    def make(code: str, zone, purpose=Camera.Purpose.IDENTITY) -> Camera:
        return Camera.objects.create(
            code=code,
            name=code,
            zone=zone,
            purpose=purpose,
            rtsp_url=f"rtsp://user:pass@10.0.0.1:554/{code}",
            measured_face_width_px=110,
        )

    return {
        "reception": make("cam-reception", org["reception"]),
        "corridor": make("cam-corridor", org["corridor"], Camera.Purpose.PRESENCE),
        "office": make("cam-office", org["office"]),
    }


# ---------------------------------------------------------------------- zones


def test_admin_sees_all_zones_except_none(users, org):
    from apps.org.models import Zone

    visible = visible_zones(users["super_admin"], Zone.objects.alive())
    codes = set(visible.values_list("code", flat=True))
    assert {"reception", "corridor", "office"} <= codes


def test_prohibited_zones_are_hidden_from_monitoring_roles(users, org):
    """The washroom must not appear in any monitoring view, for anyone."""
    from apps.org.models import Zone

    for key in ("super_admin", "security_admin", "operator", "hr", "employee"):
        visible = visible_zones(users[key], Zone.objects.alive())
        assert "washroom" not in set(visible.values_list("code", flat=True)), (
            f"{key} can see a tracking-prohibited zone"
        )


def test_operator_sees_only_assigned_zones(users, org):
    from apps.org.models import Zone

    visible = visible_zones(users["operator"], Zone.objects.alive())
    assert set(visible.values_list("code", flat=True)) == {"reception", "corridor"}


def test_auditor_sees_no_zones(users):
    from apps.org.models import Zone

    assert not visible_zones(users["auditor"], Zone.objects.alive()).exists()


# -------------------------------------------------------------------- cameras


def test_operator_sees_only_cameras_in_assigned_zones(users, cameras):
    from apps.cameras.models import Camera

    visible = visible_cameras(users["operator"], Camera.objects.alive())
    assert set(visible.values_list("code", flat=True)) == {
        "cam-reception",
        "cam-corridor",
    }


def test_hr_sees_no_cameras(users, cameras):
    from apps.cameras.models import Camera

    assert not visible_cameras(users["hr"], Camera.objects.alive()).exists()


def test_developer_sees_all_cameras(users, cameras):
    from apps.cameras.models import Camera

    assert visible_cameras(users["developer"], Camera.objects.alive()).count() == 3


# ------------------------------------------------------------------ employees


def test_hr_directory_is_limited_to_its_departments(users, employees):
    from apps.employees.models import Employee

    visible = visible_employees(users["hr"], Employee.objects.alive())
    assert set(visible.values_list("employee_code", flat=True)) == {"ENG001"}


def test_unscoped_hr_directory_is_empty(users, employees):
    from apps.employees.models import Employee

    visible = visible_employees(users["hr_unscoped"], Employee.objects.alive())
    assert not visible.exists()


def test_employee_sees_only_their_own_record(users, employees):
    from apps.employees.models import Employee

    visible = visible_employees(users["employee"], Employee.objects.alive())
    assert set(visible.values_list("employee_code", flat=True)) == {"ENG001"}


def test_operator_sees_no_directory(users, employees):
    from apps.employees.models import Employee

    assert not visible_employees(users["operator"], Employee.objects.alive()).exists()


# ------------------------------------------------------------------- presence


def test_admin_sees_all_presence(users, presence):
    from apps.tracking.models import PresenceInterval

    assert visible_presence(users["super_admin"], PresenceInterval.objects.all()).count() == len(
        presence
    )


def test_hr_presence_is_limited_to_its_department(users, presence):
    from apps.tracking.models import PresenceInterval

    visible = visible_presence(users["hr"], PresenceInterval.objects.all())
    codes = set(visible.values_list("employee__employee_code", flat=True))
    assert codes == {"ENG001"}


def test_employee_presence_is_limited_to_self(users, presence):
    from apps.tracking.models import PresenceInterval

    visible = visible_presence(users["employee"], PresenceInterval.objects.all())
    codes = set(visible.values_list("employee__employee_code", flat=True))
    assert codes == {"ENG001"}


def test_operator_sees_no_presence_history(users, presence):
    from apps.tracking.models import PresenceInterval

    visible = visible_presence(users["operator"], PresenceInterval.objects.all())
    # An operator holds HISTORY_OWN but has no linked employee record, so
    # there is nothing for them to see.
    assert not visible.exists()


def test_presence_in_prohibited_zones_is_never_returned(users, org, employees):
    """Even if a row somehow exists, no scope may surface it."""
    from django.utils import timezone

    from apps.tracking.models import PresenceInterval

    now = timezone.now()
    PresenceInterval.objects.create(
        employee=employees["eng"],
        zone=org["washroom"],
        entered_at=now,
        last_seen_at=now,
    )
    for key in ("super_admin", "developer", "hr", "employee"):
        visible = visible_presence(users[key], PresenceInterval.objects.all())
        assert not visible.filter(zone__code="washroom").exists(), (
            f"{key} can read presence in a tracking-prohibited zone"
        )


# ----------------------------------------------------------------- detections


def test_only_developer_sees_raw_detections(users, cameras):
    import datetime as dt

    from django.utils import timezone

    from apps.tracking.models import Detection

    Detection.objects.create(
        camera=cameras["reception"],
        captured_at=timezone.now() - dt.timedelta(minutes=1),
        track_id="t1",
        bbox=[0, 0, 10, 10],
        face_width_px=100,
    )

    assert visible_detections(users["developer"], Detection.objects.all()).count() == 1
    for key in ("super_admin", "security_admin", "hr", "operator", "auditor", "employee"):
        assert not visible_detections(users[key], Detection.objects.all()).exists(), (
            f"{key} can read raw detections, which contain biometric vectors"
        )
