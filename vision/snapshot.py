"""
Snapshot writing.

Snapshots are the evidence that makes a timeline checkable: without them, a
movement record is an assertion nobody can verify or dispute. With them, a
mis-identification can be seen and corrected.

Two rules the rest of the system depends on:

* **A face crop, not the whole frame.** Storing full frames would capture
  every uninvolved person who happened to be in shot, which is more personal
  data than the purpose needs.
* **Written to a private location, never a browsable path.** They are only
  ever served through the permission-checked streaming endpoint in
  ``apps/tracking/views.py``, which re-checks access and audits every fetch.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from pathlib import Path

import cv2
import numpy as np

from vision.config import settings

logger = logging.getLogger("vision.snapshot")

JPEG_QUALITY = 88
# Context around the face box. A bare crop is hard for a human to judge;
# a little surrounding detail makes verification much easier.
CONTEXT_RATIO = 0.35


def _key_for(camera_code: str, track_id: str, when: dt.datetime) -> str:
    safe_track = track_id.replace(":", "-").replace("/", "-")
    return (
        f"snapshots/{when:%Y/%m/%d}/{camera_code}/"
        f"{when:%H%M%S%f}-{safe_track}.jpg"
    )


def crop_with_context(
    frame: np.ndarray, bbox: tuple[int, int, int, int]
) -> np.ndarray:
    """Crop the face plus a margin, clipped to the frame."""
    height, width = frame.shape[:2]
    x1, y1, x2, y2 = bbox

    box_w, box_h = max(1, x2 - x1), max(1, y2 - y1)
    pad_x, pad_y = int(box_w * CONTEXT_RATIO), int(box_h * CONTEXT_RATIO)

    cx1 = max(0, x1 - pad_x)
    cy1 = max(0, y1 - pad_y)
    cx2 = min(width, x2 + pad_x)
    cy2 = min(height, y2 + pad_y)

    if cx2 <= cx1 or cy2 <= cy1:
        return frame
    return frame[cy1:cy2, cx1:cx2]


class SnapshotWriter:
    """Writes snapshots to S3 if configured, otherwise to the media volume.

    Both destinations are private. The local path is deliberately the same
    volume the web container mounts, so the streaming endpoint can read what
    a vision worker wrote.
    """

    def __init__(self) -> None:
        self.bucket = settings.s3_bucket
        self.use_s3 = bool(
            settings.s3_endpoint_url and settings.s3_access_key and settings.s3_secret_key
        )
        self.media_root = Path(os.environ.get("MEDIA_ROOT", "/app/media"))
        self._client = None

        if self.use_s3:
            logger.info("snapshots -> s3 bucket %s", self.bucket)
        else:
            logger.info("snapshots -> local volume %s", self.media_root)

    @property
    def client(self):
        if self._client is None:
            import boto3

            self._client = boto3.client(
                "s3",
                endpoint_url=settings.s3_endpoint_url,
                aws_access_key_id=settings.s3_access_key,
                aws_secret_access_key=settings.s3_secret_key,
            )
        return self._client

    def write(
        self,
        frame: np.ndarray,
        bbox: tuple[int, int, int, int],
        *,
        camera_code: str,
        track_id: str,
        when: dt.datetime | None = None,
    ) -> str:
        """Store one snapshot. Returns its key, or "" on failure.

        Failures are logged and swallowed: losing a snapshot is a degraded
        record, while raising here would lose the movement event itself.
        """
        return self.write_image(
            crop_with_context(frame, bbox),
            camera_code=camera_code,
            track_id=track_id,
            when=when,
        )

    def write_image(
        self,
        image: np.ndarray,
        *,
        camera_code: str,
        track_id: str,
        when: dt.datetime | None = None,
    ) -> str:
        """Store an already-cropped snapshot. Returns its key, or "" on failure."""
        when = when or dt.datetime.now(dt.UTC)
        key = _key_for(camera_code, track_id, when)

        ok, buffer = cv2.imencode(
            ".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY]
        )
        if not ok:
            logger.warning("could not encode snapshot for %s", track_id)
            return ""

        payload = buffer.tobytes()

        try:
            if self.use_s3:
                self.client.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=payload,
                    ContentType="image/jpeg",
                    # No public ACL, ever.
                    ACL="private",
                )
            else:
                target = self.media_root / key
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
            return key
        except Exception:
            logger.warning("failed to store snapshot %s", key, exc_info=True)
            return ""
