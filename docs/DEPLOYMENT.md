# Deployment guide - production

How to install the system for real use. The development setup in the README
is deliberately permissive (debug on, demo accounts, MFA off); production is
the opposite on every one of those points. Finish with `make golive-check`
showing no failures.

## 1. Hosts and network

| Host | Runs | Size (up to ~20 cameras) |
|---|---|---|
| App server | web, worker, beat, aggregator, Postgres, Redis | 8 vCPU, 16-32 GB RAM, NVMe |
| GPU server (can be the same host) | vision workers | RTX 3060 12 GB for 1-8 cameras; A4000 16 GB for 8-20 |

- Cameras on their own VLAN with **no route to the internet**. Only the
  vision host may reach them, on RTSP (554).
- Users reach the web app through a TLS reverse proxy (nginx, Traefik or the
  organisation's load balancer). Nothing else is exposed: Postgres, Redis
  and the vision host stay internal.
- All cameras and hosts use the **same NTP server**. Drifting camera clocks
  make room-to-room times nonsense.
- The dry run measured ~2.5 frames/s per camera on CPU. Use the GPU image
  for more than one camera.

## 2. Configuration (`.env`)

Start from `.env.example` (`make init` generates secrets). Required changes:

| Setting | Production value | Why |
|---|---|---|
| `DJANGO_DEBUG` | `0` | Debug pages leak settings and stack traces |
| `ENVIRONMENT` | `production` | Developers then need break-glass for identities |
| `WEB_REQUIREMENTS` | `base.txt` | No test tools or debug toolbar in the image |
| `DJANGO_ALLOWED_HOSTS` | the real DNS name | |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | `https://<that name>` | |
| `MFA_ENFORCED` | `1` (after admins enrol) | Second factor for privileged roles |
| `ASSETS_LOCAL` | `1` | Page libraries served from the server, not CDNs |
| `VISION_DB_USER` / `VISION_DB_PASSWORD` | `vision_ro` / strong | Vision workers can only read |
| `BOOTSTRAP_ADMIN_*` | remove after first start | |
| `S3_*` | your private bucket, or leave empty for a local volume | Snapshots |
| `BACKUP_PASSPHRASE` | set, stored in your secrets manager | Encrypted backups |

**Keep copies of `FIELD_ENCRYPTION_KEY`, `DJANGO_SECRET_KEY` and
`BACKUP_PASSPHRASE` outside this host** (secrets manager or sealed envelope).
Losing the encryption key makes every stored camera password unreadable;
losing the backup passphrase makes every backup useless.

## 3. Install

```bash
make init                       # .env with generated keys, builds images
# edit .env as in section 2
make up                         # db, redis, migrate, web, worker, beat, aggregator
docker compose --profile vision run --rm vision python scripts/download_models.py
docker compose exec web python manage.py grant_vision_ro --password '<strong>'
make vision-gpu                 # or `make vision` on CPU
```

Do **not** run `make demo` on a production database: it creates accounts with
a published password.

## 4. First administrator and accounts

1. Sign in as the bootstrap admin, change the password, enrol an
   authenticator (save the backup codes).
2. Create named Super Admin accounts for the real people; disable the
   bootstrap account; remove `BOOTSTRAP_ADMIN_*` from `.env`.
3. Create the other users under **Users & roles**. HR users need
   departments, Security Operators need zones.
4. Every privileged user signs in and enrols an authenticator. Then set
   `MFA_ENFORCED=1` and `make restart`.

## 5. Site set-up

Follow `docs/PILOT_RUNBOOK.md` "before day 1": buildings, floors and plans,
zones and doors, never-tracked rooms, departments and designations, cameras
(with vendors), alert rules. Check progress with:

```bash
make pilot-check
docker compose exec web python manage.py pilot_check --floor "First Floor"
```

## 6. Backups

```bash
make backup                      # daily, from cron or a systemd timer
make restore-test                # once now, then monthly
```

Example cron entry on the app host (03:00, after the nightly retention run):

```
0 3 * * * cd /opt/cctv && BACKUP_PASSPHRASE=... make backup >> /var/log/cctv-backup.log 2>&1
```

Copy `backups/` off the host (encrypted) to storage with its own retention.
Backups are subject to the same retention as the data: the script prunes
dumps older than `BACKUP_RETENTION_DAYS` (14 by default).

## 7. Go-live check

```bash
make golive-check
```

It must report **0 fail**. It checks debug, environment, secrets, MFA, host
names, the raw admin, local assets, Django's deployment checks, demo and
placeholder accounts, authenticators, superusers, backups, the nightly jobs,
the read-only vision account, and enabled test cameras.

## 8. Updating

```bash
git pull
make build
make migrate && make restart      # every Django service, not just web
make vision-gpu                   # if vision/ changed
make golive-check
```

A service left on old code fails against a migrated schema. The aggregator
replays events it failed on its next start, so nothing is lost - but nothing
is processed until it restarts.
