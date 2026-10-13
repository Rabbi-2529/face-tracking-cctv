"""
Synthetic movement generator - ``MOCK_VISION=1``.

Lets the whole realtime path be exercised with no cameras, no models and no
GPU: Redis Stream, aggregator, presence intervals, movement events, live
board. That matters in Phase 1, because it means the data model and the
access controls can be tested against moving data months before the camera
estate is ready.

Two things it does on purpose:

* It walks people along **real zone adjacency edges**, so the movements it
  produces are plausible and exercise the happy path.
* It occasionally emits an **implausible jump** and a **low-confidence
  match**, so the review queue and the confidence badges get exercised too.
  A demo where everything is perfect teaches you nothing about the parts that
  matter when recognition is imperfect.
"""

from __future__ import annotations

import datetime as dt
import logging
import random
import time

import psycopg

from vision.bus import EventBus, build_detection_event
from vision.config import settings

logger = logging.getLogger("vision.mock")

# One in this many transitions is deliberately impossible, to exercise the
# plausibility check and the review queue.
IMPLAUSIBLE_EVERY = 25
# One in this many matches lands in the "probable" band.
PROBABLE_EVERY = 6

PEOPLE_SQL = """
SELECT e.employee_code
  FROM employees_employee e
 WHERE e.deleted_at IS NULL
   AND e.is_trackable = TRUE
   AND e.status = 'active'
   AND EXISTS (SELECT 1 FROM employees_consentrecord cr
                WHERE cr.employee_id = e.id AND cr.withdrawn_at IS NULL)
 ORDER BY e.employee_code
"""

# A camera per zone, so a walk between zones lands on a real camera code.
ZONE_CAMERA_SQL = """
SELECT z.id, z.code, MIN(c.code) AS camera_code
  FROM org_zone z
  JOIN cameras_camera c ON c.zone_id = z.id
 WHERE z.tracking_prohibited = FALSE
   AND z.deleted_at IS NULL
   AND c.is_enabled = TRUE
   AND c.deleted_at IS NULL
 GROUP BY z.id, z.code
"""

ADJACENCY_SQL = """
SELECT from_zone_id, to_zone_id, bidirectional,
       typical_transit_seconds, min_transit_seconds
  FROM org_zoneadjacency
"""


class MockWalker:
    """Moves a population of people around the real zone graph."""

    def __init__(self) -> None:
        self.bus = EventBus()
        self.people: list[str] = []
        self.zone_camera: dict[int, str] = {}
        self.zone_code: dict[int, str] = {}
        self.neighbours: dict[int, list[int]] = {}
        # (from_zone, to_zone) -> (min_seconds, typical_seconds)
        self.transit: dict[tuple[int, int], tuple[int, int]] = {}
        self.position: dict[str, int] = {}
        self.next_move: dict[str, float] = {}
        # While a person is walking between rooms, no camera sees them. That
        # gap is what makes the transit time realistic - without it every
        # transition looks instantaneous and the aggregator correctly flags
        # the lot as physically impossible.
        self.in_transit_until: dict[str, float] = {}
        self.frame_index = 0
        self.transitions = 0

    def load_topology(self) -> None:
        with psycopg.connect(settings.database_url, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute(PEOPLE_SQL)
                self.people = [r[0] for r in cur.fetchall()]

                cur.execute(ZONE_CAMERA_SQL)
                for zone_id, zone_code, camera_code in cur.fetchall():
                    self.zone_camera[zone_id] = camera_code
                    self.zone_code[zone_id] = zone_code

                cur.execute(ADJACENCY_SQL)
                for a, b, bidirectional, typical, minimum in cur.fetchall():
                    self.neighbours.setdefault(a, []).append(b)
                    self.transit[(a, b)] = (minimum, typical)
                    if bidirectional:
                        self.neighbours.setdefault(b, []).append(a)
                        self.transit[(b, a)] = (minimum, typical)

        if not self.people:
            raise RuntimeError(
                "No consenting, trackable employees found. Run "
                "`manage.py seed_demo` first."
            )
        if not self.zone_camera:
            raise RuntimeError(
                "No zones with enabled cameras found. Run `manage.py seed_demo` first."
            )

        start_zones = list(self.zone_camera.keys())
        now = time.monotonic()
        for code in self.people:
            self.position[code] = random.choice(start_zones)
            self.next_move[code] = now + random.uniform(2, 20)

        logger.info(
            "mock topology: %s people, %s zones, %s edges",
            len(self.people),
            len(self.zone_camera),
            sum(len(v) for v in self.neighbours.values()),
        )

    def tick(self) -> None:
        """Emit detections for everyone, and move whoever is due to move."""
        now_mono = time.monotonic()
        captured_at = dt.datetime.now(dt.UTC)

        for code in self.people:
            # Walking between rooms: unseen by any camera until they arrive.
            if now_mono < self.in_transit_until.get(code, 0.0):
                continue

            zone_id = self.position[code]
            camera_code = self.zone_camera.get(zone_id)
            if camera_code is None:
                continue

            self._emit(code, camera_code, captured_at)

            if now_mono >= self.next_move[code]:
                walk_seconds = self._move(code, zone_id)
                self.in_transit_until[code] = now_mono + walk_seconds
                self.next_move[code] = now_mono + walk_seconds + random.uniform(20, 120)

        self.frame_index += 1

    def _move(self, code: str, zone_id: int) -> float:
        """Move one person. Returns how long they are out of sight walking."""
        options = self.neighbours.get(zone_id, [])
        self.transitions += 1

        # Every so often, jump somewhere unreachable so the plausibility
        # check and the review queue get exercised. A demo where everything
        # is perfect teaches you nothing about the parts that matter when
        # recognition is imperfect.
        if self.transitions % IMPLAUSIBLE_EVERY == 0:
            far = [z for z in self.zone_camera if z != zone_id and z not in options]
            if far:
                self.position[code] = random.choice(far)
                logger.info("mock: %s jumps to an unreachable zone (on purpose)", code)
                # No walking time, which is exactly what makes it impossible.
                return 0.0

        if not options:
            return 0.0

        destination = random.choice(options)
        minimum, typical = self.transit.get((zone_id, destination), (2, 10))
        # Comfortably above the minimum, so a normal walk is never mistaken
        # for an impossible one.
        walk_seconds = random.uniform(max(minimum + 1.0, typical * 0.8), typical * 1.6)
        self.position[code] = destination
        return walk_seconds

    def _emit(self, code: str, camera_code: str, captured_at: dt.datetime) -> None:
        # Mix in some probable-band matches so the UI confidence states and
        # the "read this with care" banner are exercised.
        if random.randint(1, PROBABLE_EVERY) == 1:
            confidence = "probable"
            score = random.uniform(0.31, 0.44)
        else:
            confidence = "confirmed"
            score = random.uniform(0.46, 0.85)

        event = build_detection_event(
            camera_code=camera_code,
            captured_at=captured_at.isoformat(),
            track_id=f"{camera_code}:mock:{code}",
            frame_index=self.frame_index,
            bbox=[100, 100, 220, 260],
            face_width_px=random.randint(70, 130),
            quality_score=random.uniform(0.5, 0.95),
            employee_code=code,
            match_score=score,
            confidence=confidence,
            snapshot_key="",
            embedding=None,
        )
        self.bus.publish(event)

    def run(self) -> None:
        self.load_topology()
        interval = 1.0 / max(settings.sample_fps, 0.5)
        logger.warning(
            "MOCK VISION ACTIVE - synthetic movement, no cameras are being read"
        )
        while True:
            started = time.monotonic()
            try:
                self.tick()
            except Exception:
                logger.exception("mock tick failed")
            elapsed = time.monotonic() - started
            time.sleep(max(0.0, interval - elapsed))


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(asctime)s %(name)s %(message)s",
    )
    MockWalker().run()


if __name__ == "__main__":
    main()
