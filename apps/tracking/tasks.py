"""
Scheduled tracking work: closing stale intervals, nightly rollups, and
retention enforcement.

Retention here is a *job*, not a policy document. Raw detections, embeddings
and snapshots age out automatically because a system that only promises to
delete data eventually does not.
"""

from __future__ import annotations

import datetime as dt
import logging

from celery import shared_task
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger("cctv.aggregator")


@shared_task(name="apps.tracking.tasks.close_stale_intervals")
def close_stale_intervals() -> dict[str, int]:
    """Close presence intervals whose subject has not been seen recently.

    ``exited_at`` is set to ``last_seen_at`` - the last moment the person was
    actually observed - rather than to now. Writing "now" would inflate every
    dwell time by however long the sweeper took to notice.
    """
    from apps.cameras.models import PipelineConfig
    from apps.tracking.models import PresenceInterval

    gap = dt.timedelta(seconds=PipelineConfig.active().presence_gap_seconds)
    cutoff = timezone.now() - gap

    stale = PresenceInterval.objects.filter(
        exited_at__isnull=True, last_seen_at__lt=cutoff
    )

    closed = 0
    zone_ids: set[int] = set()
    for interval in stale.iterator(chunk_size=500):
        interval.exited_at = interval.last_seen_at
        interval.save(update_fields=["exited_at", "updated_at"])
        zone_ids.add(interval.zone_id)
        closed += 1

    if closed:
        from apps.tracking.aggregator import broadcast_zone_occupancy

        broadcast_zone_occupancy(zone_ids)
        logger.info("closed %s stale presence interval(s)", closed)
    return {"closed": closed}


@shared_task(name="apps.tracking.tasks.build_daily_summaries")
def build_daily_summaries(for_date: str | None = None) -> dict[str, int]:
    """Roll yesterday's presence intervals into per-employee daily summaries."""
    from apps.tracking.models import (
        DailySummary,
        MatchConfidence,
        MovementEvent,
        PresenceInterval,
    )

    target = (
        dt.date.fromisoformat(for_date)
        if for_date
        else (timezone.localdate() - dt.timedelta(days=1))
    )
    tz = timezone.get_current_timezone()
    day_start = timezone.make_aware(
        dt.datetime.combine(target, dt.time.min), tz
    )
    day_end = day_start + dt.timedelta(days=1)

    employee_ids = (
        PresenceInterval.objects.filter(
            entered_at__gte=day_start, entered_at__lt=day_end
        )
        .values_list("employee_id", flat=True)
        .distinct()
    )

    built = 0
    for employee_id in employee_ids:
        intervals = list(
            PresenceInterval.objects.filter(
                employee_id=employee_id, entered_at__gte=day_start, entered_at__lt=day_end
            ).select_related("zone")
        )
        if not intervals:
            continue

        dwell: dict[str, int] = {}
        present_seconds = 0
        restricted_entries = 0
        low_confidence = 0

        for interval in intervals:
            seconds = interval.duration_seconds or 0
            dwell[interval.zone.code] = dwell.get(interval.zone.code, 0) + seconds
            present_seconds += seconds
            if interval.zone.is_restricted:
                restricted_entries += 1
            if interval.confidence != MatchConfidence.CONFIRMED:
                low_confidence += 1

        first = min(intervals, key=lambda i: i.entered_at)
        last = max(intervals, key=lambda i: i.last_seen_at)

        movements = MovementEvent.objects.filter(
            employee_id=employee_id, occurred_at__gte=day_start, occurred_at__lt=day_end
        ).count()

        DailySummary.objects.update_or_create(
            employee_id=employee_id,
            date=target,
            defaults={
                "first_seen_at": first.entered_at,
                "last_seen_at": last.last_seen_at,
                "first_seen_zone": first.zone,
                "total_zones_visited": len(dwell),
                "total_movements": movements,
                "present_seconds": present_seconds,
                "dwell_by_zone": dwell,
                "restricted_zone_entries": restricted_entries,
                "low_confidence_ratio": round(low_confidence / len(intervals), 3),
            },
        )
        built += 1

    logger.info("built %s daily summar(ies) for %s", built, target)
    return {"date": target.isoformat(), "built": built}


@shared_task(name="apps.tracking.tasks.enforce_retention")
def enforce_retention() -> dict[str, int]:
    """Delete data that has aged past its retention period.

    Order matters: embeddings are dropped first and earliest, because the
    biometric template is the most sensitive part of a detection row and is
    only needed if you want to re-run matching.
    """
    from apps.accounts.models import AuditLog, LoginEvent
    from apps.alerts.models import Alert
    from apps.cameras.models import CameraHealth
    from apps.tracking.models import (
        DailySummary,
        Detection,
        MovementEvent,
        PresenceInterval,
        UnknownFaceCluster,
    )

    now = timezone.now()
    policy = settings.RETENTION
    results: dict[str, int] = {}

    # 1. Embeddings: drop after half the detection retention window.
    embedding_cutoff = now - dt.timedelta(days=max(1, policy["detection_days"] // 2))
    results["embeddings_cleared"] = Detection.objects.filter(
        captured_at__lt=embedding_cutoff, embedding__isnull=False
    ).update(embedding=None)

    # 2. Snapshots: delete the objects, then clear the keys.
    snapshot_cutoff = now - dt.timedelta(days=policy["snapshot_days"])
    results["snapshots_deleted"] = _purge_snapshots(snapshot_cutoff)

    # 3. Raw detections.
    detection_cutoff = now - dt.timedelta(days=policy["detection_days"])
    deleted, _ = Detection.objects.filter(captured_at__lt=detection_cutoff).delete()
    results["detections_deleted"] = deleted

    # Unknown-face clusters hold a centroid embedding - a biometric template
    # of someone who never consented to enrolment - so they age out with the
    # raw detections, measured from the last time the face was seen.
    deleted, _ = UnknownFaceCluster.objects.filter(last_seen__lt=detection_cutoff).delete()
    results["unknown_clusters_deleted"] = deleted

    # Camera health samples arrive every few seconds per camera. They are
    # operational, not personal, but unbounded they swamp the database.
    deleted, _ = CameraHealth.objects.filter(at__lt=detection_cutoff).delete()
    results["camera_health_deleted"] = deleted

    # 4. Presence and everything derived from it. Movement events do not
    # cascade from intervals (the link is SET_NULL, so a correction can
    # outlive its interval), so each derived table is aged out on its own.
    presence_cutoff = now - dt.timedelta(days=policy["presence_days"])
    deleted, _ = PresenceInterval.objects.filter(
        entered_at__lt=presence_cutoff
    ).delete()
    results["presence_deleted"] = deleted

    deleted, _ = MovementEvent.objects.filter(occurred_at__lt=presence_cutoff).delete()
    results["movements_deleted"] = deleted

    deleted, _ = DailySummary.objects.filter(date__lt=presence_cutoff.date()).delete()
    results["daily_summaries_deleted"] = deleted

    # Alerts name people and carry evidence keys; they age like presence.
    deleted, _ = Alert.objects.filter(triggered_at__lt=presence_cutoff).delete()
    results["alerts_deleted"] = deleted

    # 5. Audit log last and longest. It must outlive the data it describes,
    # so that "who looked at what" survives the purge of what they looked at.
    # Sign-in history is part of that record and ages with it.
    audit_cutoff = now - dt.timedelta(days=policy["audit_days"])
    deleted, _ = AuditLog.objects.filter(at__lt=audit_cutoff).delete()
    results["audit_deleted"] = deleted

    deleted, _ = LoginEvent.objects.filter(at__lt=audit_cutoff).delete()
    results["login_events_deleted"] = deleted

    logger.info("retention enforced: %s", results)
    return results


def _purge_snapshots(cutoff) -> int:
    """Remove snapshot objects from storage and clear their keys."""
    from django.core.files.storage import default_storage

    from apps.tracking.models import Detection, PresenceInterval

    purged = 0
    detections = Detection.objects.filter(
        captured_at__lt=cutoff, snapshot_key__gt=""
    ).only("id", "snapshot_key")

    for detection in detections.iterator(chunk_size=500):
        try:
            default_storage.delete(detection.snapshot_key)
            purged += 1
        except Exception:
            logger.warning("could not delete snapshot %s", detection.snapshot_key)

    Detection.objects.filter(captured_at__lt=cutoff, snapshot_key__gt="").update(
        snapshot_key=""
    )
    PresenceInterval.objects.filter(entered_at__lt=cutoff).update(
        first_snapshot_key="", last_snapshot_key=""
    )
    return purged


@shared_task(name="apps.tracking.tasks.recheck_movement_plausibility")
def recheck_movement_plausibility(days: int = 1) -> dict[str, int]:
    """Re-evaluate recent movements after the adjacency graph is edited.

    Useful right after mapping a new door: events previously flagged
    implausible because no edge existed can be cleared in bulk.
    """
    from apps.org.models import ZoneAdjacency
    from apps.tracking.models import MovementEvent

    since = timezone.now() - dt.timedelta(days=days)
    cleared = 0

    candidates = MovementEvent.objects.filter(
        occurred_at__gte=since,
        is_plausible=False,
        from_zone__isnull=False,
        review_status=MovementEvent.ReviewStatus.PENDING,
    ).select_related("from_zone", "to_zone")

    for event in candidates.iterator(chunk_size=500):
        edge = ZoneAdjacency.objects.filter(
            Q(from_zone=event.from_zone, to_zone=event.to_zone)
            | Q(from_zone=event.to_zone, to_zone=event.from_zone, bidirectional=True)
        ).first()
        if edge and (event.transit_seconds or 0) >= edge.min_transit_seconds:
            event.is_plausible = True
            event.implausible_reason = ""
            event.review_status = MovementEvent.ReviewStatus.NONE
            event.save(
                update_fields=["is_plausible", "implausible_reason", "review_status"]
            )
            cleared += 1

    return {"cleared": cleared}
