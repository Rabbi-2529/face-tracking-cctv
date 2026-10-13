"""
Tracking requests - the approval gate for following a named individual.

Why this app exists
-------------------
Searching for a specific person's movements is the highest-risk capability in
the system, and the one most likely to be misused. So it is not a search box.
It is a request with a written reason, an approver who is not the requester, a
start and end time, automatic expiry, and an audit trail.

The practical effect: HR can investigate a specific concern, on record, and
cannot browse the organisation. Zone-first questions ("who entered the server
room between 14:00 and 16:00") need no request at all and answer most real
investigations - see :mod:`apps.tracking.views`.
"""

from __future__ import annotations

import datetime as dt

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from apps.common.models import TimeStampedModel, UUIDPrimaryKeyModel


class TrackingRequest(UUIDPrimaryKeyModel, TimeStampedModel):
    class Scope(models.TextChoices):
        HISTORICAL = "historical", "Historical only"
        LIVE = "live", "Live follow only"
        BOTH = "both", "Live and historical"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending approval"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        EXPIRED = "expired", "Expired"
        REVOKED = "revoked", "Revoked"
        WITHDRAWN = "withdrawn", "Withdrawn by requester"

    class Purpose(models.TextChoices):
        SECURITY_INCIDENT = "security_incident", "Security incident"
        THEFT_INVESTIGATION = "theft", "Theft or loss investigation"
        HR_GRIEVANCE = "hr_grievance", "HR grievance or complaint"
        SAFETY = "safety", "Safety or welfare concern"
        ACCESS_VIOLATION = "access_violation", "Access control violation"
        AUDIT = "audit", "Compliance audit"
        OTHER = "other", "Other (explain in reason)"

    # Longest window an approver may grant in one go. A longer investigation
    # means repeated, re-justified approvals rather than one open-ended one.
    MAX_WINDOW = dt.timedelta(days=7)

    employee = models.ForeignKey(
        "employees.Employee",
        on_delete=models.CASCADE,
        related_name="tracking_requests",
        help_text="The subject of the request.",
    )

    requested_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="tracking_requests_made"
    )
    requested_at = models.DateTimeField(auto_now_add=True, db_index=True)

    purpose = models.CharField(max_length=24, choices=Purpose.choices)
    reason = models.TextField(
        help_text="Why this specific person needs to be tracked. Required, and "
        "shown to the approver, the subject on request, and any auditor."
    )
    scope = models.CharField(
        max_length=12, choices=Scope.choices, default=Scope.HISTORICAL
    )

    # The window of *data* the request covers, not the window of access.
    data_from = models.DateTimeField(
        help_text="Earliest movement data this request authorises access to."
    )
    data_to = models.DateTimeField(
        help_text="Latest movement data this request authorises access to."
    )

    # The window during which the requester may *use* the authorisation.
    start_at = models.DateTimeField(null=True, blank=True)
    end_at = models.DateTimeField(null=True, blank=True)

    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True
    )

    approved_by = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="tracking_requests_approved",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True)

    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    # Transparency toggle. Default is to tell the subject, because covert
    # monitoring is exactly what privacy law treats most harshly. Suppress it
    # only for an active investigation where notice would defeat the purpose,
    # and record who decided that.
    subject_notified = models.BooleanField(default=False)
    subject_notified_at = models.DateTimeField(null=True, blank=True)
    notification_suppressed = models.BooleanField(default=False)
    suppression_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-requested_at"]
        indexes = [
            models.Index(fields=["status", "-requested_at"]),
            models.Index(fields=["employee", "status"]),
            models.Index(fields=["requested_by", "-requested_at"]),
            # Serves the hot path in PermissionService.active_tracking_request.
            models.Index(
                fields=["employee", "start_at", "end_at"],
                condition=models.Q(status="approved"),
                name="idx_active_tracking_requests",
            ),
        ]

    def __str__(self) -> str:
        return f"track {self.employee.employee_code} by {self.requested_by.email} ({self.status})"

    # ------------------------------------------------------------ validation

    def clean(self):
        errors: dict[str, str] = {}

        if self.data_from and self.data_to and self.data_to <= self.data_from:
            errors["data_to"] = "Must be after data_from."

        if self.start_at and self.end_at:
            if self.end_at <= self.start_at:
                errors["end_at"] = "Must be after start_at."
            elif self.end_at - self.start_at > self.MAX_WINDOW:
                errors["end_at"] = (
                    f"An approval may not exceed {self.MAX_WINDOW.days} days. "
                    "Raise a new request if the investigation continues."
                )

        if self.notification_suppressed and not self.suppression_reason.strip():
            errors["suppression_reason"] = (
                "Suppressing notice to the subject requires a written reason."
            )

        if len(self.reason.strip()) < 20:
            errors["reason"] = (
                "Give a substantive reason (at least 20 characters). This is "
                "read by the approver and by auditors."
            )

        if errors:
            raise ValidationError(errors)

    # -------------------------------------------------------------- workflow

    def approve(self, approver, *, window_hours: int = 48, note: str = ""):
        """Approve the request. The approver may not be the requester."""
        if self.status != self.Status.PENDING:
            raise ValidationError(f"Cannot approve a request in state '{self.status}'.")
        if approver.pk == self.requested_by_id:
            raise ValidationError(
                "A tracking request cannot be approved by the person who raised it."
            )

        now = timezone.now()
        self.status = self.Status.APPROVED
        self.approved_by = approver
        self.approved_at = now
        self.start_at = now
        self.end_at = now + dt.timedelta(hours=window_hours)
        self.decision_note = note
        self.full_clean()
        self.save()
        return self

    def reject(self, approver, note: str = ""):
        if self.status != self.Status.PENDING:
            raise ValidationError(f"Cannot reject a request in state '{self.status}'.")
        self.status = self.Status.REJECTED
        self.approved_by = approver
        self.approved_at = timezone.now()
        self.decision_note = note
        self.save(
            update_fields=["status", "approved_by", "approved_at", "decision_note", "updated_at"]
        )
        return self

    def revoke(self, actor, note: str = ""):
        """End an active authorisation early."""
        if self.status != self.Status.APPROVED:
            raise ValidationError("Only an approved request can be revoked.")
        self.status = self.Status.REVOKED
        self.revoked_at = timezone.now()
        self.revoked_by = actor
        self.decision_note = (self.decision_note + "\n" + note).strip()
        self.save(
            update_fields=["status", "revoked_at", "revoked_by", "decision_note", "updated_at"]
        )
        # Close any live session immediately - the socket, not just the row.
        from apps.watchlist.live import terminate_sessions

        terminate_sessions(self.sessions.all())
        return self

    @property
    def is_active(self) -> bool:
        if self.status != self.Status.APPROVED:
            return False
        now = timezone.now()
        return bool(self.start_at and self.end_at and self.start_at <= now < self.end_at)

    @property
    def seconds_remaining(self) -> int:
        if not self.is_active:
            return 0
        return int((self.end_at - timezone.now()).total_seconds())


class TrackSession(TimeStampedModel):
    """One actual use of an approved request.

    Separating the authorisation from its use answers a question the request
    alone cannot: it was approved, but was it used, by whom, for how long,
    and how much did they look at?
    """

    request = models.ForeignKey(
        TrackingRequest, on_delete=models.CASCADE, related_name="sessions"
    )
    opened_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="track_sessions"
    )
    started_at = models.DateTimeField(auto_now_add=True, db_index=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    channel_name = models.CharField(
        max_length=255, blank=True, help_text="Channels name, for forced disconnect on revoke."
    )
    snapshots_viewed = models.PositiveIntegerField(default=0)
    intervals_viewed = models.PositiveIntegerField(default=0)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]
        indexes = [models.Index(fields=["request", "-started_at"])]

    def __str__(self) -> str:
        return f"session on {self.request_id} by {self.opened_by.email}"

    @property
    def duration_seconds(self) -> int:
        end = self.ended_at or timezone.now()
        return int((end - self.started_at).total_seconds())
