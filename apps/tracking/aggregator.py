"""
Realtime event aggregation.

Consumes the Redis Stream that vision workers publish to and turns per-frame
detections into the three layers the rest of the system reads:

    Detection  ->  PresenceInterval  ->  MovementEvent

Design decisions worth knowing:

* **Redis Streams with a consumer group**, not pub/sub. A restart of this
  service must not lose events, and several aggregator replicas must be able
  to share the load without double-processing.
* **Plausibility is checked, not assumed.** A transition that no zone
  adjacency permits is written with ``is_plausible=False`` and queued for
  review. Dropping it would hide a matching problem; accepting it silently
  would publish a false movement as fact.
* **Prohibited zones are enforced here too**, not only at ingest. Defence in
  depth: a misconfigured camera must not be able to create a presence record
  for a toilet or a changing room.
* ``exited_at`` is set to the last time the person was actually seen, never
  to the time the sweeper ran, so durations stay truthful.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import signal
import time
from dataclasses import dataclass
from typing import Any

import numpy as np
import redis
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from pgvector.django import CosineDistance

from apps.cameras.models import Camera, PipelineConfig
from apps.org.models import ZoneAdjacency
from apps.tracking.models import (
    Detection,
    MatchConfidence,
    MovementEvent,
    PresenceInterval,
    UnknownFaceCluster,
)

logger = logging.getLogger("cctv.aggregator")

EVENT_SCHEMA_VERSION = 1

# Later events of one unknown track join the cluster its first event went to,
# as long as they arrive within this window. Workers re-vote a track every 30s.
TRACK_REUSE_WINDOW = dt.timedelta(hours=1)


@dataclass
class Stats:
    consumed: int = 0
    detections: int = 0
    intervals_opened: int = 0
    intervals_closed: int = 0
    movements: int = 0
    implausible: int = 0
    unknown_sightings: int = 0
    clusters_created: int = 0
    skipped: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return self.__dict__.copy()


def broadcast_zone_occupancy(zone_ids) -> None:
    """Push current occupancy for these zones to the live board.

    Called whenever an interval opens or closes - by the aggregator, and by
    the sweeper that closes intervals nobody has been seen in. Without the
    second caller, a room people had left would keep showing them until
    someone else walked in.
    """
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer

    zone_ids = {z for z in zone_ids if z}
    if not zone_ids:
        return
    layer = get_channel_layer()
    if layer is None:
        return
    for zone_id in zone_ids:
        occupancy = PresenceInterval.objects.filter(
            zone_id=zone_id, exited_at__isnull=True
        ).count()
        try:
            async_to_sync(layer.group_send)(
                f"zone.{zone_id}",
                {"type": "zone.occupancy", "zone_id": zone_id, "occupancy": occupancy},
            )
        except Exception:
            logger.debug("channel broadcast failed for zone %s", zone_id, exc_info=True)


def _alert_safely(entry_point: str, *args) -> None:
    """Run one alert-engine entry point without risking the presence write.

    Inside its own savepoint, so a failure in alerting rolls back only the
    alert, never the interval or movement it was evaluating.
    """
    from apps.alerts import engine

    try:
        with transaction.atomic():
            getattr(engine, entry_point)(*args)
    except Exception:
        logger.exception("alert evaluation failed (%s)", entry_point)


def _unit_vector(value) -> np.ndarray | None:
    """An embedding as a unit-length float32 array, or None if unusable."""
    if value is None:
        return None
    vector = np.asarray(value, dtype=np.float32).reshape(-1)
    if vector.size == 0:
        return None
    norm = float(np.linalg.norm(vector))
    if norm == 0:
        return None
    return vector / norm


class DetectionAggregator:
    """Stateless per-event processing, with small caches for hot lookups."""

    def __init__(self) -> None:
        self._camera_cache: dict[str, Camera | None] = {}
        self._employee_cache: dict[str, Any] = {}
        self._adjacency_cache: dict[tuple[int, int], ZoneAdjacency | None] = {}
        self._config: PipelineConfig | None = None
        self._config_loaded_at: float = 0.0
        self.stats = Stats()

    # ------------------------------------------------------------- lookups

    @property
    def config(self) -> PipelineConfig:
        # Re-read every 30s so a threshold change takes effect without a
        # restart, but without a DB hit per event.
        if self._config is None or time.monotonic() - self._config_loaded_at > 30:
            self._config = PipelineConfig.active()
            self._config_loaded_at = time.monotonic()
        return self._config

    def _camera(self, code: str) -> Camera | None:
        if code not in self._camera_cache:
            self._camera_cache[code] = (
                Camera.objects.select_related("zone")
                .filter(code=code, deleted_at__isnull=True)
                .first()
            )
        return self._camera_cache[code]

    def _employee(self, code: str):
        if code not in self._employee_cache:
            from apps.employees.models import Employee

            self._employee_cache[code] = Employee.objects.filter(
                employee_code=code, deleted_at__isnull=True
            ).first()
        return self._employee_cache[code]

    def _adjacency(self, from_zone_id: int, to_zone_id: int) -> ZoneAdjacency | None:
        key = (from_zone_id, to_zone_id)
        if key not in self._adjacency_cache:
            edge = ZoneAdjacency.objects.filter(
                from_zone_id=from_zone_id, to_zone_id=to_zone_id
            ).first()
            if edge is None:
                # Reverse edge counts when the connection is bidirectional.
                edge = ZoneAdjacency.objects.filter(
                    from_zone_id=to_zone_id, to_zone_id=from_zone_id, bidirectional=True
                ).first()
            self._adjacency_cache[key] = edge
        return self._adjacency_cache[key]

    def invalidate_caches(self) -> None:
        self._camera_cache.clear()
        self._employee_cache.clear()
        self._adjacency_cache.clear()

    # --------------------------------------------------------- event intake

    def handle(self, payload: dict[str, Any]) -> None:
        """Process one detection event. Exceptions are caught by the caller."""
        if payload.get("v") != EVENT_SCHEMA_VERSION:
            logger.warning("ignoring event with schema version %s", payload.get("v"))
            self.stats.skipped += 1
            return

        camera = self._camera(payload.get("camera_code", ""))
        if camera is None:
            logger.warning("unknown camera_code=%s", payload.get("camera_code"))
            self.stats.skipped += 1
            return

        # Defence in depth - ingest should never have sent this.
        if camera.zone.tracking_prohibited:
            logger.error(
                "REFUSED event from camera %s in tracking-prohibited zone %s",
                camera.code,
                camera.zone.code,
            )
            self.stats.skipped += 1
            return

        captured_at = parse_datetime(payload["captured_at"])
        if captured_at is None:
            self.stats.skipped += 1
            return
        if timezone.is_naive(captured_at):
            captured_at = timezone.make_aware(captured_at)

        employee = None
        employee_code = payload.get("employee_code")
        if employee_code:
            employee = self._employee(employee_code)

        confidence = payload.get("confidence", MatchConfidence.UNKNOWN)

        detection = Detection(
            camera=camera,
            captured_at=captured_at,
            track_id=payload.get("track_id", "")[:64],
            frame_index=payload.get("frame_index", 0),
            bbox=payload.get("bbox", []),
            face_width_px=payload.get("face_width_px", 0),
            quality_score=payload.get("quality_score", 0.0),
            embedding=payload.get("embedding"),
            matched_employee=employee,
            match_score=payload.get("match_score"),
            confidence=confidence,
            snapshot_key=payload.get("snapshot_key", "")[:255],
        )
        detection.save()
        self.stats.detections += 1

        # An unknown face does not create presence for anybody. It is still
        # recorded, and grouped with earlier sightings of the same face so it
        # can be reviewed once rather than once per appearance.
        if employee is None or confidence == MatchConfidence.UNKNOWN:
            try:
                self._cluster_unknown(detection, camera)
            except Exception:
                # The detection itself is stored. Raising here would leave the
                # event unacknowledged and store it again on retry.
                logger.exception("could not cluster unknown detection %s", detection.pk)
            return

        self._fold_into_presence(detection, employee, camera, captured_at, confidence)

    # ------------------------------------------------------------- presence

    @transaction.atomic
    def _fold_into_presence(
        self, detection, employee, camera, captured_at, confidence: str
    ) -> None:
        zone = camera.zone
        gap = dt.timedelta(seconds=self.config.presence_gap_seconds)

        # Lock this employee's open intervals so two aggregator replicas
        # processing adjacent frames cannot both open an interval.
        open_intervals = list(
            PresenceInterval.objects.select_for_update()
            .filter(employee=employee, exited_at__isnull=True)
            .order_by("-last_seen_at")
        )

        same_zone = next((i for i in open_intervals if i.zone_id == zone.pk), None)

        if same_zone is not None and captured_at - same_zone.last_seen_at <= gap:
            # Still in the same room: extend.
            same_zone.last_seen_at = max(same_zone.last_seen_at, captured_at)
            same_zone.detection_count += 1
            same_zone.last_snapshot_key = detection.snapshot_key or same_zone.last_snapshot_key
            upgraded = (
                confidence == MatchConfidence.CONFIRMED
                and same_zone.confidence != MatchConfidence.CONFIRMED
            )
            if confidence == MatchConfidence.CONFIRMED:
                same_zone.confidence = MatchConfidence.CONFIRMED
            if detection.match_score is not None:
                prior = same_zone.mean_match_score or detection.match_score
                n = same_zone.detection_count
                same_zone.mean_match_score = prior + (detection.match_score - prior) / n
            same_zone.save(
                update_fields=[
                    "last_seen_at",
                    "detection_count",
                    "last_snapshot_key",
                    "confidence",
                    "mean_match_score",
                    "updated_at",
                ]
            )
            if upgraded:
                # Probable presence never alerts; it can once it is confirmed.
                _alert_safely("on_confirmed_presence", same_zone)
            return

        # Different zone (or a stale interval in this one): close everything
        # open and start fresh. `exited_at` is the last real sighting.
        closed_zone_ids: set[int] = set()
        for interval in open_intervals:
            interval.exited_at = interval.last_seen_at
            interval.save(update_fields=["exited_at", "updated_at"])
            self.stats.intervals_closed += 1
            closed_zone_ids.add(interval.zone_id)

        # Where did they come from? The most recent interval of the same day,
        # whether it was still open a moment ago or the sweeper had already
        # closed it. Looking only at open intervals would record everyone
        # who took longer than the presence gap to walk between rooms as a
        # fresh "arrival", and the timeline would lose the transition.
        previous = self._previous_interval(employee, captured_at)

        new_interval = PresenceInterval.objects.create(
            employee=employee,
            zone=zone,
            entered_at=captured_at,
            last_seen_at=captured_at,
            detection_count=1,
            confidence=confidence,
            mean_match_score=detection.match_score,
            first_snapshot_key=detection.snapshot_key,
            last_snapshot_key=detection.snapshot_key,
        )
        self.stats.intervals_opened += 1
        if confidence == MatchConfidence.CONFIRMED:
            _alert_safely("on_confirmed_presence", new_interval)

        # Back in the same room after a spell out of view (sat facing away,
        # say) gets a new interval, which keeps the gap honest - but a
        # movement from a room to itself is not a movement.
        if previous is None or previous.zone_id != zone.pk:
            self._emit_movement(employee, previous, new_interval, captured_at, confidence)

        broadcast_zone_occupancy(closed_zone_ids | {zone.pk})

    def _previous_interval(self, employee, captured_at) -> PresenceInterval | None:
        """The employee's latest interval earlier the same local day, if any.

        None means this is the first sighting of the day, which the timeline
        shows as an arrival.
        """
        local = timezone.localtime(captured_at)
        day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return (
            PresenceInterval.objects.filter(
                employee=employee,
                exited_at__isnull=False,
                last_seen_at__gte=day_start,
            )
            .select_related("zone")
            .order_by("-last_seen_at")
            .first()
        )

    # ------------------------------------------------------------- movement

    def _emit_movement(
        self, employee, previous, new_interval, occurred_at, confidence: str
    ) -> None:
        from_zone_id = previous.zone_id if previous else None

        transit_seconds = None
        prev_dwell = None
        plausible = True
        reason = ""

        if previous is not None:
            transit_seconds = max(
                0, int((occurred_at - previous.exited_at).total_seconds())
            )
            prev_dwell = previous.duration_seconds

            edge = self._adjacency(previous.zone_id, new_interval.zone_id)
            if edge is None:
                plausible = False
                reason = (
                    f"no adjacency defined between {previous.zone.code} and "
                    f"{new_interval.zone.code}"
                )
            elif transit_seconds < edge.min_transit_seconds:
                plausible = False
                reason = (
                    f"transit {transit_seconds}s is faster than the "
                    f"{edge.min_transit_seconds}s minimum for this route"
                )

        event = MovementEvent.objects.create(
            employee=employee,
            from_zone_id=from_zone_id,
            to_zone=new_interval.zone,
            occurred_at=occurred_at,
            transit_seconds=transit_seconds,
            prev_dwell_seconds=prev_dwell,
            confidence=confidence,
            is_plausible=plausible,
            implausible_reason=reason[:200],
            review_status=(
                MovementEvent.ReviewStatus.PENDING
                if not plausible
                else MovementEvent.ReviewStatus.NONE
            ),
            source_interval=new_interval,
        )
        self.stats.movements += 1
        if not plausible:
            self.stats.implausible += 1
            logger.warning(
                "implausible movement employee=%s %s -> %s: %s",
                employee.employee_code,
                previous.zone.code if previous else "(none)",
                new_interval.zone.code,
                reason,
            )
        self._broadcast_movement(event)

    # ------------------------------------------------------- unknown faces

    @transaction.atomic
    def _cluster_unknown(self, detection: Detection, camera: Camera) -> None:
        """Attach an unmatched face to a cluster of earlier sightings.

        Workers send an embedding only on the first event of an unknown
        track. Later events for the same track inherit that track's cluster,
        so one walk past a camera is one sighting, not one per re-vote.
        """
        cluster = None
        if detection.track_id:
            earlier = (
                Detection.objects.filter(
                    camera=camera,
                    track_id=detection.track_id,
                    unknown_cluster__isnull=False,
                    captured_at__gte=detection.captured_at - TRACK_REUSE_WINDOW,
                )
                .exclude(pk=detection.pk)
                .select_related("unknown_cluster")
                .order_by("-captured_at")
                .first()
            )
            if earlier is not None:
                cluster = earlier.unknown_cluster
                if detection.captured_at > cluster.last_seen:
                    cluster.last_seen = detection.captured_at
                    cluster.save(update_fields=["last_seen", "updated_at"])

        if cluster is None:
            embedding = _unit_vector(detection.embedding)
            if embedding is None:
                return
            cluster = self._nearest_cluster(embedding, detection.captured_at)
            if cluster is None:
                cluster = UnknownFaceCluster.objects.create(
                    centroid_embedding=embedding.tolist(),
                    first_seen=detection.captured_at,
                    last_seen=detection.captured_at,
                    representative_snapshot_key=detection.snapshot_key,
                )
                self.stats.clusters_created += 1
            else:
                self._absorb(cluster, embedding, detection)

        cluster.zones_seen.add(camera.zone)
        detection.unknown_cluster = cluster
        detection.save(update_fields=["unknown_cluster"])
        self.stats.unknown_sightings += 1
        _alert_safely("on_unknown_sighting", cluster, detection, camera)

    def _nearest_cluster(self, embedding, captured_at) -> UnknownFaceCluster | None:
        """The closest recent cluster, if it is close enough to be the same face.

        Uses the matcher's own "confirmed" threshold: if a similarity would
        be enough to name an enrolled employee, it is enough to say two
        unknown sightings are the same person.
        """
        lookback = dt.timedelta(days=settings.RETENTION["detection_days"])
        nearest = (
            UnknownFaceCluster.objects.filter(last_seen__gte=captured_at - lookback)
            .annotate(distance=CosineDistance("centroid_embedding", embedding.tolist()))
            .order_by("distance")
            .first()
        )
        if nearest is None:
            return None
        similarity = 1.0 - float(nearest.distance)
        if similarity < self.config.match_threshold_high:
            return None
        return nearest

    @staticmethod
    def _absorb(cluster: UnknownFaceCluster, embedding, detection: Detection) -> None:
        """Fold one more sighting into a cluster's running centroid."""
        count = cluster.sighting_count
        centroid = _unit_vector(cluster.centroid_embedding)
        if centroid is None:
            centroid = embedding
        merged = _unit_vector(centroid * count + embedding)
        if merged is not None:
            cluster.centroid_embedding = merged.tolist()
        cluster.sighting_count = count + 1
        cluster.first_seen = min(cluster.first_seen, detection.captured_at)
        cluster.last_seen = max(cluster.last_seen, detection.captured_at)
        if not cluster.representative_snapshot_key and detection.snapshot_key:
            cluster.representative_snapshot_key = detection.snapshot_key
        cluster.save(
            update_fields=[
                "centroid_embedding",
                "sighting_count",
                "first_seen",
                "last_seen",
                "representative_snapshot_key",
                "updated_at",
            ]
        )

    # ------------------------------------------------------------ broadcast

    def _broadcast_movement(self, event: MovementEvent) -> None:
        """Notify anyone with an approved live-follow session for this subject.

        The payload deliberately carries no snapshot URL; a viewer fetches
        imagery through the API, which re-checks permission and audits.
        """
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        layer = get_channel_layer()
        if layer is None:
            return
        try:
            async_to_sync(layer.group_send)(
                f"subject.{event.employee_id}",
                {
                    "type": "subject.movement",
                    "employee_id": event.employee_id,
                    "from_zone": event.from_zone.code if event.from_zone else None,
                    "to_zone": event.to_zone.code,
                    "occurred_at": event.occurred_at.isoformat(),
                    "confidence": event.confidence,
                    "is_plausible": event.is_plausible,
                },
            )
        except Exception:
            logger.debug("movement broadcast failed", exc_info=True)


class AggregatorRunner:
    """Redis Stream consumer loop with a consumer group and graceful stop."""

    def __init__(
        self,
        *,
        consumer_name: str,
        batch_size: int = 200,
        block_ms: int = 1000,
    ) -> None:
        self.stream = settings.EVENT_STREAM
        self.health_stream = f"{settings.EVENT_STREAM}:health"
        self.group = settings.EVENT_STREAM_GROUP
        self.consumer_name = consumer_name
        self.batch_size = batch_size
        self.block_ms = block_ms
        self.client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
        self.aggregator = DetectionAggregator()
        self._stop = False

    def install_signal_handlers(self) -> None:
        def _handle(signum, frame):
            logger.info("signal %s received, finishing current batch", signum)
            self._stop = True

        signal.signal(signal.SIGTERM, _handle)
        signal.signal(signal.SIGINT, _handle)

    def ensure_group(self) -> None:
        for stream in (self.stream, self.health_stream):
            try:
                # mkstream so the consumer can start before any producer exists.
                self.client.xgroup_create(stream, self.group, id="0", mkstream=True)
                logger.info("created consumer group %s on %s", self.group, stream)
            except redis.ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise

    def _drain_health(self) -> None:
        """Write camera health samples.

        Kept on its own stream and drained without blocking, so health
        reporting never competes with detections for the hot path - but it
        still lands in the database, which is what lets a stalled camera be
        shown as a coverage gap rather than as an absence of people.
        """
        from apps.cameras.models import CameraHealth

        try:
            batches = self.client.xreadgroup(
                groupname=self.group,
                consumername=self.consumer_name,
                streams={self.health_stream: ">"},
                count=200,
                block=None,
            )
        except redis.RedisError:
            return

        for _stream, messages in batches or []:
            for message_id, fields in messages:
                try:
                    payload = json.loads(fields["data"])
                    camera = self.aggregator._camera(payload.get("camera_code", ""))
                    if camera is not None:
                        CameraHealth.objects.create(
                            camera=camera,
                            status=payload.get("status", "unknown"),
                            fps_actual=payload.get("fps_actual"),
                            frames_dropped=payload.get("frames_dropped", 0),
                            reconnects=payload.get("reconnects", 0),
                            error_message=(payload.get("error_message") or "")[:500],
                            worker_id=(payload.get("worker_id") or "")[:64],
                            faces_seen=payload.get("faces_seen", 0),
                            faces_gated=payload.get("faces_gated", 0),
                            faces_identified=payload.get("faces_identified", 0),
                        )
                except Exception:
                    logger.exception("bad health sample %s", message_id)
                finally:
                    self.client.xack(self.health_stream, self.group, message_id)

    def recover_pending(self) -> int:
        """Re-process events this consumer read earlier but never acknowledged.

        A failed event is deliberately left unacknowledged - but reading with
        ">" only ever returns new events, so without this step a failure is
        never retried. That happens for real: a deploy that changes the
        schema leaves the old aggregator failing every event until restart.

        Walks the pending list once with an advancing cursor, so an event
        that still fails stays pending (and visible in XPENDING) instead of
        being retried in a tight loop.
        """
        recovered = 0
        cursor = "0"
        while not self._stop:
            try:
                batches = self.client.xreadgroup(
                    groupname=self.group,
                    consumername=self.consumer_name,
                    streams={self.stream: cursor},
                    count=self.batch_size,
                )
            except redis.RedisError:
                logger.warning("could not read pending events; continuing with new ones")
                break
            messages = batches[0][1] if batches else []
            if not messages:
                break
            before = self.aggregator.stats.errors
            self._process_messages(messages)
            recovered += len(messages) - (self.aggregator.stats.errors - before)
            cursor = messages[-1][0]
        if recovered:
            logger.info("recovered %s pending event(s) from before the restart", recovered)
        return recovered

    def run(self) -> None:
        self.install_signal_handlers()
        self.ensure_group()
        logger.info(
            "aggregator %s consuming %s (group=%s)",
            self.consumer_name,
            self.stream,
            self.group,
        )
        self.recover_pending()

        last_report = time.monotonic()

        while not self._stop:
            try:
                batches = self.client.xreadgroup(
                    groupname=self.group,
                    consumername=self.consumer_name,
                    streams={self.stream: ">"},
                    count=self.batch_size,
                    block=self.block_ms,
                )
            except redis.ConnectionError:
                logger.warning("redis connection lost; retrying in 2s")
                time.sleep(2)
                continue

            if batches:
                for _stream, messages in batches:
                    self._process_messages(messages)

            self._drain_health()

            # Periodic heartbeat so an idle aggregator is visibly alive, and
            # caches are dropped so config/camera edits are picked up.
            if time.monotonic() - last_report > 60:
                logger.info("aggregator stats %s", self.aggregator.stats.as_dict())
                self.aggregator.invalidate_caches()
                last_report = time.monotonic()

        logger.info(
            "aggregator stopping, final stats %s", self.aggregator.stats.as_dict()
        )

    def _process_messages(self, messages) -> None:
        for message_id, fields in messages:
            self.aggregator.stats.consumed += 1
            try:
                payload = json.loads(fields["data"])
            except (KeyError, json.JSONDecodeError):
                logger.exception("malformed event %s; acknowledging to skip", message_id)
                self.aggregator.stats.errors += 1
                self.client.xack(self.stream, self.group, message_id)
                continue

            try:
                self.aggregator.handle(payload)
            except Exception:
                # Do not ack: the message stays pending, is retried by
                # recover_pending() on the next start, and can be inspected
                # with XPENDING meanwhile.
                logger.exception("failed to process event %s", message_id)
                self.aggregator.stats.errors += 1
                continue

            self.client.xack(self.stream, self.group, message_id)
