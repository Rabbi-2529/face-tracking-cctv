"""
Admin for tracking data. Read-only throughout.

Movement history is evidence about people. It is corrected through
:class:`employees.IdentityCorrection`, which keeps a record of what changed
and why, never by editing a row in place.
"""

from __future__ import annotations

from django.contrib import admin
from django.utils.html import format_html

from apps.tracking.models import (
    DailySummary,
    Detection,
    MovementEvent,
    PresenceInterval,
    UnknownFaceCluster,
)


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        # Deletion is the retention job's business, not a manual action.
        return False


@admin.register(Detection)
class DetectionAdmin(ReadOnlyAdmin):
    """Raw detections. Developer-only; contains biometric vectors."""

    list_display = (
        "captured_at",
        "camera",
        "matched_employee",
        "confidence",
        "match_score",
        "face_width_px",
        "track_id",
    )
    list_filter = ("confidence", "camera", "captured_at")
    search_fields = ("track_id", "matched_employee__employee_code")
    date_hierarchy = "captured_at"
    # The embedding is never rendered, not even to a Developer, in a page
    # that could be screenshotted or cached by a browser.
    exclude = ("embedding",)
    readonly_fields = [
        f.name for f in Detection._meta.fields if f.name != "embedding"
    ]


@admin.register(PresenceInterval)
class PresenceIntervalAdmin(ReadOnlyAdmin):
    list_display = (
        "employee",
        "zone",
        "entered_at",
        "exited_at",
        "duration_display",
        "confidence_badge",
        "detection_count",
    )
    list_filter = ("confidence", "zone", "is_inferred", "entered_at")
    search_fields = ("employee__employee_code", "employee__full_name", "zone__code")
    date_hierarchy = "entered_at"

    @admin.display(description="Duration")
    def duration_display(self, obj) -> str:
        seconds = obj.duration_seconds
        if seconds is None:
            return format_html('<b style="color:green">still present</b>')
        minutes, secs = divmod(seconds, 60)
        return f"{minutes}m {secs}s"

    @admin.display(description="Confidence")
    def confidence_badge(self, obj) -> str:
        colours = {"confirmed": "green", "probable": "orange", "unknown": "red"}
        return format_html(
            '<b style="color:{}">{}</b>',
            colours.get(obj.confidence, "gray"),
            obj.get_confidence_display(),
        )


@admin.register(MovementEvent)
class MovementEventAdmin(admin.ModelAdmin):
    """Movements. Only ``review_status`` is editable - the outcome of a human
    looking at a flagged transition."""

    list_display = (
        "occurred_at",
        "employee",
        "from_zone",
        "to_zone",
        "transit_seconds",
        "plausible_badge",
        "review_status",
    )
    list_filter = ("is_plausible", "review_status", "confidence", "occurred_at")
    search_fields = ("employee__employee_code", "from_zone__code", "to_zone__code")
    date_hierarchy = "occurred_at"
    fields = (
        "employee",
        "from_zone",
        "to_zone",
        "occurred_at",
        "transit_seconds",
        "prev_dwell_seconds",
        "confidence",
        "is_plausible",
        "implausible_reason",
        "review_status",
    )
    readonly_fields = (
        "employee",
        "from_zone",
        "to_zone",
        "occurred_at",
        "transit_seconds",
        "prev_dwell_seconds",
        "confidence",
        "is_plausible",
        "implausible_reason",
    )

    def has_add_permission(self, request) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False

    @admin.display(description="Plausible")
    def plausible_badge(self, obj) -> str:
        if obj.is_plausible:
            return format_html('<span style="color:green">yes</span>')
        return format_html(
            '<b style="color:red" title="{}">NO</b>', obj.implausible_reason
        )


@admin.register(UnknownFaceCluster)
class UnknownFaceClusterAdmin(admin.ModelAdmin):
    list_display = ("label_display", "sighting_count", "first_seen", "last_seen", "resolution")
    list_filter = ("resolution", "last_seen")
    exclude = ("centroid_embedding",)
    readonly_fields = ("uuid", "first_seen", "last_seen", "sighting_count")
    filter_horizontal = ("zones_seen",)

    @admin.display(description="Cluster")
    def label_display(self, obj) -> str:
        return obj.label or f"unlabelled {str(obj.uuid)[:8]}"

    def has_add_permission(self, request) -> bool:
        return False


@admin.register(DailySummary)
class DailySummaryAdmin(ReadOnlyAdmin):
    list_display = (
        "date",
        "employee",
        "first_seen_at",
        "last_seen_at",
        "total_zones_visited",
        "total_movements",
        "confidence_warning",
    )
    list_filter = ("date", "employee__department")
    search_fields = ("employee__employee_code", "employee__full_name")
    date_hierarchy = "date"

    @admin.display(description="Data quality")
    def confidence_warning(self, obj) -> str:
        if obj.low_confidence_ratio > 0.4:
            return format_html(
                '<b style="color:red">{:.0%} low confidence</b>',
                obj.low_confidence_ratio,
            )
        return format_html("{:.0%} low confidence", obj.low_confidence_ratio)
