"""Celery application and the recurring schedule."""

import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("cctv")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# Queue routing keeps slow retention sweeps off the aggregation path, so a
# nightly purge cannot delay interval closing.
app.conf.task_routes = {
    "apps.tracking.tasks.enforce_retention": {"queue": "retention"},
    "apps.tracking.tasks.*": {"queue": "aggregation"},
    "apps.cameras.tasks.*": {"queue": "default"},
    "apps.watchlist.tasks.*": {"queue": "default"},
}

app.conf.beat_schedule = {
    # Closes presence intervals whose subject has not been seen for
    # PRESENCE_GAP_SECONDS, and emits the resulting movement events.
    "close-stale-presence-intervals": {
        "task": "apps.tracking.tasks.close_stale_intervals",
        "schedule": 30.0,
        "options": {"queue": "aggregation", "expires": 25},
    },
    # Marks cameras that have stopped delivering frames and raises an alert.
    "check-camera-health": {
        "task": "apps.cameras.tasks.check_camera_health",
        "schedule": 60.0,
        "options": {"queue": "default", "expires": 55},
    },
    # Expires approved tracking requests whose window has passed. Runs often
    # so an expired authorisation cannot be used for long.
    "expire-tracking-requests": {
        "task": "apps.watchlist.tasks.expire_tracking_requests",
        "schedule": 60.0,
        "options": {"queue": "default", "expires": 55},
    },
    # Retention enforcement. Deliberately a scheduled job, not a policy
    # document: raw detections, snapshots and audit rows all age out.
    "enforce-retention": {
        "task": "apps.tracking.tasks.enforce_retention",
        "schedule": crontab(hour=3, minute=30),
        "options": {"queue": "retention"},
    },
    # Rolls yesterday's presence intervals into daily per-employee summaries
    # so reports never scan the raw tables.
    "build-daily-summaries": {
        "task": "apps.tracking.tasks.build_daily_summaries",
        "schedule": crontab(hour=1, minute=0),
        "options": {"queue": "aggregation"},
    },
}


@app.task(bind=True, ignore_result=True)
def debug_task(self) -> str:
    return f"request: {self.request!r}"
