# Office CCTV Employee Movement Tracking — Django Project Plan

**Goal:** Enrol an employee (name + face images), then see that person's movement
across the office — which room they were in, when they moved, where they went next —
reconstructed from existing CCTV cameras. Access is split across Developer, Admin
and HR roles.

---

## 1. Scope and realistic expectations

Read this section before budgeting. It decides whether the project succeeds.

### What is achievable
- Zone-level tracking: "Rahul was in Reception 09:12–09:18, Meeting Room 2 09:19–10:05, Pantry 10:07–10:14."
- Movement timeline per employee per day, with snapshot evidence per event.
- Live "who is in which room right now" board.
- Alerts: restricted-zone entry, after-hours presence, unknown face in secure area.
- Dwell-time and attendance-style reports.

### What is NOT achievable with normal CCTV
- Continuous pixel-level "follow the dot on a floor plan". You get **zone transitions**, not a continuous path, unless you have near-total camera coverage.
- Reliable identity from cameras that only see the back/top of heads. Ceiling dome cameras angled steeply down are often **unusable** for face recognition.
- 100% accuracy. Expect ~95–99% on good frontal captures at a door, and far worse on corridor/side views.

### The single biggest success factor: camera placement
Before writing any code, audit every camera:

| Requirement | Target |
|---|---|
| Face width in frame | ≥ 80–100 px (ideally 120 px) |
| Mounting height | 1.8 – 2.2 m (face height, not ceiling) |
| Downward tilt | ≤ 15–20° |
| Position | At doorways / choke points, facing people as they enter |
| Lighting | No strong backlight (avoid camera facing a window) |
| Stream | RTSP/ONVIF accessible, ≥ 1080p, ≥ 15 fps |

**Plan on adding 1 dedicated "face camera" per doorway you care about.** Existing
general-surveillance cameras can still be used for presence/body tracking, but
identity should be established at doors.

---

## 2. Architecture

Django is the **control plane**, not the video processor. Keep inference in separate
worker processes so a camera hang never takes down the web app.

```
 IP Cameras / NVR
   (RTSP, ONVIF)
        |
        v
+-------------------------------+
|  Ingest Workers (1 per cam)   |  Python + FFmpeg/OpenCV
|  - connect RTSP, auto-reconnect|  Samples 3-5 fps (not 25)
|  - frame sampling + decode     |
+---------------+---------------+
                v
+-------------------------------+
|  Vision Pipeline (GPU)        |
|  1. Face detect   (SCRFD)     |
|  2. Align + quality gate      |
|  3. Embed        (ArcFace 512d)|
|  4. Track in-frame (ByteTrack) |
|  5. Body re-ID    (OSNet, ph.2)|
+---------------+---------------+
                v
+-------------------------------+
|  Identity Matcher             |
|  - cosine search vs enrolled  |  pgvector / FAISS
|  - track-level majority vote  |  (never decide per frame)
|  - threshold + unknown bucket  |
+---------------+---------------+
                v
        Redis Streams / RabbitMQ
                |
                v
+-------------------------------+
|  Event Aggregator (Celery)    |
|  raw detections ->            |
|    PresenceInterval ->        |
|      MovementEvent            |
|  validated against zone graph |
+---------------+---------------+
                v
+-------------------------------+        +------------------+
|  Django + DRF                 |<------>| PostgreSQL       |
|  - RBAC / API / Admin         |        | + pgvector       |
|  - Channels (WebSocket live)  |        | + partitioned    |
|  - Celery Beat (retention)    |        |   detections     |
+---------------+---------------+        +------------------+
                |                        +------------------+
                v                        | MinIO / S3       |
   Web UI (HTMX or React)                | snapshots, clips |
   Dev / Admin / HR / Security / Employee+------------------+
```

### Why this split
- A hung RTSP connection or a CUDA OOM kills one worker, not the portal.
- You can scale GPU boxes independently of the web tier.
- Django stays testable without a camera attached (mock the event stream).

---

## 3. Tech stack

| Layer | Choice | Notes |
|---|---|---|
| Web framework | Django 5.x + Django REST Framework | |
| DB | PostgreSQL 16 + `pgvector` | Vector search in the same DB = far simpler than a separate vector store at this scale |
| Async tasks | Celery + Redis | Aggregation, reports, retention purge |
| Realtime | Django Channels + Redis layer | Live room occupancy board |
| Face detection | InsightFace **SCRFD** (or YOLOv8-face) | |
| Face embedding | InsightFace **ArcFace** (`buffalo_l`, 512-d) | Industry standard, open licence |
| Multi-object tracking | ByteTrack / BoT-SORT | Within a single camera view |
| Body re-ID (phase 2) | OSNet / FastReID | Covers "face not visible" gaps |
| Inference runtime | ONNX Runtime GPU, or TensorRT | TensorRT ~2–3x throughput |
| Video decode | FFmpeg (NVDEC hardware decode) | CPU decode will bottleneck you |
| Object storage | MinIO (self-host) or S3 | Snapshots/clips never in the DB |
| Frontend | Django templates + HTMX + Alpine (fast), or React + DRF (richer) | HTMX gets you there faster; React if you want a heavy floor-plan UI |
| Deployment | Docker Compose, then Kubernetes later | GPU workers need `nvidia-container-toolkit` |
| Monitoring | Prometheus + Grafana, Sentry | Per-camera FPS, lag, match rate |

### Optional at scale (30+ cameras)
**NVIDIA DeepStream** or **Frigate** as the ingest/detect layer, with Django consuming
its event output. Saves months of pipeline engineering, costs flexibility.

---

## 4. Hardware sizing

| Cameras | GPU | Notes |
|---|---|---|
| 1–8 | RTX 3060 12GB / T4 | ~3 fps sampled per camera |
| 8–20 | RTX 4070 Ti / A4000 16GB | Use TensorRT + NVDEC |
| 20–50 | 2x A4000 or 1x L4, or DeepStream | Shard cameras across workers |

Also budget:
- **App server:** 8 vCPU / 16 GB (Django + Celery + Redis)
- **DB:** 8 vCPU / 32 GB, fast NVMe. The detections table grows fast.
- **Storage:** snapshots only (not full video) ~20–60 GB/camera/month at 3 fps with dedup. Full clip retention multiplies this — keep clips short (±10 s around events).
- **Network:** isolate cameras on their own VLAN, no internet route.

---

## 5. Data model

Django apps and their core models.

### `accounts`
- **User** (custom, `AbstractUser`) — email login, MFA flag, `must_change_password`
- **Role** — Developer, SuperAdmin, SecurityAdmin, HR, SecurityOperator, Auditor, Employee
- **UserScope** — limits a user to specific `departments[]` and `zones[]` (object-level scoping)
- **AuditLog** — `actor`, `action`, `object_type`, `object_id`, `target_employee`, `reason`, `ip`, `user_agent`, `at`. **Append-only.** Every identity lookup, every video view, every export.
- **LoginAttempt**, **SessionRecord**

### `org`
- **Building**, **Floor**
- **Zone** — `name`, `floor`, `zone_type` (room / corridor / entry / restricted / pantry), `is_restricted`, `tracking_prohibited`, `floor_plan_polygon` (GeoJSON for the map UI)
- **ZoneAdjacency** — `from_zone`, `to_zone`, `min_transit_seconds`, `typical_transit_seconds`. This is the **topology graph**; it is what lets you reject impossible transitions (someone "teleporting" between floors = bad match).
- **Department**, **Designation**

### `employees`
- **Employee** — `employee_code`, `full_name`, `department`, `designation`, `date_of_joining`, `status`, `linked_user` (nullable), `is_trackable`
- **FaceEnrollment** — `employee`, `image_key` (object storage), `embedding` (`VectorField(512)`), `quality_score`, `pose` (frontal/left/right), `is_active`, `enrolled_by`, `enrolled_at`, `source` (upload / captured / auto-reinforced)
  - Store **5–10 enrollments per person**: frontal, ±15° left/right, with and without glasses, different lighting. Single-image enrollment is the most common cause of poor accuracy.
- **ConsentRecord** — `employee`, `policy_version`, `consented_at`, `method`, `document_key`, `withdrawn_at`. Gate tracking on this.

### `cameras`
- **Camera** — `name`, `zone`, `rtsp_url` (**encrypted field**), `username`/`password` (encrypted), `resolution`, `native_fps`, `sample_fps`, `is_enabled`, `purpose` (identity / presence), `roi_polygon`, `calibration_notes`
- **CameraHealth** — `camera`, `last_frame_at`, `fps_actual`, `status`, `error_message`, `latency_ms`
- **CameraGroup** — for scoping access

### `tracking`
- **Detection** (high volume — **partition by day**, retain 7–30 days)
  `camera`, `captured_at`, `bbox`, `track_id`, `embedding` (optional, drop after match), `matched_employee` (nullable), `match_score`, `snapshot_key`, `is_unknown`
- **PresenceInterval** (the main query table)
  `employee`, `zone`, `entered_at`, `exited_at`, `confidence`, `detection_count`, `first_snapshot_key`, `last_snapshot_key`
- **MovementEvent**
  `employee`, `from_zone`, `to_zone`, `occurred_at`, `prev_dwell_seconds`, `confidence`, `is_plausible` (checked against `ZoneAdjacency`), `review_status`
- **UnknownFaceCluster** — groups unmatched embeddings so you can later label a visitor or spot an intruder; `cluster_id`, `centroid_embedding`, `first_seen`, `last_seen`, `sighting_count`, `resolved_as` (visitor / employee / contractor / unidentified)
- **IdentityCorrection** — `detection`/`interval`, `was_employee`, `should_be_employee`, `corrected_by`, `reason`. Feeds a re-enrollment loop and satisfies the "right to rectification".

### `watchlist` — this is the "select an employee and track them" feature
- **TrackingRequest** — `employee` (subject), `requested_by`, `reason` (**mandatory free text**), `scope` (live / historical), `start_at`, `end_at`, `status` (pending / approved / rejected / expired), `approved_by`, `approved_at`
- **ActiveTrackSession** — `tracking_request`, `started_at`, `ended_at`, `viewers[]`

Rationale: *ad-hoc following of a named individual is the highest-risk capability in the
system.* Making it a requested, reasoned, time-boxed, approved, audited object — rather
than a search box anyone can type a name into — is what keeps this defensible. HR can
request; Admin approves; the session auto-expires.

### `alerts`
- **AlertRule** — `rule_type` (restricted_zone / after_hours / unknown_face / loitering / camera_down), `zone`, `applies_to` (dept or employee set), `schedule`, `severity`, `notify_channels[]`
- **Alert** — `rule`, `employee` (nullable), `zone`, `triggered_at`, `evidence_snapshot_key`, `status`, `acknowledged_by`, `notes`

### `reports`
- **ReportDefinition**, **GeneratedReport** (`requested_by`, `params`, `file_key`, `expires_at`) — all exports are themselves audited artefacts with an expiry.

### `system`
- **RetentionPolicy** — `data_type`, `retain_days`, `last_purge_at`
- **PipelineConfig** — match threshold, min quality, sample fps, vote window (editable by Developer only, versioned)
- **SystemHealth**, **ModelVersion**

---

## 6. The recognition pipeline — detail that matters

### Enrollment
1. HR uploads 5–10 images, or captures from a kiosk camera.
2. Quality gate: face size, blur (Laplacian variance), pose angle, brightness, single-face check. **Reject bad images at upload** with a clear reason — do not let them silently poison the gallery.
3. Compute an embedding per image, store all of them.
4. Cross-check against the existing gallery: if the new face matches an already-enrolled different employee above threshold, flag for manual review (catches duplicate, twin, or mis-labelled enrollments).

### Matching — the two rules that determine your accuracy
1. **Never identify from a single frame.** Accumulate embeddings over a ByteTrack track (e.g. 10–30 frames), then take the top-k mean or a majority vote. Single-frame matching is what produces the "it keeps saying the wrong name" problem.
2. **Use two thresholds.** Above `high` (e.g. cosine 0.45 with ArcFace) accept. Below `low` (e.g. 0.30) mark unknown. In between, mark "probable", show it in the UI with visible uncertainty, and never trigger an alert on it alone.

Tune these on your own data with a labelled validation set; published thresholds will
not transfer to your cameras.

### Cross-camera continuity
- Each camera belongs to one `Zone`. Appearance in a zone = presence.
- A transition A to B is only emitted if `ZoneAdjacency` says A and B are connected and the time gap is at least `min_transit_seconds`. Otherwise flag `is_plausible=False` and surface it for review rather than writing a fake movement.
- Gaps (person in a corridor with no face visible) are shown as "in transit" with low confidence, not invented as presence.
- Phase 2: body re-ID bridges those gaps using appearance within a single day.

### Aggregation
A Celery task every ~30 s rolls raw `Detection` rows into `PresenceInterval`
(merge detections of the same employee in the same zone with gaps under 60 s) and then
emits `MovementEvent`. Query the aggregates from the UI, never the raw table.

---

## 7. Access control — the role model

You asked for Developer, Admin and HR. Three roles is not enough to make this safe or
auditable; I recommend six. Developer/Admin/HR stay exactly as you described them.

### Permission dimensions
Role alone is insufficient. Gate on all of these:
1. **Which employees** (all / own department / specific approved subject)
2. **Which zones/cameras** (all / assigned set)
3. **Live vs historical**
4. **Metadata vs raw imagery** (timeline text vs snapshots vs video clips)
5. **Export rights**
6. **Lookback depth** (today / 30 days / full retention)

### Role matrix

| Capability | Developer | Super Admin | HR | Security Operator | Auditor | Employee |
|---|:--:|:--:|:--:|:--:|:--:|:--:|
| **System / technical** | | | | | | |
| Camera CRUD, RTSP credentials | YES | YES | no | no | no | no |
| Pipeline config, thresholds, models | YES | view | no | no | no | no |
| Raw `Detection` table, embeddings | YES | no | no | no | no | no |
| Logs, metrics, Django admin, DB | YES | limited | no | no | no | no |
| Zone / topology graph editing | YES | YES | no | no | no | no |
| **Identity** | | | | | | |
| Employee CRUD | staging only | YES | YES | no | no | no |
| Face enrollment / re-enrollment | test data | YES | YES | no | no | no |
| Consent records | no | YES | YES | no | read | own |
| **Tracking** | | | | | | |
| Live occupancy board (all zones) | YES | YES | no | assigned | no | no |
| Live follow a named employee | non-prod | YES | **request** | no | no | no |
| Historical timeline — any employee | YES | YES | **request** | no | no | no |
| Historical timeline — own department | — | YES | YES | no | no | no |
| Own movement history | YES | YES | YES | YES | YES | YES |
| View snapshots as evidence | YES | YES | approved only | live only | no | own |
| View / replay video clips | YES | YES | no | last 24h | no | no |
| Approve a `TrackingRequest` | no | YES | no | no | no | no |
| **Alerts and reports** | | | | | | |
| Configure alert rules | YES | YES | no | no | no | no |
| Acknowledge alerts | YES | YES | no | YES | no | no |
| Aggregate reports (dept/zone/hours) | YES | YES | YES | no | no | no |
| Export CSV/PDF | audited | YES | aggregate only | no | audit log only | own |
| **Governance** | | | | | | |
| Read audit log | own actions | YES | own actions | no | full | own |
| User and role management | no | YES | no | no | no | no |
| Retention policy | propose | YES | no | no | read | no |
| Delete / purge footage | no | YES (dual-auth) | no | no | no | no |

### Notes on the three roles you named

**Developer — "full details"**
Full technical visibility: every table, every log, pipeline internals, thresholds,
embeddings, model versions. Two guard rails worth having:
- **Prod data in dev/staging should be pseudonymised** (employee codes, not names; no face images). Developers debug pipeline behaviour, which does not require knowing whose face it is.
- When production debugging genuinely needs real identities, use **break-glass**: time-boxed elevation, mandatory reason, auto-expiry, notification to the Admin. The access still happens; it just leaves a record.
- Developer cannot approve tracking requests and cannot manage users. Technical power and people-power stay separated.

**Admin — full operational authority**
All employees, all cameras, live and historical, alert config, user management, approves
tracking requests, owns retention. Cannot change pipeline internals or read raw
embeddings (that is Developer). Enforce **MFA** and **dual authorisation for deletion**.

**HR — people, not surveillance**
Enrollment, consent, aggregate reports, own-department timelines. For anything beyond
that — following a specific person live, or pulling another department's history — HR
files a `TrackingRequest` with a reason and Admin approves it. This is the control that
turns "HR can watch anyone" into "HR can investigate a specific concern, on record".

### Implementation in Django
- Custom `User` plus Django **Groups** as roles; model-level `Permission` codenames for actions.
- **Object-level scoping** via a `UserScope` model and a `ScopedQuerySetMixin` that filters in `get_queryset()` — never only in the template.
- DRF `permission_classes` plus per-field serializer control (a `SnapshotField` returns a signed URL only if the requester may see imagery).
- A single `PermissionService.can(user, action, target)` function so the rule lives in one place and is unit-testable. Write a test that asserts the whole matrix above.
- `django-guardian` only if you need true per-object ACLs; the scope model is usually simpler.
- Snapshots served as **short-lived signed URLs** (60–300 s), never public paths.
- Every identity read goes through a decorator that writes to `AuditLog`. If it is not audited, it is not a feature.

---

## 8. Screens

**Developer console** — pipeline topology, per-camera FPS/lag/match-rate, model
versions, threshold tuner with live preview, raw detection explorer, task queue,
error feed, replay a stored clip through the pipeline.

**Admin** — org/zone/camera setup, floor-plan editor, user and role management,
tracking-request approval queue, alert rules, retention, audit log viewer.

**HR** — employee directory, enrollment wizard (upload, quality check, confirm),
consent dashboard, department presence/attendance reports, tracking-request form.

**Security operator** — live wall for assigned zones, occupancy counts, alert inbox.

**Shared core views**
- *Live board:* floor plan with room occupancy counts, click a room for who is inside.
- *Employee timeline:* vertical day timeline — zone blocks, transitions, dwell times, confidence badges, snapshot thumbnails, "report wrong identification" button.
- *Movement map:* floor plan with the day's path animated between zones.
- *Search:* "who was in Server Room between 14:00 and 16:00 yesterday". Zone-first queries are lower-risk than person-first and cover most real investigations.

**Employee self-service** — own movement history, own enrolled images, consent status,
correction request. Cheap to build, and it is the strongest trust signal you have.

---

## 9. Phased roadmap

Assumes 2 backend devs + 1 ML engineer + 1 frontend dev, part-time overlap.

| Phase | Work | Duration |
|---|---|---|
| **0. Discovery** | Camera audit (the table in section 1), zone and adjacency map, legal/consent sign-off, accuracy targets agreed in writing, GPU procurement | 2 weeks |
| **1. Foundation** | Django project, custom user, RBAC + scoping + audit log, org/zone/camera/employee CRUD, Docker Compose, CI | 3–4 weeks |
| **2. Single-camera PoC** | RTSP ingest, SCRFD + ArcFace, pgvector gallery, enrollment wizard with quality gate, live recognition on **one** camera. **Gate: measured accuracy on your own cameras before proceeding.** | 3–4 weeks |
| **3. Multi-camera** | Worker-per-camera + supervision, ByteTrack, track-level voting, Redis event bus, aggregator to PresenceInterval to MovementEvent, topology validation, health monitoring | 4 weeks |
| **4. Interfaces** | Live board, floor plan, employee timeline, movement map, zone search, reports/export, WebSocket updates | 4 weeks |
| **5. Governance and hardening** | Tracking-request approval flow, alert engine, retention purge jobs, signed URLs, encryption at rest, MFA, pen-test fixes, self-service portal | 3 weeks |
| **6. Pilot and tuning** | One floor, real users, threshold tuning, false-positive review loop, camera repositioning, load test | 4 weeks |
| **7. Rollout** | Remaining floors, runbooks, training, handover docs | 2–3 weeks |

**Total about 5–6 months.** Phase 2's accuracy gate is the real decision point — if
faces are not clean enough, the answer is camera work, not more model tuning.

### Phase 2 optional fork
If Phase 2 reveals that significant camera replacement is needed, consider running
**Frigate or DeepStream** as the detect layer and keeping Django purely as the
portal/API. Loses tuning control, saves roughly 6 weeks.

---

## 10. Legal and compliance — build these as features

Face embeddings are **biometric data**: sensitive personal data under essentially every
modern privacy law. This is normal, legal workplace security technology, but it is
*conditionally* legal, and the conditions translate directly into engineering
requirements.

**If you are in India (DPDP Act 2023):**
- Clear **notice** and **consent** before processing, in plain language, per purpose
- Purpose limitation — "office security" does not authorise performance monitoring
- Data principal rights: access, correction, erasure (your self-service portal)
- Reasonable security safeguards; breach notification to the Data Protection Board
- A named contact for grievances

**If you have EU staff (GDPR Art. 9):** explicit consent or a substantial-public-interest
basis, mandatory **DPIA**, works-council consultation in several member states.
**US:** Illinois BIPA, Texas CUBI, Washington. BIPA carries per-violation private
damages and has produced very large settlements.

**Universal requirements, and where each lives in the build:**

| Requirement | Implementation |
|---|---|
| Written policy + employee notice | `ConsentRecord.policy_version` |
| Consent capture and withdrawal | `ConsentRecord`; pipeline skips non-consenting employees |
| Visible signage at camera locations | Physical, plus listed in the portal |
| No covert tracking | Employees can see their own data |
| Purpose limitation | Role matrix; no productivity/discipline use without a separate basis |
| Data minimisation | Store embeddings + snapshots, not 24/7 video; drop raw embeddings after match |
| Retention limits | `RetentionPolicy` + Celery purge; 7–30 days raw, 90–180 days aggregates |
| Access logging | `AuditLog`, append-only |
| Subject access and correction | Employee self-service + `IdentityCorrection` |
| DPIA | Phase 0 deliverable |
| Vendor/DPA agreements | If any cloud processing is used |

**Two design choices carry most of the risk reduction:**
1. **No silent person-search.** Named-individual tracking goes through `TrackingRequest`: reason, approval, expiry, audit. Zone-first search ("who entered the server room") handles most legitimate needs without it.
2. **Exclude private areas absolutely.** Toilets, changing rooms, prayer rooms, medical rooms, and in some jurisdictions break areas. Enforce in code (`Zone.tracking_prohibited = True` blocks ingest), not just in policy.

Get written sign-off from legal/HR leadership at the end of Phase 0. Retrofitting
consent after deployment is much harder than building it in.

---

## 11. Security hardening

- Camera VLAN isolated, no internet route; change all default camera passwords
- RTSP credentials encrypted at rest (`django-cryptography` or KMS), never in settings or git
- Embeddings encrypted at rest; they are biometric templates and are not anonymous
- TLS everywhere; HSTS; secure and `SameSite` cookies
- MFA mandatory for Developer, Admin, HR
- Short session timeouts (15–30 min) on identity-bearing screens
- Signed, expiring URLs for every snapshot and clip; private bucket ACLs
- Rate-limit identity lookups and exports; alert on bulk-export patterns
- Dual authorisation for purge and for role elevation
- Append-only audit log on separate storage (WORM or hash-chained)
- Watermark exported snapshots with viewer identity and timestamp — deters leaks
- Dependency scanning in CI; pen-test before go-live
- Backups encrypted, restore-tested, and themselves subject to retention

---

## 12. Key risks

| Risk | Impact | Mitigation |
|---|---|---|
| Existing cameras cannot see faces | Project fails | **Phase 0 audit.** Budget for door cameras. |
| Accuracy below expectation | Loss of trust | Honest targets up front; confidence shown in UI; correction loop; multi-image enrollment |
| Mis-identification used in an HR action | Serious harm and liability | Policy: system output is a lead, never sole evidence; snapshot required; correction right |
| Raw table growth | DB collapse | Daily partitioning, aggressive purge, aggregates for queries |
| Camera/stream instability | Silent data gaps | Health monitoring, auto-reconnect, `camera_down` alert, gaps shown explicitly in timelines |
| Scope creep into productivity monitoring | Legal exposure, morale | Purpose limitation in policy **and** in the role matrix; no "time at desk" reports |
| GPU bottleneck as cameras grow | Lag, dropped frames | Load-test in Phase 6; per-camera sample fps; horizontal GPU workers |
| Insider misuse of tracking | Privacy breach | Approval workflow, audit log, anomaly alerts on access patterns |

---

## 13. Repository layout

```
cctv_tracking/
├── docker-compose.yml           # web, db(pgvector), redis, minio, worker, beat
├── docker-compose.gpu.yml       # inference workers
├── config/                      # settings/{base,dev,prod}.py, celery, asgi
├── apps/
│   ├── accounts/                # user, roles, scopes, audit, MFA
│   ├── org/                     # building, floor, zone, adjacency, department
│   ├── employees/               # employee, enrollment, consent
│   ├── cameras/                 # camera, health, credentials
│   ├── tracking/                # detection, presence, movement, unknown clusters
│   ├── watchlist/               # tracking requests + approval workflow
│   ├── alerts/                  # rules, engine, notifications
│   ├── reports/                 # definitions, generation, export
│   ├── api/                     # DRF routers, serializers, permissions
│   └── common/                  # PermissionService, ScopedQuerySet, audit decorators
├── vision/                      # standalone, importable without Django
│   ├── ingest/                  # rtsp reader, frame sampler, reconnect
│   ├── detect/                  # scrfd wrapper
│   ├── embed/                   # arcface wrapper
│   ├── track/                   # bytetrack
│   ├── match/                   # gallery search, voting, thresholds
│   ├── aggregate/               # detection -> presence -> movement
│   └── runner.py                # worker entrypoint, one process per camera
├── models/                      # .onnx weights (git-lfs or object storage)
├── frontend/                    # templates + HTMX, or React app
├── tests/                       # unit, permission matrix tests, pipeline fixtures
├── scripts/                     # camera_audit.py, benchmark_accuracy.py, seed_demo.py
└── docs/                        # DPIA, privacy policy, runbooks, role matrix
```

Keep `vision/` free of Django imports. It should run standalone against a video file,
which makes accuracy benchmarking and CI far easier.

---

## 14. Recommended first two weeks

1. **Camera audit** — run `scripts/camera_audit.py` against every RTSP URL: grab frames, detect faces, measure face pixel width, log results per camera. This one script tells you whether the project is viable as-is.
2. **Zone map** — floor plan, name every room, draw the adjacency graph.
3. **Legal kickoff** — notice and consent draft, DPIA started, list prohibited zones.
4. **Accuracy target in writing** — e.g. "at least 95% correct identification at doorways, at most 2% false-positive rate, zone transitions accurate within 30 s."
5. **Django skeleton** — custom user, roles, audit log, Docker Compose up.
6. **Buy the GPU.** Lead times will bite you otherwise.

If step 1 shows that most cameras give faces under about 60 px wide, stop and re-plan
the camera layer before building anything else.
