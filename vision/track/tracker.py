"""
Within-camera tracking and track-level identity voting.

**This module is where recognition accuracy is actually won.** A single frame
of a moving person, seen at 90px across a corridor, produces an embedding
that is often closer to the wrong gallery entry than the right one. Deciding
identity per frame is precisely what produces the "it keeps showing the wrong
name" complaint. Deciding once per *track*, from an average of a dozen views,
removes most of that error for free.

Association uses IOU **and** embedding similarity rather than IOU alone.
At the 3-5 fps this system samples, a walking person moves far enough between
frames that boxes may not overlap at all, and appearance is what bridges the
gap. This is a deliberately lightweight stand-in for ByteTrack with a Kalman
filter: adequate at low sample rates and dependency-free, and the place to
upgrade first if you move to crowded scenes or higher frame rates.
"""

from __future__ import annotations

import itertools
import logging
import time
from dataclasses import dataclass, field

import numpy as np

logger = logging.getLogger("vision.track")

# Association weights. Appearance is trusted more than geometry because the
# sample rate makes geometry unreliable.
IOU_WEIGHT = 0.35
EMBEDDING_WEIGHT = 0.65
# A candidate pairing worse than this is treated as a different person.
MIN_ASSOCIATION_SCORE = 0.35
# Drop a track that has not been seen for this long (seconds).
MAX_AGE_SECONDS = 3.0


def iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b

    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    intersection = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    if intersection == 0:
        return 0.0

    area_a = max(0, ax2 - ax1) * max(0, ay2 - ay1)
    area_b = max(0, bx2 - bx1) * max(0, by2 - by1)
    union = area_a + area_b - intersection
    return intersection / union if union > 0 else 0.0


@dataclass
class Track:
    """One person being followed within a single camera view."""

    track_id: str
    bbox: tuple[int, int, int, int]
    created_at: float
    last_seen_at: float

    # Starts at zero because FaceTracker always calls update() immediately
    # after constructing a track; the first update is what makes it one hit.
    hits: int = 0
    # Embeddings collected for voting. Capped so a person who stands in
    # front of a camera all afternoon does not grow this without bound.
    embeddings: list[np.ndarray] = field(default_factory=list)
    qualities: list[float] = field(default_factory=list)

    # Exponential moving average of appearance, used for association only.
    # Kept separate from the voting buffer so association drift cannot bias
    # the identity decision.
    appearance: np.ndarray | None = None

    # Decision state.
    decided_employee: str | None = None
    decided_score: float = 0.0
    decided_confidence: str = "unknown"
    decided_at: float = 0.0
    last_published_at: float = 0.0

    best_quality: float = 0.0
    best_aligned: np.ndarray | None = None
    best_bbox: tuple[int, int, int, int] | None = None
    # Set when the latest update produced a new best view, so the worker can
    # cut the evidence snapshot from *that* frame. Cutting it later, from
    # whatever frame happens to be current, would often miss the person -
    # a short track is usually decided after they have already left.
    best_improved: bool = False
    best_snapshot: np.ndarray | None = None

    MAX_EMBEDDINGS = 48

    def update(
        self,
        bbox: tuple[int, int, int, int],
        embedding: np.ndarray | None,
        quality_score: float,
        aligned: np.ndarray | None,
        now: float,
    ) -> None:
        self.bbox = bbox
        self.last_seen_at = now
        self.hits += 1

        if embedding is not None:
            if len(self.embeddings) < self.MAX_EMBEDDINGS:
                self.embeddings.append(embedding)
                self.qualities.append(quality_score)
            else:
                # Replace the weakest sample rather than discarding the new
                # one: late, better views should be able to improve the vote.
                weakest = int(np.argmin(self.qualities))
                if quality_score > self.qualities[weakest]:
                    self.embeddings[weakest] = embedding
                    self.qualities[weakest] = quality_score

            if self.appearance is None:
                self.appearance = embedding.copy()
            else:
                self.appearance = 0.7 * self.appearance + 0.3 * embedding
                norm = float(np.linalg.norm(self.appearance))
                if norm > 0:
                    self.appearance /= norm

        # Keep the single best crop as the evidence snapshot for this track.
        if aligned is not None and quality_score > self.best_quality:
            self.best_quality = quality_score
            self.best_aligned = aligned
            self.best_bbox = bbox
            self.best_improved = True

    def age(self, now: float) -> float:
        return now - self.last_seen_at

    @property
    def sample_count(self) -> int:
        return len(self.embeddings)

    def voted_embedding(self, top_k: int = 10) -> np.ndarray | None:
        """Quality-weighted mean of this track's best embeddings.

        Takes the best ``top_k`` by quality rather than all of them: a track
        that includes a few sharp frontal views and twenty blurred profiles
        should be decided by the former.
        """
        if not self.embeddings:
            return None

        order = np.argsort(self.qualities)[::-1][:top_k]
        selected = np.stack([self.embeddings[i] for i in order])
        weights = np.asarray([self.qualities[i] for i in order], dtype=np.float32)
        if weights.sum() <= 0:
            weights = np.ones_like(weights)
        weights = weights / weights.sum()

        mean = (selected * weights[:, None]).sum(axis=0)
        norm = float(np.linalg.norm(mean))
        return (mean / norm).astype(np.float32) if norm > 0 else None


class FaceTracker:
    """Maintains tracks for one camera and decides identity once per track."""

    def __init__(
        self,
        camera_code: str,
        *,
        vote_window: int = 12,
        max_age_seconds: float = MAX_AGE_SECONDS,
        republish_seconds: float = 5.0,
        redecide_seconds: float = 30.0,
    ) -> None:
        self.camera_code = camera_code
        self.vote_window = vote_window
        self.max_age_seconds = max_age_seconds
        # How often a decided track re-emits an event, so a person standing
        # still keeps their presence interval alive in the aggregator.
        self.republish_seconds = republish_seconds
        # How often a long-lived track re-runs its vote, so an identity can
        # be corrected as better views arrive.
        self.redecide_seconds = redecide_seconds

        self.tracks: dict[str, Track] = {}
        self._counter = itertools.count(1)

    def _new_track_id(self) -> str:
        return f"{self.camera_code}:{next(self._counter)}"

    # --------------------------------------------------------- association

    def _score_pair(self, track: Track, face) -> float:
        geometry = iou(track.bbox, face.bbox)
        if face.embedding is not None and track.appearance is not None:
            appearance = float(np.dot(track.appearance, face.embedding))
            # Cosine lands in [-1, 1]; map to [0, 1] for blending.
            appearance = (appearance + 1.0) / 2.0
            return IOU_WEIGHT * geometry + EMBEDDING_WEIGHT * appearance
        # No appearance available on either side: geometry is all there is.
        return geometry

    def update(self, faces: list, now: float | None = None) -> list[Track]:
        """Associate this frame's faces with existing tracks.

        Greedy matching on the combined score. Hungarian assignment would be
        marginally better but needs scipy; with at most a handful of faces per
        frame at a doorway, greedy is indistinguishable in practice.
        """
        now = time.monotonic() if now is None else now

        self.retire_stale(now)

        unmatched_faces = list(range(len(faces)))
        available = list(self.tracks.values())

        pairs: list[tuple[float, int, Track]] = []
        for face_index in unmatched_faces:
            for track in available:
                score = self._score_pair(track, faces[face_index])
                if score >= MIN_ASSOCIATION_SCORE:
                    pairs.append((score, face_index, track))

        pairs.sort(key=lambda p: p[0], reverse=True)

        claimed_faces: set[int] = set()
        claimed_tracks: set[str] = set()
        touched: list[Track] = []

        # The score only mattered for the sort; greedy takes best-first.
        for _score, face_index, track in pairs:
            if face_index in claimed_faces or track.track_id in claimed_tracks:
                continue
            face = faces[face_index]
            track.update(
                face.bbox, face.embedding, face.quality.score, face.aligned, now
            )
            claimed_faces.add(face_index)
            claimed_tracks.add(track.track_id)
            touched.append(track)

        # Anything left over is somebody new.
        for face_index in unmatched_faces:
            if face_index in claimed_faces:
                continue
            face = faces[face_index]
            track = Track(
                track_id=self._new_track_id(),
                bbox=face.bbox,
                created_at=now,
                last_seen_at=now,
            )
            track.update(
                face.bbox, face.embedding, face.quality.score, face.aligned, now
            )
            self.tracks[track.track_id] = track
            touched.append(track)

        return touched

    def retire_stale(self, now: float | None = None) -> list[Track]:
        """Drop tracks nobody has seen for ``max_age_seconds``.

        Must run on every loop of the worker, not only when faces are in
        view. If retirement waited for the next face to appear, a decided
        track would keep re-publishing after its person left, and their
        presence interval would never close.
        """
        now = time.monotonic() if now is None else now
        stale = [
            track for track in self.tracks.values() if track.age(now) > self.max_age_seconds
        ]
        for track in stale:
            del self.tracks[track.track_id]
        return stale

    # ------------------------------------------------------------ decisions

    def tracks_ready_to_decide(self, now: float | None = None) -> list[Track]:
        """Tracks with enough evidence for a first or refreshed decision."""
        now = time.monotonic() if now is None else now
        ready: list[Track] = []
        for track in self.tracks.values():
            if track.sample_count == 0:
                continue
            if track.decided_at == 0.0:
                if track.sample_count >= self.vote_window:
                    ready.append(track)
            elif now - track.decided_at >= self.redecide_seconds:
                ready.append(track)
        return ready

    def tracks_due_to_republish(self, now: float | None = None) -> list[Track]:
        """Decided tracks that should re-emit to keep presence alive.

        Only tracks still in view. A track past ``max_age_seconds`` is on its
        way out; re-emitting it would claim someone is present when the
        camera has stopped seeing them.
        """
        now = time.monotonic() if now is None else now
        return [
            track
            for track in self.tracks.values()
            if track.decided_employee is not None
            and track.age(now) <= self.max_age_seconds
            and now - track.last_published_at >= self.republish_seconds
        ]

    def finish_stragglers(self, now: float | None = None) -> list[Track]:
        """Tracks about to be retired that never reached the vote window.

        Someone who crosses a doorway in half a second may only ever yield
        four frames. Deciding on four is worse than deciding on twelve, but
        far better than never identifying them at all - so they are decided
        on what exists, and the lower sample count feeds the confidence.
        """
        now = time.monotonic() if now is None else now
        return [
            track
            for track in self.tracks.values()
            if track.decided_at == 0.0
            and track.sample_count > 0
            and track.age(now) > self.max_age_seconds * 0.6
        ]

    @staticmethod
    def confidence_for(
        base_confidence: str, sample_count: int, vote_window: int
    ) -> str:
        """Downgrade a confident match that rests on too few views.

        A single-frame "confirmed" is not the same claim as a twelve-frame
        one, and the UI should not present them identically.
        """
        if base_confidence == "confirmed" and sample_count < max(3, vote_window // 3):
            return "probable"
        return base_confidence
