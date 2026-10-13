"""
Annotated preview frames for the live camera view.

The camera worker already decodes every frame and already runs detection on
the sampled ones. Publishing an annotated JPEG from there costs almost
nothing, and it means the web tier never decodes video or runs inference to
show somebody a picture.

The earlier design proxied the RTSP stream through Django and annotated it
per viewer. That was wrong in three ways: it duplicated decode and inference
work the worker was already doing, it needed a long-lived connection per
viewer, and an abandoned browser tab left a generator running for minutes.
This version has none of those problems and supports any number of viewers.

Flow:

1. A viewer opens the live view. The web tier sets ``preview:want:<code>``
   in Redis with a short TTL.
2. The worker checks that key about once a second. Only while it is set does
   it draw overlays and publish - so nothing is wasted when nobody is
   looking.
3. The worker writes the JPEG to ``preview:frame:<code>`` with a short TTL.
4. The web tier serves the latest frame as a plain image. A stale or absent
   key means "no live picture", which is an honest answer.
"""

from __future__ import annotations

import logging
import time

import redis

from vision.config import settings

logger = logging.getLogger("vision.preview")

WANT_KEY = "preview:want:{code}"
FRAME_KEY = "preview:frame:{code}"

# The frame expires quickly, so a dead worker shows as "no picture" rather
# than a stale image that looks live.
FRAME_TTL_SECONDS = 6
# How often the worker re-checks whether anyone is watching.
WANT_POLL_SECONDS = 1.0
PREVIEW_FPS = 4.0
JPEG_QUALITY = 70
MAX_WIDTH = 960

# Overlay colours are BGR.
COLOUR_GOOD = (80, 180, 80)
COLOUR_MARGINAL = (60, 170, 220)
COLOUR_UNUSABLE = (80, 80, 200)


class PreviewPublisher:
    """Draws overlays and publishes frames, only while someone is watching."""

    def __init__(self, camera_code: str) -> None:
        self.camera_code = camera_code
        self.want_key = WANT_KEY.format(code=camera_code)
        self.frame_key = FRAME_KEY.format(code=camera_code)
        self._client: redis.Redis | None = None
        self._wanted = False
        self._checked_at = 0.0
        self._published_at = 0.0
        self.published = 0

    @property
    def client(self) -> redis.Redis:
        if self._client is None:
            # Binary: the value is JPEG bytes, not text.
            self._client = redis.Redis.from_url(
                settings.redis_url, decode_responses=False
            )
        return self._client

    def wanted(self) -> bool:
        """Is anyone watching? Cached so this is not a Redis call per frame."""
        now = time.monotonic()
        if now - self._checked_at < WANT_POLL_SECONDS:
            return self._wanted
        self._checked_at = now
        try:
            self._wanted = bool(self.client.exists(self.want_key))
        except redis.RedisError:
            self._wanted = False
            self._client = None
        return self._wanted

    def due(self) -> bool:
        """Rate-limit publishing independently of the sampling rate."""
        return time.monotonic() - self._published_at >= 1.0 / PREVIEW_FPS

    def publish(self, frame, faces, *, min_face_px: int) -> None:
        """Annotate a frame and store it for the web tier to serve.

        ``faces`` are the already-analysed faces for this frame, so the
        overlay reflects exactly what the pipeline decided - including which
        ones it threw away and why.
        """
        import cv2

        try:
            annotated = draw_overlay(
                frame.copy(), faces, min_face_px=min_face_px, camera_code=self.camera_code
            )
            annotated = fit_width(annotated)
            ok, buffer = cv2.imencode(
                ".jpg", annotated, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY]
            )
            if not ok:
                return
            self.client.set(
                self.frame_key, buffer.tobytes(), ex=FRAME_TTL_SECONDS
            )
            self._published_at = time.monotonic()
            self.published += 1
        except redis.RedisError:
            logger.debug("preview publish failed for %s", self.camera_code, exc_info=True)
            self._client = None
        except Exception:
            logger.warning(
                "could not build preview for %s", self.camera_code, exc_info=True
            )


def fit_width(frame, max_width: int = MAX_WIDTH):
    import cv2

    height, width = frame.shape[:2]
    if width <= max_width:
        return frame
    scale = max_width / width
    return cv2.resize(
        frame, (max_width, int(height * scale)), interpolation=cv2.INTER_AREA
    )


def draw_overlay(frame, faces, *, min_face_px: int, camera_code: str = ""):
    """Draw boxes, face widths and gate verdicts onto a frame.

    The colour encodes the one thing an installer is trying to fix: whether
    a face is big and clean enough to be identified at all. The pixel width
    is written next to every box because that number, not any threshold, is
    what decides whether a camera can name anyone.
    """
    import cv2

    for face in faces:
        x1, y1, x2, y2 = face.bbox
        quality = face.quality

        if not quality.passed:
            colour = COLOUR_UNUSABLE
            verdict = "unusable"
        elif face.face_width_px >= 100:
            colour = COLOUR_GOOD
            verdict = "good"
        else:
            colour = COLOUR_MARGINAL
            verdict = "marginal"

        label = f"{face.face_width_px}px {verdict}"
        name = getattr(face, "preview_label", "")
        if name:
            label = f"{name}  {label}"

        cv2.rectangle(frame, (x1, y1), (x2, y2), colour, 2)

        text_y = y1 - 8 if y1 > 26 else y2 + 20
        cv2.putText(
            frame, label, (x1, text_y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, colour, 2,
            cv2.LINE_AA,
        )

        if not quality.passed and quality.reasons:
            cv2.putText(
                frame, quality.reasons[0][:44], (x1, y2 + 18),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, colour, 1, cv2.LINE_AA,
            )

    header = f"{camera_code}  min usable face {min_face_px}px"
    cv2.putText(
        frame, header, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
        (255, 255, 255), 2, cv2.LINE_AA,
    )
    return frame
