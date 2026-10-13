"""
Face alignment: warp a detected face onto ArcFace's canonical 112x112 frame.

Why this matters more than it looks: ArcFace was trained on faces aligned to a
fixed five-point template. Feeding it a raw crop instead of an aligned one
costs a large chunk of accuracy, and the loss is worst exactly where this
project needs help - faces turned away from the camera. Alignment is the
cheapest accuracy you will ever buy in this pipeline.

The similarity transform is computed with Umeyama's method rather than pulling
in scikit-image, keeping the vision package to numpy and OpenCV.
"""

from __future__ import annotations

import cv2
import numpy as np

# ArcFace's canonical five-point template for a 112x112 crop:
# left eye, right eye, nose tip, left mouth corner, right mouth corner.
ARCFACE_TEMPLATE = np.array(
    [
        [38.2946, 51.6963],
        [73.5318, 51.5014],
        [56.0252, 71.7366],
        [41.5493, 92.3655],
        [70.7299, 92.2041],
    ],
    dtype=np.float32,
)

FACE_SIZE = 112


def umeyama_similarity(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """Least-squares similarity transform (scale + rotation + translation).

    Returns a 2x3 affine matrix mapping ``src`` onto ``dst``. Deliberately
    similarity rather than full affine: a face does not shear, and allowing
    shear lets a bad landmark drag the whole crop out of shape.

    Umeyama, "Least-squares estimation of transformation parameters between
    two point patterns" (1991).
    """
    src = np.asarray(src, dtype=np.float64)
    dst = np.asarray(dst, dtype=np.float64)
    num = src.shape[0]

    src_mean = src.mean(axis=0)
    dst_mean = dst.mean(axis=0)
    src_centred = src - src_mean
    dst_centred = dst - dst_mean

    covariance = dst_centred.T @ src_centred / num
    u, singular, vt = np.linalg.svd(covariance)

    # Guard against a reflection: a mirrored face would otherwise be
    # "aligned" by flipping it, which is never the right answer.
    correction = np.eye(2)
    if np.linalg.det(covariance) < 0:
        correction[1, 1] = -1

    rotation = u @ correction @ vt

    src_variance = src_centred.var(axis=0).sum()
    scale = 1.0 if src_variance == 0 else (singular * correction.diagonal()).sum() / src_variance

    translation = dst_mean - scale * (rotation @ src_mean)

    matrix = np.zeros((2, 3), dtype=np.float32)
    matrix[:2, :2] = scale * rotation
    matrix[:, 2] = translation
    return matrix


def align_face(
    image: np.ndarray, landmarks: np.ndarray, size: int = FACE_SIZE
) -> np.ndarray:
    """Warp one face to a ``size`` x ``size`` aligned crop.

    ``landmarks`` is the 5x2 array of image-space points from the detector.
    """
    template = ARCFACE_TEMPLATE
    if size != FACE_SIZE:
        template = ARCFACE_TEMPLATE * (size / FACE_SIZE)

    matrix = umeyama_similarity(np.asarray(landmarks, dtype=np.float32), template)
    return cv2.warpAffine(
        image, matrix, (size, size), flags=cv2.INTER_LINEAR, borderValue=0.0
    )


def estimate_pose(landmarks: np.ndarray) -> tuple[float, float]:
    """Rough yaw and pitch in degrees from the five landmarks.

    Not a calibrated head-pose estimate - it needs no camera intrinsics and
    costs nothing. It is accurate enough for the two jobs it has: rejecting
    badly turned faces at enrolment, and labelling an enrolment image as
    frontal / left / right so a gallery can be checked for pose coverage.

    Yaw is positive when the face is turned to the subject's left.
    """
    landmarks = np.asarray(landmarks, dtype=np.float32)
    left_eye, right_eye, nose, left_mouth, right_mouth = landmarks

    eye_centre = (left_eye + right_eye) / 2.0
    mouth_centre = (left_mouth + right_mouth) / 2.0

    # Yaw: how far the nose sits from the midline between the eyes,
    # normalised by interocular distance so it is scale-free.
    interocular = float(np.linalg.norm(right_eye - left_eye))
    if interocular < 1e-6:
        return 0.0, 0.0
    yaw_ratio = float((nose[0] - eye_centre[0]) / interocular)
    yaw = float(np.clip(yaw_ratio * 90.0, -90.0, 90.0))

    # Pitch: where the nose falls between the eye line and the mouth line.
    vertical = float(np.linalg.norm(mouth_centre - eye_centre))
    if vertical < 1e-6:
        return yaw, 0.0
    nose_fraction = float((nose[1] - eye_centre[1]) / vertical)
    # ~0.5 is level; smaller means looking up, larger means looking down.
    pitch = float(np.clip((nose_fraction - 0.5) * 120.0, -90.0, 90.0))

    return yaw, pitch


def pose_label(yaw: float, pitch: float, frontal_within: float = 15.0) -> str:
    """Map a pose to one of FaceEnrollment.Pose values."""
    if abs(yaw) <= frontal_within and abs(pitch) <= frontal_within:
        return "frontal"
    if abs(yaw) >= abs(pitch):
        return "left" if yaw > 0 else "right"
    return "down" if pitch > 0 else "up"
