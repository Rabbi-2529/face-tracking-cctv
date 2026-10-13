"""
Camera worker behaviour over time.

The tracker tests in test_vision_pipeline.py check each step in isolation.
These drive :class:`vision.runner.CameraWorker` through a sequence of frames
and empty loops, because the failures worth catching here only show up over
time: a person who has left still being reported as present, or a quick walk
through a doorway never being named because nobody else came along after.

No models, no Redis, no database - the detector, gallery, bus and snapshot
store are replaced with small fakes.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

FRAME_SHAPE = (480, 640, 3)
BBOX = (200, 100, 320, 250)


def _unit(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    vector = rng.normal(size=512).astype(np.float32)
    return vector / np.linalg.norm(vector)


class _Quality:
    passed = True
    reasons: list[str] = []

    def __init__(self, score: float) -> None:
        self.score = score


class _Face:
    def __init__(self, embedding, quality: float = 0.8, bbox=BBOX) -> None:
        self.bbox = bbox
        self.embedding = embedding
        self.aligned = np.zeros((112, 112, 3), dtype=np.uint8)
        self.quality = _Quality(quality)
        self.face_width_px = bbox[2] - bbox[0]


class _Pipeline:
    def __init__(self) -> None:
        self.faces: list[_Face] = []
        self.calls: list[dict] = []

    def analyse_frame(self, frame, **kwargs):
        self.calls.append(kwargs)
        return list(self.faces)


class _Gallery:
    def __init__(self, result=("ENG001", 0.7, "confirmed"), vote_window: int = 12) -> None:
        from vision.gallery import PipelineParams

        self.result = result
        self.params = PipelineParams(vote_window=vote_window)

    def match(self, embedding):
        return self.result

    def refresh_if_stale(self) -> bool:
        return False


class _Bus:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def publish(self, event):
        self.events.append(event)
        return "0-1"


class _Snapshots:
    def __init__(self) -> None:
        self.images: list[np.ndarray] = []

    def write_image(self, image, *, camera_code, track_id, when=None) -> str:
        self.images.append(image)
        return f"snapshots/{track_id}.jpg"

    def write(self, frame, bbox, *, camera_code, track_id, when=None) -> str:
        from vision.snapshot import crop_with_context

        return self.write_image(
            crop_with_context(frame, bbox), camera_code=camera_code, track_id=track_id
        )


class _Preview:
    def wanted(self) -> bool:
        return False


def _worker(*, gallery=None, identity: bool = True):
    from vision.runner import CameraWorker

    spec = SimpleNamespace(
        code="cam-1",
        is_identity=identity,
        sample_fps=4.0,
        roi_polygon=[],
        width=640,
        height=480,
    )
    worker = CameraWorker(spec)
    worker.pipeline = _Pipeline()
    worker.gallery = gallery or _Gallery()
    worker.bus = _Bus()
    worker.snapshots = _Snapshots()
    worker.preview = _Preview()
    return worker


def _frame(value: int = 0) -> np.ndarray:
    return np.full(FRAME_SHAPE, value, dtype=np.uint8)


def _step(worker, now: float, faces=None, frame=None) -> None:
    """One loop of the worker: an analysed frame (if any) plus housekeeping."""
    if faces is not None:
        worker.pipeline.faces = faces
        worker.process_frame(frame if frame is not None else _frame(), now)
    worker.housekeep(now)


# --------------------------------------------------------------- lifecycle


def test_a_quick_doorway_crossing_is_named_after_the_person_has_left():
    """Three usable frames, then nothing. The straggler must be decided by
    housekeeping alone - no other face is coming along to trigger it."""
    worker = _worker()
    embedding = _unit(1)

    for now in (0.0, 0.25, 0.5):
        _step(worker, now, faces=[_Face(embedding)])
    assert worker.bus.events == []

    # The person is gone. Only empty loops from here on.
    for now in (1.0, 1.5, 2.0, 2.5):
        _step(worker, now)

    assert len(worker.bus.events) == 1
    event = worker.bus.events[0]
    assert event["employee_code"] == "ENG001"
    # Three views is thin evidence; the confidence says so.
    assert event["confidence"] == "probable"


def test_a_person_who_has_left_stops_being_reported_present():
    """The ghost-presence regression: a decided track must not keep
    re-publishing after its person walks out of view."""
    worker = _worker()
    embedding = _unit(2)

    now = 0.0
    for _ in range(12):
        _step(worker, now, faces=[_Face(embedding)])
        now += 0.25
    assert len(worker.bus.events) == 1, "decided once the vote window filled"

    # Nobody in view for the next minute.
    while now < 60.0:
        _step(worker, now)
        now += 0.5

    assert len(worker.bus.events) == 1
    assert worker.tracker.tracks == {}


def test_a_person_who_stays_keeps_their_presence_alive():
    worker = _worker()
    embedding = _unit(3)

    now = 0.0
    while now < 9.0:
        _step(worker, now, faces=[_Face(embedding)])
        now += 0.25

    # Decided at 2.75s, then re-published five seconds later.
    assert len(worker.bus.events) == 2
    assert {e["employee_code"] for e in worker.bus.events} == {"ENG001"}


def test_vote_window_follows_the_pipeline_config():
    worker = _worker(gallery=_Gallery(vote_window=4))
    embedding = _unit(4)

    for index in range(4):
        _step(worker, index * 0.25, faces=[_Face(embedding)])

    assert worker.tracker.vote_window == 4
    assert len(worker.bus.events) == 1
    assert worker.bus.events[0]["confidence"] == "confirmed"


# ---------------------------------------------------------------- evidence


def test_snapshot_comes_from_the_frame_with_the_best_view():
    """A short track is decided after the person left, so the current frame
    is empty. The snapshot must be the crop taken when the best view was
    seen - here the second frame, painted 50."""
    worker = _worker()
    embedding = _unit(5)

    qualities = (0.5, 0.9, 0.6)
    for index, quality in enumerate(qualities):
        _step(
            worker,
            index * 0.25,
            faces=[_Face(embedding, quality=quality)],
            frame=_frame(value=index * 50),
        )
    for now in (1.0, 2.0, 2.5):
        _step(worker, now)

    assert len(worker.snapshots.images) == 1
    assert int(worker.snapshots.images[0].mean()) == 50
    assert worker.bus.events[0]["snapshot_key"].endswith(".jpg")


def test_only_unknown_faces_carry_an_embedding_and_only_once():
    unknown = _worker(gallery=_Gallery(result=(None, 0.12, "unknown")))
    embedding = _unit(6)
    now = 0.0
    while now < 40.0:
        faces = [_Face(embedding)] if now < 35.0 else None
        _step(unknown, now, faces=faces)
        now += 0.25

    # First decision plus at least one 30-second re-vote.
    assert len(unknown.bus.events) >= 2
    first, *later = unknown.bus.events
    assert first["employee_code"] is None
    assert first["embedding"] is not None and len(first["embedding"]) == 512
    assert all(e["embedding"] is None for e in later)

    known = _worker()
    for index in range(12):
        _step(known, index * 0.25, faces=[_Face(embedding)])
    assert known.bus.events[0]["embedding"] is None


def test_worker_passes_roi_and_quality_floor_to_the_pipeline():
    worker = _worker()
    worker.spec.roi_polygon = [[0, 0], [0.5, 0], [0.5, 1], [0, 1]]
    worker.gallery.params.min_quality_score = 0.42

    _step(worker, 0.0, faces=[])

    call = worker.pipeline.calls[-1]
    assert call["roi"] == worker.spec.roi_polygon
    assert call["min_quality_score"] == pytest.approx(0.42)


# ----------------------------------------------------------- region / gate


def test_inside_roi():
    from vision.pipeline import inside_roi

    left_half = [[0, 0], [0.5, 0], [0.5, 1], [0, 1]]
    assert inside_roi((10, 10, 50, 50), left_half, 640, 480)
    assert not inside_roi((500, 10, 600, 80), left_half, 640, 480)
    # Fewer than three points is no mask at all.
    assert inside_roi((500, 10, 600, 80), [[0, 0], [1, 1]], 640, 480)
    assert inside_roi((500, 10, 600, 80), [], 640, 480)


def test_faces_outside_the_roi_are_never_analysed():
    from vision.detect.scrfd import Face
    from vision.pipeline import FacePipeline

    def face_at(x: float) -> Face:
        landmarks = np.array(
            [[x + 30, 140], [x + 70, 140], [x + 50, 165], [x + 35, 190], [x + 65, 190]],
            dtype=np.float32,
        )
        return Face(
            bbox=np.array([x, 100, x + 100, 220], dtype=np.float32),
            landmarks=landmarks,
            score=0.9,
        )

    class _Detector:
        def detect(self, image, *, max_faces=0):
            return [face_at(20), face_at(500)]

    class _Embedder:
        model_version = "test"

        def embed_batch(self, crops):
            return np.stack([_unit(i) for i in range(len(crops))])

    pipeline = FacePipeline(detector=_Detector(), embedder=_Embedder())
    frame = np.random.default_rng(0).integers(0, 255, FRAME_SHAPE, dtype=np.uint8)

    everything = pipeline.analyse_frame(frame, min_face_px=60)
    left_only = pipeline.analyse_frame(
        frame, min_face_px=60, roi=[[0, 0], [0.5, 0], [0.5, 1], [0, 1]]
    )

    assert len(everything) == 2
    assert len(left_only) == 1
    assert left_only[0].bbox[0] < 320


def test_live_quality_floor_is_applied():
    from vision.quality import assess_for_matching

    crop = np.random.default_rng(1).integers(0, 255, (112, 112, 3), dtype=np.uint8)
    params = {
        "face_width_px": 90,
        "yaw_degrees": 0.0,
        "pitch_degrees": 0.0,
        "detection_score": 0.9,
    }

    assert assess_for_matching(crop, min_face_px=60, **params).passed
    strict = assess_for_matching(crop, min_face_px=60, min_quality_score=0.99, **params)
    assert not strict.passed
    assert any("overall quality" in reason for reason in strict.reasons)
