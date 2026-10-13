"""
Phase 4 interfaces: floor plans, the movement map, snapshot evidence, live
follow, the review queues, reports and exports.

Most of what is worth testing here is access, not layout: who can open
what, what each screen leaves out, and that every identity-bearing fetch and
every decision leaves an audit row.
"""

from __future__ import annotations

import datetime as dt
import io

import pytest
from django.core.files.base import ContentFile
from django.urls import reverse
from django.utils import timezone

PLAN_W, PLAN_H = 400, 300


def _png(width: int = PLAN_W, height: int = PLAN_H) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (40, 40), "grey").save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    """Keep uploaded plans and snapshots out of the real media volume."""
    settings.STORAGES = {
        **settings.STORAGES,
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": str(tmp_path), "base_url": None},
        },
    }
    settings.MEDIA_ROOT = str(tmp_path)


@pytest.fixture
def plans(org):
    """Plans on both floors, every zone outlined - the washroom included."""
    for floor in (org["ground"], org["first"]):
        floor.plan_image.save("plan.png", ContentFile(_png()), save=False)
        floor.plan_width_px, floor.plan_height_px = PLAN_W, PLAN_H
        floor.save()
    shapes = {
        "reception": [[10, 10], [100, 10], [100, 100], [10, 100]],
        "corridor": [[110, 10], [200, 10], [200, 100], [110, 100]],
        "washroom": [[210, 10], [300, 10], [300, 100], [210, 100]],
        "office": [[10, 110], [200, 110], [200, 250], [10, 250]],
    }
    for code, points in shapes.items():
        zone = org[code]
        zone.plan_polygon = points
        zone.save(update_fields=["plan_polygon"])
    return shapes


def _audit(action: str):
    from apps.accounts.models import AuditLog

    return AuditLog.objects.filter(action=action)


# --------------------------------------------------------------- floor plans


def test_plan_image_needs_a_signed_in_user(client, users, plans, org):
    url = reverse("org:floor_plan_image", args=[org["ground"].pk])
    assert client.get(url).status_code == 302

    client.force_login(users["employee"])
    response = client.get(url)
    assert response.status_code == 200
    assert response["Content-Type"] == "image/png"


def test_plan_image_404_without_a_plan(client, users, org):
    client.force_login(users["super_admin"])
    response = client.get(reverse("org:floor_plan_image", args=[org["ground"].pk]))
    assert response.status_code == 404


def test_uploading_a_plan_records_its_size(client, users, org):
    from django.core.files.uploadedfile import SimpleUploadedFile

    client.force_login(users["super_admin"])
    floor = org["first"]
    response = client.post(
        reverse("org:edit", args=["floor", floor.pk]),
        {
            "building": floor.building_id,
            "name": floor.name,
            "level": floor.level,
            "plan_image": SimpleUploadedFile("p.png", _png(640, 480), "image/png"),
        },
    )
    assert response.status_code == 302
    floor.refresh_from_db()
    assert (floor.plan_width_px, floor.plan_height_px) == (640, 480)


def _save_outline(client, floor, zone, points):
    return client.post(
        reverse("org:floor_plan", args=[floor.pk]),
        data={"zone": zone.pk, "points": points},
        content_type="application/json",
    )


def test_only_zone_managers_draw_outlines(client, users, plans, org):
    client.force_login(users["hr"])
    response = _save_outline(client, org["ground"], org["reception"], [[1, 1], [5, 1], [5, 5]])
    assert response.status_code == 403


def test_outline_is_validated_and_audited(client, users, plans, org):
    client.force_login(users["super_admin"])
    floor, zone = org["ground"], org["reception"]

    assert _save_outline(client, floor, zone, [[1, 1], [5, 5]]).status_code == 400
    assert _save_outline(client, floor, zone, [[1, 1], [5, 1], [999, 5]]).status_code == 400

    response = _save_outline(client, floor, zone, [[1, 1], [50, 1], [50, 40]])
    assert response.status_code == 200
    zone.refresh_from_db()
    assert zone.plan_polygon == [[1.0, 1.0], [50.0, 1.0], [50.0, 40.0]]
    assert _audit("org.zone.outline_saved").exists()

    # A zone on another floor cannot be drawn on this one.
    assert _save_outline(client, floor, org["office"], [[1, 1], [5, 1], [5, 5]]).status_code == 404


def test_live_board_draws_only_rooms_the_viewer_may_monitor(client, users, plans, org):
    client.force_login(users["operator"])
    html = client.get(reverse("tracking:live_board")).content.decode()

    # Operator is scoped to reception and corridor.
    assert f'data-zone="{org["reception"].pk}"' in html
    assert f'data-zone="{org["corridor"].pk}"' in html
    assert f'data-zone="{org["office"].pk}"' not in html
    assert "<svg" in html

    client.force_login(users["super_admin"])
    html = client.get(reverse("tracking:live_board")).content.decode()
    assert f'data-zone="{org["office"].pk}"' in html
    # Never-tracked rooms are never drawn, for an administrator either.
    assert f'data-zone="{org["washroom"].pk}"' not in html
    assert "Washroom" not in html


# ------------------------------------------------------------- the timeline


def test_timeline_has_a_movement_map_in_day_order(client, users, plans, presence, employees):
    client.force_login(users["super_admin"])
    url = reverse("tracking:employee_timeline", args=[employees["eng"].uuid])
    response = client.get(url)

    stops = [s for d in response.context["movement_map"] for s in d["stops"]]
    assert [s["n"] for s in stops] == [1, 2, 3]
    assert [s["zone"] for s in stops] == ["Reception", "Corridor", "Open Office"]
    assert "Play the day" in response.content.decode()


def test_timeline_without_plans_still_renders(client, users, presence, employees):
    client.force_login(users["super_admin"])
    response = client.get(
        reverse("tracking:employee_timeline", args=[employees["eng"].uuid])
    )
    assert response.status_code == 200
    assert response.context["movement_map"] == []


def _with_snapshot(interval, key="snapshots/test/a.jpg"):
    from django.core.files.storage import default_storage

    default_storage.save(key, ContentFile(_jpeg()))
    interval.first_snapshot_key = key
    interval.save(update_fields=["first_snapshot_key"])
    return interval


def test_evidence_is_offered_only_to_snapshot_viewers(client, users, presence, employees):
    _with_snapshot(presence[0])
    url = reverse("tracking:employee_timeline", args=[employees["eng"].uuid])

    client.force_login(users["super_admin"])
    assert 'data-evidence="' in client.get(url).content.decode()

    # HR may read its own department's timeline but not see imagery.
    client.force_login(users["hr"])
    assert 'data-evidence="' not in client.get(url).content.decode()


def test_interval_snapshot_is_streamed_and_audited(client, users, presence):
    interval = _with_snapshot(presence[0])
    client.force_login(users["super_admin"])

    response = client.get(reverse("tracking:interval_snapshot", args=[interval.pk, "first"]))
    assert response.status_code == 200
    assert b"".join(response.streaming_content)
    assert response["Cache-Control"].startswith("no-store")
    assert _audit("snapshot.view").filter(target_employee=interval.employee).exists()


def test_interval_snapshot_refuses_without_snapshot_permission(client, users, presence):
    interval = _with_snapshot(presence[0])
    client.force_login(users["hr"])

    response = client.get(reverse("tracking:interval_snapshot", args=[interval.pk, "first"]))
    assert response.status_code == 403
    assert _audit("snapshot.denied").filter(success=False).exists()


def test_interval_snapshot_never_serves_a_prohibited_zone(client, users, org, employees):
    from apps.tracking.models import PresenceInterval

    now = timezone.now()
    interval = _with_snapshot(
        PresenceInterval.objects.create(
            employee=employees["eng"],
            zone=org["washroom"],
            entered_at=now,
            last_seen_at=now,
        )
    )
    client.force_login(users["super_admin"])
    response = client.get(reverse("tracking:interval_snapshot", args=[interval.pk, "first"]))
    assert response.status_code == 404


def test_correction_can_name_an_entry_but_only_the_subjects_own(
    client, users, presence, employees
):
    from apps.employees.models import IdentityCorrection

    eng, ops = employees["eng"], employees["ops"]
    own = next(i for i in presence if i.employee_id == eng.pk)
    other = next(i for i in presence if i.employee_id == ops.pk)
    url = reverse("tracking:report_correction", args=[eng.uuid])

    client.force_login(users["employee"])
    client.post(url, {"reason": "that was not me", "interval": own.pk})
    client.post(url, {"reason": "nor was this one", "interval": other.pk})

    first, second = IdentityCorrection.objects.order_by("pk")
    assert first.presence_interval_id == own.pk
    assert second.presence_interval_id is None


# ------------------------------------------------------------- live follow


def _request(users, employees, *, scope="live", approve=True, requester="hr"):
    from apps.watchlist.models import TrackingRequest

    now = timezone.now()
    tr = TrackingRequest.objects.create(
        employee=employees["ops"],
        requested_by=users[requester],
        purpose=TrackingRequest.Purpose.SECURITY_INCIDENT,
        reason="Badge reported stolen; checking where it is being used today.",
        scope=scope,
        data_from=now - dt.timedelta(days=1),
        data_to=now,
    )
    if approve:
        tr.approve(users["super_admin"], window_hours=4)
    return tr


def test_requester_can_open_live_follow(client, users, employees):
    tr = _request(users, employees)
    client.force_login(users["hr"])

    response = client.get(reverse("watchlist:follow", args=[tr.uuid]))
    assert response.status_code == 200
    assert f"/ws/track/{tr.uuid}/" in response.content.decode()
    assert _audit("track.follow_page").exists()


@pytest.mark.parametrize(
    "case",
    ["other_user", "history_only", "pending"],
)
def test_live_follow_is_refused_and_audited(client, users, employees, case):
    if case == "history_only":
        tr = _request(users, employees, scope="historical")
        viewer = users["hr"]
    elif case == "pending":
        tr = _request(users, employees, approve=False)
        viewer = users["hr"]
    else:
        tr = _request(users, employees)
        viewer = users["security_admin"]

    client.force_login(viewer)
    response = client.get(reverse("watchlist:follow", args=[tr.uuid]))
    assert response.status_code == 403
    assert _audit("track.follow_denied").filter(success=False).exists()


# --------------------------------------------------------------- review


def _implausible(employees, org):
    from apps.tracking.models import MovementEvent

    return MovementEvent.objects.create(
        employee=employees["eng"],
        from_zone=org["reception"],
        to_zone=org["office"],
        occurred_at=timezone.now(),
        is_plausible=False,
        implausible_reason="no adjacency defined",
        review_status=MovementEvent.ReviewStatus.PENDING,
    )


def test_review_queues_are_for_history_any(client, users):
    for name in ("movement_review", "correction_queue", "unknown_faces"):
        client.force_login(users["hr"])
        assert client.get(reverse(f"tracking:{name}")).status_code == 403
        client.force_login(users["super_admin"])
        assert client.get(reverse(f"tracking:{name}")).status_code == 200


def test_movement_review_decision(client, users, employees, org):
    from apps.tracking.models import MovementEvent

    event = _implausible(employees, org)
    url = reverse("tracking:movement_decide", args=[event.pk])

    client.force_login(users["hr"])
    assert client.post(url, {"decision": "reject"}).status_code == 403

    client.force_login(users["super_admin"])
    client.post(url, {"decision": "reject"})
    event.refresh_from_db()
    assert event.review_status == MovementEvent.ReviewStatus.REJECTED
    assert _audit("movement.review_rejected").exists()

    # Decided once; a second decision finds nothing pending.
    assert client.post(url, {"decision": "confirm"}).status_code == 404


def test_accepted_correction_marks_the_entry(client, users, presence, employees):
    from apps.employees.models import IdentityCorrection

    interval = next(i for i in presence if i.employee_id == employees["eng"].pk)
    correction = IdentityCorrection.objects.create(
        presence_interval=interval,
        was_employee=employees["eng"],
        reported_by=users["employee"],
        reason="not me",
    )
    client.force_login(users["super_admin"])
    url = reverse("tracking:correction_decide", args=[correction.pk])

    client.post(url, {"decision": "reject"})
    correction.refresh_from_db()
    assert correction.status == IdentityCorrection.Status.OPEN, "reject needs a note"

    client.post(url, {"decision": "accept"})
    correction.refresh_from_db()
    assert correction.status == IdentityCorrection.Status.ACCEPTED
    assert correction.resolved_by == users["super_admin"]
    assert _audit("correction.accepted").exists()

    timeline = client.get(
        reverse("tracking:employee_timeline", args=[employees["eng"].uuid])
    ).content.decode()
    assert "not this person" in timeline


def _cluster():
    from apps.tracking.models import UnknownFaceCluster

    now = timezone.now()
    return UnknownFaceCluster.objects.create(
        centroid_embedding=[1.0] + [0.0] * 511, first_seen=now, last_seen=now
    )


def test_unknown_face_is_labelled(client, users):
    cluster = _cluster()
    client.force_login(users["super_admin"])
    client.post(
        reverse("tracking:unknown_resolve", args=[cluster.uuid]),
        {"resolution": "visitor", "label": "Courier, Tuesdays", "notes": ""},
    )
    cluster.refresh_from_db()
    assert cluster.resolution == "visitor"
    assert cluster.label == "Courier, Tuesdays"
    assert cluster.resolved_by == users["super_admin"]
    assert _audit("unknown.resolved").exists()


def test_non_consenting_staff_are_never_linked_to_a_record(client, users, employees):
    cluster = _cluster()
    client.force_login(users["super_admin"])
    client.post(
        reverse("tracking:unknown_resolve", args=[cluster.uuid]),
        {
            "resolution": "non_consenting",
            "resolved_as_employee": employees["ops"].pk,
            "label": "",
            "notes": "",
        },
    )
    cluster.refresh_from_db()
    assert cluster.resolution == "non_consenting"
    assert cluster.resolved_as_employee is None


def test_cluster_snapshot_needs_review_access(client, users):
    from django.core.files.storage import default_storage

    cluster = _cluster()
    cluster.representative_snapshot_key = default_storage.save(
        "snapshots/u.jpg", ContentFile(_jpeg())
    )
    cluster.save()
    url = reverse("tracking:cluster_snapshot", args=[cluster.uuid])

    client.force_login(users["operator"])
    assert client.get(url).status_code == 403
    client.force_login(users["super_admin"])
    assert client.get(url).status_code == 200


# --------------------------------------------------------------- reports


@pytest.fixture
def summaries(employees, org):
    """Yesterday: one Engineering person, three Operations people."""
    from apps.employees.models import Employee
    from apps.tracking.models import DailySummary

    day = timezone.localdate() - dt.timedelta(days=1)
    people = [employees["eng"], employees["ops"]]
    for n in (2, 3):
        people.append(
            Employee.objects.create(
                employee_code=f"OPS00{n}",
                full_name=f"=HYPERLINK(\"x\") {n}",
                department=org["operations"],
            )
        )
    for person in people:
        DailySummary.objects.create(
            employee=person,
            date=day,
            present_seconds=3600,
            total_zones_visited=1,
            dwell_by_zone={"office": 3600},
        )
    return day


def test_reports_are_refused_without_aggregate_access(client, users):
    client.force_login(users["auditor"])
    assert client.get(reverse("tracking:reports")).status_code == 403


def test_hr_report_is_scoped_and_small_counts_are_suppressed(client, users, summaries):
    client.force_login(users["hr"])
    report = client.get(reverse("tracking:reports")).context["report"]

    assert report["dept_names"] == ["Engineering"], "HR sees its own department only"
    row = next(r for r in report["headcount_rows"] if r["date"] == summaries)
    assert row["cells"] == ["<3"]
    office = report["zone_rows"][0]
    assert office["people"] == "<3" and office["hours"] is None
    assert _audit("report.view").exists()


def test_admin_report_shows_exact_counts(client, users, summaries):
    client.force_login(users["super_admin"])
    report = client.get(reverse("tracking:reports")).context["report"]

    row = next(r for r in report["headcount_rows"] if r["date"] == summaries)
    assert row["total"] == 4
    assert report["zone_rows"][0]["people"] == 4
    assert report["zone_rows"][0]["hours"] == 4.0


def _csv(response) -> str:
    return b"".join(response.streaming_content).decode()


def test_aggregate_export_for_hr_but_not_identity(client, users, summaries):
    client.force_login(users["hr"])
    response = client.get(reverse("tracking:report_export", args=["headcount"]))
    assert response.status_code == 200
    assert "<3" in _csv(response)
    assert _audit("report.export").exists()

    response = client.get(reverse("tracking:report_export", args=["people"]))
    assert response.status_code == 403
    assert _audit("export.denied:identity").exists()


def test_identity_export_neutralises_formulas(client, users, summaries):
    client.force_login(users["super_admin"])
    body = _csv(client.get(reverse("tracking:report_export", args=["people"])))

    assert "OPS002" in body
    assert "'=HYPERLINK" in body
    # Neither bare nor inside CSV quoting may a cell start with "=".
    assert ",=HYPERLINK" not in body
    assert '"=HYPERLINK' not in body


def test_audit_export_is_for_auditors(client, users):
    client.force_login(users["hr"])
    assert client.get(reverse("tracking:audit_export")).status_code == 403

    client.force_login(users["auditor"])
    response = client.get(reverse("tracking:audit_export"))
    assert response.status_code == 200
    assert _csv(response).startswith("at,actor,action")
    assert _audit("audit.export").exists()


def test_exports_are_rate_limited(client, users, summaries):
    from django.core.cache import cache

    from apps.tracking.report_views import EXPORTS_PER_HOUR

    cache.delete(f"export-rate:{users['super_admin'].pk}")
    client.force_login(users["super_admin"])
    url = reverse("tracking:report_export", args=["zones"])
    for _ in range(EXPORTS_PER_HOUR):
        assert client.get(url).status_code == 200
    assert client.get(url).status_code == 429
    assert _audit("export.denied:aggregate").exists()
    cache.delete(f"export-rate:{users['super_admin'].pk}")
