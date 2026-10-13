"""Ending live-follow sessions the moment their authorisation ends."""

from __future__ import annotations

import logging

from django.utils import timezone

logger = logging.getLogger("cctv.audit")


def terminate_sessions(sessions) -> int:
    """Close the sockets behind these open sessions and mark them ended.

    Each session stored its Channels name when it opened. Telling those
    channels to terminate disconnects the viewer now, rather than at their
    next revalidation - which matters most for a revocation, where an
    approver has decided the watching must stop.
    """
    open_sessions = list(sessions.filter(ended_at__isnull=True))
    if not open_sessions:
        return 0

    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer

    layer = get_channel_layer()
    for session in open_sessions:
        if layer is None or not session.channel_name:
            continue
        try:
            async_to_sync(layer.send)(session.channel_name, {"type": "session.terminate"})
        except Exception:
            # The socket also revalidates itself on a timer, so a failed send
            # delays the disconnect by at most that interval.
            logger.warning("could not signal live session %s to close", session.pk, exc_info=True)

    now = timezone.now()
    type(open_sessions[0]).objects.filter(pk__in=[s.pk for s in open_sessions]).update(
        ended_at=now
    )
    return len(open_sessions)
