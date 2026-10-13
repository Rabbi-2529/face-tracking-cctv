from __future__ import annotations

from django.contrib import admin
from django.utils.html import format_html

from apps.employees.models import (
    ConsentRecord,
    Employee,
    FaceEnrollment,
    IdentityCorrection,
)


class FaceEnrollmentInline(admin.TabularInline):
    model = FaceEnrollment
    # FaceEnrollment has two FKs to Employee (`employee` and `conflict_with`);
    # this inline is the person's own enrolments.
    fk_name = "employee"
    extra = 0
    # The embedding is a biometric template and is never rendered.
    fields = (
        "pose",
        "source",
        "quality_score",
        "face_width_px",
        "is_active",
        "conflict_flagged",
        "model_version",
        "enrolled_by",
    )
    readonly_fields = (
        "quality_score",
        "face_width_px",
        "model_version",
        "enrolled_by",
        "conflict_flagged",
    )
    can_delete = True


class ConsentRecordInline(admin.TabularInline):
    model = ConsentRecord
    extra = 0
    fields = ("policy_version", "method", "consented_at", "withdrawn_at", "document")


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = (
        "employee_code",
        "full_name",
        "department",
        "status",
        "enrollment_badge",
        "consent_badge",
        "processable_badge",
    )
    list_filter = ("status", "department", "is_trackable")
    search_fields = ("employee_code", "full_name")
    inlines = [FaceEnrollmentInline, ConsentRecordInline]
    readonly_fields = ("uuid", "created_at", "updated_at")

    fieldsets = (
        (None, {"fields": ("employee_code", "full_name", "uuid")}),
        ("Organisation", {"fields": ("department", "designation", "date_of_joining", "home_zone")}),
        (
            "Tracking",
            {
                "fields": ("status", "is_trackable"),
                "description": "Matching requires all of: active status, is_trackable, "
                "an active consent record, and at least one active enrolment. "
                "The 'Matchable' column shows the combined result.",
            },
        ),
    )

    @admin.display(description="Enrolments")
    def enrollment_badge(self, obj) -> str:
        count = obj.enrollment_count
        # Fewer than 5 images is the most common cause of poor accuracy.
        colour = "green" if count >= 5 else ("orange" if count > 0 else "red")
        label = f"{count}" + (" (need 5+)" if 0 < count < 5 else "")
        return format_html('<b style="color:{}">{}</b>', colour, label)

    @admin.display(description="Consent", boolean=True)
    def consent_badge(self, obj) -> bool:
        return obj.has_active_consent

    @admin.display(description="Matchable", boolean=True)
    def processable_badge(self, obj) -> bool:
        return obj.is_processable()


@admin.register(FaceEnrollment)
class FaceEnrollmentAdmin(admin.ModelAdmin):
    list_display = (
        "employee",
        "pose",
        "quality_score",
        "face_width_px",
        "is_active",
        "conflict_flagged",
        "conflict_with",
        "created_at",
    )
    list_filter = ("is_active", "pose", "source", "conflict_flagged", "model_version")
    search_fields = ("employee__employee_code", "employee__full_name")
    autocomplete_fields = ("employee", "conflict_with")
    # Never editable, never displayed.
    exclude = ("embedding",)
    readonly_fields = (
        "quality_score",
        "face_width_px",
        "sharpness",
        "brightness",
        "yaw_degrees",
        "pitch_degrees",
        "model_version",
        "enrolled_by",
    )


@admin.register(ConsentRecord)
class ConsentRecordAdmin(admin.ModelAdmin):
    list_display = (
        "employee",
        "policy_version",
        "method",
        "consented_at",
        "withdrawn_at",
        "recorded_by",
    )
    list_filter = ("policy_version", "method", "withdrawn_at")
    search_fields = ("employee__employee_code", "employee__full_name")
    autocomplete_fields = ("employee",)
    actions = ["withdraw_consent"]

    @admin.action(description="Withdraw consent (stops matching within ~30s)")
    def withdraw_consent(self, request, queryset):
        from apps.common.audit import record

        count = 0
        for consent in queryset.filter(withdrawn_at__isnull=True):
            consent.withdraw(note=f"Withdrawn via admin by {request.user.email}")
            record(
                "consent.withdrawn",
                target_employee=consent.employee,
                object_type="employees.ConsentRecord",
                object_id=consent.pk,
            )
            count += 1
        self.message_user(
            request,
            f"{count} consent record(s) withdrawn. Enrolments deactivated; the "
            "vision workers drop these people on their next gallery refresh.",
        )


@admin.register(IdentityCorrection)
class IdentityCorrectionAdmin(admin.ModelAdmin):
    """Reported mis-identifications.

    Treat this queue as the accuracy dashboard that matters: the labelled
    errors here are what tuning should be driven by.
    """

    list_display = (
        "created_at",
        "was_employee",
        "should_be_employee",
        "reported_by",
        "status",
    )
    list_filter = ("status", "created_at")
    search_fields = (
        "was_employee__employee_code",
        "should_be_employee__employee_code",
        "reason",
    )
    autocomplete_fields = ("was_employee", "should_be_employee")
    readonly_fields = ("created_at", "reported_by")
