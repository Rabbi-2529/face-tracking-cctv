"""
Live camera view: serve the latest annotated frame the worker published.

The camera worker already decodes every frame and runs detection on the
sampled ones, so it draws the overlay and writes a JPEG to Redis. The web
tier just hands that frame to the browser, which refreshes it a few times a
second.

This replaced an earlier design that proxied the RTSP stream through Django
and annotated per viewer. That version duplicated work the worker was already
doing, needed a long-lived connection per viewer, and left a generator
decoding video for minutes after a browser tab was closed. Serving a
published frame has none of those problems and supports any number of
viewers at once.

Two permissions, not one. CAMERA_VIEW gets you the picture; **names are only
drawn for viewers with organisation-wide history access**, so the live view
cannot be used to get round the tracking-request gate.
"""

from __future__ import annotations

import logging

import redis
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse, JsonResponse

from apps.common.audit import record
from apps.common.permissions import PermissionService
from apps.common.roles import Action

logger = logging.getLogger("cctv.live")

WANT_KEY = "preview:want:{code}"
FRAME_KEY = "preview:frame:{code}"

# How long a single "someone is watching" signal lasts. The browser renews it
# while the view is open; when the tab closes it lapses and the worker stops
# drawing overlays within a second or two.
WANT_TTL_SECONDS = 12

# Viewing is audited once per this window rather than per frame - at 4 fps,
# auditing every frame would bury the log in noise while telling you nothing
# more than "they watched this camera".
AUDIT_WINDOW_SECONDS = 60
AUDIT_MARKER_KEY = "preview:audited:{code}:{user}"


def _client() -> redis.Redis:
    return redis.Redis.from_url(settings.REDIS_URL, decode_responses=False)


def request_preview(request, camera) -> JsonResponse:
    """Signal that someone is watching, and report whether a frame exists.

    The browser calls this on open and then periodically. Nothing is drawn
    or published while nobody is asking, so an idle camera costs no extra
    work at all.
    """
    decision = PermissionService.can(request.user, Action.CAMERA_VIEW)
    if not decision:
        raise PermissionDenied(decision.reason)

    granted = PermissionService.granted_actions(request.user)
    show_names = Action.HISTORY_ANY in granted

    try:
        client = _client()
        client.set(WANT_KEY.format(code=camera.code), b"1", ex=WANT_TTL_SECONDS)
        has_frame = bool(client.exists(FRAME_KEY.format(code=camera.code)))

        marker = AUDIT_MARKER_KEY.format(code=camera.code, user=request.user.pk)
        # set-if-absent with a TTL: the first viewer in the window audits.
        first_in_window = client.set(marker, b"1", ex=AUDIT_WINDOW_SECONDS, nx=True)
    except redis.RedisError:
        logger.warning("preview signalling failed", exc_info=True)
        return JsonResponse(
            {
                "watching": False,
                "has_frame": False,
                "detail": "The event bus is unavailable, so live view cannot run.",
            },
            status=503,
        )

    if first_in_window:
        record(
            "camera.live_view",
            object_type="cameras.Camera",
            object_id=camera.pk,
            reason=f"live view of {camera.code}",
            metadata={"camera": camera.code, "names_shown": show_names},
        )

    detail = ""
    if not has_frame:
        if not camera.is_enabled:
            detail = (
                "This camera is disabled, so no worker is reading it. Enable it "
                "to see a live picture."
            )
        else:
            detail = (
                "Waiting for the worker. If nothing appears, check that the "
                "vision service is running and that this camera is online."
            )

    return JsonResponse(
        {
            "watching": True,
            "has_frame": has_frame,
            "names_shown": show_names,
            "detail": detail,
        }
    )


def preview_frame(request, camera) -> HttpResponse:
    """Return the most recent annotated frame for this camera.

    204 when there is no current frame, which the browser treats as "keep
    waiting" rather than an error. A frame older than its short TTL simply
    does not exist, so a dead worker can never show a stale picture that
    looks live.
    """
    decision = PermissionService.can(request.user, Action.CAMERA_VIEW)
    if not decision:
        raise PermissionDenied(decision.reason)

    try:
        client = _client()
        # Renew the watch here too, so a browser that only fetches frames
        # still counts as watching.
        client.set(WANT_KEY.format(code=camera.code), b"1", ex=WANT_TTL_SECONDS)
        payload = client.get(FRAME_KEY.format(code=camera.code))
    except redis.RedisError:
        logger.warning("preview fetch failed", exc_info=True)
        return HttpResponse(status=503)

    if not payload:
        return HttpResponse(status=204)

    response = HttpResponse(payload, content_type="image/jpeg")
    # Never cached: the next request must get the next frame, and a frame of
    # a person must not sit in a proxy or a browser cache.
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    response["X-Content-Type-Options"] = "nosniff"
    response["Referrer-Policy"] = "no-referrer"
    return response
