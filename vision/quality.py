"""
The face quality gate.

This is the component that decides what the matcher is allowed to see, and it
does more for real-world accuracy than any threshold tuning. A blurred,
tiny, backlit or side-on crop produces an embedding that sits nowhere useful
in the vector space; letting it reach the matcher is how a system starts
confidently naming the wrong person.

Two uses, deliberately with different strictness:

* **Enrolment** (``assess_for_enrollment``) is strict. A bad gallery image
  poisons every future match against that person, so it is cheaper to reject
  it and ask HR for another photo.
* **Live recognition** (``assess_for_matching``) is permissive but honest. A
  marginal frame still contributes to the track vote, weighted by its quality,
  because ten mediocre views of the same person beat one good one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np


@dataclass
class QualityReport:
    """Measured properties of one face crop, plus the verdict."""

    face_width_px: int
    sharpness: float
    brightness: float
    contrast: float
    yaw_degrees: float
    pitch_degrees: float
    detection_score: float
    score: float = 0.0
    passed: bool = True
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "face_width_px": self.face_width_px,
            "sharpness": round(self.sharpness, 2),
            "brightness": round(self.brightness, 2),
            "contrast": round(self.contrast, 2),
            "yaw_degrees": round(self.yaw_degrees, 2),
            "pitch_degrees": round(self.pitch_degrees, 2),
            "detection_score": round(self.detection_score, 4),
            "score": round(self.score, 4),
            "passed": self.passed,
            "reasons": list(self.reasons),
        }


# --- enrolment thresholds -------------------------------------------------
# Strict: these images go in the gallery and affect every future match.
ENROLL_MIN_FACE_PX = 100
ENROLL_MIN_SHARPNESS = 60.0
ENROLL_BRIGHTNESS_RANGE = (55.0, 200.0)
ENROLL_MIN_CONTRAST = 25.0
ENROLL_MAX_YAW = 35.0
ENROLL_MAX_PITCH = 25.0
ENROLL_MIN_DETECTION_SCORE = 0.70
ENROLL_MIN_SCORE = 0.50

# --- live thresholds -----------------------------------------------------
# Permissive: a weak frame still contributes to a track vote.
LIVE_MIN_SHARPNESS = 12.0
LIVE_BRIGHTNESS_RANGE = (25.0, 232.0)
LIVE_MAX_YAW = 55.0
LIVE_MAX_PITCH = 45.0


def measure(
    crop: np.ndarray,
    *,
    face_width_px: int,
    yaw_degrees: float,
    pitch_degrees: float,
    detection_score: float,
) -> QualityReport:
    """Measure a face crop. No verdict yet - that is the caller's policy."""
    grey = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop

    # Variance of the Laplacian: the standard cheap focus measure. Scaled by
    # crop area so a big soft face is not mistaken for a small sharp one.
    sharpness = float(cv2.Laplacian(grey, cv2.CV_64F).var())
    brightness = float(np.mean(grey))
    contrast = float(np.std(grey))

    return QualityReport(
        face_width_px=int(face_width_px),
        sharpness=sharpness,
        brightness=brightness,
        contrast=contrast,
        yaw_degrees=float(yaw_degrees),
        pitch_degrees=float(pitch_degrees),
        detection_score=float(detection_score),
    )


def _composite_score(report: QualityReport, *, reference_face_px: int) -> float:
    """Blend the measurements into a single 0..1 usability score.

    Weighted towards face size and sharpness because those are what actually
    determine whether an embedding is discriminative; exposure and pose are
    secondary and partially recoverable.
    """
    size_term = min(1.0, report.face_width_px / reference_face_px)
    sharp_term = min(1.0, report.sharpness / 150.0)

    # Exposure: 1.0 at mid-grey, falling off towards clipped black or white.
    exposure_term = 1.0 - min(1.0, abs(report.brightness - 128.0) / 128.0)
    contrast_term = min(1.0, report.contrast / 60.0)

    pose_penalty = min(1.0, (abs(report.yaw_degrees) / 90.0) * 0.8
                       + (abs(report.pitch_degrees) / 90.0) * 0.6)
    pose_term = max(0.0, 1.0 - pose_penalty)

    confidence_term = min(1.0, report.detection_score)

    return float(
        0.30 * size_term
        + 0.28 * sharp_term
        + 0.14 * exposure_term
        + 0.08 * contrast_term
        + 0.12 * pose_term
        + 0.08 * confidence_term
    )


def assess_for_enrollment(
    crop: np.ndarray,
    *,
    face_width_px: int,
    yaw_degrees: float,
    pitch_degrees: float,
    detection_score: float,
) -> QualityReport:
    """Strict gate for gallery images.

    Every rejection carries a reason written for the person uploading, not
    for a developer reading logs - HR needs to know what to photograph
    differently, not that a Laplacian variance was 41.3.
    """
    report = measure(
        crop,
        face_width_px=face_width_px,
        yaw_degrees=yaw_degrees,
        pitch_degrees=pitch_degrees,
        detection_score=detection_score,
    )
    report.score = _composite_score(report, reference_face_px=ENROLL_MIN_FACE_PX * 2)

    reasons: list[str] = []

    if report.face_width_px < ENROLL_MIN_FACE_PX:
        reasons.append(
            f"The face is only {report.face_width_px}px wide; at least "
            f"{ENROLL_MIN_FACE_PX}px is needed. Use a closer or higher-resolution photo."
        )
    if report.sharpness < ENROLL_MIN_SHARPNESS:
        reasons.append("The image is too blurred or out of focus.")
    low, high = ENROLL_BRIGHTNESS_RANGE
    if report.brightness < low:
        reasons.append("The face is too dark. Add light or avoid shooting into shadow.")
    elif report.brightness > high:
        reasons.append(
            "The face is washed out. Avoid strong backlight or direct sun."
        )
    if report.contrast < ENROLL_MIN_CONTRAST:
        reasons.append("The image is too flat; facial detail is not visible.")
    if abs(report.yaw_degrees) > ENROLL_MAX_YAW:
        reasons.append(
            f"The head is turned too far to the side ({abs(report.yaw_degrees):.0f} degrees). "
            "Face the camera more directly."
        )
    if abs(report.pitch_degrees) > ENROLL_MAX_PITCH:
        reasons.append("The head is tilted too far up or down.")
    if report.detection_score < ENROLL_MIN_DETECTION_SCORE:
        reasons.append("The face was detected with low confidence; try a clearer photo.")
    if report.score < ENROLL_MIN_SCORE and not reasons:
        reasons.append("Overall image quality is too low for reliable recognition.")

    report.reasons = reasons
    report.passed = not reasons
    return report


def assess_for_matching(
    crop: np.ndarray,
    *,
    face_width_px: int,
    yaw_degrees: float,
    pitch_degrees: float,
    detection_score: float,
    min_face_px: int,
    min_quality_score: float = 0.0,
) -> QualityReport:
    """Permissive gate for live frames.

    ``min_face_px`` and ``min_quality_score`` come from the active
    PipelineConfig so they can be tuned per deployment without a redeploy.
    """
    report = measure(
        crop,
        face_width_px=face_width_px,
        yaw_degrees=yaw_degrees,
        pitch_degrees=pitch_degrees,
        detection_score=detection_score,
    )
    report.score = _composite_score(report, reference_face_px=max(min_face_px * 2, 80))

    reasons: list[str] = []
    if report.face_width_px < min_face_px:
        reasons.append(f"face {report.face_width_px}px below minimum {min_face_px}px")
    if report.sharpness < LIVE_MIN_SHARPNESS:
        reasons.append("motion blur")
    low, high = LIVE_BRIGHTNESS_RANGE
    if not (low <= report.brightness <= high):
        reasons.append("exposure outside usable range")
    if abs(report.yaw_degrees) > LIVE_MAX_YAW:
        reasons.append("turned too far from camera")
    if abs(report.pitch_degrees) > LIVE_MAX_PITCH:
        reasons.append("tilted too far")
    if not reasons and report.score < min_quality_score:
        reasons.append(f"overall quality {report.score:.2f} below {min_quality_score:.2f}")

    report.reasons = reasons
    report.passed = not reasons
    return report
