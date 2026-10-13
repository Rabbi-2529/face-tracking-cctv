"""
Cameras and their health.

Dahua / Hikvision / generic ONVIF devices are all addressed the same way
here: an RTSP URL plus a service account. Credentials are stored encrypted
(see :mod:`apps.common.fields`) because an RTSP URL is a working credential
for a device on your network.
"""

from __future__ import annotations

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from apps.common.fields import EncryptedCharField, EncryptedTextField
from apps.common.models import SoftDeleteModel, TimeStampedModel


class CameraGroup(TimeStampedModel):
    """Logical grouping for access scoping and bulk configuration."""

    name = models.CharField(max_length=100, unique=True)
    description = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Camera(SoftDeleteModel, TimeStampedModel):
    """One video source.

    ``purpose`` is the field that most affects accuracy. Only cameras marked
    IDENTITY are used to put a name to a face; PRESENCE cameras contribute
    occupancy and movement continuity but never establish identity. Mounting
    a ceiling dome and expecting names out of it is the single most common
    way these projects fail - record the honest purpose here.
    """

    class Vendor(models.TextChoices):
        DAHUA = "dahua", "Dahua"
        HIKVISION = "hikvision", "Hikvision"
        AXIS = "axis", "Axis"
        CP_PLUS = "cpplus", "CP Plus"
        ONVIF = "onvif", "Generic ONVIF"
        FILE = "file", "Video file (testing)"

    class Purpose(models.TextChoices):
        IDENTITY = "identity", "Identity (doorway, face-height)"
        PRESENCE = "presence", "Presence only (overview, no face recognition)"

    class Status(models.TextChoices):
        ONLINE = "online", "Online"
        DEGRADED = "degraded", "Degraded"
        OFFLINE = "offline", "Offline"
        DISABLED = "disabled", "Disabled"
        UNKNOWN = "unknown", "Unknown"

    name = models.CharField(max_length=100)
    code = models.SlugField(max_length=40, unique=True)
    zone = models.ForeignKey(
        "org.Zone", on_delete=models.PROTECT, related_name="cameras"
    )
    group = models.ForeignKey(
        CameraGroup, null=True, blank=True, on_delete=models.SET_NULL, related_name="cameras"
    )

    vendor = models.CharField(
        max_length=16, choices=Vendor.choices, default=Vendor.DAHUA
    )
    purpose = models.CharField(
        max_length=10, choices=Purpose.choices, default=Purpose.PRESENCE
    )

    # ------------------------------------------------------------ connection
    # Dahua main stream:
    #   rtsp://USER:PASS@HOST:554/cam/realmonitor?channel=1&subtype=0
    # Hikvision main stream:
    #   rtsp://USER:PASS@HOST:554/Streaming/Channels/101
    rtsp_url = EncryptedTextField(
        help_text="Full RTSP URL including credentials. Encrypted at rest. "
        "Use the main stream (Dahua subtype=0) for identity cameras."
    )
    rtsp_username = EncryptedCharField(
        max_length=64,
        blank=True,
        help_text="Optional, if kept separately from the URL. Use a dedicated "
        "Operator-level service account, never the camera's admin login.",
    )
    rtsp_password = EncryptedCharField(max_length=128, blank=True)

    ip_address = models.GenericIPAddressField(
        null=True, blank=True, help_text="For health checks and the camera inventory."
    )
    onvif_port = models.PositiveIntegerField(default=80)

    # ------------------------------------------------------------- streaming
    width = models.PositiveIntegerField(default=1920)
    height = models.PositiveIntegerField(default=1080)
    native_fps = models.PositiveSmallIntegerField(default=15)
    sample_fps = models.FloatField(
        default=4.0,
        validators=[MinValueValidator(0.5), MaxValueValidator(15.0)],
        help_text="Frames per second actually analysed. 3-5 is plenty for "
        "room-to-room movement; higher just burns GPU.",
    )

    # Region of interest as [[x, y], ...] in normalised 0-1 coordinates.
    # Masking out a window or a corridor you do not own cuts false detections.
    roi_polygon = models.JSONField(default=list, blank=True)

    # ----------------------------------------------------------- calibration
    measured_face_width_px = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Median face width observed by the audit script. Below ~60px "
        "this camera cannot reliably identify anyone.",
    )
    mount_height_cm = models.PositiveIntegerField(null=True, blank=True)
    tilt_degrees = models.FloatField(
        null=True, blank=True, help_text="Downward tilt. Above ~20 degrees, faces "
        "are foreshortened and recognition degrades sharply."
    )
    calibration_notes = models.TextField(blank=True)

    is_enabled = models.BooleanField(default=True, db_index=True)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.UNKNOWN, db_index=True
    )

    class Meta:
        ordering = ["zone", "name"]
        indexes = [
            models.Index(fields=["is_enabled", "status"]),
            models.Index(fields=["zone", "purpose"]),
        ]

    def __str__(self) -> str:
        return f"{self.name} [{self.code}] @ {self.zone.name}"

    def clean(self):
        from django.core.exceptions import ValidationError

        # Hard stop: a camera in a prohibited zone must never be ingested.
        if self.zone_id and self.zone.tracking_prohibited and self.is_enabled:
            raise ValidationError(
                {
                    "is_enabled": f"Zone '{self.zone.name}' has tracking prohibited; "
                    "cameras there cannot be enabled."
                }
            )

    @property
    def is_identity_capable(self) -> bool:
        """Honest answer to "can this camera name a person?"."""
        if self.purpose != self.Purpose.IDENTITY:
            return False
        if self.measured_face_width_px is None:
            return False
        return self.measured_face_width_px >= 60

    def mark_status(self, status: str, message: str = "") -> None:
        self.status = status
        self.save(update_fields=["status", "updated_at"])
        CameraHealth.objects.create(camera=self, status=status, error_message=message[:500])


class CameraHealth(models.Model):
    """Rolling health samples, written by the vision workers.

    Why this matters beyond ops: a camera that silently stops delivering
    frames creates a *gap* in someone's movement history. Gaps must be
    visible as gaps, never as absence - otherwise a timeline implies someone
    was not somewhere when in fact nobody was looking.
    """

    camera = models.ForeignKey(
        Camera, on_delete=models.CASCADE, related_name="health_samples"
    )
    at = models.DateTimeField(auto_now_add=True, db_index=True)
    status = models.CharField(max_length=10, choices=Camera.Status.choices)
    fps_actual = models.FloatField(null=True, blank=True)
    latency_ms = models.PositiveIntegerField(null=True, blank=True)
    frames_dropped = models.PositiveIntegerField(default=0)
    reconnects = models.PositiveIntegerField(default=0)
    error_message = models.CharField(max_length=500, blank=True)
    worker_id = models.CharField(max_length=64, blank=True)

    # Recognition counters since the previous sample. The ratio of gated to
    # seen is the most useful single number for explaining disappointing
    # accuracy: a camera that discards most of the faces it finds is badly
    # placed, and no amount of threshold tuning will fix that.
    faces_seen = models.PositiveIntegerField(default=0)
    faces_gated = models.PositiveIntegerField(
        default=0, help_text="Faces rejected by the quality gate."
    )
    faces_identified = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-at"]
        indexes = [models.Index(fields=["camera", "-at"])]
        verbose_name_plural = "camera health samples"

    def __str__(self) -> str:
        return f"{self.camera.code} {self.status} @ {self.at:%H:%M:%S}"

    @property
    def gate_rate(self) -> float | None:
        """Share of detected faces discarded as unusable."""
        if not self.faces_seen:
            return None
        return self.faces_gated / self.faces_seen


class PipelineConfig(TimeStampedModel):
    """Versioned pipeline tuning. Developer-editable only.

    Thresholds are kept in the database rather than only in environment
    variables so a change is attributable and reversible: who changed the
    match threshold, when, and why.
    """

    name = models.CharField(max_length=50, unique=True, default="default")
    is_active = models.BooleanField(default=True)

    match_threshold_high = models.FloatField(
        default=0.45, help_text="Cosine similarity at or above which a match is accepted."
    )
    match_threshold_low = models.FloatField(
        default=0.30, help_text="Below this, the face is recorded as unknown."
    )
    vote_window = models.PositiveSmallIntegerField(
        default=12,
        help_text="Frames aggregated per track before deciding identity. "
        "Never decide from a single frame.",
    )
    min_face_px = models.PositiveSmallIntegerField(default=60)
    min_quality_score = models.FloatField(default=0.35)
    presence_gap_seconds = models.PositiveIntegerField(default=60)

    model_version = models.CharField(max_length=50, default="buffalo_l")
    changed_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    change_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "pipeline configuration"

    def __str__(self) -> str:
        return f"{self.name} (high={self.match_threshold_high}, low={self.match_threshold_low})"

    def clean(self):
        from django.core.exceptions import ValidationError

        if self.match_threshold_low >= self.match_threshold_high:
            raise ValidationError(
                {"match_threshold_low": "Must be below match_threshold_high."}
            )

    @classmethod
    def active(cls) -> PipelineConfig:
        obj, _ = cls.objects.get_or_create(name="default", defaults={"is_active": True})
        return obj


class CameraAuditResult(models.Model):
    """Output of scripts/camera_audit.py - the Phase 0 viability check.

    Run this before writing any recognition code. If most cameras report a
    median face width under ~60px, the fix is camera placement, not model
    tuning, and you want to know that in week one.
    """

    camera = models.ForeignKey(
        Camera, on_delete=models.CASCADE, related_name="audit_results"
    )
    ran_at = models.DateTimeField(default=timezone.now, db_index=True)
    frames_sampled = models.PositiveIntegerField(default=0)
    faces_detected = models.PositiveIntegerField(default=0)
    median_face_width_px = models.FloatField(null=True, blank=True)
    p90_face_width_px = models.FloatField(null=True, blank=True)
    mean_sharpness = models.FloatField(null=True, blank=True)
    mean_brightness = models.FloatField(null=True, blank=True)
    verdict = models.CharField(
        max_length=20,
        choices=[
            ("identity_ok", "Suitable for identity"),
            ("marginal", "Marginal - reposition recommended"),
            ("presence_only", "Presence only"),
            ("unusable", "Unusable - no faces detected"),
        ],
        default="unusable",
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-ran_at"]

    def __str__(self) -> str:
        return f"{self.camera.code} audit: {self.verdict}"
