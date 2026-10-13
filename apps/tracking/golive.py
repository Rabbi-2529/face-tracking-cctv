"""
Go-live checks: what must be true before the system is used for real.

The pilot readiness check (:mod:`apps.tracking.pilot`) asks "can the pilot
start?". This asks the rollout question: is this installation safe to put in
front of real people - production settings, no demo accounts, MFA on,
backups taken, retention running. Run as ``manage.py golive_check``.

A FAIL here means "do not go live": each one is either a security hole
(debug mode, demo passwords, no MFA) or a broken promise (no backups, no
retention).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from apps.tracking.pilot import FAIL, PASS, WARN, Check, _names

PLACEHOLDERS = ("replace-me", "change-me", "change-this", "changeme", "example")


def _placeholder(value: str) -> bool:
    lowered = (value or "").lower()
    return not lowered or any(p in lowered for p in PLACEHOLDERS)


def check_settings() -> list[Check]:
    checks = []
    debug = settings.DEBUG
    checks.append(Check("Settings", "Debug mode off", FAIL if debug else PASS,
                        "DJANGO_DEBUG is on." if debug else "DJANGO_DEBUG is off.",
                        "Set DJANGO_DEBUG=0. Debug pages leak settings and stack traces." if debug else ""))

    environment = getattr(settings, "ENVIRONMENT", "")
    checks.append(Check("Settings", "Environment is production", PASS if environment == "production" else FAIL,
                        f"ENVIRONMENT={environment}.",
                        "" if environment == "production" else
                        "Set ENVIRONMENT=production; outside it, Developers hold identity access without break-glass."))

    secret = settings.SECRET_KEY or ""
    weak_secret = _placeholder(secret) or len(secret) < 50
    checks.append(Check("Settings", "Secret key is real", FAIL if weak_secret else PASS,
                        "DJANGO_SECRET_KEY is a placeholder or short." if weak_secret else "Set, 50+ characters.",
                        "Generate one: python -c \"import secrets;print(secrets.token_urlsafe(64))\"" if weak_secret else ""))

    key = str(getattr(settings, "FIELD_ENCRYPTION_KEY", "") or "")
    weak_key = _placeholder(key) or len(key) < 40
    checks.append(Check("Settings", "Field encryption key is real", FAIL if weak_key else PASS,
                        "FIELD_ENCRYPTION_KEY is a placeholder." if weak_key
                        else "Set. Keep a copy outside this host - without it no camera password can be read.",
                        "Generate a Fernet key and store it in your secrets manager." if weak_key else ""))

    mfa = getattr(settings, "MFA_ENFORCED", False)
    checks.append(Check("Settings", "MFA enforced", PASS if mfa else FAIL,
                        "MFA_ENFORCED is on." if mfa else "MFA_ENFORCED is off.",
                        "" if mfa else "Set MFA_ENFORCED=1 once every privileged user has enrolled."))

    hosts = [h for h in settings.ALLOWED_HOSTS if h not in ("localhost", "127.0.0.1", "[::1]")]
    checks.append(Check("Settings", "Real host name configured", PASS if hosts else WARN,
                        f"ALLOWED_HOSTS: {', '.join(settings.ALLOWED_HOSTS)}.",
                        "" if hosts else "Add the server's DNS name to DJANGO_ALLOWED_HOSTS and CSRF_TRUSTED_ORIGINS."))

    admin = getattr(settings, "DJANGO_ADMIN_ENABLED", False)
    checks.append(Check("Settings", "Raw Django admin off", WARN if admin else PASS,
                        "DJANGO_ADMIN_ENABLED is on." if admin else "Off.",
                        "Turn it off; it bypasses the panel's validation and warnings." if admin else ""))

    local_assets = getattr(settings, "ASSETS_LOCAL", False)
    checks.append(Check("Settings", "Page libraries served locally", PASS if local_assets else WARN,
                        "Served from this server." if local_assets else "Loaded from public CDNs.",
                        "" if local_assets else
                        "Run `python scripts/vendor_assets.py` and set ASSETS_LOCAL=1 - needed if the server has no internet."))

    from django.core.checks import WARNING, run_checks

    issues = [m for m in run_checks(include_deployment_checks=True) if m.level >= WARNING]
    checks.append(Check("Settings", "Django deployment checks", WARN if issues else PASS,
                        f"{len(issues)} issue(s): {_names([m.id for m in issues])}." if issues else "No issues.",
                        "Run `python manage.py check --deploy` for details." if issues else ""))
    return checks


def check_accounts() -> list[Check]:
    from django_otp.plugins.otp_totp.models import TOTPDevice

    from apps.accounts.models import User
    from apps.common.roles import MFA_REQUIRED_ROLES

    checks = []
    active = User.objects.filter(is_active=True)
    demo = list(active.filter(email__iendswith="@demo.local").values_list("email", flat=True))
    checks.append(Check("Accounts", "No demo accounts", FAIL if demo else PASS,
                        f"{len(demo)} demo account(s) with a published password: {_names(demo)}." if demo
                        else "None active.",
                        "Disable them under Users & roles, or rebuild without `seed_demo`." if demo else ""))

    placeholder_admins = list(active.filter(email__iendswith="@yourcompany.com").values_list("email", flat=True))
    if placeholder_admins:
        checks.append(Check("Accounts", "Bootstrap admin replaced", WARN,
                            f"Placeholder bootstrap account still active: {_names(placeholder_admins)}.",
                            "Create named admin accounts, disable this one, and clear BOOTSTRAP_ADMIN_* from .env."))

    privileged = list(active.filter(role__in=list(MFA_REQUIRED_ROLES)))
    enrolled = set(TOTPDevice.objects.filter(confirmed=True).values_list("user_id", flat=True))
    missing = [u.email for u in privileged if u.pk not in enrolled]
    checks.append(Check("Accounts", "Privileged users have authenticators", FAIL if missing else PASS,
                        f"{len(missing)} of {len(privileged)} without one: {_names(missing)}." if missing
                        else f"All {len(privileged)} enrolled.",
                        "They cannot work once MFA is enforced; have them enrol from My account." if missing else ""))

    superusers = list(active.filter(is_superuser=True).values_list("email", flat=True))
    checks.append(Check("Accounts", "No Django superusers", WARN if superusers else PASS,
                        f"Superuser flag set on: {_names(superusers)}." if superusers else "None.",
                        "Use the Super Admin role instead; the flag is a broad bypass." if superusers else ""))

    temp = list(active.filter(must_change_password=True).values_list("email", flat=True))
    if temp:
        checks.append(Check("Accounts", "No temporary passwords outstanding", WARN,
                            f"Still on a temporary password: {_names(temp)}.",
                            "Make sure each person has signed in and set their own password."))
    return checks


def check_data_protection() -> list[Check]:
    from django.db import connection
    from django_celery_beat.models import PeriodicTask

    checks = []

    backup_dir = Path(getattr(settings, "BACKUP_DIR", Path(settings.BASE_DIR) / "backups"))
    dumps = sorted(backup_dir.glob("cctv-*.dump*"), key=lambda p: p.stat().st_mtime, reverse=True) if backup_dir.exists() else []
    if not dumps:
        checks.append(Check("Data protection", "Recent backup", FAIL, "No backups in ./backups.",
                            "Run `make backup`, schedule it daily, and run `make restore-test` once."))
    else:
        newest = dumps[0]
        age = timezone.now() - dt.datetime.fromtimestamp(newest.stat().st_mtime, tz=dt.UTC)
        hours = age.total_seconds() / 3600
        encrypted = newest.name.endswith(".enc")
        status = FAIL if hours > 72 else WARN if hours > 26 or not encrypted else PASS
        checks.append(Check("Data protection", "Recent backup", status,
                            f"Newest: {newest.name}, {hours:.0f} h old, {'encrypted' if encrypted else 'NOT encrypted'}.",
                            "" if status == PASS else
                            "Schedule `make backup` daily with BACKUP_PASSPHRASE set; keep copies off this host."))

    for name, label in (("enforce-retention", "Retention job scheduled"), ("build-daily-summaries", "Daily summaries scheduled")):
        task = PeriodicTask.objects.filter(name=name).first()
        if task is None or not task.enabled:
            checks.append(Check("Data protection", label, FAIL, "Not scheduled.",
                                "Start the beat container; it registers the schedule."))
            continue
        ran = task.last_run_at
        if ran is None:
            checks.append(Check("Data protection", label, WARN, "Scheduled, has not run yet.",
                                "Check again after its first nightly run."))
        elif timezone.now() - ran > dt.timedelta(hours=26):
            checks.append(Check("Data protection", label, FAIL, f"Last ran {ran:%Y-%m-%d %H:%M}.",
                                "Check the worker and beat containers."))
        else:
            checks.append(Check("Data protection", label, PASS, f"Last ran {timezone.localtime(ran):%Y-%m-%d %H:%M}."))

    with connection.cursor() as cur:
        cur.execute(
            "SELECT rolcanlogin, has_table_privilege('vision_ro', 'cameras_camera', 'SELECT') "
            "FROM pg_roles WHERE rolname = 'vision_ro'"
        )
        row = cur.fetchone()
    granted = bool(row and row[0] and row[1])
    checks.append(Check("Data protection", "Read-only database account for vision workers", PASS if granted else WARN,
                        "vision_ro exists with its grants." if granted else "vision_ro is missing its grants.",
                        "" if granted else
                        "Run `manage.py grant_vision_ro --password ...` and set VISION_DB_USER=vision_ro."))

    from apps.cameras.models import Camera, CameraVendor

    test_cams = list(
        Camera.objects.alive().filter(is_enabled=True, vendor__connection=CameraVendor.Connection.FILE)
        .values_list("code", flat=True)
    )
    if test_cams:
        checks.append(Check("Data protection", "No test cameras enabled", WARN,
                            f"Video-file test camera(s) enabled: {_names(test_cams)}.",
                            "Disable them; their sightings would mix with real ones."))
    return checks


def golive_readiness() -> dict:
    checks = check_settings() + check_accounts() + check_data_protection()
    counts = {s: sum(1 for c in checks if c.status == s) for s in (PASS, WARN, FAIL)}
    return {"checks": checks, "counts": counts, "ready": counts[FAIL] == 0, "at": timezone.now()}
