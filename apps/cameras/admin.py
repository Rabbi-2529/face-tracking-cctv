from __future__ import annotations

from django.contrib import admin
from django.utils.html import format_html

from apps.cameras.models import (
    Camera,
    CameraAuditResult,
    CameraGroup,
    CameraHealth,
    PipelineConfig,
)


@admin.register(CameraGroup)
class CameraGroupAdmin(admin.ModelAdmin):
    list_display = ("name", "description", "camera_count")

    @admin.display(description="Cameras")
    def camera_count(self, obj) -> int:
        return obj.cameras.count()


@admin.register(Camera)
class CameraAdmin(admin.ModelAdmin):
    list_display = (
        "code",
        "name",
        "zone",
        "purpose",
        "capability_badge",
        "status",
        "is_enabled",
        "sample_fps",
    )
    list_filter = ("status", "purpose", "vendor", "is_enabled", "zone__floor")
    search_fields = ("code", "name", "ip_address")
    autocomplete_fields = ("zone",)
    readonly_fields = ("status", "created_at", "updated_at")

    fieldsets = (
        (None, {"fields": ("code", "name", "zone", "group", "vendor", "purpose")}),
        (
            "Connection (encrypted at rest)",
            {
                "fields": ("rtsp_url", "rtsp_username", "rtsp_password", "ip_address", "onvif_port"),
                "description": (
                    "Dahua main stream: "
                    "<code>rtsp://USER:PASS@HOST:554/cam/realmonitor?channel=1&amp;subtype=0</code>"
                    " (subtype=0 is the full-resolution stream; use it for identity "
                    "cameras). Hikvision: "
                    "<code>rtsp://USER:PASS@HOST:554/Streaming/Channels/101</code>. "
                    "Use a dedicated Operator-level service account, never the "
                    "camera's admin login."
                ),
            },
        ),
        ("Streaming", {"fields": ("width", "height", "native_fps", "sample_fps", "roi_polygon")}),
        (
            "Calibration",
            {
                "fields": (
                    "measured_face_width_px",
                    "mount_height_cm",
                    "tilt_degrees",
                    "calibration_notes",
                ),
                "description": "Populated by scripts/camera_audit.py. A median face "
                "width below ~60px means this camera cannot reliably identify "
                "anyone, whatever its purpose says.",
            },
        ),
        ("State", {"fields": ("is_enabled", "status")}),
    )

    @admin.display(description="Face capability")
    def capability_badge(self, obj) -> str:
        if obj.purpose != Camera.Purpose.PRESENCE and obj.measured_face_width_px is None:
            return format_html('<span style="color:gray">not audited</span>')
        if obj.purpose == Camera.Purpose.PRESENCE:
            return format_html('<span style="color:gray">presence only</span>')
        width = obj.measured_face_width_px or 0
        if width >= 100:
            return format_html('<b style="color:green">{}px good</b>', width)
        if width >= 60:
            return format_html('<b style="color:orange">{}px marginal</b>', width)
        return format_html('<b style="color:red">{}px too small</b>', width)


@admin.register(CameraHealth)
class CameraHealthAdmin(admin.ModelAdmin):
    list_display = ("at", "camera", "status", "fps_actual", "latency_ms", "reconnects")
    list_filter = ("status", "camera")
    date_hierarchy = "at"
    readonly_fields = [f.name for f in CameraHealth._meta.fields]

    def has_add_permission(self, request) -> bool:
        return False


@admin.register(PipelineConfig)
class PipelineConfigAdmin(admin.ModelAdmin):
    """Developer-only tuning. Changes are attributed and reversible."""

    list_display = (
        "name",
        "is_active",
        "match_threshold_high",
        "match_threshold_low",
        "vote_window",
        "changed_by",
        "updated_at",
    )
    readonly_fields = ("changed_by", "updated_at")

    fieldsets = (
        (None, {"fields": ("name", "is_active", "model_version")}),
        (
            "Matching",
            {
                "fields": (
                    "match_threshold_high",
                    "match_threshold_low",
                    "vote_window",
                    "min_face_px",
                    "min_quality_score",
                ),
                "description": "Tune these against a labelled validation set from "
                "your own cameras. Published thresholds do not transfer. "
                "vote_window is how many frames of a track are aggregated before "
                "deciding identity - never decide from one frame.",
            },
        ),
        ("Presence", {"fields": ("presence_gap_seconds",)}),
        ("Change record", {"fields": ("change_reason", "changed_by", "updated_at")}),
    )

    def save_model(self, request, obj, form, change):
        from apps.common.audit import record

        obj.changed_by = request.user
        super().save_model(request, obj, form, change)
        record(
            "pipeline.configured",
            object_type="cameras.PipelineConfig",
            object_id=obj.pk,
            reason=obj.change_reason,
            metadata={
                "high": obj.match_threshold_high,
                "low": obj.match_threshold_low,
                "vote_window": obj.vote_window,
            },
        )


@admin.register(CameraAuditResult)
class CameraAuditResultAdmin(admin.ModelAdmin):
    """Phase 0 viability results - read this before building anything else."""

    list_display = (
        "ran_at",
        "camera",
        "verdict",
        "median_face_width_px",
        "faces_detected",
        "frames_sampled",
    )
    list_filter = ("verdict", "ran_at")
    readonly_fields = [f.name for f in CameraAuditResult._meta.fields]

    def has_add_permission(self, request) -> bool:
        return False
