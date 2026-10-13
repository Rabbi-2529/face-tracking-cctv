"""
Enrolment tests.

Enrolment sets the ceiling on the system's accuracy, and two of its
behaviours are safety properties rather than conveniences:

* a bad image is **rejected**, not stored, because a poor gallery entry
  degrades every future match for that person;
* an image matching a **different** enrolled employee is held out of the
  gallery pending review, because enrolling it causes confident
  mis-identification between two real people.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

MODEL_DIR = Path(os.environ.get("MODEL_DIR", "/models"))
MODELS_PRESENT = (MODEL_DIR / "det_10g.onnx").exists() and (
    MODEL_DIR / "w600k_r50.onnx"
).exists()

FACE_DIR = Path(__file__).resolve().parent.parent / "samples" / "faces"
FACES_PRESENT = (FACE_DIR / "face_a.jpg").exists() and (
    FACE_DIR / "face_b.jpg"
).exists()

needs_models = pytest.mark.skipif(
    not MODELS_PRESENT, reason="ONNX models absent; run scripts/download_models.py"
)
needs_faces = pytest.mark.skipif(
    not FACES_PRESENT, reason="sample faces absent; run `make fixtures`"
)

pytestmark = pytest.mark.django_db


def face_bytes(name: str) -> bytes:
    return (FACE_DIR / name).read_bytes()


# ------------------------------------------------- input handling, no models


def test_empty_upload_is_rejected(employees):
    from apps.employees.enrollment import enroll_image

    result = enroll_image(employees["eng"], b"")
    assert not result.accepted
    assert "empty" in result.message.lower()


def test_oversized_upload_is_rejected(employees):
    from apps.employees.enrollment import MAX_UPLOAD_BYTES, enroll_image

    result = enroll_image(employees["eng"], b"x" * (MAX_UPLOAD_BYTES + 1))
    assert not result.accepted
    assert "larger than" in result.message


def test_non_image_upload_is_rejected(employees):
    from apps.employees.enrollment import enroll_image

    result = enroll_image(employees["eng"], b"this is not an image at all")
    assert not result.accepted
    assert "could not be read" in result.message


def test_gallery_coverage_advises_on_an_empty_gallery(db, org):
    """Advice has to be actionable, because pose coverage is what actually
    determines whether a doorway camera will recognise someone."""
    from apps.employees.enrollment import gallery_coverage
    from apps.employees.models import Employee

    employee = Employee.objects.create(
        employee_code="NEW001", full_name="Newly Hired", department=org["engineering"]
    )
    coverage = gallery_coverage(employee)

    assert coverage["active_count"] == 0
    assert coverage["is_processable"] is False
    advice = " ".join(coverage["advice"])
    assert "frontal" in advice
    assert "turned" in advice


def test_coverage_flags_a_frontal_only_gallery(employees):
    """Five frontal photos are worth much less than a spread of poses."""
    from apps.employees.enrollment import gallery_coverage
    from apps.employees.models import FaceEnrollment

    employee = employees["eng"]
    for _ in range(5):
        FaceEnrollment.objects.create(
            employee=employee,
            image="enrollments/x.jpg",
            embedding=[0.0] * 512,
            pose=FaceEnrollment.Pose.FRONTAL,
            quality_score=0.8,
            face_width_px=140,
            sharpness=120.0,
            brightness=128.0,
        )

    coverage = gallery_coverage(employee)
    assert coverage["active_count"] == 6  # 5 plus the fixture's one
    assert "turned" in " ".join(coverage["advice"])


def test_deactivating_an_image_removes_it_from_the_gallery(employees):
    from apps.employees.enrollment import deactivate_enrollment

    enrollment = employees["eng"].enrollments.first()
    assert enrollment.is_active

    deactivate_enrollment(enrollment, reason="test")
    enrollment.refresh_from_db()
    assert not enrollment.is_active


def test_withdrawing_consent_makes_an_employee_unmatchable(employees):
    """The lawful-basis switch. The gallery query applies the same rule, so
    withdrawal stops processing within one refresh interval."""
    employee = employees["eng"]
    assert employee.is_processable()

    employee.active_consent.withdraw(note="withdrawn in test")
    employee.refresh_from_db()

    assert not employee.is_processable()
    assert not employee.enrollments.filter(is_active=True).exists()


# ----------------------------------------------------------- with real models


@needs_models
@needs_faces
def test_a_good_portrait_is_enrolled_with_a_real_embedding(employees):
    from apps.employees.enrollment import enroll_image

    employee = employees["eng"]
    employee.enrollments.all().delete()

    result = enroll_image(
        employee, face_bytes("face_a.jpg"), filename="face_a.jpg"
    )

    assert result.accepted, result.message
    assert result.enrollment is not None

    enrollment = result.enrollment
    enrollment.refresh_from_db()
    assert enrollment.is_active
    assert not enrollment.conflict_flagged
    assert enrollment.face_width_px >= 100
    assert enrollment.model_version == "buffalo_l"
    assert 0.0 < enrollment.quality_score <= 1.0

    vector = np.asarray(enrollment.embedding, dtype=np.float32)
    assert vector.shape == (512,)
    # Stored normalised, which everything downstream assumes.
    assert float(np.linalg.norm(vector)) == pytest.approx(1.0, abs=1e-3)


@needs_models
@needs_faces
def test_enrolment_advises_when_the_gallery_is_still_thin(employees):
    from apps.employees.enrollment import enroll_image

    employee = employees["eng"]
    employee.enrollments.all().delete()

    result = enroll_image(employee, face_bytes("face_a.jpg"))
    assert result.accepted
    assert "recommended images" in result.message


@needs_models
@needs_faces
def test_an_image_matching_a_different_employee_is_held_for_review(employees):
    """The duplicate / twin / mislabelled-photo check.

    Enrolling the same face against two people would cause confident
    mis-identification between them, so the second one is stored but kept
    out of the live gallery until a human decides.
    """
    from apps.employees.enrollment import enroll_image

    first, second = employees["eng"], employees["ops"]
    first.enrollments.all().delete()
    second.enrollments.all().delete()

    accepted = enroll_image(first, face_bytes("face_a.jpg"))
    assert accepted.accepted

    # The same photograph, now offered for a different person.
    duplicate = enroll_image(second, face_bytes("face_a.jpg"))

    assert not duplicate.accepted
    assert duplicate.needs_review
    assert duplicate.conflict_with == first
    assert duplicate.conflict_similarity > 0.55
    assert "closely matches" in duplicate.message

    enrollment = duplicate.enrollment
    enrollment.refresh_from_db()
    # Stored for review, but NOT in use.
    assert enrollment.conflict_flagged
    assert not enrollment.is_active
    assert not second.is_processable()


@needs_models
@needs_faces
def test_two_different_people_do_not_trigger_a_conflict(employees):
    from apps.employees.enrollment import enroll_image

    first, second = employees["eng"], employees["ops"]
    first.enrollments.all().delete()
    second.enrollments.all().delete()

    assert enroll_image(first, face_bytes("face_a.jpg")).accepted
    other = enroll_image(second, face_bytes("face_b.jpg"))

    assert other.accepted, other.message
    assert not other.needs_review


@needs_models
@needs_faces
def test_a_conflict_can_be_accepted_for_genuine_twins(employees):
    from apps.employees.enrollment import enroll_image, resolve_conflict

    first, second = employees["eng"], employees["ops"]
    first.enrollments.all().delete()
    second.enrollments.all().delete()

    enroll_image(first, face_bytes("face_a.jpg"))
    conflicted = enroll_image(second, face_bytes("face_a.jpg"))
    enrollment = conflicted.enrollment

    resolve_conflict(enrollment, activate=True, note="confirmed identical twins")

    enrollment.refresh_from_db()
    assert enrollment.is_active
    assert not enrollment.conflict_flagged


@needs_models
@needs_faces
def test_a_tiny_face_is_rejected_with_actionable_advice(employees):
    import cv2

    from apps.employees.enrollment import enroll_image

    image = cv2.imdecode(
        np.frombuffer(face_bytes("face_a.jpg"), np.uint8), cv2.IMREAD_COLOR
    )
    tiny = cv2.resize(image, (image.shape[1] // 5, image.shape[0] // 5))
    encoded = cv2.imencode(".jpg", tiny)[1].tobytes()

    result = enroll_image(employees["eng"], encoded, filename="tiny.jpg")

    assert not result.accepted
    assert not result.needs_review
    # Nothing is stored for a rejected image.
    assert result.enrollment is None


@needs_models
@needs_faces
def test_a_group_photo_is_rejected(employees):
    import cv2

    from apps.employees.enrollment import enroll_image

    def decode(name):
        return cv2.imdecode(
            np.frombuffer(face_bytes(name), np.uint8), cv2.IMREAD_COLOR
        )

    a, b = decode("face_a.jpg"), decode("face_b.jpg")
    height = min(a.shape[0], b.shape[0])
    together = np.hstack([a[:height], cv2.resize(b, (b.shape[1], height))])
    encoded = cv2.imencode(".jpg", together)[1].tobytes()

    result = enroll_image(employees["eng"], encoded, filename="group.jpg")
    assert not result.accepted
    assert "only" in result.message or "faces were found" in result.message


@needs_models
@needs_faces
def test_every_enrolment_attempt_is_audited(employees):
    from apps.accounts.models import AuditLog
    from apps.employees.enrollment import enroll_image

    employee = employees["eng"]
    employee.enrollments.all().delete()

    enroll_image(employee, face_bytes("face_a.jpg"))
    enroll_image(employee, b"not an image")

    actions = set(AuditLog.objects.values_list("action", flat=True))
    assert "enrollment.created" in actions
    assert "enrollment.rejected" in actions

    rejection = AuditLog.objects.filter(action="enrollment.rejected").first()
    assert rejection.success is False
    assert rejection.target_employee_code == employee.employee_code


# ------------------------------------------------------------------ the view


@needs_models
@needs_faces
def test_hr_can_reach_the_enrolment_page_for_its_own_department(client, users, employees):
    from django.urls import reverse

    client.force_login(users["hr"])
    response = client.get(
        reverse("employees:enroll", args=[employees["eng"].uuid])
    )
    assert response.status_code == 200


def test_hr_cannot_enrol_outside_its_department(client, users, employees):
    """Enrolment is scoped like the directory: HR enrols its own people.

    Unlike history, there is no tracking-request path here - enrolling
    somebody is an administrative act, not an investigation.
    """
    from django.urls import reverse

    client.force_login(users["hr"])
    response = client.get(
        reverse("employees:enroll", args=[employees["ops"].uuid])
    )
    assert response.status_code == 404


def test_roles_without_enrollment_manage_are_refused(client, users, employees):
    from django.urls import reverse

    url = reverse("employees:enroll", args=[employees["eng"].uuid])
    for role in ("operator", "auditor", "employee", "security_admin"):
        client.force_login(users[role])
        response = client.get(url)
        assert response.status_code == 403, (
            f"{role} reached the enrolment page but holds no enrollment.manage"
        )
