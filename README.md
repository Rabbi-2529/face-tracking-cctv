# CCTV Employee Movement Tracking

Django system that reconstructs employee movement across an office from CCTV:
enrol a person's face, then see which room they were in, when they moved, and
where they went next. Access is split across Developer, Admin, HR, Security
Operator, Auditor and Employee roles.

Full design, roadmap and compliance requirements: [CCTV_TRACKING_PLAN.md](CCTV_TRACKING_PLAN.md)

**Status: Phase 5 complete.** Foundation and access control (Phase 1), face
pipeline and enrolment (Phase 2), the multi-camera pipeline (Phase 3), the
interfaces (Phase 4), and governance and hardening (Phase 5) - MFA verified
at every sign-in and on every WebSocket, tracking requests limited to their
approved data window, live follow ended the moment an approval does, the
alert engine and inbox, complete retention, and employee self-service - all
run and are tested. `MOCK_VISION=1` exercises the realtime path with
synthetic movement when no cameras are available. Next is Phase 6, the pilot;
see the end of this file.

---

## Quick start

Needs Docker only. Nothing else is installed on the host.

```bash
make init     # writes .env with generated keys, builds the image
make up       # db, redis, web, worker, beat, aggregator
make demo     # seeds an office, cameras, employees and one user per role
make mock     # starts synthetic movement so the live board has data
```

Then open <http://localhost:8000> and sign in as any demo account
(the password is printed by `make demo`):

| Account | Role | What it is for |
|---|---|---|
| `admin@demo.local` | Super Admin | Everything operational; approves tracking requests |
| `dev@demo.local` | Developer | Pipeline internals, raw detections, thresholds |
| `secadmin@demo.local` | Security Admin | Live and historical, approves requests |
| `hr@demo.local` | HR | Enrolment, consent, **Engineering + HR only** |
| `guard@demo.local` | Security Operator | Live board for 3 assigned zones |
| `auditor@demo.local` | Auditor | Audit log; no footage |
| `aarav@demo.local` | Employee | Own record only |

### See the access design working

Sign in as `hr@demo.local` and open the timeline for an **Operations**
employee (Vikram Singh or Karan Mehta). HR is scoped to Engineering and HR, so
instead of the timeline you get redirected to a tracking-request form. Submit
it with a reason, then approve it as `admin@demo.local` — and the timeline
opens. Revoke it, and it closes again. Every one of those steps is in the
audit log at `/audit/`.

That loop is the point of the whole system: following a named individual needs
a written reason, someone else's approval, and a time limit, and it expires by
itself.

Useful URLs: `/live/` live occupancy · `/search/zone/` zone-first search ·
`/requests/` approvals · `/audit/` audit log · `/admin/` configuration ·
`/api/v1/docs/` API.

---

## Before you connect real cameras

**Run the camera audit first.** It is the cheapest way to find out whether this
project is viable with the cameras you have.

```bash
make audit secs=120
```

It samples each stream, finds faces and measures how wide they are in pixels:

| Median face width | Verdict |
|---|---|
| ≥ 100 px | good — reliable identification |
| 60–99 px | marginal — works, with errors |
| < 60 px | presence only — **cannot name anyone** |

If most cameras come back under 60 px, the fix is camera placement, not model
tuning — and you want that finding in week one, not month four. Ceiling domes
angled steeply down are usually unusable for faces. Identity needs a camera at
a doorway, 1.8–2.2 m high, tilted under 15–20°, facing people as they approach.

### Dahua / Hikvision setup

```
Dahua      rtsp://USER:PASS@HOST:554/cam/realmonitor?channel=1&subtype=0
Hikvision  rtsp://USER:PASS@HOST:554/Streaming/Channels/101
```

`subtype=0` is the full-resolution main stream — use it for identity cameras.
On the camera: create a dedicated **Operator**-level service account (never the
admin login), enable RTSP and ONVIF, **disable UPnP and P2P** (P2P phones home
to the vendor's cloud, which you do not want on a biometric system), use H.264
rather than H.265, and set the I-frame interval equal to the frame rate.

**Point every camera at the same NTP server as the application host.** Drifting
camera clocks turn room-to-room transitions into nonsense, and it is the most
commonly missed item on the list.

Cameras belong on their own VLAN with no route to the internet.

---

## Architecture

Django is the control plane, not the video processor. Inference runs in
separate per-camera processes, so a hung RTSP stream or a CUDA OOM costs you
one camera rather than the whole portal.

```
Cameras (RTSP)
  -> ingest worker per camera          vision/runner.py     (3-5 fps sampled)
  -> detect / embed / track / vote     vision/pipeline.py, vision/track/
  -> Redis Stream                      vision/bus.py
  -> aggregator                        apps/tracking/aggregator.py
       Detection -> PresenceInterval -> MovementEvent
  -> Postgres + pgvector, WebSocket to the live board
```

Three data layers, written in that order:

1. **Detection** — raw, one row per matched face per sampled frame. Short
   retention, Developer-only, contains the biometric vector.
2. **PresenceInterval** — "this person was in this zone from X to Y". Every UI
   and report reads this; nothing queries Detection.
3. **MovementEvent** — "A to B", validated against the zone adjacency graph
   before being written as fact.

Design decisions worth knowing:

- **Identity is never decided from one frame.** Embeddings are aggregated
  across a track and voted. Per-frame matching is what produces the "it keeps
  showing the wrong name" complaint.
- **Three-way confidence**, not a boolean: confirmed / probable / unknown. A
  probable match is shown with visible uncertainty and never raises an alert
  on its own.
- **Implausible transitions are kept and flagged**, not dropped. Dropping them
  would hide a matching problem; accepting them silently would publish a false
  movement as fact. They go to a review queue at `/review/movements/`.
- **`exited_at` is the last time the person was actually seen**, never the time
  the sweeper ran, so durations stay truthful.
- **A track ends when its person leaves view**, not when the next face
  appears. Worker housekeeping runs every loop, so presence stops being
  re-published within seconds of someone walking out, and a short doorway
  crossing is still decided (with lowered confidence) before it is dropped.
- **Unknown faces are clustered, not discarded.** Only an unmatched face's
  embedding leaves the worker, once per track; the aggregator groups repeat
  sightings so a visitor seen all week is one cluster. Clusters age out with
  raw detections.
- **Prohibited zones are enforced in code at three layers** — ingest refuses
  the camera, the aggregator refuses the event, and every scoping query
  excludes the zone, for administrators too.

---

## Access control

Two things are required for every piece of gated data, and one without the
other is a leak:

1. `PermissionService.can(user, action)` — may they do this at all?
2. A filter from `apps/common/scoping.py` — which rows may they see?

A role check without a queryset filter leaks rows through list pages, search
and CSV export. A queryset filter without a role check lets someone reach a
page they should not see at all.

- **The matrix lives in one place:** `apps/common/roles.py`. Nothing else
  hard-codes a role name.
- **`tests/test_permission_matrix.py` is the executable form of that matrix.**
  If it fails, either the matrix changed on purpose — in which case update
  section 7 of the plan too — or a permission regression was just caught.
- **Separation of duties is deliberate.** A Developer can change how matching
  works but cannot approve a tracking request, manage users, or purge data.
  Break-glass elevation can grant data access temporarily; it can never grant
  authority over people.
- **Snapshots stream through a permission-checked view**, not a signed URL. A
  signed URL leaves the system and can be pasted into a chat; the endpoint
  re-checks permission and writes an audit row on every fetch.
- **If an identity read is not audited, it is not a feature.** Denials are
  audited too — a run of denials is exactly the signal worth having.

---

## Development

```bash
make help              # every target
make logs s=aggregator # tail one service
make test              # full suite
make lint              # ruff + django check --deploy
make shell             # Django shell
make dbshell           # psql
make health            # readiness probe
```

### GPU

```bash
make vision-gpu        # CUDA image, verifies CUDAExecutionProvider is visible
```

Needs the NVIDIA Container Toolkit on the host. Sizing: 1–8 cameras on an
RTX 3060 12GB, 8–20 on an A4000 16GB with TensorRT and NVDEC. CPU video decode
will bottleneck you well before inference does.

### Snapshot storage

Snapshots go to a local volume by default. For production, set `S3_*` in `.env`
to your own bucket — AWS S3, Wasabi, Backblaze B2, or a self-hosted MinIO.
MinIO's container images now require a registry login, so the bundled `minio`
service sits behind the `s3` compose profile and is not started by default.
Either way, snapshots are only ever served through the permission-checked
streaming endpoint.

### Locking down the vision workers

Vision workers need read access to eight tables and nothing else. They publish
to Redis and never write to Postgres, so a compromised camera-side worker
cannot alter anyone's movement history.

```bash
docker compose exec web python manage.py grant_vision_ro --password '<strong>'
# then set VISION_DB_USER=vision_ro and VISION_DB_PASSWORD in .env
```

---

## Compliance — these are features, not paperwork

Face embeddings are biometric data: sensitive personal data under the DPDP Act
2023 (India), GDPR Article 9 (EU) and US state biometric laws. This is normal,
legal workplace security technology, but it is *conditionally* legal, and the
conditions are implemented here:

| Requirement | Where it lives |
|---|---|
| Consent, and withdrawal | `employees.ConsentRecord`. The gallery query checks it, so withdrawing consent stops processing within one refresh (~30s) with nobody having to remember to act |
| No covert tracking | Employees see their own record and their own history at `/accounts/me/` |
| Purpose limitation | The role matrix. There are no "time at desk" reports |
| Data minimisation | Embeddings and snapshots, not 24/7 video; embeddings dropped early by the retention job |
| Retention limits | A scheduled Celery task, not a policy document |
| Access logging | `accounts.AuditLog`, append-only — the model refuses updates |
| Rectification | `employees.IdentityCorrection`, reachable from any timeline |
| Private areas excluded | `Zone.tracking_prohibited`, enforced in code at three layers |

Two rules for anyone operating this:

1. **System output is a lead, never sole evidence.** Automated recognition is
   wrong sometimes, and a timeline built largely from "probable" matches says
   so on its face. Verify before acting on it.
2. **Get written sign-off from legal and HR before go-live.** Retrofitting
   consent after deployment is far harder than building it in.

---

## Layout

```
config/           settings, ASGI, Celery, WebSocket routing
apps/
  common/         roles.py (the matrix), permissions.py, scoping.py, audit.py
  accounts/       user, scopes, break-glass elevation, audit log
  org/            building, floor, zone, zone adjacency graph
  employees/      employee, face enrolment, consent, corrections
  cameras/        camera, health, pipeline config, audit results
  tracking/       detections, presence, movement, realtime aggregator
  watchlist/      tracking requests and the approval workflow
  api/            DRF viewsets, default-deny permissions
vision/           standalone, no Django import; runs against a video file
scripts/          camera_audit.py - run this first
tests/            permission matrix, scoping, pipeline, worker and aggregator
```

`vision/` deliberately has no Django dependency, which is what makes accuracy
benchmarking and CI practical.

---

## Screens added in Phase 4

| Where | What | Who |
|---|---|---|
| Site → floor → *Draw zones* | Click-to-draw zone outlines on an uploaded plan | Zone managers |
| `/live/` | Occupancy drawn on the floor plan, updated over the socket | Live-board roles, own scope only |
| Employee timeline | Movement map with playback; evidence images on request; dispute an entry | Anyone allowed that timeline; images need snapshot access |
| Requests → *Follow live* | Live movements for an approved live request | Requester or approver only |
| `/review/` | Implausible movements, correction reports, unknown-face clusters | Organisation-wide history access |
| `/reports/` | Daily headcount and space use; CSV export | Aggregate-report roles; per-person export admins only |
| `/audit/` → *Export CSV* | Audit log export | Auditor |

Reports read the nightly daily summaries. To fill past days (after install
or a scheduler outage):

```bash
docker compose exec web python manage.py build_daily_summaries --days 30
```

Counts below 3 are shown as "<3" to anyone without organisation-wide history
access, because a count that small names the people in it. Every evidence
image, every export and every review decision is in the audit log; exports
are limited to 10 per user per hour and CSV cells are protected against
spreadsheet formula injection.

## Phase 5: governance and hardening

**MFA.** Anyone with an enrolled authenticator signs in with password, then a
code (or a one-time backup code). With `MFA_ENFORCED=1` (the default when
`DJANGO_DEBUG=0`), Developer, Admin, Security Admin and HR are refused every
gated action until the session is verified - on HTTP and on WebSockets
alike. Enrolment issues ten backup codes, shown once; a user can issue a new
set from *My account*. Someone who has lost both phone and codes is reset by
another Super Admin from their user page, never by themselves.

**Tracking requests mean what they say.** A historical approval opens only
its `data_from`..`data_to` window - on the timeline, in the API, in zone
search results and for evidence images. A live-only approval opens no
history at all, and the live-follow page starts from the moment of approval.

**Live follow ends when the approval does.** Revoking a request, or its
expiry, disconnects the viewer's socket immediately; the socket also
re-checks its authorisation every 30 seconds on the server side, whatever
the browser does.

**Alerts** (`/alerts/`). Four rule types - unauthorised entry to a
restricted zone, presence after hours (in the building's own timezone),
an unknown face in a restricted zone, and a camera going offline. Only
confirmed matches alert; a probable sighting alerts only if it is later
confirmed. Repeats within 15 minutes fold into one alert. The inbox is
scoped to the zones a viewer monitors, and names are shown only to viewers
with organisation-wide history access - an operator sees what and where,
and the evidence inside their live window. Rules are managed at
`/alerts/rules/`; `seed_demo` creates one of each.

**Retention** now also ages out movement events, daily summaries, alerts
(with presence), camera health samples (with raw detections) and sign-in
history (with the audit log).

**Self-service** (*My account*). Everyone linked to an employee record sees
their consent, their own enrolled face images, and every tracking request
about them that has not been formally suppressed. Developers in production
can request break-glass access there; a Super Admin approves it.

## Next: Phase 6 - pilot and tuning

This is operational work rather than code:

- One floor, real users, real cameras. Run `make audit` first.
- **Measure accuracy against a labelled set** and tune `PipelineConfig`
  thresholds from it. The published defaults will not transfer to your estate.
- Work the correction and review queues daily for the first weeks: they are
  the error data that tuning needs.
- Reposition cameras the audit or the gate rate flags.
- Load test with the real camera count before rolling out further.
- Turn on `MFA_ENFORCED=1` once every privileged user has enrolled.
