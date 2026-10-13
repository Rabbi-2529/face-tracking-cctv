"""
Aggregator behaviour: detections in, presence and movement out.

Times are pinned to midday yesterday, local time, so the presence sweeper
always sees them as stale and no test can straddle midnight.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pytest
from django.utils import timezone


@pytest.fixture
def cameras(db, org):
    from apps.cameras.models import Camera

    def make(code: str, zone) -> Camera:
        return Camera.objects.create(
            code=code,
            name=code,
            zone=zone,
            purpose=Camera.Purpose.IDENTITY,
            rtsp_url=f"rtsp://user:pass@10.0.0.1:554/{code}",
            measured_face_width_px=110,
        )

    return {
        "reception": make("cam-reception", org["reception"]),
        "corridor": make("cam-corridor", org["corridor"]),
        "office": make("cam-office", org["office"]),
    }


@pytest.fixture
def broadcasts(monkeypatch):
    """Record live-board pushes instead of sending them over Redis."""
    import apps.tracking.aggregator as aggregator

    sent: list[set[int]] = []
    monkeypatch.setattr(
        aggregator, "broadcast_zone_occupancy", lambda zone_ids: sent.append(set(zone_ids))
    )
    monkeypatch.setattr("channels.layers.get_channel_layer", lambda *a, **k: None)
    return sent


@pytest.fixture
def noon():
    yesterday = timezone.localdate() - dt.timedelta(days=1)
    return timezone.make_aware(
        dt.datetime.combine(yesterday, dt.time(12, 0)), timezone.get_current_timezone()
    )


def _event(camera, at, *, employee=None, track="t-1", embedding=None, confidence=None):
    return {
        "v": 1,
        "camera_code": camera.code,
        "captured_at": at.isoformat(),
        "track_id": track,
        "frame_index": 1,
        "bbox": [0, 0, 100, 120],
        "face_width_px": 100,
        "quality_score": 0.8,
        "employee_code": employee.employee_code if employee else None,
        "match_score": 0.7 if employee else 0.1,
        "confidence": confidence or ("confirmed" if employee else "unknown"),
        "snapshot_key": "",
        "embedding": embedding,
    }


def _sweep():
    from apps.tracking.tasks import close_stale_intervals

    return close_stale_intervals()


def _unit(seed: int) -> list[float]:
    rng = np.random.default_rng(seed)
    vector = rng.normal(size=512)
    return (vector / np.linalg.norm(vector)).tolist()


# ---------------------------------------------------------------- movement


def test_first_sighting_of_the_day_is_an_arrival(cameras, employees, broadcasts, noon):
    from apps.tracking.aggregator import DetectionAggregator
    from apps.tracking.models import MovementEvent

    DetectionAggregator().handle(_event(cameras["reception"], noon, employee=employees["eng"]))

    event = MovementEvent.objects.get()
    assert event.from_zone is None
    assert event.to_zone.code == "reception"


def test_transition_survives_the_sweeper_closing_the_first_room(
    cameras, employees, broadcasts, noon
):
    """Walking from reception to the corridor took longer than the presence
    gap, so the sweeper closed reception first. That is still a transition
    from reception, not a second arrival."""
    from apps.tracking.aggregator import DetectionAggregator
    from apps.tracking.models import MovementEvent

    aggregator = DetectionAggregator()
    eng = employees["eng"]
    aggregator.handle(_event(cameras["reception"], noon, employee=eng))
    assert _sweep()["closed"] == 1

    aggregator.handle(
        _event(cameras["corridor"], noon + dt.timedelta(seconds=90), employee=eng, track="t-2")
    )

    latest = MovementEvent.objects.order_by("-occurred_at").first()
    assert latest.from_zone.code == "reception"
    assert latest.to_zone.code == "corridor"
    assert latest.transit_seconds == 90
    assert latest.is_plausible


def test_returning_to_the_same_room_is_not_a_movement(cameras, employees, broadcasts, noon):
    from apps.tracking.aggregator import DetectionAggregator
    from apps.tracking.models import MovementEvent, PresenceInterval

    aggregator = DetectionAggregator()
    eng = employees["eng"]
    aggregator.handle(_event(cameras["office"], noon, employee=eng))
    _sweep()
    aggregator.handle(
        _event(cameras["office"], noon + dt.timedelta(minutes=5), employee=eng, track="t-2")
    )

    assert PresenceInterval.objects.filter(employee=eng).count() == 2
    # Only the morning arrival - no office -> office, and nothing implausible.
    assert MovementEvent.objects.filter(employee=eng).count() == 1
    assert not MovementEvent.objects.filter(is_plausible=False).exists()


def test_stale_open_interval_in_the_same_room_is_not_a_movement(
    cameras, employees, broadcasts, noon
):
    """The sweeper has not run yet, but the gap has passed: the aggregator
    closes and reopens the interval itself. Still not a movement."""
    from apps.tracking.aggregator import DetectionAggregator
    from apps.tracking.models import MovementEvent, PresenceInterval

    aggregator = DetectionAggregator()
    eng = employees["eng"]
    aggregator.handle(_event(cameras["office"], noon, employee=eng))
    aggregator.handle(
        _event(cameras["office"], noon + dt.timedelta(minutes=3), employee=eng, track="t-2")
    )

    assert PresenceInterval.objects.filter(employee=eng).count() == 2
    assert MovementEvent.objects.filter(employee=eng).count() == 1


def test_occupancy_is_pushed_for_both_rooms_on_a_transition(
    cameras, employees, broadcasts, org, noon
):
    from apps.tracking.aggregator import DetectionAggregator

    aggregator = DetectionAggregator()
    eng = employees["eng"]
    aggregator.handle(_event(cameras["reception"], noon, employee=eng))
    aggregator.handle(
        _event(cameras["corridor"], noon + dt.timedelta(seconds=20), employee=eng, track="t-2")
    )

    assert broadcasts[-1] == {org["reception"].pk, org["corridor"].pk}


def test_sweeper_pushes_occupancy_for_rooms_it_empties(cameras, employees, broadcasts, org, noon):
    from apps.tracking.aggregator import DetectionAggregator

    DetectionAggregator().handle(_event(cameras["office"], noon, employee=employees["eng"]))
    broadcasts.clear()

    _sweep()

    assert broadcasts == [{org["office"].pk}]


# ----------------------------------------------------------- unknown faces


def test_repeat_sightings_of_one_stranger_form_one_cluster(cameras, broadcasts, org, noon):
    from apps.tracking.aggregator import DetectionAggregator
    from apps.tracking.models import Detection, UnknownFaceCluster

    aggregator = DetectionAggregator()
    face = np.asarray(_unit(10))
    nearly_the_same = face + np.asarray(_unit(11)) * 0.2

    aggregator.handle(
        _event(cameras["reception"], noon, track="cam-reception:1", embedding=face.tolist())
    )
    aggregator.handle(
        _event(
            cameras["office"],
            noon + dt.timedelta(minutes=20),
            track="cam-office:7",
            embedding=nearly_the_same.tolist(),
        )
    )

    cluster = UnknownFaceCluster.objects.get()
    assert cluster.sighting_count == 2
    assert set(cluster.zones_seen.values_list("code", flat=True)) == {"reception", "office"}
    assert cluster.first_seen == noon
    assert Detection.objects.filter(unknown_cluster=cluster).count() == 2


def test_different_strangers_get_different_clusters(cameras, broadcasts, noon):
    from apps.tracking.aggregator import DetectionAggregator
    from apps.tracking.models import UnknownFaceCluster

    aggregator = DetectionAggregator()
    aggregator.handle(_event(cameras["reception"], noon, track="a", embedding=_unit(20)))
    aggregator.handle(_event(cameras["reception"], noon, track="b", embedding=_unit(21)))

    assert UnknownFaceCluster.objects.count() == 2


def test_later_events_of_one_track_join_its_cluster_without_recounting(
    cameras, broadcasts, noon
):
    """Workers send the embedding once per unknown track; re-votes come
    without one and must land in the same cluster as one sighting."""
    from apps.tracking.aggregator import DetectionAggregator
    from apps.tracking.models import Detection, UnknownFaceCluster

    aggregator = DetectionAggregator()
    camera = cameras["reception"]
    aggregator.handle(_event(camera, noon, track="cam-reception:3", embedding=_unit(30)))
    aggregator.handle(
        _event(camera, noon + dt.timedelta(seconds=30), track="cam-reception:3")
    )

    cluster = UnknownFaceCluster.objects.get()
    assert cluster.sighting_count == 1
    assert cluster.last_seen == noon + dt.timedelta(seconds=30)
    assert Detection.objects.filter(unknown_cluster=cluster).count() == 2


def test_unknown_without_embedding_or_track_history_is_kept_unclustered(
    cameras, broadcasts, noon
):
    from apps.tracking.aggregator import DetectionAggregator
    from apps.tracking.models import Detection, UnknownFaceCluster

    DetectionAggregator().handle(_event(cameras["reception"], noon, track="x"))

    assert Detection.objects.get().unknown_cluster is None
    assert not UnknownFaceCluster.objects.exists()


def test_unknown_faces_create_no_presence(cameras, broadcasts, noon):
    from apps.tracking.aggregator import DetectionAggregator
    from apps.tracking.models import PresenceInterval

    DetectionAggregator().handle(_event(cameras["reception"], noon, embedding=_unit(40)))

    assert not PresenceInterval.objects.exists()


def test_retention_removes_clusters_not_seen_recently(db, settings):
    from apps.tracking.models import UnknownFaceCluster
    from apps.tracking.tasks import enforce_retention

    now = timezone.now()
    old = now - dt.timedelta(days=settings.RETENTION["detection_days"] + 1)
    for seen in (old, now):
        UnknownFaceCluster.objects.create(
            centroid_embedding=_unit(50), first_seen=seen, last_seen=seen
        )

    results = enforce_retention()

    assert results["unknown_clusters_deleted"] == 1
    assert UnknownFaceCluster.objects.get().last_seen == now


# ---------------------------------------------------------- restart recovery


class _PendingRedis:
    """Just enough of a Redis client to replay a consumer's pending list."""

    def __init__(self, pending):
        self.pending = list(pending)  # [(id, fields)]
        self.acked: list[str] = []

    def xreadgroup(self, groupname, consumername, streams, count=None, block=None):
        cursor = next(iter(streams.values()))
        if cursor == ">":
            return []
        after = [m for m in self.pending if cursor == "0" or m[0] > cursor]
        return [("events:detections", after[:count])] if after else []

    def xack(self, stream, group, message_id):
        self.acked.append(message_id)
        self.pending = [m for m in self.pending if m[0] != message_id]


def test_events_left_pending_by_a_crash_are_processed_on_restart(
    cameras, employees, broadcasts, noon
):
    import json

    from apps.tracking.aggregator import AggregatorRunner
    from apps.tracking.models import Detection

    good = _event(cameras["office"], noon, employee=employees["eng"])
    broken = {"v": 1, "camera_code": cameras["office"].code}  # no captured_at
    runner = AggregatorRunner(consumer_name="test-1")
    runner.client = _PendingRedis(
        [
            ("1-0", {"data": json.dumps(good)}),
            ("2-0", {"data": json.dumps(broken)}),
            ("3-0", {"data": json.dumps({**good, "track_id": "t-9"})}),
        ]
    )

    recovered = runner.recover_pending()

    assert recovered == 2
    assert Detection.objects.count() == 2
    assert runner.client.acked == ["1-0", "3-0"]
    # The broken event stays pending for inspection, and the walk ended
    # rather than retrying it in a loop.
    assert [m[0] for m in runner.client.pending] == ["2-0"]
