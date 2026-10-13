"""
The tracking data model.

Three layers, written by the realtime aggregator in this order:

1. :class:`Detection` - raw, one row per matched face per sampled frame.
   High volume, short retention, Developer-only visibility.
2. :class:`PresenceInterval` - "this person was in this zone from X to Y".
   This is what every UI and report queries. Never query Detection.
3. :class:`MovementEvent` - "this person went from zone A to zone B".
   Validated against the zone adjacency graph before being written as fact.

The separation is what keeps the system honest: raw matches are noisy and
per-frame, while what a human is shown is aggregated, confidence-scored and
checked for physical plausibility.
"""

from __future__ import annotations

from django.db import models
from pgvector.django import VectorField

from apps.common.models import TimeStampedModel, UUIDPrimaryKeyModel
from apps.employees.models import EMBEDDING_DIM


class MatchConfidence(models.TextChoices):
    """Three-way outcome, not a boolean.

    ``PROBABLE`` exists so the UI can show uncertainty honestly. A probable
    match is never sufficient on its own to raise an alert or to support an
    HR action.
    """

    CONFIRMED = "confirmed", "Confirmed"
    PROBABLE = "probable", "Probable"
    UNKNOWN = "unknown", "Unknown"


class Detection(models.Model):
    """One matched (or unmatched) face in one sampled frame.

    Partitioned by day in production and aged out by the retention job after
    ``RETENTION["detection_days"]``. Visible only to the Developer role: it
    contains raw biometric vectors.
    """

    camera = models.ForeignKey(
        "cameras.Camera", on_delete=models.CASCADE, related_name="detections"
    )
    captured_at = models.DateTimeField(db_index=True)
    ingested_at = models.DateTimeField(auto_now_add=True)

    # In-camera track id from ByteTrack. All detections sharing a track id
    # are the same physical person within one camera view, which is what
    # makes track-level voting possible.
    track_id = models.CharField(max_length=64, db_index=True)
    frame_index = models.BigIntegerField(default=0)

    # [x1, y1, x2, y2] in pixels.
    bbox = models.JSONField(default=list)
    face_width_px = models.PositiveIntegerField(default=0)
    quality_score = models.FloatField(default=0.0)

    # Dropped by the retention job well before the row itself expires - the
    # embedding is the sensitive part and is only needed to re-run matching.
    embedding = VectorField(dimensions=EMBEDDING_DIM, null=True, blank=True)

    matched_employee = models.ForeignKey(
        "employees.Employee",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="detections",
    )
    match_score = models.FloatField(
        null=True, blank=True, help_text="Cosine similarity to the matched gallery entry."
    )
    confidence = models.CharField(
        max_length=10, choices=MatchConfidence.choices, default=MatchConfidence.UNKNOWN
    )

    unknown_cluster = models.ForeignKey(
        "UnknownFaceCluster",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="detections",
    )

    snapshot_key = models.CharField(
        max_length=255,
        blank=True,
        help_text="Object-storage key. Served only via a short-lived signed URL.",
    )

    class Meta:
        ordering = ["-captured_at"]
        indexes = [
            models.Index(fields=["camera", "-captured_at"]),
            models.Index(fields=["matched_employee", "-captured_at"]),
            models.Index(fields=["track_id", "camera"]),
            models.Index(fields=["confidence", "-captured_at"]),
        ]

    def __str__(self) -> str:
        who = self.matched_employee.employee_code if self.matched_employee else "unknown"
        return f"{who} @ {self.camera.code} {self.captured_at:%H:%M:%S}"


class PresenceInterval(TimeStampedModel):
    """A continuous period one person spent in one zone.

    Built by merging detections of the same employee in the same zone
    separated by less than ``PipelineConfig.presence_gap_seconds``.
    ``exited_at`` is null while the interval is still open (the person is
    currently there) - that is what the live occupancy board reads.
    """

    employee = models.ForeignKey(
        "employees.Employee",
        on_delete=models.CASCADE,
        related_name="presence_intervals",
    )
    zone = models.ForeignKey(
        "org.Zone", on_delete=models.CASCADE, related_name="presence_intervals"
    )

    entered_at = models.DateTimeField(db_index=True)
    exited_at = models.DateTimeField(null=True, blank=True, db_index=True)
    # Timestamp of the most recent detection folded into this interval. The
    # interval is closed when this falls further behind than the configured
    # presence gap, and `exited_at` is then set to this value - not to the
    # time the sweeper happened to run.
    last_seen_at = models.DateTimeField(db_index=True)

    detection_count = models.PositiveIntegerField(default=1)
    confidence = models.CharField(
        max_length=10, choices=MatchConfidence.choices, default=MatchConfidence.PROBABLE
    )
    mean_match_score = models.FloatField(null=True, blank=True)

    # Evidence. Two snapshots per interval is enough to verify a claim
    # without storing every frame.
    first_snapshot_key = models.CharField(max_length=255, blank=True)
    last_snapshot_key = models.CharField(max_length=255, blank=True)

    # True when the interval was inferred rather than observed - e.g. the
    # person must have crossed a corridor with no identity camera. Shown
    # distinctly in the UI so inference is never mistaken for observation.
    is_inferred = models.BooleanField(default=False)

    class Meta:
        ordering = ["-entered_at"]
        indexes = [
            models.Index(fields=["employee", "-entered_at"]),
            models.Index(fields=["zone", "-entered_at"]),
            # Serves the live board: open intervals only.
            models.Index(
                fields=["zone", "entered_at"],
                condition=models.Q(exited_at__isnull=True),
                name="idx_open_presence_by_zone",
            ),
            models.Index(
                fields=["employee", "entered_at"],
                condition=models.Q(exited_at__isnull=True),
                name="idx_open_presence_by_emp",
            ),
        ]

    def __str__(self) -> str:
        end = f"{self.exited_at:%H:%M}" if self.exited_at else "now"
        return f"{self.employee.employee_code} in {self.zone.code} {self.entered_at:%H:%M}-{end}"

    @property
    def duration_seconds(self) -> int | None:
        if self.exited_at is None:
            return None
        return int((self.exited_at - self.entered_at).total_seconds())

    @property
    def is_open(self) -> bool:
        return self.exited_at is None


class MovementEvent(TimeStampedModel):
    """A zone-to-zone transition.

    ``is_plausible`` is checked against :class:`org.ZoneAdjacency`. An
    implausible transition is written with the flag set rather than
    discarded: silently dropping it would hide a systematic matching problem,
    and silently keeping it would present a false movement as fact. It goes
    into a review queue instead.
    """

    class ReviewStatus(models.TextChoices):
        NONE = "none", "No review needed"
        PENDING = "pending", "Pending review"
        CONFIRMED = "confirmed", "Confirmed correct"
        REJECTED = "rejected", "Rejected - bad match"

    employee = models.ForeignKey(
        "employees.Employee", on_delete=models.CASCADE, related_name="movements"
    )
    from_zone = models.ForeignKey(
        "org.Zone",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="movements_out",
        help_text="Null for the first sighting of the day (arrival).",
    )
    to_zone = models.ForeignKey(
        "org.Zone", on_delete=models.CASCADE, related_name="movements_in"
    )

    occurred_at = models.DateTimeField(db_index=True)
    transit_seconds = models.PositiveIntegerField(
        null=True, blank=True, help_text="Gap between leaving from_zone and entering to_zone."
    )
    prev_dwell_seconds = models.PositiveIntegerField(null=True, blank=True)

    confidence = models.CharField(
        max_length=10, choices=MatchConfidence.choices, default=MatchConfidence.PROBABLE
    )
    is_plausible = models.BooleanField(
        default=True,
        db_index=True,
        help_text="False when no zone adjacency permits this transition in this time.",
    )
    implausible_reason = models.CharField(max_length=200, blank=True)
    review_status = models.CharField(
        max_length=10, choices=ReviewStatus.choices, default=ReviewStatus.NONE
    )

    source_interval = models.ForeignKey(
        PresenceInterval,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="movement_events",
    )

    class Meta:
        ordering = ["-occurred_at"]
        indexes = [
            models.Index(fields=["employee", "-occurred_at"]),
            models.Index(fields=["to_zone", "-occurred_at"]),
            models.Index(fields=["is_plausible", "review_status"]),
        ]

    def __str__(self) -> str:
        src = self.from_zone.code if self.from_zone else "(arrival)"
        return f"{self.employee.employee_code}: {src} -> {self.to_zone.code} @ {self.occurred_at:%H:%M:%S}"


class UnknownFaceCluster(UUIDPrimaryKeyModel, TimeStampedModel):
    """Repeated sightings of a face that matches nobody enrolled.

    Clustering rather than discarding gives you two things: a visitor who
    appears all week can be labelled once, and a genuinely unknown person in
    a restricted zone becomes visible instead of being lost in noise.
    """

    class Resolution(models.TextChoices):
        UNRESOLVED = "unresolved", "Unresolved"
        VISITOR = "visitor", "Visitor"
        CONTRACTOR = "contractor", "Contractor"
        EMPLOYEE = "employee", "Employee (enrol them)"
        STAFF_NON_CONSENTING = "non_consenting", "Non-consenting staff - do not enrol"
        FALSE_DETECTION = "false_detection", "Not a real face"

    centroid_embedding = VectorField(dimensions=EMBEDDING_DIM)
    first_seen = models.DateTimeField(db_index=True)
    last_seen = models.DateTimeField(db_index=True)
    sighting_count = models.PositiveIntegerField(default=1)

    zones_seen = models.ManyToManyField("org.Zone", blank=True, related_name="unknown_clusters")
    representative_snapshot_key = models.CharField(max_length=255, blank=True)

    resolution = models.CharField(
        max_length=20,
        choices=Resolution.choices,
        default=Resolution.UNRESOLVED,
        db_index=True,
    )
    resolved_as_employee = models.ForeignKey(
        "employees.Employee",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    label = models.CharField(max_length=100, blank=True)
    resolved_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-last_seen"]
        indexes = [models.Index(fields=["resolution", "-last_seen"])]

    def __str__(self) -> str:
        name = self.label or f"cluster {str(self.uuid)[:8]}"
        return f"{name} ({self.sighting_count} sightings)"


class DailySummary(TimeStampedModel):
    """Per-employee per-day rollup, built nightly.

    Reports read this instead of scanning presence intervals, which keeps
    dashboards fast once the raw tables are large.
    """

    employee = models.ForeignKey(
        "employees.Employee", on_delete=models.CASCADE, related_name="daily_summaries"
    )
    date = models.DateField(db_index=True)

    first_seen_at = models.DateTimeField(null=True, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    first_seen_zone = models.ForeignKey(
        "org.Zone", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    total_zones_visited = models.PositiveIntegerField(default=0)
    total_movements = models.PositiveIntegerField(default=0)
    present_seconds = models.PositiveIntegerField(default=0)
    # {zone_code: seconds}
    dwell_by_zone = models.JSONField(default=dict, blank=True)

    restricted_zone_entries = models.PositiveIntegerField(default=0)
    after_hours_seconds = models.PositiveIntegerField(default=0)
    low_confidence_ratio = models.FloatField(
        default=0.0,
        help_text="Share of the day's intervals that were only 'probable'. "
        "A high value means this person's enrolment or cameras need attention.",
    )

    class Meta:
        ordering = ["-date"]
        constraints = [
            models.UniqueConstraint(fields=["employee", "date"], name="uniq_daily_summary")
        ]
        indexes = [models.Index(fields=["date", "employee"])]
        verbose_name_plural = "daily summaries"

    def __str__(self) -> str:
        return f"{self.employee.employee_code} {self.date}"


class PurgeRequest(UUIDPrimaryKeyModel, TimeStampedModel):
    """Early deletion of tracking data, under dual authorisation.

    Data normally leaves only through the nightly retention job. Sometimes it
    must go sooner - an erasure request, data captured in error, an incident
    period that must not be kept. That is irreversible and touches biometric
    data, so it takes two people: one Super Admin requests it with a reason,
    a *different* Super Admin approves, and only approval executes it.

    The request row and the audit log survive the purge: the record of what
    was deleted, why and by whom must outlive the data itself. The audit log
    can never be purged this way.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Awaiting second approval"
        EXECUTED = "executed", "Approved and executed"
        REJECTED = "rejected", "Rejected"
        CANCELLED = "cancelled", "Cancelled by requester"
        EXPIRED = "expired", "Expired without a decision"

    class Kind(models.TextChoices):
        MOVEMENT = "movement", "Movement history (presence, movements, daily summaries)"
        DETECTIONS = "detections", "Raw detections"
        SNAPSHOTS = "snapshots", "Snapshot images"
        ALERTS = "alerts", "Alerts"
        FACE_TEMPLATES = "face_templates", "Enrolled face images and templates"

    # Kinds that only make sense for one person.
    PERSON_ONLY_KINDS = frozenset({Kind.FACE_TEMPLATES})
    EXPIRY_DAYS = 7

    employee = models.ForeignKey(
        "employees.Employee",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="purge_requests",
        help_text="Blank: everyone's data in the date range.",
    )
    # Kept as text so the record stays readable after the employee is gone.
    employee_code = models.CharField(max_length=32, blank=True)
    date_from = models.DateField(null=True, blank=True)
    date_to = models.DateField(null=True, blank=True)
    kinds = models.JSONField(default=list)
    reason = models.TextField()

    requested_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="purges_requested"
    )
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    decided_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="purges_decided"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True)

    preview = models.JSONField(default=dict, blank=True, help_text="Counts when requested.")
    result = models.JSONField(default=dict, blank=True, help_text="Counts actually deleted.")
    executed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        who = self.employee_code or "everyone"
        return f"purge {who} {self.date_from or '...'}-{self.date_to or '...'} ({self.status})"

    @property
    def scope_label(self) -> str:
        who = self.employee_code or "Everyone"
        if self.date_from or self.date_to:
            return f"{who}, {self.date_from or 'start'} to {self.date_to or 'today'}"
        return f"{who}, all retained data"

    def kind_labels(self) -> list[str]:
        labels = dict(self.Kind.choices)
        return [labels.get(k, k) for k in self.kinds]
