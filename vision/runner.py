"""
Per-camera worker: RTSP in, identified movement events out.

The full pipeline for one camera::

    RTSP -> sample -> SCRFD detect -> quality gate -> align
         -> ArcFace embed -> track -> vote -> match -> publish

Decisions worth knowing about:

* **Identity is decided per track, never per frame.** See
  :mod:`vision.track.tracker` - this is the single biggest determinant of
  real-world accuracy.
* **Decode every frame, analyse only the sampled ones.** The decoder needs a
  continuous stream, but running inference at 25 fps buys nothing for
  room-to-room movement and costs you most of your GPU budget.
* **A decided track re-publishes periodically** so someone sitting still
  keeps their presence interval alive in the aggregator, and **re-votes**
  periodically so an identity can improve as better views arrive.
* **Track housekeeping runs on every loop, not only when faces are in view.**
  Retiring a track, and deciding a short one before it is retired, cannot
  wait for the next face: otherwise a person who has left keeps being
  re-published as present, and a quick walk through a doorway is never
  named at all.
* **Presence cameras never identify anyone.** A camera marked
  ``purpose=presence`` still detects faces, so its health counters and live
  preview are useful for positioning it, but it never embeds a face and so
  publishes no identity events. Putting a name to a face needs a camera that
  can actually see one; bridging gaps between identity cameras is the job of
  body re-ID, which is not built yet.
* **Embeddings leave the worker only for unknown faces.** A matched person's
  template stays here. An unmatched face's voted embedding is published once
  per track so the aggregator can group repeated sightings of the same
  stranger - a visitor seen all week is labelled once, and an unknown person
  in a restricted area stands out instead of being lost in noise.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import signal
import time

from vision.bus import EventBus, build_detection_event
from vision.config import settings

logger = logging.getLogger("vision.runner")

# Stop reading and reconnect if no frame arrives within this long.
READ_TIMEOUT_SECONDS = 10
HEALTH_INTERVAL_SECONDS = 15
# Faces analysed per frame. A doorway sees one or two; a cap stops a crowd
# scene from stalling the loop.
MAX_FACES_PER_FRAME = 6


class CameraWorker:
    def __init__(self, spec) -> None:
        self.spec = spec
        self.bus = EventBus()
        self.capture = None
        self.frame_index = 0
        self.frames_read = 0
        self.frames_dropped = 0
        self.reconnects = 0
        self.faces_seen = 0
        self.faces_gated = 0
        self.identified = 0
        self.last_frame_at = 0.0
        self.last_health_at = 0.0
        self._stop = False
        # Most recent frame kept for snapshot writing, so a snapshot is cut
        # from the frame the decision was actually made on.
        self._last_frame = None

        from vision.gallery import Gallery
        from vision.pipeline import FacePipeline
        from vision.preview import PreviewPublisher
        from vision.snapshot import SnapshotWriter
        from vision.track.tracker import FaceTracker

        self.gallery = Gallery()
        self.pipeline = FacePipeline()
        self.snapshots = SnapshotWriter()
        self.tracker = FaceTracker(
            spec.code, vote_window=settings.vote_window
        )
        # Annotated frames for the live view. Costs nothing unless somebody
        # is actually watching this camera.
        self.preview = PreviewPublisher(spec.code)

    # ------------------------------------------------------------- lifecycle

    def install_signal_handlers(self) -> None:
        def _handle(signum, _frame):
            logger.info("[%s] signal %s, stopping", self.spec.code, signum)
            self._stop = True

        signal.signal(signal.SIGTERM, _handle)
        signal.signal(signal.SIGINT, _handle)

    def open_stream(self) -> bool:
        import cv2

        if self.capture is not None:
            self.capture.release()

        # Force TCP and a socket timeout: over UDP a dropped camera silently
        # blocks forever rather than erroring, which is the single most common
        # cause of a worker that looks alive but delivers nothing.
        os.environ.setdefault(
            "OPENCV_FFMPEG_CAPTURE_OPTIONS",
            "rtsp_transport;tcp|stimeout;5000000",
        )

        self.capture = cv2.VideoCapture(self.spec.rtsp_url, cv2.CAP_FFMPEG)
        # Keep the buffer tiny: for tracking we want the newest frame, never
        # a backlog of stale ones.
        self.capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        if not self.capture.isOpened():
            logger.error("[%s] could not open stream", self.spec.code)
            return False

        logger.info(
            "[%s] stream open (%sx%s requested, sampling at %.1f fps)",
            self.spec.code,
            self.spec.width,
            self.spec.height,
            self.spec.sample_fps,
        )
        self.last_frame_at = time.monotonic()
        return True

    # ----------------------------------------------------------------- loop

    def run(self) -> None:
        self.install_signal_handlers()
        self.gallery.refresh()

        if not self.open_stream():
            self.report_health("offline", "could not open stream")
            time.sleep(5)
            return

        interval = 1.0 / max(self.spec.sample_fps, 0.5)
        next_sample = time.monotonic()

        while not self._stop:
            now = time.monotonic()

            # Reconnect on a stalled stream.
            if now - self.last_frame_at > READ_TIMEOUT_SECONDS:
                logger.warning(
                    "[%s] no frame for %.0fs, reconnecting",
                    self.spec.code,
                    now - self.last_frame_at,
                )
                self.reconnects += 1
                self.report_health("degraded", "stream stalled, reconnecting")
                if not self.open_stream():
                    self.report_health("offline", "reconnect failed")
                    time.sleep(min(30, 2 * self.reconnects))
                continue

            ok, frame = self.capture.read()
            if not ok or frame is None:
                self.frames_dropped += 1
                time.sleep(0.05)
                continue

            self.frames_read += 1
            self.last_frame_at = now

            # Decode every frame (the decoder requires it) but only analyse
            # at the sampled rate. 3-5 fps is ample for room-to-room movement
            # and is the difference between 8 cameras and 20 on one GPU.
            if now >= next_sample:
                next_sample = now + interval
                try:
                    self.process_frame(frame, now)
                except Exception:
                    logger.exception("[%s] frame processing failed", self.spec.code)

            # Housekeeping runs on every loop, not only on sampled frames with
            # faces in them: see the module docstring.
            try:
                self.housekeep(now)
            except Exception:
                logger.exception("[%s] track housekeeping failed", self.spec.code)

            if now - self.last_health_at > HEALTH_INTERVAL_SECONDS:
                self.report_health("online")
                self.gallery.refresh_if_stale()

        if self.capture is not None:
            self.capture.release()
        self.bus.close()
        logger.info("[%s] worker stopped", self.spec.code)

    # -------------------------------------------------------------- stages

    def process_frame(self, frame, now: float) -> None:
        """Detect, gate, align, embed and track one sampled frame.

        The order is fixed and each step earns its place:

        1. ``detect``      SCRFD - boxes plus five landmarks
        2. quality gate    drop crops too small, blurred or turned away, so
                           bad embeddings never reach the matcher
        3. ``align``       warp to ArcFace's canonical 112x112
        4. ``embed``       ArcFace, 512-d, batched per frame
        5. ``track``       group frames of the same person within this camera
        6. ``vote``        decide identity once per track, from many views

        Matching is *not* done here - that happens in :meth:`decide_tracks`,
        once a track has accumulated enough evidence.
        """
        self.frame_index += 1
        self._last_frame = frame

        params = self.gallery.params
        min_face_px = params.min_face_px
        # Tuned in the panel; applies from the next gallery refresh.
        self.tracker.vote_window = params.vote_window

        faces = self.pipeline.analyse_frame(
            frame,
            min_face_px=min_face_px,
            min_quality_score=params.min_quality_score,
            max_faces=MAX_FACES_PER_FRAME,
            # A presence-only camera still detects faces, but never embeds
            # them, so it cannot put a name to anyone.
            embed=self.spec.is_identity,
            roi=self.spec.roi_polygon,
        )
        if not faces:
            return

        self.faces_seen += len(faces)
        rejected = [f for f in faces if not f.quality.passed]
        self.faces_gated += len(rejected)

        if rejected and self.frame_index % 50 == 0:
            # Sampled, not per frame: a camera where everything fails for one
            # reason is telling you about its mounting, and that is worth
            # seeing in the logs without drowning them.
            reasons = {r for f in rejected for r in f.quality.reasons}
            logger.info(
                "[%s] %s/%s faces gated out: %s",
                self.spec.code,
                len(rejected),
                len(faces),
                "; ".join(sorted(reasons)),
            )

        usable = [f for f in faces if f.quality.passed]
        if not usable:
            return

        touched = self.tracker.update(usable, now)
        self.capture_evidence(frame, touched)
        self.decide_tracks(now)
        self.publish_preview(frame, faces, min_face_px)

    def capture_evidence(self, frame, tracks) -> None:
        """Keep a crop of each track's best view, cut from that same frame.

        Small (a face plus margin), one per track, and replaced only when a
        better view arrives - so the snapshot written at decision time shows
        the person, even when the decision comes after they have left.
        """
        from vision.snapshot import crop_with_context

        for track in tracks:
            if track.best_improved and track.best_bbox is not None:
                track.best_snapshot = crop_with_context(frame, track.best_bbox).copy()
                track.best_improved = False

    def housekeep(self, now: float) -> None:
        """Per-loop track maintenance, independent of whether faces are in view.

        Order matters: decide short tracks *before* retiring them, so a
        quick doorway crossing is named on what it yielded rather than
        discarded; then retire whatever has left; then re-publish only the
        tracks that are still in view.
        """
        self.decide_tracks(now)
        self.tracker.retire_stale(now)
        self.publish_due(now)

    def publish_preview(self, frame, faces, min_face_px: int) -> None:
        """Push an annotated frame for the live view, if anyone is watching.

        Deliberately after decide_tracks, so the labels show the identity the
        pipeline actually settled on rather than a per-frame guess.
        """
        if not self.preview.wanted() or not self.preview.due():
            return

        # Attach the decided name to each face, so the overlay reflects the
        # track-level decision rather than re-matching per frame.
        decided = {
            t.bbox: (t.decided_employee, t.decided_confidence)
            for t in self.tracker.tracks.values()
            if t.decided_employee
        }
        for face in faces:
            label = ""
            for bbox, (code, confidence) in decided.items():
                if _boxes_overlap(face.bbox, bbox):
                    label = f"{code} ({confidence})"
                    break
            face.preview_label = label

        self.preview.publish(frame, faces, min_face_px=min_face_px)

    def decide_tracks(self, now: float) -> None:
        """Run the vote for tracks with enough evidence, and publish."""
        ready = self.tracker.tracks_ready_to_decide(now)
        ready += [t for t in self.tracker.finish_stragglers(now) if t not in ready]

        for track in ready:
            voted = track.voted_embedding()
            if voted is None:
                continue

            employee_code, score, confidence = self.gallery.match(voted)
            confidence = self.tracker.confidence_for(
                confidence, track.sample_count, self.tracker.vote_window
            )

            previous = track.decided_employee
            track.decided_employee = employee_code
            track.decided_score = score
            track.decided_confidence = confidence
            track.decided_at = now

            if previous is not None and previous != employee_code:
                # A re-vote changed its mind. Worth a log line: a camera that
                # does this often has a matching problem, and the aggregator
                # will be seeing contradictory presence for two people.
                logger.info(
                    "[%s] track %s re-identified %s -> %s (score %.3f, %s samples)",
                    self.spec.code,
                    track.track_id,
                    previous,
                    employee_code,
                    score,
                    track.sample_count,
                )

            if employee_code is not None:
                self.identified += 1

            self.publish_track(track, now)

    def publish_due(self, now: float) -> None:
        """Re-emit decided tracks so presence intervals stay open."""
        for track in self.tracker.tracks_due_to_republish(now):
            self.publish_track(track, now)

    def publish_track(self, track, now: float) -> None:
        """Emit one detection event for a decided track.

        Unknown faces are published too, with ``employee_code=None``. They
        create no presence for anybody, but the aggregator records them so an
        unidentified person in a restricted zone is visible rather than lost.
        """
        first_publish = track.last_published_at == 0.0
        snapshot_key = ""
        # Snapshot only on the first publish for a track. One image per track
        # is enough to verify a claim and keeps storage proportionate. It is
        # the track's best view, cut when that view was seen.
        if first_publish:
            if track.best_snapshot is not None:
                snapshot_key = self.snapshots.write_image(
                    track.best_snapshot,
                    camera_code=self.spec.code,
                    track_id=track.track_id,
                )
            elif self._last_frame is not None:
                snapshot_key = self.snapshots.write(
                    self._last_frame,
                    track.best_bbox or track.bbox,
                    camera_code=self.spec.code,
                    track_id=track.track_id,
                )

        # A matched person's template never leaves this process. An unknown
        # face's voted embedding goes out once per track, for clustering.
        embedding = None
        if first_publish and track.decided_employee is None:
            voted = track.voted_embedding()
            if voted is not None:
                embedding = [round(float(x), 6) for x in voted]

        event = build_detection_event(
            camera_code=self.spec.code,
            captured_at=dt.datetime.now(dt.UTC).isoformat(),
            track_id=track.track_id,
            frame_index=self.frame_index,
            bbox=list(track.bbox),
            face_width_px=max(1, track.bbox[2] - track.bbox[0]),
            quality_score=track.best_quality,
            employee_code=track.decided_employee,
            # Sent for unknown faces too: the best gallery similarity, with no
            # name attached. Threshold tuning needs the scores that fell
            # *below* the line as much as the ones above it.
            match_score=track.decided_score,
            confidence=track.decided_confidence,
            snapshot_key=snapshot_key,
            embedding=embedding,
        )
        self.bus.publish(event)
        track.last_published_at = now

    # -------------------------------------------------------------- health

    def report_health(self, status: str, message: str = "") -> None:
        """Publish a health sample.

        Deliberately frequent: a silent camera means a gap in somebody's
        movement history, and the system has to be able to show that as a gap
        rather than as absence.
        """
        now = time.monotonic()
        window = max(1e-6, now - self.last_health_at)
        fps_actual = self.frames_read / window if self.last_health_at else 0.0

        self.bus.publish_health(
            {
                "camera_code": self.spec.code,
                "at": dt.datetime.now(dt.UTC).isoformat(),
                "status": status,
                "fps_actual": round(fps_actual, 2),
                "frames_dropped": self.frames_dropped,
                "reconnects": self.reconnects,
                "error_message": message,
                "worker_id": f"{settings.shard_index}:{os.getpid()}",
                "faces_seen": self.faces_seen,
                "faces_gated": self.faces_gated,
                "faces_identified": self.identified,
            }
        )
        self.last_health_at = now
        # Counters are per-interval, so the UI can show a current rate rather
        # than a number that only ever grows.
        self.frames_read = 0
        self.faces_seen = 0
        self.faces_gated = 0
        self.identified = 0


def _boxes_overlap(a, b, threshold: float = 0.3) -> bool:
    """Loose IOU test, to tie a drawn box to a decided track."""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    if inter == 0:
        return False
    area_a = max(1, (ax2 - ax1) * (ay2 - ay1))
    area_b = max(1, (bx2 - bx1) * (by2 - by1))
    return inter / float(area_a + area_b - inter) >= threshold


def run_camera(spec) -> None:
    """Entry point for a supervised child process."""
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(levelname)s %(asctime)s %(name)s %(message)s",
    )
    worker = CameraWorker(spec)
    try:
        worker.run()
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.exception("[%s] worker crashed", spec.code)
        raise
