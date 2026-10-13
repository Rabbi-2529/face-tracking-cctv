"""
Phase 6 pilot tooling: the Accuracy page, the offline benchmark, the load
test, and smoke checks for the admin layout.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pytest
from django.urls import reverse
from django.utils import timezone


@pytest.fixture
def camera(db, org):
    from apps.cameras.models import Camera

    return Camera.objects.create(
        code="cam-office",
        name="office",
        zone=org["office"],
        purpose=Camera.Purpose.IDENTITY,
        rtsp_url="rtsp://u:p@10.0.0.1/office",
        measured_face_width_px=110,
    )


def _detection(camera, score, *, employee=None, confidence="confirmed", at=None, track="t"):
    from apps.tracking.models import Detection

    return Detection.objects.create(
        camera=camera,
        captured_at=at or timezone.now(),
        track_id=track,
        matched_employee=employee,
        match_score=score,
        confidence=confidence,
    )


# ---------------------------------------------------------------- accuracy


def test_accuracy_page_is_for_pipeline_viewers(client, users):
    client.force_login(users["hr"])
    assert client.get(reverse("tracking:accuracy")).status_code == 403
    client.force_login(users["developer"])
    assert client.get(reverse("tracking:accuracy")).status_code == 200


def test_what_if_counts_scores_against_proposed_thresholds(client, users, camera):
    for score in (0.2, 0.35, 0.40, 0.50, 0.70, 1.0):
        _detection(camera, score)
    client.force_login(users["developer"])

    ctx = client.get(reverse("tracking:accuracy"), {"high": "0.6", "low": "0.38"}).context
    assert ctx["current"] == {"confirmed": 3, "probable": 2, "unknown": 1}
    assert ctx["proposed"] == {"confirmed": 2, "probable": 2, "unknown": 2}
    histogram = [b["n"] for b in ctx["histogram"]]
    assert sum(histogram) == 6
    assert histogram[-1] == 1, "a perfect score lands in the top bucket"


def test_known_errors_drive_the_suggested_threshold(client, users, camera, employees, org):
    from apps.employees.models import IdentityCorrection
    from apps.tracking.models import PresenceInterval

    now = timezone.now()
    eng = employees["eng"]
    interval = PresenceInterval.objects.create(
        employee=eng, zone=org["office"], entered_at=now - dt.timedelta(minutes=5),
        last_seen_at=now, exited_at=now, confidence="confirmed",
    )
    _detection(camera, 0.61, employee=eng, at=now - dt.timedelta(minutes=2))
    IdentityCorrection.objects.create(
        presence_interval=interval, was_employee=eng, reason="not me",
        status=IdentityCorrection.Status.ACCEPTED,
    )

    client.force_login(users["developer"])
    ctx = client.get(reverse("tracking:accuracy")).context
    assert ctx["known_errors"] == [0.61]
    assert ctx["errors_above_high"] == [0.61]
    assert ctx["suggested_high"] == 0.63


def test_badly_placed_camera_is_called_out(client, users, camera):
    from apps.cameras.models import CameraHealth

    CameraHealth.objects.create(camera=camera, status="online", faces_seen=100, faces_gated=80)
    client.force_login(users["developer"])
    rows = client.get(reverse("tracking:accuracy")).context["cameras"]
    advice = " ".join(rows[0]["advice"])
    assert "reposition" in advice


def test_invalid_proposal_is_rejected(client, users):
    client.force_login(users["developer"])
    ctx = client.get(reverse("tracking:accuracy"), {"high": "0.3", "low": "0.5"}).context
    assert ctx["valid"] is False
    assert ctx["proposed"] is None


# --------------------------------------------------------------- benchmark


def _unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


def _people(n=5, seed=0):
    rng = np.random.default_rng(seed)
    return {f"p{i}": _unit(rng.normal(size=512)) for i in range(n)}


def _noisy(base, rng, scale):
    return _unit(base + rng.normal(size=512) * scale)


def test_identification_on_well_separated_faces_passes():
    from vision.benchmark import Gallery, identify, recommend, score_distributions

    rng = np.random.default_rng(1)
    people = _people()
    gallery = Gallery.build({n: [_noisy(v, rng, 0.02) for _ in range(3)] for n, v in people.items()})
    probes = [(n, _noisy(v, rng, 0.03)) for n, v in people.items() for _ in range(10)]
    probes += [("_unknown", _unit(rng.normal(size=512))) for _ in range(10)]

    genuine, impostor = score_distributions(gallery, [p for p in probes if p[0] != "_unknown"])
    suggestion = recommend(genuine, impostor)
    assert genuine.min() > impostor.max()
    assert suggestion["low"] < suggestion["high"]

    report = identify(gallery, probes, high=suggestion["high"], low=suggestion["low"])
    assert report.enrolled_probes == 50 and report.stranger_probes == 10
    assert report.confirmed_wrong == 0 and report.stranger_confirmed == 0
    assert report.passes


def test_a_confident_wrong_name_is_counted_and_fails_the_gate():
    from vision.benchmark import Gallery, identify

    people = _people(2)
    gallery = Gallery.build({"p0": [people["p0"]], "p1": [people["p1"]]})
    # A probe labelled p0 that is really p1's face.
    report = identify(gallery, [("p0", people["p1"])], high=0.45, low=0.30)
    assert report.confirmed_wrong == 1
    assert report.false_positive_rate == 1.0
    assert not report.passes
    assert report.wrong_examples[0]["matched"] == "p1"


def test_sweep_is_monotonic():
    from vision.benchmark import sweep

    rng = np.random.default_rng(2)
    rows = sweep(rng.uniform(0.4, 0.9, 200), rng.uniform(-0.1, 0.3, 200))
    tars = [r["tar"] for r in rows]
    fars = [r["far"] for r in rows]
    assert tars == sorted(tars, reverse=True)
    assert fars == sorted(fars, reverse=True)


# --------------------------------------------------------------- load test


def test_load_test_events_touch_nobody(monkeypatch):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "scripts" / "load_test.py"
    spec = importlib.util.spec_from_file_location("load_test", path)
    load_test = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(load_test)

    event = load_test.make_event("cam-1", 7)
    assert event["track_id"].startswith("loadtest:")
    assert event["employee_code"] is None
    assert event["embedding"] is None
    assert event["confidence"] == "unknown"
    assert load_test.schedule(20, 4, 60) == 4800


def test_load_test_events_create_detections_only(camera):
    import importlib.util
    from pathlib import Path

    from apps.tracking.aggregator import DetectionAggregator
    from apps.tracking.models import Detection, PresenceInterval, UnknownFaceCluster

    path = Path(__file__).resolve().parent.parent / "scripts" / "load_test.py"
    spec = importlib.util.spec_from_file_location("load_test", path)
    load_test = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(load_test)

    aggregator = DetectionAggregator()
    for i in range(5):
        aggregator.handle(load_test.make_event(camera.code, i))

    assert Detection.objects.count() == 5
    assert not PresenceInterval.objects.exists()
    assert not UnknownFaceCluster.objects.exists()


def test_purge_removes_only_load_test_rows(camera):
    from django.core.management import call_command

    from apps.tracking.models import Detection

    _detection(camera, 0.5, track="loadtest:cam:1")
    _detection(camera, 0.5, track="cam-office:42")
    call_command("purge_load_test")
    assert list(Detection.objects.values_list("track_id", flat=True)) == ["cam-office:42"]


# ------------------------------------------------------------------- layout


def test_signed_in_pages_use_the_admin_shell(client, users):
    client.force_login(users["super_admin"])
    html = client.get(reverse("tracking:dashboard")).content.decode()
    assert 'class="sidebar"' in html
    # Local assets carry a content hash in production (theme.<hash>.css).
    for asset in ("dataTables.min.js", "select2.min.js", "flatpickr.min.js", "js/app.", "css/theme."):
        assert asset in html, asset


def test_sidebar_follows_the_permission_matrix(client, users):
    client.force_login(users["employee"])
    html = client.get(reverse("tracking:dashboard")).content.decode()
    assert reverse("useradmin:list") not in html
    assert reverse("cameras:list") not in html
    assert reverse("accounts:my_account") in html


def test_login_page_has_no_shell(client):
    html = client.get(reverse("accounts:login")).content.decode()
    assert 'class="sidebar"' not in html
    assert "public" in html


# ---------------------------------------------------------------- readiness


def test_readiness_fails_without_an_identity_camera(db, org, employees):
    from apps.tracking.pilot import FAIL, pilot_readiness

    result = pilot_readiness()
    assert not result["ready"]
    names = {c.name: c.status for c in result["checks"]}
    assert names["Identity cameras"] == FAIL


def test_readiness_flags_thin_enrolment_and_small_faces(db, org, employees, camera):
    from apps.tracking.pilot import FAIL, WARN, pilot_readiness

    camera.measured_face_width_px = 42
    camera.save()
    checks = {c.name: c for c in pilot_readiness()["checks"]}
    assert checks["Identity cameras see faces of 60px or more"].status == FAIL
    assert "42px" in checks["Identity cameras see faces of 60px or more"].detail
    assert checks["At least 5 enrolment images each"].status == WARN


def test_readiness_is_on_the_accuracy_page(client, users, camera):
    client.force_login(users["developer"])
    response = client.get(reverse("tracking:accuracy"))
    assert response.context["readiness"]["checks"]
    assert "Pilot readiness" in response.content.decode()


def test_recommendation_never_goes_below_the_safety_floor():
    """Two easy identities make every threshold look safe. The recommender
    must not turn that into a 0.00 threshold."""
    from vision.benchmark import FLOOR_HIGH, FLOOR_LOW, recommend

    genuine = np.full(40, 0.92, dtype=np.float32)
    impostor = np.full(40, -0.02, dtype=np.float32)
    result = recommend(genuine, impostor, identities=2)

    assert result["high"] >= FLOOR_HIGH and result["low"] >= FLOOR_LOW
    assert result["floored"] is True
    assert result["sufficient"] is False
    assert len(result["insufficient_reasons"]) == 2


def test_recommendation_is_trusted_only_with_enough_comparisons():
    from vision.benchmark import recommend

    rng = np.random.default_rng(3)
    genuine = rng.uniform(0.5, 0.95, 3000).astype(np.float32)
    impostor = rng.uniform(-0.2, 0.42, 5000).astype(np.float32)
    result = recommend(genuine, impostor, identities=25)

    assert result["sufficient"] is True
    assert 0.42 <= result["high"] <= 0.44
    assert result["far_at_high"] <= 0.001
