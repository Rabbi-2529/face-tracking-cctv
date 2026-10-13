"""
Vision pipeline tests.

Split into two kinds:

* **Pure-geometry tests** that need no model weights - alignment, pose
  estimation, NMS, the quality gate, the tracker and the voter. These are
  most of the logic and they run in CI with no downloads.
* **Model tests** marked ``needs_models``, skipped automatically when the
  ONNX files are absent. They verify the detector and embedder against the
  real weights: that a face is found where one was drawn, that the same face
  embeds consistently, and that two different faces do not collide.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

MODEL_DIR = Path(os.environ.get("MODEL_DIR", "/models"))
MODELS_PRESENT = (MODEL_DIR / "det_10g.onnx").exists() and (
    MODEL_DIR / "w600k_r50.onnx"
).exists()

needs_models = pytest.mark.skipif(
    not MODELS_PRESENT,
    reason=f"ONNX models not present in {MODEL_DIR}; run scripts/download_models.py",
)

# Two photographs of two different people, fetched by `make fixtures` into
# samples/faces/. Having real faces here matters: synthetic patterns tell you
# nothing about whether the detector and embedder actually work.
FACE_DIR = Path(__file__).resolve().parent.parent / "samples" / "faces"
FACES_PRESENT = (FACE_DIR / "face_a.jpg").exists() and (
    FACE_DIR / "face_b.jpg"
).exists()

needs_faces = pytest.mark.skipif(
    not FACES_PRESENT,
    reason=f"sample faces not present in {FACE_DIR}; run `make fixtures`",
)


def load_face(name: str):
    import cv2

    image = cv2.imread(str(FACE_DIR / name))
    if image is None:
        pytest.skip(f"could not read {name}")
    return image


# ---------------------------------------------------------------- alignment


def test_umeyama_recovers_a_known_similarity_transform():
    """A transform built from a known scale/rotation/translation must be
    recovered exactly, otherwise every aligned crop is subtly wrong."""
    from vision.align import umeyama_similarity

    source = np.array([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]])
    angle = np.deg2rad(30.0)
    scale = 2.5
    rotation = np.array(
        [[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]]
    )
    offset = np.array([7.0, -3.0])
    target = (scale * (rotation @ source.T)).T + offset

    matrix = umeyama_similarity(source, target)
    recovered = (matrix[:, :2] @ source.T).T + matrix[:, 2]

    assert np.allclose(recovered, target, atol=1e-4)


def test_align_face_produces_canonical_landmark_positions():
    """Warping a face by its landmarks must land them on the ArcFace
    template - that template is what the recogniser was trained against."""
    import cv2

    from vision.align import ARCFACE_TEMPLATE, align_face

    image = np.zeros((400, 400, 3), dtype=np.uint8)
    # A scaled, shifted version of the template standing in for a real face.
    landmarks = (ARCFACE_TEMPLATE * 2.0) + np.array([80.0, 60.0], dtype=np.float32)
    for x, y in landmarks:
        cv2.circle(image, (int(x), int(y)), 3, (255, 255, 255), -1)

    aligned = align_face(image, landmarks)
    assert aligned.shape == (112, 112, 3)

    # Each template point should now hold a bright pixel.
    grey = cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY)
    for x, y in ARCFACE_TEMPLATE:
        patch = grey[
            max(0, int(y) - 4) : int(y) + 5, max(0, int(x) - 4) : int(x) + 5
        ]
        assert patch.max() > 100, f"template point ({x:.0f},{y:.0f}) is not aligned"


def test_align_rejects_reflection():
    """A mirrored point set must not be 'aligned' by flipping it."""
    from vision.align import umeyama_similarity

    source = np.array([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]])
    mirrored = np.array([[0.0, 0.0], [-1.0, 0.0], [0.0, 1.0]])
    matrix = umeyama_similarity(source, mirrored)
    # A reflection would give a negative determinant.
    assert np.linalg.det(matrix[:, :2]) > 0


def test_pose_estimation_signs_and_labels():
    from vision.align import estimate_pose, pose_label

    frontal = np.array(
        [[38.3, 51.7], [73.5, 51.5], [56.0, 71.7], [41.5, 92.4], [70.7, 92.2]],
        dtype=np.float32,
    )
    yaw, pitch = estimate_pose(frontal)
    assert abs(yaw) < 10.0
    assert pose_label(yaw, pitch) == "frontal"

    # Push the nose well to one side: that is a turned head.
    turned = frontal.copy()
    turned[2, 0] += 18.0
    yaw_turned, pitch_turned = estimate_pose(turned)
    assert yaw_turned > 15.0
    assert pose_label(yaw_turned, pitch_turned) == "left"


# --------------------------------------------------------------------- NMS


def test_nms_collapses_overlaps_and_keeps_distinct_boxes():
    from vision.detect.scrfd import nms

    boxes = np.array(
        [
            [0, 0, 100, 100],  # strongest
            [5, 5, 105, 105],  # near-duplicate, should go
            [500, 500, 600, 600],  # separate face, should stay
        ],
        dtype=np.float32,
    )
    scores = np.array([0.9, 0.85, 0.8], dtype=np.float32)

    keep = nms(boxes, scores, 0.4)
    assert keep == [0, 2]


def test_nms_on_empty_input():
    from vision.detect.scrfd import nms

    assert nms(np.zeros((0, 4), dtype=np.float32), np.zeros(0), 0.4) == []


# ------------------------------------------------------------ quality gate


def _crop(brightness: int = 128, noise: int = 40, size: int = 112) -> np.ndarray:
    rng = np.random.default_rng(1234)
    base = np.full((size, size, 3), brightness, dtype=np.int16)
    base += rng.integers(-noise, noise, base.shape)
    return np.clip(base, 0, 255).astype(np.uint8)


def test_enrollment_gate_rejects_a_tiny_face_with_a_usable_reason():
    from vision.quality import assess_for_enrollment

    report = assess_for_enrollment(
        _crop(),
        face_width_px=40,
        yaw_degrees=0.0,
        pitch_degrees=0.0,
        detection_score=0.95,
    )
    assert not report.passed
    joined = " ".join(report.reasons)
    assert "40px" in joined
    # The message has to tell HR what to do, not quote a metric.
    assert "closer" in joined or "resolution" in joined


def test_enrollment_gate_rejects_dark_washed_out_and_turned_faces():
    from vision.quality import assess_for_enrollment

    dark = assess_for_enrollment(
        _crop(brightness=20), face_width_px=160, yaw_degrees=0, pitch_degrees=0,
        detection_score=0.95,
    )
    assert not dark.passed and any("dark" in r for r in dark.reasons)

    bright = assess_for_enrollment(
        _crop(brightness=245), face_width_px=160, yaw_degrees=0, pitch_degrees=0,
        detection_score=0.95,
    )
    assert not bright.passed and any("washed out" in r for r in bright.reasons)

    turned = assess_for_enrollment(
        _crop(), face_width_px=160, yaw_degrees=55.0, pitch_degrees=0,
        detection_score=0.95,
    )
    assert not turned.passed and any("turned" in r for r in turned.reasons)


def test_enrollment_gate_is_stricter_than_the_live_gate():
    """The whole point of two gates: a frame good enough to contribute to a
    track vote is not necessarily good enough to enter the gallery."""
    from vision.quality import assess_for_enrollment, assess_for_matching

    crop = _crop()
    marginal = {
        "face_width_px": 70,
        "yaw_degrees": 25.0,
        "pitch_degrees": 10.0,
        "detection_score": 0.8,
    }

    assert not assess_for_enrollment(crop, **marginal).passed
    assert assess_for_matching(crop, min_face_px=60, **marginal).passed


def test_quality_score_rises_with_size_and_sharpness():
    from vision.quality import assess_for_matching

    small = assess_for_matching(
        _crop(noise=5), face_width_px=65, yaw_degrees=0, pitch_degrees=0,
        detection_score=0.9, min_face_px=60,
    )
    large = assess_for_matching(
        _crop(noise=60), face_width_px=200, yaw_degrees=0, pitch_degrees=0,
        detection_score=0.99, min_face_px=60,
    )
    assert large.score > small.score


# -------------------------------------------------------- tracking / voting


class _FakeFace:
    """Stands in for vision.pipeline.AnalysedFace."""

    def __init__(self, bbox, embedding, quality=0.8):
        self.bbox = bbox
        self.embedding = embedding
        self.aligned = np.zeros((112, 112, 3), dtype=np.uint8)

        class _Q:
            score = quality
            passed = True
            reasons: list[str] = []

        self.quality = _Q()


def _unit(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    vector = rng.normal(size=512).astype(np.float32)
    return vector / np.linalg.norm(vector)


def test_iou():
    from vision.track.tracker import iou

    assert iou((0, 0, 10, 10), (0, 0, 10, 10)) == pytest.approx(1.0)
    assert iou((0, 0, 10, 10), (100, 100, 110, 110)) == 0.0
    assert 0.0 < iou((0, 0, 10, 10), (5, 5, 15, 15)) < 1.0


def test_tracker_follows_one_person_across_frames():
    from vision.track.tracker import FaceTracker

    tracker = FaceTracker("cam-1", vote_window=5)
    embedding = _unit(1)

    for step in range(6):
        # Moving steadily across the frame, as a walking person would.
        bbox = (100 + step * 12, 100, 200 + step * 12, 220)
        tracker.update([_FakeFace(bbox, embedding)], now=step * 0.25)

    assert len(tracker.tracks) == 1
    track = next(iter(tracker.tracks.values()))
    assert track.hits == 6
    assert track.sample_count == 6


def test_tracker_separates_two_different_people_in_the_same_frame():
    from vision.track.tracker import FaceTracker

    tracker = FaceTracker("cam-1", vote_window=5)
    first, second = _unit(1), _unit(2)

    for step in range(4):
        tracker.update(
            [
                _FakeFace((100, 100, 200, 220), first),
                _FakeFace((600, 100, 700, 220), second),
            ],
            now=step * 0.25,
        )

    assert len(tracker.tracks) == 2


def test_appearance_bridges_a_jump_that_iou_alone_would_lose():
    """At 4 fps a walking person can move clear of their previous box.

    Appearance is what keeps them one track instead of becoming a new one
    every few frames - which would mean never accumulating enough views to
    identify them.
    """
    from vision.track.tracker import FaceTracker

    tracker = FaceTracker("cam-1", vote_window=5)
    embedding = _unit(7)

    tracker.update([_FakeFace((100, 100, 180, 200), embedding)], now=0.0)
    # Non-overlapping box, same person.
    tracker.update([_FakeFace((300, 100, 380, 200), embedding)], now=0.25)

    assert len(tracker.tracks) == 1, "appearance did not bridge the gap"


def test_voted_embedding_is_quality_weighted():
    """A track containing one sharp view and several poor ones should be
    decided by the sharp one."""
    from vision.track.tracker import FaceTracker

    tracker = FaceTracker("cam-1", vote_window=3)
    good, bad = _unit(11), _unit(12)

    tracker.update([_FakeFace((0, 0, 100, 120), good, quality=0.95)], now=0.0)
    for step in range(1, 5):
        tracker.update(
            [_FakeFace((0, 0, 100, 120), bad, quality=0.05)], now=step * 0.2
        )

    track = next(iter(tracker.tracks.values()))
    voted = track.voted_embedding()
    assert voted is not None
    assert float(np.dot(voted, good)) > float(np.dot(voted, bad))


def test_track_is_not_decided_before_the_vote_window():
    from vision.track.tracker import FaceTracker

    tracker = FaceTracker("cam-1", vote_window=10)
    embedding = _unit(3)

    for step in range(4):
        tracker.update([_FakeFace((0, 0, 100, 120), embedding)], now=step * 0.2)

    assert tracker.tracks_ready_to_decide(now=1.0) == []


def test_short_track_is_still_decided_rather_than_lost():
    """Someone crossing a doorway quickly yields few frames. Deciding on
    four views is worse than twelve, but far better than never naming them."""
    from vision.track.tracker import FaceTracker

    tracker = FaceTracker("cam-1", vote_window=12, max_age_seconds=1.0)
    embedding = _unit(4)

    for step in range(3):
        tracker.update([_FakeFace((0, 0, 100, 120), embedding)], now=step * 0.1)

    stragglers = tracker.finish_stragglers(now=0.9)
    assert len(stragglers) == 1


def test_confidence_is_downgraded_when_evidence_is_thin():
    from vision.track.tracker import FaceTracker

    assert FaceTracker.confidence_for("confirmed", 1, 12) == "probable"
    assert FaceTracker.confidence_for("confirmed", 12, 12) == "confirmed"
    # A weak match never gets promoted.
    assert FaceTracker.confidence_for("probable", 20, 12) == "probable"


def test_stale_tracks_are_retired():
    from vision.track.tracker import FaceTracker

    tracker = FaceTracker("cam-1", max_age_seconds=1.0)
    tracker.update([_FakeFace((0, 0, 100, 120), _unit(5))], now=0.0)
    assert len(tracker.tracks) == 1

    tracker.update([], now=5.0)
    assert len(tracker.tracks) == 0


# ------------------------------------------------------------------ models


@needs_models
def test_detector_loads_and_reports_its_providers():
    from vision.detect.scrfd import SCRFDDetector

    detector = SCRFDDetector()
    assert detector.providers
    assert len(detector.output_names) == 9


@needs_models
def test_detector_finds_no_faces_in_noise():
    """A false positive rate this high would flood the gallery with rubbish."""
    from vision.detect.scrfd import SCRFDDetector

    detector = SCRFDDetector()
    rng = np.random.default_rng(99)
    noise = rng.integers(0, 255, (480, 640, 3), dtype=np.uint8)
    assert detector.detect(noise) == []


@needs_models
@needs_faces
@pytest.mark.parametrize("name", ["face_a.jpg", "face_b.jpg"])
def test_detector_finds_a_real_face_and_measures_it(name):
    """The core detector check: a real photograph of a real person."""
    from vision.detect.scrfd import SCRFDDetector

    image = load_face(name)
    faces = SCRFDDetector().detect(image)

    assert len(faces) >= 1, f"detector found no face in {name}"
    face = faces[0]
    assert face.score > 0.5
    assert face.width > 20
    assert face.landmarks.shape == (5, 2)
    assert face.interocular > 5

    # Landmarks must sit inside the box, or every aligned crop is nonsense.
    x1, y1, x2, y2 = face.bbox
    assert (face.landmarks[:, 0] >= x1 - 5).all()
    assert (face.landmarks[:, 0] <= x2 + 5).all()
    assert (face.landmarks[:, 1] >= y1 - 5).all()
    assert (face.landmarks[:, 1] <= y2 + 5).all()

    # The box must lie within the frame; a letterboxing error shows up here.
    height, width = image.shape[:2]
    assert 0 <= x1 < x2 <= width
    assert 0 <= y1 < y2 <= height


@needs_models
def test_embeddings_are_unit_length_and_self_consistent():
    from vision.embed.arcface import ArcFaceEmbedder, cosine_similarity

    embedder = ArcFaceEmbedder()
    crop = _crop(size=112)

    first = embedder.embed(crop)
    second = embedder.embed(crop)

    assert first.shape == (512,)
    assert float(np.linalg.norm(first)) == pytest.approx(1.0, abs=1e-4)
    # Deterministic: the same input must give the same vector, or nothing
    # downstream can be reasoned about.
    assert cosine_similarity(first, second) == pytest.approx(1.0, abs=1e-5)


@needs_models
def test_batched_embedding_matches_single_embedding():
    from vision.embed.arcface import ArcFaceEmbedder, cosine_similarity

    embedder = ArcFaceEmbedder()
    crops = [_crop(brightness=b, size=112) for b in (90, 128, 170)]

    batched = embedder.embed_batch(crops)
    assert batched.shape == (3, 512)

    for index, crop in enumerate(crops):
        single = embedder.embed(crop)
        assert cosine_similarity(batched[index], single) == pytest.approx(
            1.0, abs=1e-4
        )


@needs_models
def test_embedder_rejects_an_unaligned_crop():
    """A wrongly sized crop must fail loudly, not silently embed garbage."""
    from vision.embed.arcface import ArcFaceEmbedder

    embedder = ArcFaceEmbedder()
    with pytest.raises(ValueError, match="Align it first"):
        embedder.embed(_crop(size=64))


def _embed_face(image):
    """Detect, align and embed the largest face in an image."""
    from vision.align import align_face
    from vision.detect.scrfd import SCRFDDetector
    from vision.embed.arcface import ArcFaceEmbedder

    faces = SCRFDDetector().detect(image)
    if not faces:
        pytest.skip("detector found no face")
    return ArcFaceEmbedder().embed(align_face(image, faces[0].landmarks))


@needs_models
@needs_faces
def test_same_face_matches_itself_far_better_than_a_different_person():
    """The property the entire system rests on.

    Two views of one person must score far higher against each other than
    against a different person, with a clear gap between the two. If this
    fails, no choice of threshold can rescue the system.
    """
    import cv2

    from vision.embed.arcface import cosine_similarity

    face_a = load_face("face_a.jpg")
    face_b = load_face("face_b.jpg")

    original = _embed_face(face_a)

    # The same person, degraded the way a camera degrades them: softened,
    # darker, lower contrast. Still obviously the same person to a human.
    degraded = cv2.GaussianBlur(face_a, (5, 5), 0)
    degraded = np.clip(degraded.astype(np.int16) * 0.82 + 12, 0, 255).astype(np.uint8)
    same_person = _embed_face(degraded)

    different_person = _embed_face(face_b)

    self_similarity = cosine_similarity(original, same_person)
    other_similarity = cosine_similarity(original, different_person)

    assert self_similarity > 0.6, (
        f"two views of the same person scored only {self_similarity:.3f}; "
        "alignment or normalisation is wrong"
    )
    assert other_similarity < 0.4, (
        f"two different people scored {other_similarity:.3f}; "
        "this would cause confident mis-identification"
    )
    # The gap is what a threshold lives in. Without it, no setting works.
    assert self_similarity - other_similarity > 0.3, (
        f"separation is only {self_similarity - other_similarity:.3f}"
    )


@needs_models
@needs_faces
def test_default_thresholds_sit_inside_the_separation_gap():
    """Sanity-check the shipped defaults against real faces.

    Not a substitute for tuning on your own cameras - these are studio
    photographs, not corridor frames - but if the defaults fell outside the
    gap even here, they would be indefensible as a starting point.
    """
    from django.conf import settings

    from vision.embed.arcface import cosine_similarity

    original = _embed_face(load_face("face_a.jpg"))
    other = _embed_face(load_face("face_b.jpg"))

    high = settings.MATCH_THRESHOLD_HIGH
    low = settings.MATCH_THRESHOLD_LOW

    assert low < high, "the low threshold must sit below the high one"
    # A different person must not reach even the "probable" band.
    assert cosine_similarity(original, other) < low, (
        "two different people score above MATCH_THRESHOLD_LOW; the floor is "
        "too permissive"
    )
    # And a face matched against itself must clear "confirmed".
    assert cosine_similarity(original, original) > high


@needs_models
@needs_faces
def test_pipeline_rejects_a_group_photo_for_enrollment():
    """Silently enrolling the wrong face out of a group photo is far worse
    than refusing the upload, so the gate refuses rather than guesses."""
    import cv2

    from vision.pipeline import FacePipeline

    face_a = load_face("face_a.jpg")
    face_b = load_face("face_b.jpg")
    height = min(face_a.shape[0], face_b.shape[0])
    together = np.hstack(
        [face_a[:height], cv2.resize(face_b, (face_b.shape[1], height))]
    )

    face, error = FacePipeline().analyse_enrollment(together)
    assert face is None
    assert "faces were found" in error


@needs_models
@needs_faces
def test_pipeline_accepts_a_good_enrollment_image_and_embeds_it():
    from vision.pipeline import FacePipeline

    face, error = FacePipeline().analyse_enrollment(load_face("face_a.jpg"))

    assert error == "", f"a clear portrait was rejected: {error}"
    assert face is not None
    assert face.embedding is not None
    assert face.embedding.shape == (512,)
    assert face.quality.passed
    assert face.face_width_px >= 100
    assert face.pose in {"frontal", "left", "right", "up", "down"}


@needs_models
@needs_faces
def test_pipeline_rejects_a_downscaled_face_as_too_small():
    """A face below the enrolment size floor must be refused with advice,
    which is what stops a low-resolution photo poisoning the gallery."""
    import cv2

    from vision.pipeline import FacePipeline

    image = load_face("face_a.jpg")
    tiny = cv2.resize(image, (image.shape[1] // 6, image.shape[0] // 6))

    face, error = FacePipeline().analyse_enrollment(tiny)
    # Either the detector loses it entirely or the gate rejects it; both are
    # acceptable, silently accepting it is not.
    assert error != ""
    if face is not None:
        assert not face.quality.passed


@needs_models
def test_pipeline_rejects_an_image_with_no_face():
    from vision.pipeline import FacePipeline

    rng = np.random.default_rng(3)
    noise = rng.integers(0, 255, (480, 640, 3), dtype=np.uint8)

    face, error = FacePipeline().analyse_enrollment(noise)
    assert face is None
    assert "No face" in error
