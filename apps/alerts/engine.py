"""
The alert engine: called by the aggregator and the camera health check.

Entry points, one per kind of signal:

* :func:`on_confirmed_presence` - a presence interval has become confirmed
  (opened confirmed, or upgraded from probable). Probable presence never
  reaches here, which is how "a probable match never alerts on its own" is
  enforced.
* :func:`on_unknown_sighting` - an unmatched face was clustered.
* :func:`on_camera_offline` - a camera stopped delivering frames.

Repeat triggers for the same thing within ``DEDUPE_WINDOW`` fold into one
alert with a growing ``occurrences`` count, so a person sitting in a
restricted room is one alert, not one per re-published sighting.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from apps.alerts.models import Alert, AlertRule

logger = logging.getLogger("cctv.alerts")

DEDUPE_WINDOW = dt.timedelta(minutes=15)
RULE_CACHE_SECONDS = 30

_cache: dict[str, object] = {"at": 0.0, "rules": []}


def _rules(rule_type: str) -> list[AlertRule]:
    """Enabled rules of one type, cached briefly - the aggregator calls this
    per confirmed interval, and rules change rarely."""
    if time.monotonic() - _cache["at"] > RULE_CACHE_SECONDS:
        _cache["rules"] = list(
            AlertRule.objects.filter(is_enabled=True)
            .select_related("zone")
            .prefetch_related(
                "authorised_departments", "authorised_employees", "applies_to_departments"
            )
        )
        _cache["at"] = time.monotonic()
    return [r for r in _cache["rules"] if r.rule_type == rule_type]


def clear_rule_cache() -> None:
    _cache["at"] = 0.0


def _zone_matches(rule: AlertRule, zone, *, restricted_only: bool) -> bool:
    if zone is None or zone.tracking_prohibited:
        return False
    if rule.zone_id is not None:
        return rule.zone_id == zone.pk
    return zone.is_restricted if restricted_only else True


def _local(when: dt.datetime, zone) -> dt.datetime:
    """``when`` in the building's own timezone - after hours is a local idea."""
    try:
        tz = ZoneInfo(zone.floor.building.timezone)
    except Exception:
        tz = timezone.get_current_timezone()
    return when.astimezone(tz)


# ------------------------------------------------------------- entry points


def on_confirmed_presence(interval) -> list[Alert]:
    """Evaluate restricted-zone and after-hours rules for one interval."""
    zone, employee = interval.zone, interval.employee
    raised: list[Alert] = []

    for rule in _rules(AlertRule.RuleType.RESTRICTED_ZONE):
        if not _zone_matches(rule, zone, restricted_only=True):
            continue
        if employee.department_id in {d.pk for d in rule.authorised_departments.all()}:
            continue
        if employee.pk in {e.pk for e in rule.authorised_employees.all()}:
            continue
        raised.append(
            _raise(
                rule,
                zone=zone,
                employee=employee,
                interval=interval,
                at=interval.entered_at,
                dedupe_key=f"r{rule.pk}:e{employee.pk}:z{zone.pk}",
                message=f"Entry to {zone.name} by someone not authorised for it",
                snapshot=interval.first_snapshot_key,
            )
        )

    for rule in _rules(AlertRule.RuleType.AFTER_HOURS):
        if not _zone_matches(rule, zone, restricted_only=False):
            continue
        departments = {d.pk for d in rule.applies_to_departments.all()}
        if departments and employee.department_id not in departments:
            continue
        if not rule.in_after_hours(_local(interval.entered_at, zone)):
            continue
        raised.append(
            _raise(
                rule,
                zone=zone,
                employee=employee,
                interval=interval,
                at=interval.entered_at,
                dedupe_key=f"r{rule.pk}:e{employee.pk}",
                message=f"Presence in {zone.name} after hours",
                snapshot=interval.first_snapshot_key,
            )
        )
    return raised


def on_unknown_sighting(cluster, detection, camera) -> list[Alert]:
    zone = camera.zone
    return [
        _raise(
            rule,
            zone=zone,
            camera=camera,
            unknown_cluster=cluster,
            at=detection.captured_at,
            dedupe_key=f"r{rule.pk}:c{cluster.pk}:z{zone.pk}",
            message=f"Unknown face in {zone.name}",
            snapshot=detection.snapshot_key or cluster.representative_snapshot_key,
        )
        for rule in _rules(AlertRule.RuleType.UNKNOWN_FACE)
        if _zone_matches(rule, zone, restricted_only=True)
    ]


def on_camera_offline(camera, message: str = "") -> list[Alert]:
    zone = camera.zone
    return [
        _raise(
            rule,
            zone=zone,
            camera=camera,
            at=timezone.now(),
            dedupe_key=f"r{rule.pk}:cam{camera.pk}",
            message=f"Camera {camera.code} offline - coverage gap in {zone.name}"
            + (f" ({message})" if message else ""),
        )
        for rule in _rules(AlertRule.RuleType.CAMERA_DOWN)
        if rule.zone_id is None or rule.zone_id == zone.pk
    ]


# ------------------------------------------------------------------ raising


@transaction.atomic
def _raise(
    rule: AlertRule,
    *,
    zone,
    at: dt.datetime,
    dedupe_key: str,
    message: str,
    employee=None,
    interval=None,
    camera=None,
    unknown_cluster=None,
    snapshot: str = "",
) -> Alert:
    existing = (
        Alert.objects.select_for_update()
        .filter(
            dedupe_key=dedupe_key,
            status__in=[Alert.Status.OPEN, Alert.Status.ACKNOWLEDGED],
            last_seen_at__gte=at - DEDUPE_WINDOW,
        )
        .order_by("-last_seen_at")
        .first()
    )
    if existing is not None:
        existing.occurrences += 1
        existing.last_seen_at = max(existing.last_seen_at, at)
        existing.save(update_fields=["occurrences", "last_seen_at", "updated_at"])
        return existing

    alert = Alert.objects.create(
        rule=rule,
        rule_type=rule.rule_type,
        severity=rule.severity,
        zone=zone,
        camera=camera,
        employee=employee,
        interval=interval,
        unknown_cluster=unknown_cluster,
        triggered_at=at,
        last_seen_at=at,
        dedupe_key=dedupe_key[:200],
        message=message[:300],
        evidence_snapshot_key=snapshot or "",
    )
    logger.warning("alert raised: %s [%s]", message, rule.name)
    transaction.on_commit(lambda: _broadcast(alert))
    return alert


def _broadcast(alert: Alert) -> None:
    """Push a new alert to open inboxes.

    No name and no image in the payload: consumers forward it only to users
    who may see that zone, and the inbox page decides what each viewer may
    see about who it concerns.
    """
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer

    layer = get_channel_layer()
    if layer is None:
        return
    try:
        async_to_sync(layer.group_send)(
            "alerts",
            {
                "type": "alert.raised",
                "alert": str(alert.uuid),
                "zone_id": alert.zone_id,
                "zone": alert.zone.name if alert.zone else "",
                "rule_type": alert.rule_type,
                "severity": alert.severity,
                "message": alert.message,
                "triggered_at": alert.triggered_at.isoformat(),
            },
        )
    except Exception:
        logger.debug("alert broadcast failed", exc_info=True)
