# Handover

Everything the team taking over needs: what the system is, how it fits
together, where each thing lives, who owns what, and what is still open.

## What it is

A Django system that records which room employees are in, from doorway
cameras and face recognition, under strict access control: following a named
person needs a written reason and someone else's approval, and every look at
identity data is audited. Design and rationale: `CCTV_TRACKING_PLAN.md`.

## Architecture

```
Cameras (RTSP) --> vision workers (one process per camera, GPU)
                     detect (SCRFD) -> quality gate -> align -> embed (ArcFace)
                     -> track -> vote -> match against the face gallery
                     |
                     v  Redis Stream (events:detections)
                   aggregator  -->  Postgres + pgvector
                     Detection -> PresenceInterval -> MovementEvent
                     -> alert engine, live board (WebSocket)
                     |
                   web (Django + Channels): panel, API, WebSockets
                   worker + beat (Celery): health, expiry, summaries, retention
```

| Component | Code | Container |
|---|---|---|
| Web panel, API, WebSockets | `apps/`, `templates/`, `config/` | `web` |
| Realtime aggregation | `apps/tracking/aggregator.py` | `aggregator` |
| Scheduled jobs | `config/celery.py`, `apps/*/tasks.py` | `worker`, `beat` |
| Face pipeline (no Django) | `vision/` | `vision` |
| Database | Postgres 16 + pgvector | `db` |
| Event bus, cache, channels | Redis 7 | `redis` |

## Where the important rules live

| Rule | File |
|---|---|
| Who may do what (the role matrix) | `apps/common/roles.py` |
| Permission decisions incl. MFA, data windows | `apps/common/permissions.py` |
| Which rows each user may see | `apps/common/scoping.py` |
| Audit writing | `apps/common/audit.py` |
| Never-tracked rooms (enforced in 3 places) | `vision/gallery.py` SQL, `aggregator.py`, `scoping.py` |
| Consent gate for matching | `vision/gallery.py` `GALLERY_SQL` |
| Alert rules and engine | `apps/alerts/` |
| Retention | `apps/tracking/tasks.py` `enforce_retention` |
| Early deletion, two approvers | `apps/tracking/purge.py` |
| Pilot readiness / go-live checks | `apps/tracking/pilot.py`, `apps/tracking/golive.py` |

`tests/test_permission_matrix.py` is the executable form of the role matrix:
if it fails, a permission changed.

## Operating documents

| Document | For |
|---|---|
| `README.md` | Developers: quick start, architecture, development |
| `docs/DEPLOYMENT.md` | Installing and updating production |
| `docs/OPERATIONS.md` | Daily running, incidents, privacy requests, retention |
| `docs/PILOT_RUNBOOK.md` | Running the pilot |
| `docs/PILOT_LOG.md` | Measured results, newest first |
| `docs/ROLLOUT_PLAN.md` | Floor-by-floor rollout |
| **Help & training** (in the app) | Every user, per role |

## Commands worth knowing

```bash
make up / make restart / make logs s=<service>
make migrate && make restart        # after every update
make test                           # full suite (270+ tests)
make audit secs=120                 # camera viability
make pilot-check                    # pilot readiness (also on /accuracy/)
make golive-check                   # production readiness
make benchmark data=samples/benchmark
make loadtest cams=20 fps=4         # never on production
make backup / make restore-test     # backups
```

## Ownership (fill in)

| Area | Owner | Backup |
|---|---|---|
| System owner (accountable) | | |
| Super Admins (people, approvals) | | |
| Developer (pipeline, thresholds, updates) | | |
| Infrastructure (hosts, network, backups) | | |
| Data-protection contact (grievances, breaches) | | |
| Camera installation and maintenance | | |

## Secrets and where they must be kept

| Secret | Loss means |
|---|---|
| `FIELD_ENCRYPTION_KEY` | Every stored camera password unreadable |
| `DJANGO_SECRET_KEY` | All sessions invalidated; password-reset tokens break |
| `BACKUP_PASSPHRASE` | Every backup unusable |
| Postgres passwords | Rotate with the database |

All in the organisation's secrets manager, never in git (`.env` is ignored).

## State at handover (2026-10-13)

- Phases 1-5 complete: foundation and access control, face pipeline and
  enrolment, multi-camera pipeline, interfaces, governance and hardening.
- Phase 6: tooling complete and rehearsed on the development stack (see the
  pilot log); **the on-site pilot has not been run**.
- Phase 7: rollout tooling and documents complete; **waves not started**.

## Known limitations

- Accuracy has not been measured on real cameras. The only accuracy numbers
  so far come from synthetic images and mean nothing for a real doorway.
- CPU inference manages ~2.5 frames/s for one camera; production needs a GPU.
- Body re-identification (bridging corridors without a face) is not built;
  gaps between identity cameras show as gaps.
- Alerts are delivered in the app only (inbox and live push), not by email
  or SMS.
- Snapshots are not backed up, by design.

## Open items

1. Run the on-site pilot and record the go / no-go.
2. Push the repository to its remote (`git -C <path> push -u origin main`).
3. Fill in the ownership table and the wave plan.
4. Set up the daily backup schedule and off-host copy.
5. Enrol privileged users' authenticators, then `MFA_ENFORCED=1`.
6. `ASSETS_LOCAL=1` on the production server.
