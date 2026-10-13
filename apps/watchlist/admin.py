"""
Admin for tracking requests.

Approval is possible here as well as in the portal, with the same rules
enforced by the model: a request cannot be approved by the person who raised
it, and the window is capped.
"""

from __future__ import annotations

from django.contrib import admin
from django.core.exceptions import ValidationError
from django.utils.html import format_html

from apps.watchlist.models import TrackingRequest, TrackSession


class TrackSessionInline(admin.TabularInline):
    model = TrackSession
    extra = 0
    fields = ("opened_by", "started_at", "ended_at", "snapshots_viewed", "ip_address")
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None) -> bool:
        return False


@admin.register(TrackingRequest)
class TrackingRequestAdmin(admin.ModelAdmin):
    list_display = (
        "requested_at",
        "employee",
        "requested_by",
        "purpose",
        "scope",
        "status_badge",
        "approved_by",
        "window_display",
        "used_display",
    )
    list_filter = ("status", "purpose", "scope", "requested_at")
    search_fields = (
        "employee__employee_code",
        "employee__full_name",
        "requested_by__email",
        "reason",
    )
    autocomplete_fields = ("employee",)
    inlines = [TrackSessionInline]
    actions = ["approve_48h", "reject_selected", "revoke_selected"]

    readonly_fields = (
        "uuid",
        "requested_by",
        "requested_at",
        "approved_by",
        "approved_at",
        "revoked_at",
        "revoked_by",
    )

    fieldsets = (
        (
            "Subject and justification",
            {
                "fields": ("employee", "purpose", "reason", "scope"),
                "description": "The reason is read by the approver, by auditors, "
                "and - unless notice is formally suppressed - by the subject.",
            },
        ),
        ("Data window", {"fields": ("data_from", "data_to")}),
        (
            "Authorisation window",
            {
                "fields": ("status", "start_at", "end_at", "decision_note"),
                "description": "Expires automatically. The scheduled "
                "expire_tracking_requests task also closes any live session.",
            },
        ),
        (
            "Transparency",
            {
                "fields": (
                    "subject_notified",
                    "subject_notified_at",
                    "notification_suppressed",
                    "suppression_reason",
                ),
                "description": "Default is to tell the subject. Suppress only for "
                "an active investigation where notice would defeat the purpose, "
                "and say why.",
            },
        ),
        ("Record", {"classes": ("collapse",), "fields": readonly_fields}),
    )

    @admin.display(description="Status")
    def status_badge(self, obj) -> str:
        colours = {
            "pending": "orange",
            "approved": "green" if obj.is_active else "gray",
            "rejected": "red",
            "expired": "gray",
            "revoked": "red",
            "withdrawn": "gray",
        }
        label = obj.get_status_display()
        if obj.status == TrackingRequest.Status.APPROVED and not obj.is_active:
            label = "approved (window passed)"
        return format_html(
            '<b style="color:{}">{}</b>', colours.get(obj.status, "black"), label
        )

    @admin.display(description="Window")
    def window_display(self, obj) -> str:
        if not (obj.start_at and obj.end_at):
            return "-"
        if obj.is_active:
            minutes = obj.seconds_remaining // 60
            return format_html('<b style="color:green">{}m left</b>', minutes)
        return f"{obj.start_at:%d %b %H:%M} - {obj.end_at:%d %b %H:%M}"

    @admin.display(description="Used")
    def used_display(self, obj) -> str:
        count = obj.sessions.count()
        # An approval never used is worth noticing: it means authority was
        # granted that nobody needed.
        return f"{count} session(s)" if count else "never opened"

    def _decide(self, request, queryset, verb: str, **kwargs):
        from apps.common.audit import record

        done, failed = 0, 0
        for tracking_request in queryset:
            try:
                getattr(tracking_request, verb)(request.user, **kwargs)
            except ValidationError as exc:
                self.message_user(
                    request,
                    f"{tracking_request.employee.employee_code}: {'; '.join(exc.messages)}",
                    level="ERROR",
                )
                failed += 1
                continue
            record(
                f"trackrequest.{verb}d" if verb != "approve" else "trackrequest.approved",
                target_employee=tracking_request.employee,
                object_type="watchlist.TrackingRequest",
                object_id=tracking_request.pk,
                reason=tracking_request.reason[:500],
            )
            done += 1
        self.message_user(request, f"{done} succeeded, {failed} refused.")

    @admin.action(description="Approve for 48 hours")
    def approve_48h(self, request, queryset):
        self._decide(
            request,
            queryset.filter(status=TrackingRequest.Status.PENDING),
            "approve",
            window_hours=48,
            note=f"Approved in admin by {request.user.email}",
        )

    @admin.action(description="Reject")
    def reject_selected(self, request, queryset):
        self._decide(
            request,
            queryset.filter(status=TrackingRequest.Status.PENDING),
            "reject",
            note=f"Rejected in admin by {request.user.email}",
        )

    @admin.action(description="Revoke now (closes live sessions)")
    def revoke_selected(self, request, queryset):
        self._decide(
            request,
            queryset.filter(status=TrackingRequest.Status.APPROVED),
            "revoke",
            note=f"Revoked in admin by {request.user.email}",
        )


@admin.register(TrackSession)
class TrackSessionAdmin(admin.ModelAdmin):
    list_display = (
        "started_at",
        "opened_by",
        "subject",
        "ended_at",
        "duration_display",
        "snapshots_viewed",
        "ip_address",
    )
    list_filter = ("started_at", "opened_by")
    search_fields = ("opened_by__email", "request__employee__employee_code")
    readonly_fields = [f.name for f in TrackSession._meta.fields]

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    @admin.display(description="Subject")
    def subject(self, obj) -> str:
        return obj.request.employee.employee_code

    @admin.display(description="Duration")
    def duration_display(self, obj) -> str:
        minutes, seconds = divmod(obj.duration_seconds, 60)
        return f"{minutes}m {seconds}s"
