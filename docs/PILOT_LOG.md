# Pilot log

Measured results, newest first. Each entry records what was run, against
what, and what it showed - the evidence behind the week-4 go / no-go.

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
