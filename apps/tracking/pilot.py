"""
Pilot readiness: the runbook's "before day 1" checklist, checked from data.

Each check returns a status (pass / warn / fail), what it found, and what to
do about it. Used by the Accuracy page and by ``manage.py pilot_check``, so
the same answer appears in the panel and in a terminal.

Only things the database can actually know are checked. Signage, legal
sign-off and camera NTP still need a human; they stay on the runbook.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from django.conf import settings
from django.db.models import Count, Q
from django.utils import timezone

PASS, WARN, FAIL = "pass", "warn", "fail"
MIN_IMAGES = 5
MIN_FACE_PX = 60


@dataclass
class Check:
    area: str
    name: str
    status: str
    detail: str
    fix: str = ""


def _names(items, limit: int = 5) -> str:
    items = list(items)
    shown = ", ".join(str(i) for i in items[:limit])
    return shown + (f" and {len(items) - limit} more" if len(items) > limit else "")


def check_site() -> list[Check]:
    from apps.org.models import Floor, Zone, ZoneAdjacency
    from apps.org.plans import valid_polygon

    checks = []
    zones = list(Zone.objects.alive().select_related("floor"))
    trackable = [z for z in zones if not z.tracking_prohibited]

    if not any(z.tracking_prohibited for z in zones):
        checks.append(Check("Site", "Private rooms marked never-tracked", WARN,
                            "No zone is marked never-tracked.",
                            "Mark washrooms, prayer and medical rooms as never-tracked under Site & zones."))
    else:
        checks.append(Check("Site", "Private rooms marked never-tracked", PASS,
                            f"{sum(z.tracking_prohibited for z in zones)} zone(s) are never tracked."))

    connected = set(ZoneAdjacency.objects.values_list("from_zone_id", flat=True)) | set(
        ZoneAdjacency.objects.values_list("to_zone_id", flat=True)
    )
    orphans = [z.name for z in trackable if z.pk not in connected]
    checks.append(Check(
        "Site", "Every zone connected by a door", FAIL if orphans else PASS,
        f"{len(orphans)} zone(s) with no connection: {_names(orphans)}." if orphans
        else f"All {len(trackable)} trackable zones are connected.",
        "Add the doors under Site & zones; unconnected zones flag every arrival as impossible." if orphans else "",
    ))

    floors = list(Floor.objects.all())
    without_plan = [str(f) for f in floors if not f.plan_image]
    unoutlined = [z.name for z in trackable if z.floor.plan_image and valid_polygon(z.plan_polygon) is None]
    if without_plan or unoutlined:
        detail = []
        if without_plan:
            detail.append(f"no plan: {_names(without_plan)}")
        if unoutlined:
            detail.append(f"not outlined: {_names(unoutlined)}")
        checks.append(Check("Site", "Floor plans and zone outlines", WARN, "; ".join(detail) + ".",
                            "Upload plans and draw outlines - the live board and movement map need them."))
    else:
        checks.append(Check("Site", "Floor plans and zone outlines", PASS, "Every floor has a plan and every zone an outline."))
    return checks


def check_cameras() -> list[Check]:
    from apps.cameras.models import Camera, CameraAuditResult

    checks = []
    cameras = list(
        Camera.objects.alive().filter(is_enabled=True, zone__tracking_prohibited=False).select_related("zone")
    )
    identity = [c for c in cameras if c.purpose == Camera.Purpose.IDENTITY]
    if not identity:
        checks.append(Check("Cameras", "Identity cameras", FAIL, "No enabled identity camera.",
                            "Mark the doorway cameras as identity cameras; presence cameras never name anyone."))
        return checks

    unmeasured = [c.code for c in identity if c.measured_face_width_px is None]
    small = [f"{c.code} ({c.measured_face_width_px}px)" for c in identity
             if c.measured_face_width_px is not None and c.measured_face_width_px < MIN_FACE_PX]
    status = FAIL if small else WARN if unmeasured else PASS
    parts = []
    if small:
        parts.append(f"under {MIN_FACE_PX}px: {_names(small)}")
    if unmeasured:
        parts.append(f"never measured: {_names(unmeasured)}")
    checks.append(Check(
        "Cameras", f"Identity cameras see faces of {MIN_FACE_PX}px or more", status,
        "; ".join(parts) + "." if parts else f"All {len(identity)} identity cameras measure {MIN_FACE_PX}px or more.",
        "Run `make audit secs=120`, then reposition anything under 60px before the pilot." if parts else "",
    ))

    audited = set(CameraAuditResult.objects.values_list("camera_id", flat=True))
    not_audited = [c.code for c in identity if c.pk not in audited]
    checks.append(Check(
        "Cameras", "Camera audit run", WARN if not_audited else PASS,
        f"No audit result for {_names(not_audited)}." if not_audited else "Every identity camera has an audit result.",
        "Run `make audit secs=120`." if not_audited else "",
    ))

    offline = [f"{c.code} ({c.status})" for c in cameras if c.status != Camera.Status.ONLINE]
    checks.append(Check(
        "Cameras", "Cameras online", FAIL if len(offline) == len(cameras) else WARN if offline else PASS,
        f"{len(cameras) - len(offline)} of {len(cameras)} online; not online: {_names(offline)}." if offline
        else f"All {len(cameras)} enabled cameras online.",
        "Check the stream address, credentials and network path; start the vision service." if offline else "",
    ))
    return checks


def check_people() -> list[Check]:
    from apps.employees.models import Employee, FaceEnrollment

    checks = []
    employees = (
        Employee.objects.alive()
        .filter(status__in=[Employee.Status.ACTIVE, Employee.Status.ON_LEAVE], is_trackable=True)
        .annotate(
            images=Count("enrollments", filter=Q(enrollments__is_active=True), distinct=True),
            consents=Count("consent_records", filter=Q(consent_records__withdrawn_at__isnull=True), distinct=True),
        )
    )
    employees = list(employees)
    if not employees:
        return [Check("People", "Participants enrolled", FAIL, "No trackable employees.", "Add the pilot participants.")]

    no_consent = [e.employee_code for e in employees if not e.consents]
    thin = [f"{e.employee_code} ({e.images})" for e in employees if e.consents and e.images < MIN_IMAGES]
    ready = [e for e in employees if e.consents and e.images >= MIN_IMAGES]

    checks.append(Check(
        "People", "Consent recorded", FAIL if len(no_consent) == len(employees) else WARN if no_consent else PASS,
        f"{len(no_consent)} without active consent: {_names(no_consent)}." if no_consent
        else f"All {len(employees)} have active consent.",
        "Record consent before enrolling; people without it are never matched." if no_consent else "",
    ))
    checks.append(Check(
        "People", f"At least {MIN_IMAGES} enrolment images each", WARN if thin else PASS,
        f"{len(ready)} of {len(employees)} ready; too few images: {_names(thin)}." if thin
        else f"All consenting participants have {MIN_IMAGES}+ images.",
        "Add turned-left, turned-right and with/without-glasses photos." if thin else "",
    ))

    conflicts = FaceEnrollment.objects.filter(conflict_flagged=True).count()
    checks.append(Check(
        "People", "Enrolment conflicts resolved", WARN if conflicts else PASS,
        f"{conflicts} image(s) flagged as matching a different person." if conflicts else "None outstanding.",
        "Resolve them on each employee's enrolment page." if conflicts else "",
    ))
    return checks


def check_governance() -> list[Check]:
    from django_otp.plugins.otp_totp.models import TOTPDevice

    from apps.accounts.models import User
    from apps.alerts.models import AlertRule
    from apps.cameras.models import PipelineConfig
    from apps.common.roles import MFA_REQUIRED_ROLES

    checks = []
    privileged = list(User.objects.filter(is_active=True, role__in=list(MFA_REQUIRED_ROLES)))
    enrolled = set(TOTPDevice.objects.filter(confirmed=True).values_list("user_id", flat=True))
    missing = [u.email for u in privileged if u.pk not in enrolled]
    checks.append(Check(
        "Governance", "Privileged users enrolled an authenticator", WARN if missing else PASS,
        f"{len(missing)} of {len(privileged)} not enrolled: {_names(missing)}." if missing
        else f"All {len(privileged)} privileged users enrolled.",
        "Each signs in and enrols from My account." if missing else "",
    ))
    enforced = getattr(settings, "MFA_ENFORCED", False)
    checks.append(Check(
        "Governance", "MFA enforced", PASS if enforced else WARN,
        "MFA_ENFORCED is on." if enforced else "MFA_ENFORCED is off.",
        "" if enforced else "Set MFA_ENFORCED=1 once everyone above has enrolled.",
    ))

    types = set(AlertRule.objects.filter(is_enabled=True).values_list("rule_type", flat=True))
    absent = [label for value, label in AlertRule.RuleType.choices if value not in types]
    checks.append(Check(
        "Governance", "Alert rules configured", WARN if absent else PASS,
        f"No enabled rule for: {_names(absent)}." if absent else "One or more enabled rules of every type.",
        "Add them under Alert rules." if absent else "",
    ))

    config = PipelineConfig.active()
    default = (config.match_threshold_high, config.match_threshold_low) == (0.45, 0.30)
    checks.append(Check(
        "Tuning", "Thresholds measured, not defaults", WARN if default else PASS,
        "Still at the published defaults (0.45 / 0.30)." if default
        else f"Set to {config.match_threshold_high} / {config.match_threshold_low}"
        + (f" - {config.change_reason}" if config.change_reason else "") + ".",
        "Run `make benchmark` in week 1 and tune from its result." if default else "",
    ))

    from apps.tracking.models import DailySummary

    latest = DailySummary.objects.order_by("-date").values_list("date", flat=True).first()
    yesterday = timezone.localdate() - dt.timedelta(days=1)
    checks.append(Check(
        "Tuning", "Nightly jobs running", PASS if latest and latest >= yesterday else WARN,
        f"Latest daily summary: {latest}." if latest else "No daily summaries yet.",
        "" if latest and latest >= yesterday else "Check the beat and worker containers are running.",
    ))
    return checks


def pilot_readiness() -> dict:
    checks = check_site() + check_cameras() + check_people() + check_governance()
    counts = {s: sum(1 for c in checks if c.status == s) for s in (PASS, WARN, FAIL)}
    return {
        "checks": checks,
        "counts": counts,
        "ready": counts[FAIL] == 0,
        "at": timezone.now(),
    }
