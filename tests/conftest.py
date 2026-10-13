"""Shared fixtures. A small, realistic office graph plus one user per role."""

from __future__ import annotations

import datetime as dt

import pytest
from django.utils import timezone


@pytest.fixture(autouse=True)
def _test_settings(settings):
    """Baseline settings for the suite.

    * MFA off: most tests assert on role logic, not on enrolment. The MFA
      tests switch it back on explicitly.
    * ENVIRONMENT=production: the stricter case, so a test that passes here
      cannot be relying on the relaxed developer grants.
    * SSL redirect off: the suite runs with DJANGO_DEBUG=0 to exercise
      production settings, which would otherwise turn every test-client
      request into a 301 before it reached a view.
    """
    settings.MFA_ENFORCED = False
    settings.ENVIRONMENT = "production"
    settings.SECURE_SSL_REDIRECT = False


@pytest.fixture
def org(db):
    """Building, two floors, four zones (one never-tracked), adjacency, depts."""
    from apps.org.models import Building, Department, Floor, Zone, ZoneAdjacency

    building = Building.objects.create(name="HQ")
    ground = Floor.objects.create(building=building, name="Ground", level=0)
    first = Floor.objects.create(building=building, name="First", level=1)

    engineering = Department.objects.create(name="Engineering", code="ENG")
    operations = Department.objects.create(name="Operations", code="OPS")

    reception = Zone.objects.create(
        code="reception", name="Reception", floor=ground, zone_type=Zone.ZoneType.RECEPTION
    )
    corridor = Zone.objects.create(
        code="corridor", name="Corridor", floor=ground, zone_type=Zone.ZoneType.CORRIDOR
    )
    office = Zone.objects.create(
        code="office", name="Open Office", floor=first, zone_type=Zone.ZoneType.ROOM
    )
    washroom = Zone.objects.create(
        code="washroom",
        name="Washroom",
        floor=ground,
        zone_type=Zone.ZoneType.PRIVATE,
    )

    ZoneAdjacency.objects.create(
        from_zone=reception, to_zone=corridor, min_transit_seconds=2, typical_transit_seconds=8
    )
    ZoneAdjacency.objects.create(
        from_zone=corridor, to_zone=office, min_transit_seconds=5, typical_transit_seconds=20
    )

    return {
        "building": building,
        "ground": ground,
        "first": first,
        "engineering": engineering,
        "operations": operations,
        "reception": reception,
        "corridor": corridor,
        "office": office,
        "washroom": washroom,
    }


@pytest.fixture
def employees(db, org):
    """One consenting employee per department, both enrolled."""
    from apps.employees.models import ConsentRecord, Employee, FaceEnrollment

    def make(code: str, name: str, department) -> Employee:
        employee = Employee.objects.create(
            employee_code=code, full_name=name, department=department
        )
        ConsentRecord.objects.create(
            employee=employee,
            policy_version="2026.1",
            method=ConsentRecord.Method.WRITTEN,
        )
        FaceEnrollment.objects.create(
            employee=employee,
            image="enrollments/test.jpg",
            embedding=[0.0] * 512,
            quality_score=0.8,
            face_width_px=110,
            sharpness=120.0,
            brightness=128.0,
        )
        return employee

    return {
        "eng": make("ENG001", "Engineering Person", org["engineering"]),
        "ops": make("OPS001", "Operations Person", org["operations"]),
    }


@pytest.fixture
def users(db, org, employees):
    """One active user per role, with scopes where the role requires them."""
    from apps.accounts.models import User, UserScope
    from apps.common.roles import Role

    def make(role: str, email: str, *, departments=(), zones=(), employee=None) -> User:
        user = User.objects.create_user(
            email=email, password="TestPassw0rd!123", full_name=email, role=role
        )
        if employee is not None:
            user.employee = employee
            user.save(update_fields=["employee"])
        if departments or zones:
            scope = UserScope.objects.create(user=user)
            scope.departments.set(departments)
            scope.zones.set(zones)
        return user

    return {
        "developer": make(Role.DEVELOPER, "dev@test.local"),
        "super_admin": make(Role.SUPER_ADMIN, "admin@test.local"),
        "security_admin": make(Role.SECURITY_ADMIN, "secadmin@test.local"),
        # Scoped to Engineering only - Operations is deliberately out of reach.
        "hr": make(Role.HR, "hr@test.local", departments=[org["engineering"]]),
        "operator": make(
            Role.SECURITY_OPERATOR,
            "guard@test.local",
            zones=[org["reception"], org["corridor"]],
        ),
        "auditor": make(Role.AUDITOR, "auditor@test.local"),
        "employee": make(
            Role.EMPLOYEE, "staff@test.local", employee=employees["eng"]
        ),
        # An HR user with no scope at all - must see nothing, not everything.
        "hr_unscoped": make(Role.HR, "hr2@test.local"),
    }


@pytest.fixture
def presence(db, org, employees):
    """A short movement history for both employees."""
    from apps.tracking.models import MatchConfidence, PresenceInterval

    now = timezone.now()
    rows = []
    for employee in employees.values():
        for offset, zone in enumerate([org["reception"], org["corridor"], org["office"]]):
            entered = now - dt.timedelta(minutes=30 - offset * 10)
            rows.append(
                PresenceInterval.objects.create(
                    employee=employee,
                    zone=zone,
                    entered_at=entered,
                    last_seen_at=entered + dt.timedelta(minutes=5),
                    exited_at=entered + dt.timedelta(minutes=5),
                    detection_count=20,
                    confidence=MatchConfidence.CONFIRMED,
                    mean_match_score=0.62,
                )
            )
    return rows
