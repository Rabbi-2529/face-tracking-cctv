# Operations runbook

Day-to-day running of the system once it is live. Each procedure says who
does it, how, and how to tell it worked.

## Routine checks

### Daily (Security Admin / Super Admin, 10 minutes)

| Check | Where | Healthy |
|---|---|---|
| Open alerts | **Alerts** | Each acknowledged or resolved with a note |
| Cameras | **Cameras** | All enabled cameras online; any offline has a ticket |
| Pending tracking requests | **Dashboard** | None older than a working day |
| Review queues | **Review queues** | Implausible movements and corrections answered |
| Backup | `ls -lt backups/ \| head -2` | A dump from the last night |

### Weekly (Developer)

- **Accuracy** page: gate rate per camera (over 50% = reposition), probable
  share, accepted corrections per zone.
- `make pilot-check` - nothing new failing.
- `docker compose ps` - every service up; `make logs s=aggregator` shows the
  minute-by-minute stats line with `errors` not climbing.

### Monthly (Super Admin)

- `make restore-test` on the newest backup.
- `make golive-check` - still 0 fail.
- **Users & roles**: disable leavers; check HR department scopes and operator
  zones still match people's jobs.
- **Audit log** with the Auditor: denials, unused approvals, break-glass use,
  exports.

## Service management

```bash
make ps                          # status of every container
make logs s=web                  # one service's log (web, worker, beat, aggregator, vision)
make restart                     # web, worker, beat, aggregator
docker compose restart vision    # camera workers
make health                      # readiness: database and Redis reachable
```

After **any** update with migrations, restart every Django service
(`make migrate && make restart`). After editing templates, restart `web`.

## Incidents

### A camera is offline

Symptoms: camera-down alert; red zone on the live board; camera status
"offline" or "degraded".

1. **Cameras** &rarr; the camera &rarr; *Test connection*.
2. "Could not open the stream": power, network, credentials, or RTSP turned
   off on the camera. Check from the vision host:
   `docker compose exec vision python -c "import cv2;print(cv2.VideoCapture('<url>').isOpened())"`.
3. Opens but no faces: someone moved the camera, or lighting changed. Re-run
   `make audit secs=120` for that camera.
4. While it is down, that zone has a **gap**: timelines show missing time,
   not absence. Say so if anyone asks about that period.

### Movements stop updating / live board frozen

1. `make logs s=aggregator` - look for repeated errors.
2. `docker compose exec redis redis-cli XPENDING events:detections aggregators`
   - a growing first number means events are failing.
3. Most common cause: the aggregator is on old code after an update. Run
   `make restart`. On start it replays every event it failed (logged as
   "recovered N pending event(s)").
4. Sustained lag with no errors: too many cameras for one aggregator. Add a
   replica with its own `AGGREGATOR_CONSUMER` name (`make loadtest` measures
   the limit; one aggregator handled 40 cameras at 4 fps at the edge).

### Someone is locked out

- **Wrong password 5 times**: locked for 1 hour automatically. To clear it
  sooner, once you are sure it is really them:
  `docker compose exec web python manage.py axes_reset_username <email>`.
- **Lost phone, has backup codes**: sign in with a backup code, then enrol a
  new authenticator from **My account**.
- **Lost phone and codes**: a Super Admin confirms the person's identity (in
  person or by a known channel), then **Users & roles** &rarr; user &rarr;
  *Reset authenticator* with the reason. The user enrols again at next
  sign-in.

### A wrong identification is reported

1. **Review queues** &rarr; Corrections: open the timeline, check the
   evidence image.
2. Accept (the entry then shows "not this person") or reject with a reason -
   the reporter is entitled to an answer.
3. Repeated errors for one person: re-enrol them with more varied photos.
   Repeated errors at one camera: check its gate rate and position.
4. **Never** use an uncorrected disputed entry as the basis for action.

### Suspected misuse of access

1. Auditor: **Audit log**, filter by the person, then by `history.`,
   `snapshot.`, `track.`, `report.export`.
2. Super Admin: revoke their tracking requests; disable the account if
   needed (**Users & roles** &rarr; disable).
3. Follow the organisation's data-protection incident procedure. A breach of
   biometric data is notifiable under most privacy laws (DPDP Act: the Data
   Protection Board; GDPR: within 72 hours).

### Server or database lost

1. Rebuild the host from `docs/DEPLOYMENT.md` with the **same**
   `FIELD_ENCRYPTION_KEY` and `DJANGO_SECRET_KEY`.
2. `make restore file=backups/<newest>` (decrypts with `BACKUP_PASSPHRASE`).
3. `make restart`, `make golive-check`.
4. Snapshots are not backed up by design (short retention); evidence images
   older than the restore point are gone.

## Privacy requests

| Request | Who | How |
|---|---|---|
| "What do you hold about me?" | The employee themselves | **My account**: consent, enrolled photos, requests about them, own timeline |
| Correct a record | Employee reports; reviewer decides | Timeline &rarr; *Something wrong here?*; **Review queues** &rarr; Corrections |
| Withdraw consent | HR | Employee &rarr; Consent &rarr; withdraw with a note. Matching stops within ~30 s |
| Delete my data | HR + Super Admin | Withdraw consent, deactivate face images, mark the employee exited. Presence history ages out under retention |
| Who looked at my data? | Auditor | **Audit log** filtered by the employee code |

## Retention (automatic)

Run nightly at 03:30 by the `enforce-retention` job:

| Data | Kept |
|---|---|
| Face templates in raw detections | half the detection period |
| Raw detections, unknown-face clusters, camera health | 14 days |
| Snapshots | 30 days |
| Presence, movements, daily summaries, alerts | 180 days |
| Audit log, sign-in history | 730 days |

Periods are set in `.env` (`RETENTION_*`). `make golive-check` warns if the
job has not run in the last day.
