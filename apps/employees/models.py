"""
Employees, face enrolment and consent.

Two things in this module carry real legal weight:

* :class:`FaceEnrollment.embedding` is a biometric template. It is sensitive
  personal data under the DPDP Act, the GDPR and US state biometric laws. It
  is not anonymous and must not be treated as "just a vector".
* :class:`ConsentRecord` is what makes processing lawful. The pipeline checks
  it: an employee without active consent is not matched, by
  :meth:`Employee.is_processable`.
"""

from __future__ import annotations

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from pgvector.django import HnswIndex, VectorField

from apps.common.models import SoftDeleteModel, TimeStampedModel, UUIDPrimaryKeyModel

# ArcFace (InsightFace buffalo_l) produces 512-dimension embeddings.
EMBEDDING_DIM = 512


class Employee(SoftDeleteModel, UUIDPrimaryKeyModel, TimeStampedModel):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        ON_LEAVE = "on_leave", "On leave"
        SUSPENDED = "suspended", "Suspended"
        EXITED = "exited", "Exited"

    employee_code = models.CharField(max_length=32, unique=True, db_index=True)
    full_name = models.CharField(max_length=150, db_index=True)

    department = models.ForeignKey(
        "org.Department",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="employees",
    )
    designation = models.ForeignKey(
        "org.Designation",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="employees",
    )

    date_of_joining = models.DateField(null=True, blank=True)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.ACTIVE, db_index=True
    )

    # Separate from consent on purpose: an employee may have consented and
    # still be excluded for an operational reason (e.g. enrolment quality too
    # poor to be reliable).
    is_trackable = models.BooleanField(
        default=True,
        help_text="Uncheck to exclude this person from matching entirely.",
    )

    home_zone = models.ForeignKey(
        "org.Zone",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="home_employees",
        help_text="Usual desk location. Used only to reduce report noise.",
    )

    class Meta:
        ordering = ["full_name"]
        indexes = [
            models.Index(fields=["status", "is_trackable"]),
            models.Index(fields=["department", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.full_name} [{self.employee_code}]"

    # --------------------------------------------------------------- consent

    @property
    def active_consent(self):
        return (
            self.consent_records.filter(withdrawn_at__isnull=True)
            .order_by("-consented_at")
            .first()
        )

    @property
    def has_active_consent(self) -> bool:
        return self.active_consent is not None

    def is_processable(self) -> bool:
        """Whether the pipeline may match this person at all.

        The gallery loader in the vision tier applies exactly this rule, so
        withdrawing consent stops processing within one gallery refresh
        (default 30 seconds) without anyone having to remember to act.
        """
        return (
            self.deleted_at is None
            and self.is_trackable
            and self.status in {self.Status.ACTIVE, self.Status.ON_LEAVE}
            and self.has_active_consent
            and self.enrollments.filter(is_active=True).exists()
        )

    @property
    def enrollment_count(self) -> int:
        return self.enrollments.filter(is_active=True).count()


class FaceEnrollment(UUIDPrimaryKeyModel, TimeStampedModel):
    """One enrolled face image and its embedding.

    Enrol **5-10 images per person** across poses and lighting. A single
    frontal image is the most common cause of poor real-world accuracy: the
    gallery then has no idea what the person looks like at 20 degrees off
    axis, which is most of what a doorway camera actually sees.
    """

    class Pose(models.TextChoices):
        FRONTAL = "frontal", "Frontal"
        LEFT = "left", "Turned left"
        RIGHT = "right", "Turned right"
        UP = "up", "Looking up"
        DOWN = "down", "Looking down"

    class Source(models.TextChoices):
        UPLOAD = "upload", "Uploaded by HR"
        KIOSK = "kiosk", "Captured at kiosk"
        REINFORCED = "reinforced", "Auto-reinforced from a confident match"

    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name="enrollments"
    )

    # Private bucket; only ever served through a short-lived signed URL.
    image = models.ImageField(upload_to="enrollments/%Y/%m/")
    embedding = VectorField(
        dimensions=EMBEDDING_DIM,
        help_text="ArcFace template. Biometric data - encrypted at rest at "
        "the storage layer and never exposed through the API.",
    )

    pose = models.CharField(max_length=10, choices=Pose.choices, default=Pose.FRONTAL)
    source = models.CharField(
        max_length=12, choices=Source.choices, default=Source.UPLOAD
    )

    # Populated by the enrolment quality gate. Bad images are rejected at
    # upload rather than being allowed to poison the gallery silently.
    quality_score = models.FloatField(
        validators=[MinValueValidator(0.0), MaxValueValidator(1.0)],
        help_text="Composite of face size, sharpness, pose and exposure.",
    )
    face_width_px = models.PositiveIntegerField()
    sharpness = models.FloatField(help_text="Variance of Laplacian.")
    brightness = models.FloatField()
    yaw_degrees = models.FloatField(default=0.0)
    pitch_degrees = models.FloatField(default=0.0)

    is_active = models.BooleanField(default=True, db_index=True)
    enrolled_by = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="enrollments_created",
    )
    model_version = models.CharField(
        max_length=50,
        default="buffalo_l",
        help_text="Embeddings from different models are not comparable; a "
        "model upgrade requires re-embedding the whole gallery.",
    )

    # Set when this image matched a *different* already-enrolled employee
    # above threshold - a duplicate, a twin, or a mislabelled upload.
    conflict_flagged = models.BooleanField(default=False)
    conflict_with = models.ForeignKey(
        Employee,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="enrollment_conflicts",
    )

    class Meta:
        ordering = ["employee", "-quality_score"]
        indexes = [
            models.Index(fields=["employee", "is_active"]),
            models.Index(fields=["model_version", "is_active"]),
            # Approximate nearest-neighbour index over the gallery. Cosine
            # distance, matching what the matcher uses.
            HnswIndex(
                name="face_embedding_hnsw",
                fields=["embedding"],
                m=16,
                ef_construction=64,
                opclasses=["vector_cosine_ops"],
            ),
        ]

    def __str__(self) -> str:
        return f"{self.employee.employee_code} / {self.pose} / q={self.quality_score:.2f}"


class ConsentRecord(TimeStampedModel):
    """Evidence that processing this person's biometrics is lawful.

    Keep the signed notice itself (``document``) - "we told everyone" is not
    evidence. ``policy_version`` matters because consent is to a specific
    notice; materially changing the purpose requires fresh consent.
    """

    class Method(models.TextChoices):
        WRITTEN = "written", "Signed paper form"
        PORTAL = "portal", "Accepted in the employee portal"
        EMAIL = "email", "Email acknowledgement"
        CONTRACT = "contract", "Employment contract clause"

    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name="consent_records"
    )
    policy_version = models.CharField(
        max_length=32, help_text="Version of the privacy notice consented to."
    )
    consented_at = models.DateTimeField(default=timezone.now)
    method = models.CharField(max_length=12, choices=Method.choices)
    document = models.FileField(
        upload_to="consent/%Y/", null=True, blank=True, help_text="Signed form or PDF."
    )

    recorded_by = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="consents_recorded",
    )

    withdrawn_at = models.DateTimeField(null=True, blank=True, db_index=True)
    withdrawal_note = models.TextField(blank=True)

    class Meta:
        ordering = ["-consented_at"]
        indexes = [models.Index(fields=["employee", "withdrawn_at"])]

    def __str__(self) -> str:
        state = "withdrawn" if self.withdrawn_at else "active"
        return f"{self.employee.employee_code} consent v{self.policy_version} ({state})"

    def withdraw(self, note: str = "", by=None) -> None:
        """Withdraw consent and stop processing.

        Deactivates the gallery entries immediately; the vision tier drops
        the person on its next gallery refresh.
        """
        self.withdrawn_at = timezone.now()
        self.withdrawal_note = note
        self.save(update_fields=["withdrawn_at", "withdrawal_note"])
        self.employee.enrollments.update(is_active=False)


class IdentityCorrection(TimeStampedModel):
    """"That was not me" - a reported mis-identification.

    Serves two purposes: it is the rectification right that privacy law
    requires, and it is the labelled data you need to tune thresholds and
    spot which cameras produce bad matches.
    """

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        ACCEPTED = "accepted", "Accepted - record corrected"
        REJECTED = "rejected", "Rejected - original stands"

    presence_interval = models.ForeignKey(
        "tracking.PresenceInterval",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="corrections",
    )
    was_employee = models.ForeignKey(
        Employee,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="corrections_against",
    )
    should_be_employee = models.ForeignKey(
        Employee,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="corrections_for",
        help_text="Leave blank if the correct subject is unknown.",
    )

    reported_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    reason = models.TextField()
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.OPEN, db_index=True
    )
    resolved_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_note = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"correction #{self.pk} ({self.status})"
