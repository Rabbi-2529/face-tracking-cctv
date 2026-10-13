"""
The face pipeline, shared by the camera workers and by Django enrolment.

One class, two callers:

* ``vision.runner`` uses :meth:`FacePipeline.analyse_frame` on live frames.
* ``apps.employees.enrollment`` uses :meth:`FacePipeline.analyse_enrollment`
  on images HR uploads.

Sharing the code matters. If enrolment aligned and normalised faces even
slightly differently from live matching, every gallery vector would sit in a
subtly different place from the vectors it is compared against, and accuracy
would drop for reasons no amount of threshold tuning could fix.

Models are loaded lazily and cached per process, so importing this module is
cheap and a web worker that never enrols anybody never pays for the 190 MB of
weights.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

import numpy as np

from vision.align import align_face, estimate_pose, pose_label
from vision.quality import QualityReport, assess_for_enrollment, assess_for_matching

logger = logging.getLogger("vision.pipeline")

_lock = threading.Lock()
_instance: FacePipeline | None = None


@dataclass
class AnalysedFace:
    """A detected face with everything downstream needs."""

    bbox: tuple[int, int, int, int]
    landmarks: np.ndarray
    detection_score: float
    face_width_px: int
    yaw_degrees: float
    pitch_degrees: float
    pose: str
    quality: QualityReport
    aligned: np.ndarray | None = None
    embedding: np.ndarray | None = None

    @property
    def usable(self) -> bool:
        return self.quality.passed and self.embedding is not None


class FacePipeline:
    """Detector plus embedder, with the quality gate between them."""

    def __init__(
        self,
        *,
        detector=None,
        embedder=None,
        detection_threshold: float = 0.5,
    ) -> None:
        self._detector = detector
        self._embedder = embedder
        self.detection_threshold = detection_threshold

    # --------------------------------------------------------- lazy loading

    @property
    def detector(self):
        if self._detector is None:
            from vision.detect.scrfd import SCRFDDetector

            self._detector = SCRFDDetector(score_threshold=self.detection_threshold)
        return self._detector

    @property
    def embedder(self):
        if self._embedder is None:
            from vision.embed.arcface import ArcFaceEmbedder

            self._embedder = ArcFaceEmbedder()
        return self._embedder

    @property
    def model_version(self) -> str:
        return self.embedder.model_version

    # ------------------------------------------------------------ live path

    def analyse_frame(
        self,
        frame: np.ndarray,
        *,
        min_face_px: int,
        min_quality_score: float = 0.0,
        max_faces: int = 8,
        embed: bool = True,
        roi: list | None = None,
    ) -> list[AnalysedFace]:
        """Detect, gate and embed every usable face in a live frame.

        Faces that fail the gate are still returned, with ``embedding`` unset
        and ``quality.reasons`` populated. The caller logs why a face was
        skipped rather than silently dropping it - an estate where most faces
        fail for one specific reason is telling you something about the
        cameras.

        Faces outside the camera's region of interest are dropped before any
        of that, and not returned at all: a masked-out window or a corridor
        the organisation does not control is not this camera's business.
        """
        faces = self.detector.detect(frame, max_faces=max_faces)
        if roi:
            height, width = frame.shape[:2]
            faces = [f for f in faces if inside_roi(f.bbox_int(), roi, width, height)]
        if not faces:
            return []

        analysed: list[AnalysedFace] = []
        to_embed: list[tuple[int, np.ndarray]] = []

        for face in faces:
            yaw, pitch = estimate_pose(face.landmarks)
            aligned = align_face(frame, face.landmarks)
            quality = assess_for_matching(
                aligned,
                face_width_px=face.width,
                yaw_degrees=yaw,
                pitch_degrees=pitch,
                detection_score=face.score,
                min_face_px=min_face_px,
                min_quality_score=min_quality_score,
            )

            record = AnalysedFace(
                bbox=face.bbox_int(),
                landmarks=face.landmarks,
                detection_score=face.score,
                face_width_px=face.width,
                yaw_degrees=yaw,
                pitch_degrees=pitch,
                pose=pose_label(yaw, pitch),
                quality=quality,
                aligned=aligned,
            )
            if quality.passed and embed:
                to_embed.append((len(analysed), aligned))
            analysed.append(record)

        # One batched call for every usable face in the frame.
        if to_embed:
            embeddings = self.embedder.embed_batch([crop for _, crop in to_embed])
            for (index, _), embedding in zip(to_embed, embeddings, strict=True):
                analysed[index].embedding = embedding

        return analysed

    # ------------------------------------------------------ enrolment path

    def analyse_enrollment(self, image: np.ndarray) -> tuple[AnalysedFace | None, str]:
        """Assess one uploaded enrolment image.

        Returns ``(face, error)``. On success ``error`` is empty; on failure
        ``face`` is None or unusable and ``error`` is a message written for
        the person uploading.

        Rejecting a multi-face image rather than guessing is deliberate: a
        group photo silently enrolled against the wrong person is far worse
        than a rejected upload.
        """
        faces = self.detector.detect(image, max_faces=5)

        if not faces:
            return None, (
                "No face was found in this image. Use a clear, front-facing "
                "photo of one person."
            )
        if len(faces) > 1:
            return None, (
                f"{len(faces)} faces were found. Upload a photo containing only "
                "the person being enrolled."
            )

        face = faces[0]
        yaw, pitch = estimate_pose(face.landmarks)
        aligned = align_face(image, face.landmarks)
        quality = assess_for_enrollment(
            aligned,
            face_width_px=face.width,
            yaw_degrees=yaw,
            pitch_degrees=pitch,
            detection_score=face.score,
        )

        record = AnalysedFace(
            bbox=face.bbox_int(),
            landmarks=face.landmarks,
            detection_score=face.score,
            face_width_px=face.width,
            yaw_degrees=yaw,
            pitch_degrees=pitch,
            pose=pose_label(yaw, pitch),
            quality=quality,
            aligned=aligned,
        )

        if not quality.passed:
            return record, " ".join(quality.reasons)

        record.embedding = self.embedder.embed(aligned)
        return record, ""


def inside_roi(
    bbox: tuple[int, int, int, int], roi: list, frame_width: int, frame_height: int
) -> bool:
    """Is the face's centre inside the camera's region of interest?

    ``roi`` is ``Camera.roi_polygon``: ``[[x, y], ...]`` in normalised 0-1
    coordinates, so it survives a resolution change. Fewer than three points
    is not a polygon and means "no mask".
    """
    if not roi or len(roi) < 3:
        return True
    import cv2

    polygon = np.asarray(
        [[float(x) * frame_width, float(y) * frame_height] for x, y in roi],
        dtype=np.float32,
    )
    x1, y1, x2, y2 = bbox
    centre = ((x1 + x2) / 2.0, (y1 + y2) / 2.0)
    return cv2.pointPolygonTest(polygon, centre, False) >= 0


def get_pipeline() -> FacePipeline:
    """Process-wide pipeline, built on first use.

    Both ONNX Runtime sessions are stateful and not thread-safe, so the
    pipeline is created under a lock and then reused. Django serves requests
    on threads, which is why the lock is here rather than a bare global.
    """
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = FacePipeline()
    return _instance


def reset_pipeline() -> None:
    """Drop the cached pipeline. Used by tests and after a model swap."""
    global _instance
    with _lock:
        _instance = None
