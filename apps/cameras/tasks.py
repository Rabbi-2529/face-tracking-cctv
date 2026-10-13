"""Camera health monitoring.

A camera that silently stops sending frames creates a gap in someone's
movement history. The system must know the difference between "this person
was not there" and "nobody was looking", so a stalled camera is marked
offline and its zone is flagged as having a coverage gap.
"""

from __future__ import annotations

import datetime as dt
import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger("cctv.aggregator")

# A camera sampling at 4 fps should produce health samples continuously;
# three minutes of silence means the worker or the stream is gone.
STALE_AFTER = dt.timedelta(minutes=3)
DEGRADED_FPS_RATIO = 0.5


@shared_task(name="apps.cameras.tasks.check_camera_health")
def check_camera_health() -> dict[str, int]:
    from apps.cameras.models import Camera, CameraHealth

    now = timezone.now()
    counts = {"online": 0, "degraded": 0, "offline": 0}

    for camera in Camera.objects.filter(
        is_enabled=True, deleted_at__isnull=True
    ).iterator():
        latest = (
            CameraHealth.objects.filter(camera=camera).order_by("-at").first()
        )

        if latest is None or now - latest.at > STALE_AFTER:
            new_status = Camera.Status.OFFLINE
            message = (
                "no health sample received"
                if latest is None
                else f"last sample {int((now - latest.at).total_seconds())}s ago"
            )
        elif (
            latest.fps_actual is not None
            and camera.sample_fps
            and latest.fps_actual < camera.sample_fps * DEGRADED_FPS_RATIO
        ):
            new_status = Camera.Status.DEGRADED
            message = (
                f"delivering {latest.fps_actual:.1f} fps, expected {camera.sample_fps:.1f}"
            )
        else:
            new_status = Camera.Status.ONLINE
            message = ""

        if new_status != camera.status:
            logger.warning(
                "camera %s: %s -> %s (%s)", camera.code, camera.status, new_status, message
            )
            camera.mark_status(new_status, message)
            if new_status == Camera.Status.OFFLINE:
                _notify_camera_down(camera, message)

        counts[new_status if new_status in counts else "offline"] += 1

    return counts


def _notify_camera_down(camera, message: str) -> None:
    """Record a camera outage, raise any camera-down alerts, and tell the board.

    The audit row, the alert inbox and the live board's coverage-gap marker
    together are what prevent a silent gap in somebody's movement history.
    """
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer

    from apps.common.audit import record

    record(
        "camera.offline",
        object_type="cameras.Camera",
        object_id=camera.pk,
        reason=message,
        success=False,
        metadata={"camera_code": camera.code, "zone": camera.zone.code},
    )

    from apps.alerts.engine import on_camera_offline

    try:
        on_camera_offline(camera, message)
    except Exception:
        logger.exception("camera-down alert failed for %s", camera.code)

    layer = get_channel_layer()
    if layer is None:
        return
    try:
        async_to_sync(layer.group_send)(
            f"zone.{camera.zone_id}",
            {
                "type": "camera.status",
                "camera_code": camera.code,
                "zone_id": camera.zone_id,
                "status": camera.status,
                "message": message,
            },
        )
    except Exception:
        logger.debug("camera status broadcast failed", exc_info=True)
