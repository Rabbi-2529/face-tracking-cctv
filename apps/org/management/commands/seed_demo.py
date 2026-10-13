"""
Seed a realistic demo office so Phase 1 can be exercised immediately:
one building, two floors, zones with a proper adjacency graph, departments,
employees, cameras, and one user per role.

Safe to re-run. Refuses to run with ENVIRONMENT=production unless --force is
given, because it creates accounts with known passwords.
"""

from __future__ import annotations

import datetime as dt

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User, UserScope
from apps.cameras.models import Camera, CameraVendor
from apps.common.roles import Role
from apps.employees.models import ConsentRecord, Employee
from apps.org.models import Building, Department, Designation, Floor, Zone, ZoneAdjacency

DEMO_PASSWORD = "DemoPassw0rd!2026"

# (code, name, floor level, type, restricted, prohibited)
ZONES = [
    ("main-entry", "Main Entrance", 0, Zone.ZoneType.ENTRY, False, False),
    ("reception", "Reception", 0, Zone.ZoneType.RECEPTION, False, False),
    ("g-corridor", "Ground Corridor", 0, Zone.ZoneType.CORRIDOR, False, False),
    ("pantry", "Pantry", 0, Zone.ZoneType.PANTRY, False, False),
    ("g-washroom", "Ground Washroom", 0, Zone.ZoneType.PRIVATE, False, True),
    ("stairwell", "Stairwell", 0, Zone.ZoneType.STAIRWELL, False, False),
    ("open-office", "Open Office", 1, Zone.ZoneType.ROOM, False, False),
    ("f1-corridor", "First Floor Corridor", 1, Zone.ZoneType.CORRIDOR, False, False),
    ("mr-1", "Meeting Room 1", 1, Zone.ZoneType.MEETING, False, False),
    ("mr-2", "Meeting Room 2", 1, Zone.ZoneType.MEETING, False, False),
    ("server-room", "Server Room", 1, Zone.ZoneType.SERVER, True, False),
    ("f1-washroom", "First Floor Washroom", 1, Zone.ZoneType.PRIVATE, False, True),
]

# (from, to, min_seconds, typical_seconds)
ADJACENCY = [
    ("main-entry", "reception", 2, 6),
    ("reception", "g-corridor", 2, 8),
    ("g-corridor", "pantry", 2, 10),
    ("g-corridor", "stairwell", 2, 8),
    ("stairwell", "f1-corridor", 6, 18),
    ("f1-corridor", "open-office", 2, 8),
    ("f1-corridor", "mr-1", 2, 6),
    ("f1-corridor", "mr-2", 2, 6),
    ("f1-corridor", "server-room", 3, 10),
    ("open-office", "mr-1", 3, 12),
    ("open-office", "mr-2", 3, 12),
]

# (code, name, department code, zone code for identity camera)
EMPLOYEES = [
    ("EMP001", "Aarav Sharma", "ENG", "open-office"),
    ("EMP002", "Priya Nair", "ENG", "open-office"),
    ("EMP003", "Rohan Gupta", "ENG", "open-office"),
    ("EMP004", "Sneha Iyer", "HR", "reception"),
    ("EMP005", "Vikram Singh", "OPS", "g-corridor"),
    ("EMP006", "Ananya Rao", "FIN", "open-office"),
    ("EMP007", "Karan Mehta", "OPS", "server-room"),
    ("EMP008", "Divya Menon", "FIN", "open-office"),
]

# (code, name, zone, purpose, measured face width)
CAMERAS = [
    ("cam-entry-01", "Main Entrance - inward", "main-entry", Camera.Purpose.IDENTITY, 118),
    ("cam-recep-01", "Reception desk", "reception", Camera.Purpose.IDENTITY, 96),
    ("cam-gcor-01", "Ground corridor overview", "g-corridor", Camera.Purpose.PRESENCE, 42),
    ("cam-pantry-01", "Pantry door", "pantry", Camera.Purpose.IDENTITY, 84),
    ("cam-stair-01", "Stairwell", "stairwell", Camera.Purpose.PRESENCE, 38),
    ("cam-office-01", "Open office entry", "open-office", Camera.Purpose.IDENTITY, 102),
    ("cam-f1cor-01", "First floor corridor", "f1-corridor", Camera.Purpose.PRESENCE, 45),
    ("cam-mr1-01", "Meeting Room 1 door", "mr-1", Camera.Purpose.IDENTITY, 91),
    ("cam-mr2-01", "Meeting Room 2 door", "mr-2", Camera.Purpose.IDENTITY, 88),
    ("cam-server-01", "Server room door", "server-room", Camera.Purpose.IDENTITY, 124),
]

# (email, name, role, department codes, zone codes)
USERS = [
    ("dev@demo.local", "Dev Engineer", Role.DEVELOPER, [], []),
    ("admin@demo.local", "Super Admin", Role.SUPER_ADMIN, [], []),
    ("secadmin@demo.local", "Security Admin", Role.SECURITY_ADMIN, [], []),
    ("hr@demo.local", "HR Manager", Role.HR, ["ENG", "HR"], []),
    (
        "guard@demo.local",
        "Security Guard",
        Role.SECURITY_OPERATOR,
        [],
        ["main-entry", "reception", "g-corridor"],
    ),
    ("auditor@demo.local", "Compliance Auditor", Role.AUDITOR, [], []),
    ("aarav@demo.local", "Aarav Sharma", Role.EMPLOYEE, [], []),
]


class Command(BaseCommand):
    help = "Seed a demo office (zones, cameras, employees, one user per role)."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--force",
            action="store_true",
            help="Allow seeding even when ENVIRONMENT=production.",
        )

    @transaction.atomic
    def handle(self, *args, **options) -> None:
        if getattr(settings, "ENVIRONMENT", "") == "production" and not options["force"]:
            raise CommandError(
                "Refusing to seed demo data in production (creates accounts with "
                "a known password). Pass --force only if you are certain."
            )

        building, _ = Building.objects.get_or_create(
            name="Head Office", defaults={"timezone": settings.TIME_ZONE}
        )
        floors = {}
        for level, name in [(0, "Ground Floor"), (1, "First Floor")]:
            floors[level], _ = Floor.objects.get_or_create(
                building=building, level=level, defaults={"name": name}
            )

        departments = {}
        for code, name in [
            ("ENG", "Engineering"),
            ("HR", "Human Resources"),
            ("OPS", "Operations"),
            ("FIN", "Finance"),
        ]:
            departments[code], _ = Department.objects.get_or_create(
                code=code, defaults={"name": name}
            )

        designation, _ = Designation.objects.get_or_create(
            title="Associate", defaults={"level": 1}
        )

        zones = {}
        for code, name, level, ztype, restricted, prohibited in ZONES:
            zones[code], _ = Zone.objects.update_or_create(
                code=code,
                defaults={
                    "name": name,
                    "floor": floors[level],
                    "zone_type": ztype,
                    "is_restricted": restricted,
                    "tracking_prohibited": prohibited,
                },
            )
        self.stdout.write(f"  zones: {len(zones)} (2 washrooms flagged never-tracked)")

        edges = 0
        for from_code, to_code, min_s, typ_s in ADJACENCY:
            _, created = ZoneAdjacency.objects.get_or_create(
                from_zone=zones[from_code],
                to_zone=zones[to_code],
                defaults={
                    "min_transit_seconds": min_s,
                    "typical_transit_seconds": typ_s,
                    "bidirectional": True,
                },
            )
            edges += int(created)
        self.stdout.write(f"  adjacency edges: {len(ADJACENCY)} ({edges} new)")

        now = timezone.now()
        for code, name, dept_code, _home in EMPLOYEES:
            employee, _ = Employee.objects.update_or_create(
                employee_code=code,
                defaults={
                    "full_name": name,
                    "department": departments[dept_code],
                    "designation": designation,
                    "date_of_joining": (now - dt.timedelta(days=400)).date(),
                    "status": Employee.Status.ACTIVE,
                    "is_trackable": True,
                },
            )
            # Demo employees consent, so the pipeline will match them. In a
            # real deployment this row only exists once a signed notice does.
            ConsentRecord.objects.get_or_create(
                employee=employee,
                policy_version="2026.1",
                defaults={
                    "method": ConsentRecord.Method.WRITTEN,
                    "consented_at": now - dt.timedelta(days=30),
                },
            )
        self.stdout.write(f"  employees: {len(EMPLOYEES)} (all with active consent)")

        for code, name, zone_code, purpose, face_px in CAMERAS:
            Camera.objects.update_or_create(
                code=code,
                defaults={
                    "name": name,
                    "zone": zones[zone_code],
                    "vendor": CameraVendor.objects.filter(code=CameraVendor.DAHUA).first(),
                    "purpose": purpose,
                    # Placeholder address. Replace with the real stream:
                    # rtsp://user:pass@HOST:554/cam/realmonitor?channel=1&subtype=0
                    "rtsp_url": f"rtsp://demo:demo@192.168.1.{100 + len(code) % 50}:554"
                    "/cam/realmonitor?channel=1&subtype=0",
                    "measured_face_width_px": face_px,
                    "mount_height_cm": 200 if purpose == Camera.Purpose.IDENTITY else 280,
                    "tilt_degrees": 12.0 if purpose == Camera.Purpose.IDENTITY else 35.0,
                    "sample_fps": 4.0,
                    "is_enabled": True,
                },
            )
        self.stdout.write(f"  cameras: {len(CAMERAS)}")

        for email, name, role, dept_codes, zone_codes in USERS:
            user, created = User.objects.get_or_create(
                email=email,
                defaults={
                    "full_name": name,
                    "role": role,
                    "is_staff": role != Role.EMPLOYEE,
                    "is_active": True,
                },
            )
            if created:
                user.set_password(DEMO_PASSWORD)
                user.save()

            from django.contrib.auth.models import Group

            group = Group.objects.filter(name=f"role:{role}").first()
            if group:
                user.groups.add(group)

            if dept_codes or zone_codes:
                scope, _ = UserScope.objects.get_or_create(user=user)
                scope.departments.set([departments[c] for c in dept_codes])
                scope.zones.set([zones[c] for c in zone_codes])

            if role == Role.EMPLOYEE:
                user.employee = Employee.objects.filter(employee_code="EMP001").first()
                user.save(update_fields=["employee"])

        self.stdout.write(f"  users: {len(USERS)}")

        self._seed_alert_rules(departments)

        self.stdout.write(
            self.style.SUCCESS(
                "\ndemo data seeded. Sign in at http://localhost:8000/ with any of:\n"
                + "\n".join(f"    {e:<24} {r}" for e, _n, r, _d, _z in USERS)
                + f"\n\n  password for all demo accounts: {DEMO_PASSWORD}\n"
            )
        )
        self.stdout.write(
            self.style.WARNING(
                "  Note: 'hr@demo.local' is scoped to Engineering + HR only. "
                "Try viewing an Operations employee's history to see the "
                "tracking-request gate in action."
            )
        )

    def _seed_alert_rules(self, departments) -> None:
        """One rule of each kind, so the alert inbox has something to show."""
        import datetime as dt

        from apps.alerts.models import AlertRule

        restricted, _ = AlertRule.objects.get_or_create(
            name="Server room - unauthorised entry",
            defaults={
                "rule_type": AlertRule.RuleType.RESTRICTED_ZONE,
                "severity": AlertRule.Severity.CRITICAL,
            },
        )
        # Engineering and Operations look after the servers.
        restricted.authorised_departments.set([departments["ENG"], departments["OPS"]])

        AlertRule.objects.get_or_create(
            name="After hours (20:00-07:00, weekends)",
            defaults={
                "rule_type": AlertRule.RuleType.AFTER_HOURS,
                "severity": AlertRule.Severity.WARNING,
                "after_hours_start": dt.time(20, 0),
                "after_hours_end": dt.time(7, 0),
                "weekends_count": True,
            },
        )
        AlertRule.objects.get_or_create(
            name="Unknown face in a restricted area",
            defaults={
                "rule_type": AlertRule.RuleType.UNKNOWN_FACE,
                "severity": AlertRule.Severity.CRITICAL,
            },
        )
        AlertRule.objects.get_or_create(
            name="Camera offline",
            defaults={
                "rule_type": AlertRule.RuleType.CAMERA_DOWN,
                "severity": AlertRule.Severity.WARNING,
            },
        )
        self.stdout.write("  alert rules: 4")
