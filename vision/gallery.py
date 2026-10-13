"""
Reads the camera list and the face gallery straight from Postgres, read-only.

Two deliberate choices:

* **No Django ORM.** Plain psycopg keeps this package importable without the
  web stack, and the worker connects as ``vision_ro``, a role with SELECT on
  eight tables and nothing else.
* **The consent rule lives in the query.** A person with withdrawn consent,
  an inactive record or no enrolments simply is not in the gallery, so
  withdrawing consent stops processing within one refresh interval
  (30 seconds by default) with nobody having to remember to act.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import numpy as np
import psycopg

from vision.config import settings

logger = logging.getLogger("vision.gallery")

# Cameras the worker should ingest. Prohibited zones are excluded in SQL as
# well as in the app, so a misconfiguration cannot start a toilet camera.
CAMERA_SQL = """
SELECT c.id,
       c.code,
       c.name,
       c.rtsp_url,
       c.purpose,
       c.sample_fps,
       c.roi_polygon,
       c.width,
       c.height,
       z.code  AS zone_code,
       z.id    AS zone_id
  FROM cameras_camera c
  JOIN org_zone z ON z.id = c.zone_id
 WHERE c.is_enabled = TRUE
   AND c.deleted_at IS NULL
   AND z.deleted_at IS NULL
   AND z.tracking_prohibited = FALSE
 ORDER BY c.id
"""

GALLERY_SQL = """
SELECT e.employee_code,
       f.embedding,
       f.quality_score
  FROM employees_faceenrollment f
  JOIN employees_employee e ON e.id = f.employee_id
 WHERE f.is_active = TRUE
   AND f.model_version = %(model_version)s
   AND e.deleted_at IS NULL
   AND e.is_trackable = TRUE
   AND e.status IN ('active', 'on_leave')
   -- Lawful basis check, in the query rather than in application code.
   AND EXISTS (
         SELECT 1
           FROM employees_consentrecord cr
          WHERE cr.employee_id = e.id
            AND cr.withdrawn_at IS NULL
       )
"""

CONFIG_SQL = """
SELECT match_threshold_high,
       match_threshold_low,
       vote_window,
       min_face_px,
       min_quality_score,
       model_version
  FROM cameras_pipelineconfig
 WHERE is_active = TRUE
 ORDER BY updated_at DESC
 LIMIT 1
"""


@dataclass
class CameraSpec:
    id: int
    code: str
    name: str
    rtsp_url: str
    purpose: str
    sample_fps: float
    roi_polygon: list
    width: int
    height: int
    zone_code: str
    zone_id: int

    @property
    def is_identity(self) -> bool:
        return self.purpose == "identity"


@dataclass
class PipelineParams:
    match_threshold_high: float = settings.match_threshold_high
    match_threshold_low: float = settings.match_threshold_low
    vote_window: int = settings.vote_window
    min_face_px: int = settings.min_face_px
    min_quality_score: float = 0.35
    model_version: str = "buffalo_l"


class Gallery:
    """In-memory face gallery with periodic refresh.

    Held as one normalised matrix so matching a face is a single matrix
    multiply rather than a database round trip per frame.
    """

    def __init__(self) -> None:
        self.codes: list[str] = []
        self.matrix: np.ndarray = np.zeros((0, 512), dtype=np.float32)
        self.params = PipelineParams()
        self._loaded_at: float = 0.0

    def _connect(self):
        return psycopg.connect(settings.database_url, connect_timeout=10)

    def refresh_if_stale(self) -> bool:
        if time.monotonic() - self._loaded_at < settings.gallery_refresh_seconds:
            return False
        self.refresh()
        return True

    def refresh(self) -> None:
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(CONFIG_SQL)
                row = cur.fetchone()
                if row:
                    self.params = PipelineParams(
                        match_threshold_high=float(row[0]),
                        match_threshold_low=float(row[1]),
                        vote_window=int(row[2]),
                        min_face_px=int(row[3]),
                        min_quality_score=float(row[4]),
                        model_version=row[5],
                    )

                cur.execute(GALLERY_SQL, {"model_version": self.params.model_version})
                rows = cur.fetchall()
        except psycopg.Error:
            logger.warning("gallery refresh failed; keeping previous copy", exc_info=True)
            return

        codes: list[str] = []
        vectors: list[np.ndarray] = []
        for code, embedding, _quality in rows:
            vec = _to_vector(embedding)
            if vec is None:
                continue
            codes.append(code)
            vectors.append(vec)

        if vectors:
            matrix = np.vstack(vectors).astype(np.float32)
            # L2-normalise once so cosine similarity is a plain dot product.
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            matrix = matrix / norms
        else:
            matrix = np.zeros((0, 512), dtype=np.float32)

        self.codes = codes
        self.matrix = matrix
        self._loaded_at = time.monotonic()
        logger.info(
            "gallery loaded: %s template(s) across %s person(s), model=%s",
            len(codes),
            len(set(codes)),
            self.params.model_version,
        )

    def match(self, embedding: np.ndarray) -> tuple[str | None, float, str]:
        """Nearest gallery entry for one embedding.

        Returns ``(employee_code, score, confidence)``. Callers should
        aggregate across a track rather than acting on a single frame - see
        :class:`vision.track.TrackVoter`.
        """
        if self.matrix.shape[0] == 0:
            return None, 0.0, "unknown"

        vec = embedding.astype(np.float32)
        norm = float(np.linalg.norm(vec))
        if norm == 0:
            return None, 0.0, "unknown"
        vec = vec / norm

        scores = self.matrix @ vec
        best = int(np.argmax(scores))
        score = float(scores[best])

        if score >= self.params.match_threshold_high:
            return self.codes[best], score, "confirmed"
        if score >= self.params.match_threshold_low:
            return self.codes[best], score, "probable"
        return None, score, "unknown"


def load_cameras() -> list[CameraSpec]:
    """Cameras this shard is responsible for."""
    from vision.crypto import decrypt

    with psycopg.connect(settings.database_url, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(CAMERA_SQL)
            rows = cur.fetchall()

    cameras: list[CameraSpec] = []
    for row in rows:
        camera_id = row[0]
        if not settings.owns_camera(camera_id):
            continue
        try:
            rtsp_url = decrypt(row[3])
        except Exception:
            logger.error(
                "could not decrypt rtsp_url for camera %s - check "
                "FIELD_ENCRYPTION_KEY matches the web tier",
                row[1],
            )
            continue
        cameras.append(
            CameraSpec(
                id=camera_id,
                code=row[1],
                name=row[2],
                rtsp_url=rtsp_url,
                purpose=row[4],
                sample_fps=float(row[5]),
                roi_polygon=row[6] or [],
                width=row[7],
                height=row[8],
                zone_code=row[9],
                zone_id=row[10],
            )
        )
    return cameras


def _to_vector(value) -> np.ndarray | None:
    """pgvector comes back as a list, a string, or a numpy array."""
    if value is None:
        return None
    if isinstance(value, np.ndarray):
        return value
    if isinstance(value, list | tuple):
        return np.asarray(value, dtype=np.float32)
    if isinstance(value, str):
        try:
            return np.fromstring(value.strip("[]"), sep=",", dtype=np.float32)
        except ValueError:
            return None
    return None
