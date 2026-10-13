# Pilot log

Measured results, newest first. Each entry records what was run, against
what, and what it showed - the evidence behind the week-4 go / no-go.

## 2026-10-13 - Week 0: dry run on the development stack

A full rehearsal of the pilot steps on the one camera this host can read:
the test camera `cam-test-file`, which plays `samples/walk.mp4` (a fixture
portrait panned across the frame). It proves the procedure and the tools,
not accuracy: the real numbers need the pilot cameras and real participants.

### 1. Camera audit (`make audit`, 30 s per camera, saved)

| Camera | Result |
|---|---|
| cam-test-file | **marginal** - median face 86.5 px (p90 88.5 px), 1280x720 |
| 11 demo cameras | **unusable** - could not open stream (demo addresses) |

### 2. Accuracy benchmark (synthetic set)

Set built with `scripts/make_synthetic_benchmark.py` from the two fixture
portraits: 5 enrolment-style gallery images each, 25 CCTV-style probes each
(face 60-120 px, blur, JPEG 35-80, lighting shifts, +/-10 deg rotation).

| | Result |
|---|---|
| Gallery | 2 people, 10 templates, 0 rejected by the enrolment gate |
| Probes | 39 usable, **11 of 50 (22%) rejected by the live quality gate** |
| Scores | genuine median 0.927; impostor 99.9th percentile -0.013 |
| At 0.45 / 0.30 | 39/39 confirmed correct, 0 confirmed wrong - meets both targets |
| Recommendation | **none - not enough data** (39 wrong-person comparisons, 2 people) |

The perfect score is what two studio portraits give; it says nothing about a
real doorway. The useful outputs are the quality-gate loss under degradation
and the refusal to recommend on thin data (see "Fixed" below).

### 3. Live tracking (3 minutes on cam-test-file)

| | Result |
|---|---|
| Camera status | online, health sample every ~15 s |
| Processing rate | **~2.5 frames/s on this host's CPU, one camera** |
| Faces seen / gated | ~2.5/s seen, 0 gated |
| Decisions | 29, all confirmed as EMP001, mean score 0.97 |
| Presence | one open interval in Reception, confirmed |

CPU inference is the bottleneck: one camera already uses the CPU budget. A
multi-camera pilot needs the GPU worker image (`make vision-gpu`).

### 4. Readiness after the dry run

**6 pass, 8 warn, 0 fail.** One identity camera passes its latest audit; the
other seven have never produced a face. Enrolment (5+ images each),
authenticators, MFA enforcement, measured thresholds and the First Floor plan
remain open.

### Fixed during the dry run

- **The benchmark recommended thresholds of 0.00.** With two easy identities,
  every threshold "passes". The recommender now never goes below 0.40 / 0.25,
  and refuses to recommend without at least 1,000 wrong-person comparisons and
  10 people.
- **Events failed during a deploy were never retried.** The aggregator kept
  running old code after the camera-vendor migration and failed 573 events,
  which then sat unacknowledged forever. It now replays its own pending events
  on every start; all 573 were recovered. After any migration, restart
  `aggregator`, `worker` and `beat` as well as `web`.
- **The test video camera was flagged degraded.** The worker read the file
  flat out, hit the end in two seconds and waited ten. Files now play at their
  own frame rate and loop.
- **The readiness check passed a camera audit that saw nothing.** It now
  judges the latest audit's verdict, not just that one exists.
- `make benchmark` wrote its result into a read-only mount; it now runs in the
  web container.

## 2026-10-13 - Week 0: pre-flight on the development stack

Environment: development stack (Docker, single host), demo site data
(2 floors, 12 zones, 12 cameras, 10 employees). No real cameras reachable
from this host, so these are tooling and capacity checks, not accuracy.

### Readiness (`make pilot-check`)

**6 pass, 7 warn, 1 fail - not ready.**

| Area | Check | Result |
|---|---|---|
| Site | Private rooms never tracked | PASS - 2 zones |
| Site | Every zone connected | PASS - 10 of 10 |
| Site | Plans and outlines | WARN - First Floor has no plan |
| Cameras | Identity cameras >= 60 px | PASS - 8 of 8 (configured values, not yet audited) |
| Cameras | Camera audit run | WARN - no audit results yet |
| Cameras | Cameras online | **FAIL - 0 of 12** (demo addresses unreachable) |
| People | Consent recorded | PASS - 10 of 10 |
| People | 5+ enrolment images | WARN - 0 of 10 ready |
| People | Enrolment conflicts | PASS |
| Governance | Authenticators enrolled | WARN - 5 of 6 privileged users not enrolled |
| Governance | MFA enforced | WARN - off |
| Governance | Alert rules | PASS - all four types |
| Tuning | Thresholds measured | WARN - still defaults 0.45 / 0.30 |
| Tuning | Nightly jobs | WARN - no daily summaries yet |

### Aggregator capacity (`make loadtest`)

| Cameras x fps | Events/s | Published | Drain after stop | Worst lag | Verdict |
|---|---|---|---|---|---|
| 12 x 4 | 48 | 1,440 in 30 s | 0.0 s | 0 | OK |
| 40 x 4 | 160 | 4,800 in 30 s | 9.0 s | 675 events | OK, at the limit |

One aggregator handles the current 12 cameras with no backlog. At 40
cameras it keeps up but only just (the target is under 10 s). For a rollout
beyond ~35 cameras, run a second aggregator replica with its own
`AGGREGATOR_CONSUMER` name, then re-test. Load-test rows were purged after
each run.

### Next actions before day 1

1. Point the pilot cameras at real streams and bring them online (clears the
   one failure).
2. `make audit secs=120` on the pilot floor's identity cameras.
3. Enrol each participant with 5+ images.
4. Privileged users enrol authenticators, then set `MFA_ENFORCED=1`.
5. Upload the First Floor plan if it is part of the pilot.
6. Collect the labelled benchmark set and run `make benchmark` (week 1).
