"""Expiry of tracking authorisations.

An approval that does not end by itself is an approval that outlives its
reason. This task closes the window and disconnects any live session the
moment it lapses, so expiry does not depend on anyone remembering.
"""

from __future__ import annotations

import datetime as dt
import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger("cctv.audit")

# A request nobody has decided on within this many days lapses, so the queue
# does not accumulate dormant authority.
PENDING_EXPIRY_DAYS = 7


@shared_task(name="apps.watchlist.tasks.expire_tracking_requests")
def expire_tracking_requests() -> dict[str, int]:
    from apps.common.audit import record
    from apps.watchlist.models import TrackingRequest, TrackSession

    now = timezone.now()

    lapsed = list(
        TrackingRequest.objects.filter(
            status=TrackingRequest.Status.APPROVED, end_at__lte=now
        ).select_related("employee", "requested_by")
    )

    for request in lapsed:
        request.status = TrackingRequest.Status.EXPIRED
        request.save(update_fields=["status", "updated_at"])
        record(
            "trackrequest.expired",
            actor=None,
            target_employee=request.employee,
            object_type="watchlist.TrackingRequest",
            object_id=request.pk,
            reason="authorisation window elapsed",
            metadata={"requested_by": request.requested_by.email},
        )

    # Close any session still open against a request that is no longer active
    # - disconnecting the socket, not only marking the row.
    from apps.watchlist.live import terminate_sessions

    closed = terminate_sessions(
        TrackSession.objects.exclude(
            request__status=TrackingRequest.Status.APPROVED,
            request__end_at__gt=now,
        )
    )

    # Also expire unanswered requests, so a stale queue does not accumulate
    # pending authority nobody reviewed.
    stale_pending = TrackingRequest.objects.filter(
        status=TrackingRequest.Status.PENDING,
        requested_at__lt=now - dt.timedelta(days=PENDING_EXPIRY_DAYS),
    )
    expired_pending = stale_pending.update(
        status=TrackingRequest.Status.EXPIRED,
        decision_note=(
            f"Expired without a decision after {PENDING_EXPIRY_DAYS} days."
        ),
    )

    if lapsed or closed or expired_pending:
        logger.info(
            "tracking requests expired=%s sessions_closed=%s pending_expired=%s",
            len(lapsed),
            closed,
            expired_pending,
        )

    return {
        "expired": len(lapsed),
        "sessions_closed": closed,
        "pending_expired": expired_pending,
    }
