"""Admin for users, scopes, elevations and the audit log."""

from __future__ import annotations

import datetime as dt

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm
from django.utils import timezone

from apps.accounts.models import AccessElevation, AuditLog, LoginEvent, User, UserScope

# How long an approved break-glass elevation lasts. Short on purpose: a
# longer investigation means approving again, with the reason restated.
ELEVATION_GRANT = dt.timedelta(hours=1)


class UserScopeInline(admin.StackedInline):
    model = UserScope
    extra = 0
    filter_horizontal = ("departments", "zones")
    verbose_name_plural = "Access scope (departments and zones)"


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    add_form = UserCreationForm
    form = UserChangeForm
    inlines = [UserScopeInline]

    list_display = (
        "email",
        "full_name",
        "role",
        "mfa_status",
        "scope_summary",
        "is_active",
        "last_login",
    )
    list_filter = ("role", "is_active", "is_staff")
    search_fields = ("email", "full_name", "employee__employee_code")
    ordering = ("email",)
    readonly_fields = ("last_login", "created_at", "password_changed_at", "last_login_ip")

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Identity", {"fields": ("full_name", "employee")}),
        (
            "Role",
            {
                "fields": ("role", "is_active", "is_staff"),
                "description": "The role determines which actions are permitted. "
                "See apps/common/roles.py for the full matrix.",
            },
        ),
        (
            "Django permissions",
            {
                "classes": ("collapse",),
                "fields": ("is_superuser", "groups", "user_permissions"),
                "description": "Django model permissions drive the admin site only. "
                "Application authorisation comes from the role above.",
            },
        ),
        (
            "Security",
            {
                "fields": (
                    "must_change_password",
                    "password_changed_at",
                    "last_login",
                    "last_login_ip",
                )
            },
        ),
    )

    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "full_name", "role", "password1", "password2"),
            },
        ),
    )

    @admin.display(description="MFA")
    def mfa_status(self, obj) -> str:
        from apps.common.roles import MFA_REQUIRED_ROLES

        if obj.has_confirmed_mfa:
            return "enrolled"
        return "REQUIRED - not enrolled" if obj.role in MFA_REQUIRED_ROLES else "-"

    @admin.display(description="Scope")
    def scope_summary(self, obj) -> str:
        scope = getattr(obj, "scope", None)
        if scope is None:
            from apps.common.scoping import SCOPE_REQUIRED_ROLES

            return (
                "none set - SEES NOTHING"
                if obj.role in SCOPE_REQUIRED_ROLES
                else "unscoped"
            )
        return f"{scope.departments.count()} dept / {scope.zones.count()} zone"

    def save_model(self, request, obj, form, change):
        # An administrator-created account starts with a password the admin
        # knows, so force a change at first sign-in.
        if not change:
            obj.must_change_password = True
        super().save_model(request, obj, form, change)


@admin.register(AccessElevation)
class AccessElevationAdmin(admin.ModelAdmin):
    """Break-glass review. Approving here grants the window immediately."""

    list_display = (
        "user",
        "status",
        "actions_summary",
        "requested_at",
        "starts_at",
        "expires_at",
        "approved_by",
    )
    list_filter = ("status", "requested_at")
    search_fields = ("user__email", "reason")
    readonly_fields = ("requested_at", "approved_at", "revoked_at")
    actions = ["approve_selected", "revoke_selected"]

    @admin.display(description="Actions")
    def actions_summary(self, obj) -> str:
        return ", ".join(obj.actions or []) or "-"

    @admin.action(description="Approve for 1 hour")
    def approve_selected(self, request, queryset):
        from apps.common.audit import record

        now = timezone.now()
        approved = 0
        for elevation in queryset.filter(status=AccessElevation.Status.PENDING):
            if elevation.user_id == request.user.pk:
                self.message_user(
                    request,
                    f"Skipped {elevation.user.email}: you cannot approve your own "
                    "elevation.",
                    level="ERROR",
                )
                continue
            elevation.status = AccessElevation.Status.ACTIVE
            elevation.approved_by = request.user
            elevation.approved_at = now
            elevation.starts_at = now
            elevation.expires_at = now + ELEVATION_GRANT
            elevation.full_clean()
            elevation.save()
            record(
                "elevation.approved",
                object_type="accounts.AccessElevation",
                object_id=elevation.pk,
                reason=elevation.reason[:500],
                metadata={"user": elevation.user.email, "actions": elevation.actions},
            )
            approved += 1
        self.message_user(request, f"{approved} elevation(s) approved for 1 hour.")

    @admin.action(description="Revoke now")
    def revoke_selected(self, request, queryset):
        from apps.common.audit import record

        now = timezone.now()
        for elevation in queryset.filter(status=AccessElevation.Status.ACTIVE):
            elevation.status = AccessElevation.Status.REVOKED
            elevation.revoked_at = now
            elevation.expires_at = now
            elevation.save(update_fields=["status", "revoked_at", "expires_at"])
            record(
                "elevation.revoked",
                object_type="accounts.AccessElevation",
                object_id=elevation.pk,
                metadata={"user": elevation.user.email},
            )
        self.message_user(request, "Selected elevations revoked.")


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    """Read-only by construction: the model refuses updates."""

    list_display = (
        "at",
        "actor",
        "action",
        "target_employee_code",
        "success",
        "ip_address",
    )
    list_filter = ("action", "success", "at")
    search_fields = (
        "actor__email",
        "action",
        "target_employee_code",
        "reason",
        "ip_address",
    )
    date_hierarchy = "at"
    readonly_fields = [f.name for f in AuditLog._meta.fields]

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        # Retention is enforced by the scheduled purge, not by hand.
        return False


@admin.register(LoginEvent)
class LoginEventAdmin(admin.ModelAdmin):
    list_display = ("at", "email_attempted", "succeeded", "ip_address", "failure_reason")
    list_filter = ("succeeded", "at")
    search_fields = ("email_attempted", "ip_address")
    date_hierarchy = "at"
    readonly_fields = [f.name for f in LoginEvent._meta.fields]

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False
