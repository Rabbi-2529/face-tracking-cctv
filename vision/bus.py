"""
Event publisher: vision workers to the aggregator, over a Redis Stream.

Why a stream rather than pub/sub: an aggregator restart must not lose
detections, and several aggregator replicas must be able to share the load
through a consumer group. ``XADD`` with ``MAXLEN`` bounds memory so a stalled
consumer degrades by dropping the oldest events rather than by taking Redis
down.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import redis

from vision.config import settings

logger = logging.getLogger("vision.bus")

EVENT_SCHEMA_VERSION = 1


class EventBus:
    def __init__(self, url: str | None = None) -> None:
        self.url = url or settings.redis_url
        self.stream = settings.event_stream
        self.maxlen = settings.event_stream_maxlen
        self._client: redis.Redis | None = None

    @property
    def client(self) -> redis.Redis:
        if self._client is None:
            self._client = redis.Redis.from_url(
                self.url,
                decode_responses=True,
                socket_keepalive=True,
                health_check_interval=30,
            )
        return self._client

    def publish(self, event: dict[str, Any]) -> str | None:
        """Publish one detection event. Returns the stream id, or None on failure.

        Failures are logged and swallowed: a Redis blip must not kill a
        camera worker, and the next frame will be published anyway.
        """
        event.setdefault("v", EVENT_SCHEMA_VERSION)
        try:
            return self.client.xadd(
                self.stream,
                {"data": json.dumps(event, separators=(",", ":"))},
                maxlen=self.maxlen,
                approximate=True,
            )
        except redis.RedisError:
            logger.warning("failed to publish event to %s", self.stream, exc_info=True)
            self._client = None  # force reconnect next time
            return None

    def publish_health(self, payload: dict[str, Any]) -> None:
        """Camera health sample, on its own stream.

        Separate from detections so health never competes with the hot path
        and so a health backlog cannot delay movement events.
        """
        try:
            self.client.xadd(
                f"{self.stream}:health",
                {"data": json.dumps(payload, separators=(",", ":"))},
                maxlen=10_000,
                approximate=True,
            )
        except redis.RedisError:
            logger.debug("health publish failed", exc_info=True)

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None


def build_detection_event(
    *,
    camera_code: str,
    captured_at: str,
    track_id: str,
    frame_index: int,
    bbox: list[int],
    face_width_px: int,
    quality_score: float,
    employee_code: str | None,
    match_score: float | None,
    confidence: str,
    snapshot_key: str = "",
    embedding: list[float] | None = None,
) -> dict[str, Any]:
    """Build an event in the schema the aggregator expects.

    One function so the producer and consumer cannot drift apart silently;
    ``apps.tracking.aggregator`` checks ``v`` and skips anything it does not
    understand.
    """
    return {
        "v": EVENT_SCHEMA_VERSION,
        "camera_code": camera_code,
        "captured_at": captured_at,
        "track_id": track_id,
        "frame_index": frame_index,
        "bbox": bbox,
        "face_width_px": face_width_px,
        "quality_score": round(quality_score, 4),
        "employee_code": employee_code,
        "match_score": round(match_score, 4) if match_score is not None else None,
        "confidence": confidence,
        "snapshot_key": snapshot_key,
        "embedding": embedding,
    }
