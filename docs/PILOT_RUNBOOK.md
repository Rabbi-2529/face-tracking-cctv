# Phase 6 - Pilot runbook

One floor, real cameras, real (consenting) people, four weeks. The pilot
answers one question with evidence: **is identification accurate enough on
these cameras to be used?** If the answer is no, the fix is camera placement,
not threshold tuning - and the pilot exists to find that out cheaply.

## Acceptance criteria (agree these in writing before starting)

| Measure | Target | Where it is measured |
|---|---|---|
| Enrolled people correctly identified at doorways | **>= 95%** | `make benchmark`, Accuracy page |
| Probes given a *confirmed wrong* name | **<= 2%** | `make benchmark`, accepted corrections |
| Zone transitions timed within | **30 s** | spot checks against door logs |
| Aggregator drains after a burst at full camera count | **< 10 s** | `make loadtest` |
| Cameras gating out more than half their faces | **none** | Accuracy page, gate rate |

Go / no-go is decided on these numbers at the end of week 4, by the people
who signed the criteria.

## Before day 1

- [ ] Legal and HR sign-off on the notice and consent wording (plan section 10).
- [ ] Signage at every pilot camera.
- [ ] Pilot floor chosen; its zones, doors (adjacency edges) and floor plan
      entered under **Site & zones**, zone outlines drawn.
- [ ] Washrooms, prayer and medical rooms marked *never tracked*.
- [ ] Cameras installed at doorways, 1.8-2.2 m, tilt under 15-20 deg, NTP set
      to the same server as the application host.
- [ ] `make audit secs=120` - every identity camera >= 60 px median face
      (>= 100 px preferred). Reposition anything below before going further.
- [ ] Participants enrolled with **5+ images each** (frontal, turned left and
      right, with and without glasses), consent recorded.
- [ ] Every privileged user has enrolled an authenticator; then set
      `MFA_ENFORCED=1`.
- [ ] Alert rules reviewed under **Alert rules** for the pilot floor.

## Week 1 - baseline

1. Run the system on the pilot floor with the default thresholds.
2. Collect a labelled probe set from the pilot cameras (see *Benchmark set*
   below) and run:

   ```bash
   make benchmark data=samples/benchmark
   ```

   Record both the "current" and "recommended" results.
3. Run the load test at the full rollout camera count:

   ```bash
   make loadtest cams=20 fps=4 secs=60
   ```

## Daily, throughout

- **Review queues** (`/review/`): work the correction reports and implausible
  movements every day. Accepted corrections are the labelled errors the
  Accuracy page tunes against; without them it has nothing to say.
- **Accuracy page** (`/accuracy/`): check gate rates and probable share per
  camera. A camera above 50% gate rate is badly placed - move it.
- **Alerts** (`/alerts/`): note false alarms, and which rule produced them.

## Week 2-3 - tune

1. On the Accuracy page, try the benchmark's recommended thresholds in the
   **what-if** panel against the live score distribution. Look at:
   - how many confirmed matches drop to probable, and
   - whether every *confirmed-wrong* identification falls below the new
     confirmed threshold.
2. Apply under **Matching** with a change reason. Changes reach the workers
   within 30 seconds and are recorded in the audit log.
3. Re-run the benchmark after any camera move or threshold change.
4. Re-enrol anyone the correction queue shows being missed or confused,
   with more varied images.

## Week 4 - decide

Re-run `make benchmark` and `make loadtest`, fill in the acceptance table
with the measured values, and decide. A *no-go* usually means cameras: the
benchmark output says so when more probes were rejected by the quality gate
than kept.

## Benchmark set

```
samples/benchmark/
  gallery/<person>/*.jpg      enrolment-style photos (the same people as above)
  probes/<person>/*.jpg       frames or face crops captured by the PILOT cameras
  probes/_unknown/*.jpg       people who are NOT enrolled
```

- At least 20 enrolled people and 20 probes each gives a meaningful number;
  more is better.
- Probes must come from the pilot cameras, at the times of day they will run.
- Include `_unknown` probes, or the false-positive rate is only half measured.
- These are biometric images of real people: only participants who consented
  to the test, kept on the server (`samples/` is git-ignored), deleted after
  the pilot.
- To check the tooling before real data exists, generate a synthetic set from
  the fixture portraits:
  `docker compose run --rm web python scripts/make_synthetic_benchmark.py`.
  With two identities the benchmark will (correctly) refuse to recommend
  thresholds - it needs at least 10 people and 1,000 wrong-person comparisons
  before its recommendation means anything.

## After the pilot

- Clean up any load-test rows: `docker compose exec web python manage.py purge_load_test`.
- Delete the benchmark set.
- Fill in the rollout plan (Phase 7) with the measured camera spec.
