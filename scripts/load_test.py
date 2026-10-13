#!/usr/bin/env python
"""
Can the aggregator keep up with N cameras? Measure it before rollout.

Publishes synthetic detection events onto the real event stream at the rate
N cameras would produce them, then watches the aggregator's consumer group:
how far behind it falls, and how long it takes to catch up.

    docker compose --profile vision run --rm vision \\
        python scripts/load_test.py --cameras 20 --fps 4 --seconds 60 --not-production

Events are unknown faces with no embedding, tagged ``loadtest:``. They
exercise the hot path (stream read, detection insert) but create no presence,
no movement and no unknown-face cluster, so nobody's timeline is touched.
Remove the detection rows afterwards with:

    docker compose exec web python manage.py purge_load_test

The camera codes must exist in the database, or the aggregator skips the
events; by default the enabled cameras are read from it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TRACK_PREFIX = "loadtest:"


def make_event(camera_code: str, sequence: int) -> dict:
    """One synthetic unknown-face event, in the real schema."""
    from vision.bus import build_detection_event

    return build_detection_event(
        camera_code=camera_code,
        captured_at=dt.datetime.now(dt.UTC).isoformat(),
        track_id=f"{TRACK_PREFIX}{camera_code}:{sequence}",
        frame_index=sequence,
        bbox=[100, 100, 200, 220],
        face_width_px=100,
        quality_score=0.8,
        employee_code=None,
        match_score=0.1,
        confidence="unknown",
        snapshot_key="",
        embedding=None,
    )


def schedule(camera_count: int, fps: float, seconds: float) -> int:
    """Events the run will publish."""
    return int(camera_count * fps * seconds)


def camera_codes_from_db(limit: int) -> list[str]:
    import psycopg

    from vision.config import settings

    with psycopg.connect(settings.database_url, connect_timeout=10) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT c.code FROM cameras_camera c JOIN org_zone z ON z.id = c.zone_id "
            "WHERE c.deleted_at IS NULL AND z.tracking_prohibited = FALSE ORDER BY c.id"
        )
        codes = [row[0] for row in cur.fetchall()]
    if not codes:
        raise SystemExit("No cameras in the database; add some or pass --codes.")
    # Reuse codes round-robin to simulate more cameras than exist.
    return [codes[i % len(codes)] for i in range(limit)]


def group_state(client, stream: str, group: str) -> tuple[int, int]:
    """(lag, pending) for the aggregator's consumer group."""
    for info in client.xinfo_groups(stream):
        if info.get("name") == group:
            lag = info.get("lag")
            return (int(lag) if lag is not None else -1), int(info.get("pending", 0))
    return -1, -1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cameras", type=int, default=10, help="cameras to simulate")
    parser.add_argument("--fps", type=float, default=4.0, help="events per camera per second")
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--codes", help="comma-separated camera codes instead of reading the DB")
    parser.add_argument("--group", default="aggregators")
    parser.add_argument("--drain-timeout", type=float, default=120.0)
    parser.add_argument("--not-production", action="store_true",
                        help="required: confirms this is not the production stream")
    args = parser.parse_args()

    if not args.not_production:
        print("Refusing to run without --not-production. This writes to the live "
              "event stream and the detections table.", file=sys.stderr)
        return 2

    from vision.bus import EventBus

    codes = (
        [c.strip() for c in args.codes.split(",") if c.strip()]
        if args.codes else camera_codes_from_db(args.cameras)
    )
    codes = [codes[i % len(codes)] for i in range(args.cameras)]
    bus = EventBus()
    client = bus.client
    total = schedule(len(codes), args.fps, args.seconds)
    interval = 1.0 / (len(codes) * args.fps)

    print(f"publishing {total} events: {len(codes)} cameras x {args.fps} fps x {args.seconds:.0f}s")
    started = time.monotonic()
    worst_lag = 0
    published = 0
    next_at = started
    for sequence in range(total):
        bus.publish(make_event(codes[sequence % len(codes)], sequence))
        published += 1
        next_at += interval
        delay = next_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        if sequence % 200 == 0:
            lag, _pending = group_state(client, bus.stream, args.group)
            worst_lag = max(worst_lag, lag)
    publish_seconds = time.monotonic() - started
    achieved = published / publish_seconds if publish_seconds else 0.0

    print(f"published {published} in {publish_seconds:.1f}s ({achieved:.0f}/s, "
          f"target {len(codes) * args.fps:.0f}/s)")

    drain_started = time.monotonic()
    while True:
        lag, pending = group_state(client, bus.stream, args.group)
        worst_lag = max(worst_lag, lag)
        if lag == 0 and pending == 0:
            break
        if time.monotonic() - drain_started > args.drain_timeout:
            print(f"NOT DRAINED after {args.drain_timeout:.0f}s: lag {lag}, pending {pending}")
            print("The aggregator cannot keep up at this rate. Add aggregator replicas "
                  "(unique AGGREGATOR_CONSUMER each) or reduce sample fps.")
            return 1
        time.sleep(0.5)

    drain = time.monotonic() - drain_started
    print(f"drained {drain:.1f}s after publishing stopped; worst lag {worst_lag} events")
    verdict = "OK" if drain < 10 else "MARGINAL" if drain < 60 else "TOO SLOW"
    print(f"verdict: {verdict} for {len(codes)} cameras at {args.fps} fps")
    print("clean up: docker compose exec web python manage.py purge_load_test")
    return 0 if verdict != "TOO SLOW" else 1


if __name__ == "__main__":
    raise SystemExit(main())
