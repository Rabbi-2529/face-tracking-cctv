"""
Vision worker configuration, from the environment only.

This package deliberately has **no Django dependency**. It must be importable
and runnable against a video file with nothing else present, which is what
makes accuracy benchmarking and CI practical.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _i(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _b(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(int(default))).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: os.environ["DATABASE_URL"])
    redis_url: str = field(
        default_factory=lambda: os.environ.get("REDIS_URL", "redis://redis:6379/0")
    )
    event_stream: str = field(
        default_factory=lambda: os.environ.get("EVENT_STREAM", "events:detections")
    )
    # Cap the stream so a stalled aggregator cannot exhaust Redis memory.
    event_stream_maxlen: int = field(
        default_factory=lambda: _i("EVENT_STREAM_MAXLEN", 200_000)
    )

    # Object storage for snapshots.
    s3_endpoint_url: str = field(
        default_factory=lambda: os.environ.get("S3_ENDPOINT_URL", "")
    )
    s3_access_key: str = field(default_factory=lambda: os.environ.get("S3_ACCESS_KEY", ""))
    s3_secret_key: str = field(default_factory=lambda: os.environ.get("S3_SECRET_KEY", ""))
    s3_bucket: str = field(
        default_factory=lambda: os.environ.get("S3_BUCKET", "cctv-snapshots")
    )

    model_dir: str = field(default_factory=lambda: os.environ.get("MODEL_DIR", "/models"))
    exec_provider: str = field(
        default_factory=lambda: os.environ.get("EXEC_PROVIDER", "CPUExecutionProvider")
    )

    # --- pipeline tuning (per-camera values in the DB override these) -----
    sample_fps: float = field(default_factory=lambda: _f("SAMPLE_FPS", 4.0))
    min_face_px: int = field(default_factory=lambda: _i("MIN_FACE_PX", 60))
    # Frames of a single track aggregated before deciding identity. Never
    # decide from one frame - that is what produces the wrong-name problem.
    vote_window: int = field(default_factory=lambda: _i("VOTE_WINDOW", 12))
    match_threshold_high: float = field(
        default_factory=lambda: _f("MATCH_THRESHOLD_HIGH", 0.45)
    )
    match_threshold_low: float = field(
        default_factory=lambda: _f("MATCH_THRESHOLD_LOW", 0.30)
    )
    gallery_refresh_seconds: int = field(
        default_factory=lambda: _i("GALLERY_REFRESH_SECONDS", 30)
    )

    # --- sharding ---------------------------------------------------------
    shard_index: int = field(default_factory=lambda: _i("SHARD_INDEX", 0))
    shard_count: int = field(default_factory=lambda: _i("SHARD_COUNT", 1))

    # --- modes ------------------------------------------------------------
    # Synthesises plausible movement with no cameras and no models, so the
    # whole realtime path can be exercised before any hardware exists.
    mock: bool = field(default_factory=lambda: _b("MOCK_VISION", False))

    field_encryption_key: str = field(
        default_factory=lambda: os.environ.get("FIELD_ENCRYPTION_KEY", "")
    )

    def owns_camera(self, camera_id: int) -> bool:
        if self.shard_count <= 1:
            return True
        return camera_id % self.shard_count == self.shard_index


settings = Settings()
