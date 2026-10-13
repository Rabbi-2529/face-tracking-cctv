"""
Face enrolment service.

Enrolment decides the ceiling on this system's accuracy. Everything the
matcher ever does is a comparison against what lands here, so the gate is
strict and every rejection explains itself in words HR can act on.

Three things this does that are easy to leave out and expensive to omit:

* **Rejects bad images at upload**, rather than storing them and wondering
  later why one person is never recognised.
* **Checks every new image against the existing gallery.** If it matches a
  *different* enrolled employee, that is a duplicate, a twin, or a
  mislabelled upload - all of which cause confident mis-identification, and
  all of which are invisible unless you look.
* **Records the model version.** Embeddings from different models are not
  comparable, so a model upgrade means re-embedding the whole gallery, and
  you need to know which rows are stale.

It uses the same :class:`vision.pipeline.FacePipeline` as the live camera
workers. That is deliberate: if enrolment aligned or normalised faces even
slightly differently, every gallery vector would sit in a different place
from the vectors it gets compared against.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
from django.core.files.base import ContentFile
from django.db import transaction
from pgvector.django import CosineDistance

from apps.common.audit import record
from apps.employees.models import Employee, FaceEnrollment

logger = logging.getLogger("cctv.enrollment")

# Minimum images for a gallery that works in the real world. One frontal
# photo is the most common cause of poor accuracy: the gallery then has no
# idea what the person looks like at 20 degrees off axis, which is most of
# what a doorway camera actually sees.
RECOMMENDED_IMAGES = 5

# A new image matching a *different* employee at or above this similarity is
# flagged for review rather than silently enrolled.
CONFLICT_SIMILARITY = 0.55

MAX_UPLOAD_BYTES = 12 * 1024 * 1024


@dataclass
class EnrollmentResult:
    """Outcome of one upload attempt."""

    accepted: bool
    message: str
    enrollment: FaceEnrollment | None = None
    quality: dict | None = None
    conflict_with: Employee | None = None
    conflict_similarity: float | None = None

    @property
    def needs_review(self) -> bool:
        return self.conflict_with is not None


def _decode_image(data: bytes) -> np.ndarray | None:
    import cv2

    array = np.frombuffer(data, dtype=np.uint8)
    image = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if image is None:
        return None
    # Very large uploads waste inference time for no gain; the detector runs
    # at 640x640 regardless.
    height, width = image.shape[:2]
    longest = max(height, width)
    if longest > 2000:
        scale = 2000 / longest
        image = cv2.resize(
            image,
            (int(width * scale), int(height * scale)),
            interpolation=cv2.INTER_AREA,
        )
    return image


def find_gallery_conflict(
    embedding: np.ndarray, employee: Employee, *, model_version: str
) -> tuple[Employee | None, float]:
    """Nearest active gallery entry belonging to somebody else.

    Only compares within the same model version - a vector from a different
    model is not meaningfully comparable, and treating it as one would
    produce nonsense conflicts.
    """
    nearest = (
        FaceEnrollment.objects.filter(is_active=True, model_version=model_version)
        .exclude(employee=employee)
        .exclude(employee__deleted_at__isnull=False)
        .annotate(distance=CosineDistance("embedding", embedding))
        .select_related("employee")
        .order_by("distance")
        .first()
    )
    if nearest is None:
        return None, 0.0

    # pgvector cosine distance is 1 - cosine similarity.
    similarity = 1.0 - float(nearest.distance)
    if similarity >= CONFLICT_SIMILARITY:
        return nearest.employee, similarity
    return None, similarity


@transaction.atomic
def enroll_image(
    employee: Employee,
    image_bytes: bytes,
    *,
    filename: str = "upload.jpg",
    enrolled_by=None,
    source: str = FaceEnrollment.Source.UPLOAD,
) -> EnrollmentResult:
    """Assess and, if acceptable, store one enrolment image."""

    def _reject(message: str, *, why: str) -> EnrollmentResult:
        """Refuse an upload, audited.

        Every enrolment attempt leaves a row, including the ones that never
        reach a model. A run of malformed uploads against this endpoint is
        worth seeing, and "every attempt is audited" is only a usable
        invariant if it has no exceptions.
        """
        record(
            "enrollment.rejected",
            actor=enrolled_by,
            target_employee=employee,
            reason=why,
            success=False,
            metadata={"filename": filename, "bytes": len(image_bytes or b"")},
        )
        return EnrollmentResult(False, message)

    if not image_bytes:
        return _reject("The uploaded file was empty.", why="empty upload")
    if len(image_bytes) > MAX_UPLOAD_BYTES:
        return _reject(
            f"The file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
            why=f"oversized upload ({len(image_bytes)} bytes)",
        )

    image = _decode_image(image_bytes)
    if image is None:
        return _reject(
            "That file could not be read as an image. Use JPEG or PNG.",
            why="undecodable upload",
        )

    from vision.pipeline import get_pipeline

    pipeline = get_pipeline()

    try:
        face, error = pipeline.analyse_enrollment(image)
    except FileNotFoundError as exc:
        # Models missing is an operator problem, not an HR one; say which.
        logger.error("enrolment failed, models unavailable: %s", exc)
        return EnrollmentResult(
            False,
            "Face recognition models are not installed on the server. "
            "Ask an administrator to run the model download step.",
        )

    quality = face.quality.as_dict() if face is not None else None

    if error or face is None or face.embedding is None:
        record(
            "enrollment.rejected",
            actor=enrolled_by,
            target_employee=employee,
            reason=error[:500] if error else "no usable face",
            success=False,
            metadata={"quality": quality, "filename": filename},
        )
        return EnrollmentResult(False, error or "No usable face found.", quality=quality)

    conflict, similarity = find_gallery_conflict(
        face.embedding, employee, model_version=pipeline.model_version
    )

    enrollment = FaceEnrollment(
        employee=employee,
        embedding=face.embedding.tolist(),
        pose=face.pose,
        source=source,
        quality_score=face.quality.score,
        face_width_px=face.face_width_px,
        sharpness=face.quality.sharpness,
        brightness=face.quality.brightness,
        yaw_degrees=face.yaw_degrees,
        pitch_degrees=face.pitch_degrees,
        enrolled_by=enrolled_by,
        model_version=pipeline.model_version,
        conflict_flagged=conflict is not None,
        conflict_with=conflict,
        # A conflicting image is stored but kept OUT of the live gallery
        # until a human resolves it. Enrolling it would cause confident
        # mis-identification between two real people.
        is_active=conflict is None,
    )
    enrollment.image.save(filename, ContentFile(image_bytes), save=False)
    enrollment.save()

    if conflict is not None:
        record(
            "enrollment.conflict",
            actor=enrolled_by,
            target_employee=employee,
            object_type="employees.FaceEnrollment",
            object_id=enrollment.pk,
            reason=(
                f"matches {conflict.employee_code} at {similarity:.3f}; "
                "held inactive pending review"
            ),
            metadata={"conflict_with": conflict.employee_code, "similarity": similarity},
        )
        return EnrollmentResult(
            accepted=False,
            message=(
                f"This face closely matches an already-enrolled employee "
                f"({conflict.full_name}, {conflict.employee_code}) at "
                f"{similarity:.0%} similarity. The image has been saved but is "
                "NOT in use, pending review. This usually means a duplicate "
                "record, a mislabelled photo, or identical twins."
            ),
            enrollment=enrollment,
            quality=quality,
            conflict_with=conflict,
            conflict_similarity=similarity,
        )

    record(
        "enrollment.created",
        actor=enrolled_by,
        target_employee=employee,
        object_type="employees.FaceEnrollment",
        object_id=enrollment.pk,
        reason=f"pose={face.pose} quality={face.quality.score:.3f}",
        metadata={"quality": quality, "model_version": pipeline.model_version},
    )

    active = employee.enrollments.filter(is_active=True).count()
    message = f"Enrolled. {face.pose.title()} view, quality {face.quality.score:.0%}."
    if active < RECOMMENDED_IMAGES:
        message += (
            f" {active} of {RECOMMENDED_IMAGES} recommended images. "
            "Add more views - slightly turned left and right, and with and "
            "without glasses - for reliable recognition."
        )
    elif not employee.has_active_consent:
        message += (
            " Note: this employee has no active consent record, so they will "
            "not be matched until one is recorded."
        )

    return EnrollmentResult(True, message, enrollment=enrollment, quality=quality)


def gallery_coverage(employee: Employee) -> dict:
    """Report what the gallery actually holds for one person.

    Pose coverage matters more than raw count. Five frontal photos are worth
    much less than one frontal plus two turned left and two turned right,
    because a doorway camera rarely sees a perfectly frontal face.
    """
    active = list(employee.enrollments.filter(is_active=True))
    by_pose: dict[str, int] = {}
    for enrollment in active:
        by_pose[enrollment.pose] = by_pose.get(enrollment.pose, 0) + 1

    advice: list[str] = []
    if len(active) < RECOMMENDED_IMAGES:
        advice.append(
            f"Add {RECOMMENDED_IMAGES - len(active)} more image(s); "
            f"{RECOMMENDED_IMAGES} is the practical minimum."
        )
    if not by_pose.get("frontal"):
        advice.append("No frontal image. Add one - it carries the most weight.")
    if not by_pose.get("left") and not by_pose.get("right"):
        advice.append(
            "No turned views. Doorway cameras rarely see a perfectly frontal "
            "face, so add one turned slightly left and one slightly right."
        )

    mean_quality = (
        sum(e.quality_score for e in active) / len(active) if active else 0.0
    )
    if active and mean_quality < 0.55:
        advice.append(
            "Average image quality is low; better photos would improve matching."
        )

    return {
        "active_count": len(active),
        "recommended": RECOMMENDED_IMAGES,
        "by_pose": by_pose,
        "mean_quality": round(mean_quality, 3),
        "flagged_count": employee.enrollments.filter(conflict_flagged=True).count(),
        "model_versions": sorted({e.model_version for e in active}),
        "is_processable": employee.is_processable(),
        "has_consent": employee.has_active_consent,
        "advice": advice,
    }


def deactivate_enrollment(enrollment: FaceEnrollment, *, actor=None, reason: str = "") -> None:
    """Remove one image from the live gallery without deleting the record."""
    enrollment.is_active = False
    enrollment.save(update_fields=["is_active"])
    record(
        "enrollment.deactivated",
        actor=actor,
        target_employee=enrollment.employee,
        object_type="employees.FaceEnrollment",
        object_id=enrollment.pk,
        reason=reason,
    )


def resolve_conflict(
    enrollment: FaceEnrollment, *, activate: bool, actor=None, note: str = ""
) -> None:
    """Resolve a flagged enrolment.

    ``activate=True`` accepts it into the gallery anyway - the legitimate
    case being genuine twins, where two people really do look alike and both
    need enrolling. Expect lower confidence for both thereafter.
    """
    enrollment.conflict_flagged = False
    enrollment.is_active = activate
    enrollment.save(update_fields=["conflict_flagged", "is_active"])
    record(
        "enrollment.conflict_resolved",
        actor=actor,
        target_employee=enrollment.employee,
        object_type="employees.FaceEnrollment",
        object_id=enrollment.pk,
        reason=f"{'activated' if activate else 'rejected'}: {note}"[:500],
    )
