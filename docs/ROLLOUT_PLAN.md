# Rollout plan - Phase 7

From one pilot floor to the whole site, in waves. Starts only after a written
**go** at the end of the pilot (`docs/PILOT_RUNBOOK.md`, acceptance table in
`docs/PILOT_LOG.md`). Plan length: 2-3 weeks per the project plan, longer if
cameras need moving.

## Principles

- **One floor per wave.** Each wave must meet the same bar as the pilot
  before the next one starts.
- **Cameras first, people second.** A floor whose cameras fail the audit is
  not rolled out, however ready everything else is.
- **No silent expansion.** Each wave is announced to the employees on that
  floor before its cameras start identifying anyone (signage, notice,
  consent), as the law and the plan require.
- **Rollback is always possible.** Disabling a floor's cameras stops all
  processing there within a minute; nothing else changes.

## Waves

Fill in with the real floors. The pilot floor is wave 0.

| Wave | Floor / area | Cameras | People | Target week | Owner | Status |
|---|---|---|---|---|---|---|
| 0 | (pilot floor) | | | done | | pilot |
| 1 | | | | | | planned |
| 2 | | | | | | planned |
| 3 | | | | | | planned |

## For each wave

### Entry criteria (before cameras are enabled)

- [ ] Floor plan uploaded, zones outlined, doors connected, private rooms
      marked never-tracked.
- [ ] Cameras installed to spec (doorway, 1.8-2.2 m, tilt under 15-20 deg,
      main stream, NTP).
- [ ] `make audit secs=120` during working hours: every identity camera
      **>= 60 px** (100+ preferred).
- [ ] Signage up; notice given; consent recorded for everyone on the floor
      who will be enrolled.
- [ ] Participants enrolled with **5+ photos** each.
- [ ] HR and operator scopes updated for the new departments and zones.
- [ ] Alert rules cover the floor's restricted rooms.
- [ ] `pilot_check --floor "<floor>"` shows **0 fail**.

```bash
docker compose exec web python manage.py pilot_check --floor "<floor>"
```

### During the wave (first two weeks)

- Daily: review queues, alerts, camera status (`docs/OPERATIONS.md`).
- Weekly: Accuracy page for the floor's cameras; capacity
  (`make loadtest cams=<total cameras>` on a non-production copy).

### Exit criteria (before the next wave)

- [ ] Accepted wrong identifications on the floor **<= 2%** of its presence
      entries (Accuracy page, Zones).
- [ ] No camera on the floor with a gate rate above 50%.
- [ ] Correction reports answered within a working day.
- [ ] Aggregator keeps up (no growing backlog in `make logs s=aggregator`).
- [ ] Sign-off by the floor's manager and the system owner.

### Rollback

1. **Cameras** &rarr; disable the floor's cameras (or `Zone` &rarr; never
   tracked for a room). Workers stop within a minute.
2. Tell the people on the floor, in writing.
3. Presence already recorded stays until the automatic retention job removes
   it. There is no early-purge screen: deleting sooner is a manual database
   operation, to be approved by the system owner and a second Super Admin and
   recorded in writing.

## Capacity as waves add cameras

| Cameras | Vision | Aggregator |
|---|---|---|
| 1-8 | one GPU (RTX 3060 class) | 1 replica |
| 8-20 | A4000 class with TensorRT/NVDEC | 1 replica |
| 20-35 | as above, `SHARD_COUNT` across 2 vision hosts | 1 replica |
| 35+ | shard further | **2+ replicas** (each its own `AGGREGATOR_CONSUMER`) |

Measured on the development stack: one aggregator drained 12 cameras at
4 fps instantly and 40 cameras with a 9-second lag. Re-measure on the
production hardware before each wave that crosses a row of this table.

## Training before each wave

- Security Operators of the new zones: **Help & training** &rarr; Security
  Operator guide; walk through the live board and an alert.
- HR for the new departments: HR guide; enrol one person together.
- Employees on the floor: what is collected, why, where they can see their
  own record (**My account**), and how to report a wrong entry.

## Communication template (to employees on a new floor)

> From <date>, cameras at the doorways of <floor> will recognise enrolled
> staff to record which rooms are occupied, for <purpose>. This applies only
> to people who have consented. You can see everything recorded about you
> under **My account**, report any mistake from your own timeline, and
> withdraw consent at any time through HR. Washrooms, prayer and medical
> rooms are never monitored. Questions: <contact>.
