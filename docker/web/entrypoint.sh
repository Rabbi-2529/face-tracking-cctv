#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# One entrypoint for every Django-tier container. The first argument selects
# the role: migrate | web | worker | beat | aggregator | shell.
# ---------------------------------------------------------------------------
set -euo pipefail

ROLE="${1:-web}"

log() { printf '[entrypoint:%s] %s\n' "$ROLE" "$*" >&2; }

wait_for_db() {
  log "waiting for postgres..."
  local tries=0
  until python - <<'PY' 2>/dev/null
import os, sys, urllib.parse as up, psycopg
url = os.environ["DATABASE_URL"]
psycopg.connect(url, connect_timeout=3).close()
PY
  do
    tries=$((tries + 1))
    if [ "$tries" -ge 60 ]; then
      log "postgres did not become ready in time"
      exit 1
    fi
    sleep 2
  done
  log "postgres ready"
}

case "$ROLE" in

  migrate)
    wait_for_db
    log "applying migrations"
    python manage.py migrate --noinput
    log "collecting static files"
    python manage.py collectstatic --noinput --clear
    log "seeding roles and permission groups"
    python manage.py seed_roles
    # Optional bootstrap admin. Skipped entirely unless both vars are set.
    if [ -n "${BOOTSTRAP_ADMIN_EMAIL:-}" ] && [ -n "${BOOTSTRAP_ADMIN_PASSWORD:-}" ]; then
      log "ensuring bootstrap admin ${BOOTSTRAP_ADMIN_EMAIL}"
      python manage.py bootstrap_admin \
        --email "${BOOTSTRAP_ADMIN_EMAIL}" \
        --password "${BOOTSTRAP_ADMIN_PASSWORD}"
    fi
    log "migrate role complete"
    ;;

  web)
    wait_for_db
    if [ "${DJANGO_DEBUG:-0}" = "1" ]; then
      log "starting uvicorn with autoreload (DEBUG)"
      exec uvicorn config.asgi:application \
        --host 0.0.0.0 --port 8000 --reload \
        --reload-dir /app/apps --reload-dir /app/config
    fi
    log "starting uvicorn (${WEB_CONCURRENCY:-4} workers)"
    exec uvicorn config.asgi:application \
      --host 0.0.0.0 --port 8000 \
      --workers "${WEB_CONCURRENCY:-4}" \
      --proxy-headers --forwarded-allow-ips='*' \
      --no-server-header \
      --log-level "${UVICORN_LOG_LEVEL:-info}"
    ;;

  worker)
    wait_for_db
    log "starting celery worker"
    exec celery -A config worker \
      --loglevel="${CELERY_LOG_LEVEL:-info}" \
      --concurrency="${CELERY_CONCURRENCY:-4}" \
      --max-tasks-per-child=200 \
      -Q "${CELERY_QUEUES:-default,aggregation,reports,retention}"
    ;;

  beat)
    wait_for_db
    log "starting celery beat"
    # Remove a stale pid from an unclean shutdown; the schedule itself lives
    # in the database via django-celery-beat.
    rm -f /tmp/celerybeat.pid
    exec celery -A config beat \
      --loglevel="${CELERY_LOG_LEVEL:-info}" \
      --scheduler django_celery_beat.schedulers:DatabaseScheduler \
      --pidfile=/tmp/celerybeat.pid
    ;;

  aggregator)
    wait_for_db
    log "starting realtime aggregator (stream=${EVENT_STREAM:-events:detections})"
    exec python manage.py run_aggregator \
      --consumer "${AGGREGATOR_CONSUMER:-aggregator-1}" \
      --batch-size "${AGGREGATOR_BATCH_SIZE:-200}" \
      --block-ms "${AGGREGATOR_BLOCK_MS:-1000}"
    ;;

  shell)
    wait_for_db
    exec python manage.py shell
    ;;

  *)
    # Anything else is run verbatim, e.g. `docker compose run web pytest`.
    log "exec: $*"
    exec "$@"
    ;;
esac
