"""
Alert rules and the alerts they raise.

Four kinds of rule, matching the plan's scope and no further:

* **Restricted zone** - a confirmed sighting of someone not authorised for a
  restricted room.
* **After hours** - a confirmed sighting inside a configured night or weekend
  window.
* **Unknown face** - a face that matches nobody enrolled, seen in a
  restricted room.
* **Camera down** - a camera stops delivering frames, which is a coverage gap.

The rule that matters most is one this module does not need code for: a
*probable* match never raises an alert. The aggregator only asks the engine
about confirmed presence, so a weak match cannot put someone's name on a
security alert.
"""

from __future__ import annotations

import datetime as dt

from django.core.exceptions import ValidationError
from django.db import models

from apps.common.models import TimeStampedModel, UUIDPrimaryKeyModel


class AlertRule(TimeStampedModel):
    class RuleType(models.TextChoices):
        RESTRICTED_ZONE = "restricted_zone", "Unauthorised entry to a restricted zone"
        AFTER_HOURS = "after_hours", "Presence after hours"
        UNKNOWN_FACE = "unknown_face", "Unknown face in a restricted zone"
        CAMERA_DOWN = "camera_down", "Camera offline"

    class Severity(models.TextChoices):
        INFO = "info", "Info"
        WARNING = "warning", "Warning"
        CRITICAL = "critical", "Critical"

    name = models.CharField(max_length=100)
    rule_type = models.CharField(max_length=20, choices=RuleType.choices)
    severity = models.CharField(
        max_length=10, choices=Severity.choices, default=Severity.WARNING
    )
    is_enabled = models.BooleanField(default=True, db_index=True)

    zone = models.ForeignKey(
        "org.Zone",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="alert_rules",
        help_text="Limit the rule to one zone. Blank: every zone the rule type "
        "applies to (for restricted-zone and unknown-face rules, every zone "
        "marked restricted).",
    )

    # Restricted-zone rules: who may enter without raising an alert.
    authorised_departments = models.ManyToManyField(
        "org.Department",
        blank=True,
        related_name="+",
        help_text="Members of these departments may enter.",
    )
    authorised_employees = models.ManyToManyField(
        "employees.Employee",
        blank=True,
        related_name="+",
        help_text="These people may enter, whatever their department.",
    )

    # After-hours rules.
    applies_to_departments = models.ManyToManyField(
        "org.Department",
        blank=True,
        related_name="+",
        help_text="After-hours rules only: limit to these departments. Blank: everyone.",
    )
    after_hours_start = models.TimeField(
        null=True, blank=True, help_text="Start of the after-hours window, e.g. 20:00."
    )
    after_hours_end = models.TimeField(
        null=True,
        blank=True,
        help_text="End of the window, e.g. 07:00. A window may cross midnight.",
    )
    weekends_count = models.BooleanField(
        default=True, help_text="Treat all of Saturday and Sunday as after hours."
    )

    created_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["rule_type", "name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.get_rule_type_display()})"

    def clean(self):
        if self.rule_type == self.RuleType.AFTER_HOURS:
            if self.after_hours_start is None or self.after_hours_end is None:
                if not self.weekends_count:
                    raise ValidationError(
                        "An after-hours rule needs a time window, weekends, or both."
                    )
            elif self.after_hours_start == self.after_hours_end:
                raise ValidationError(
                    {"after_hours_end": "Start and end must differ."}
                )
        if self.zone_id and self.zone.tracking_prohibited:
            raise ValidationError(
                {"zone": "Never-tracked zones produce no sightings to alert on."}
            )

    def in_after_hours(self, local: dt.datetime) -> bool:
        """Is this local time inside the rule's after-hours window?"""
        if self.weekends_count and local.weekday() >= 5:
            return True
        start, end = self.after_hours_start, self.after_hours_end
        if start is None or end is None:
            return False
        now = local.time()
        if start < end:
            return start <= now < end
        # Crosses midnight, e.g. 20:00 - 07:00.
        return now >= start or now < end


class Alert(UUIDPrimaryKeyModel, TimeStampedModel):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        ACKNOWLEDGED = "acknowledged", "Acknowledged"
        RESOLVED = "resolved", "Resolved"

    rule = models.ForeignKey(
        AlertRule, null=True, blank=True, on_delete=models.SET_NULL, related_name="alerts"
    )
    # Copied from the rule so an alert stays readable if the rule changes.
    rule_type = models.CharField(max_length=20, choices=AlertRule.RuleType.choices)
    severity = models.CharField(max_length=10, choices=AlertRule.Severity.choices)

    zone = models.ForeignKey(
        "org.Zone", null=True, blank=True, on_delete=models.SET_NULL, related_name="alerts"
    )
    camera = models.ForeignKey(
        "cameras.Camera", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    employee = models.ForeignKey(
        "employees.Employee",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="alerts",
    )
    interval = models.ForeignKey(
        "tracking.PresenceInterval",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="alerts",
    )
    unknown_cluster = models.ForeignKey(
        "tracking.UnknownFaceCluster",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="alerts",
    )

    triggered_at = models.DateTimeField(db_index=True)
    last_seen_at = models.DateTimeField()
    occurrences = models.PositiveIntegerField(default=1)
    # Repeat triggers inside the dedupe window fold into one alert.
    dedupe_key = models.CharField(max_length=200, db_index=True)

    # Written without anyone's name: who the alert is about is shown only to
    # viewers allowed to see identities, from ``employee``.
    message = models.CharField(max_length=300)
    evidence_snapshot_key = models.CharField(max_length=255, blank=True)

    status = models.CharField(
        max_length=14, choices=Status.choices, default=Status.OPEN, db_index=True
    )
    acknowledged_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["-triggered_at"]
        indexes = [
            models.Index(fields=["status", "-triggered_at"]),
            models.Index(fields=["zone", "status"]),
            models.Index(fields=["dedupe_key", "-last_seen_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_rule_type_display()} @ {self.triggered_at:%Y-%m-%d %H:%M} ({self.status})"
