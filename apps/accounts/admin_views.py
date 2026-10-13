"""
User and access administration.

Replaces the Django admin's user screens with one that is explicit about the
thing that actually matters here: what a given account can reach. The role
picker shows the capabilities each role grants, and the scope fields carry a
blunt warning that an empty scope means *nothing*, not everything - which is
the single most likely misconfiguration in this system.

Gated on USER_MANAGE, which only Super Admin holds. A Developer cannot reach
these screens: deep technical access must never be self-escalated into
authority over people.
"""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.crypto import get_random_string
from django.views.generic import View

from apps.accounts.models import AccessElevation, LoginEvent, User, UserScope
from apps.common.audit import record
from apps.common.permissions import PermissionService
from apps.common.roles import (
    MFA_REQUIRED_ROLES,
    Action,
    Role,
    actions_for_role,
)
from apps.common.scoping import SCOPE_REQUIRED_ROLES
from apps.employees.models import Employee
from apps.org.models import Department, Zone


class UserForm(forms.ModelForm):
    """Create or edit an account.

    No password field on purpose. A new account gets a generated temporary
    password shown once, with ``must_change_password`` set, so an
    administrator never chooses and never learns a user's standing password.
    """

    departments = forms.ModelMultipleChoiceField(
        queryset=Department.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Department scope",
        help_text=(
            "Which departments this user may see employees for. "
            "EMPTY MEANS NONE, not all - a scoped role with no scope sees nothing."
        ),
    )
    zones = forms.ModelMultipleChoiceField(
        queryset=Zone.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Zone scope",
        help_text=(
            "Which zones this user may monitor. EMPTY MEANS NONE. "
            "Zones marked never-tracked are not listed and cannot be granted."
        ),
    )

    class Meta:
        model = User
        fields = ["email", "full_name", "role", "employee", "is_active"]
        help_texts = {
            "employee": (
                "Link this login to a person so they can see their own movement "
                "history. Required for the Employee self-service role."
            ),
            "role": "Determines every capability. See the summary below.",
        }

    def __init__(self, *args, **kwargs):
        self.editing = kwargs.pop("editing", False)
        super().__init__(*args, **kwargs)

        self.fields["departments"].queryset = Department.objects.order_by("name")
        self.fields["zones"].queryset = (
            Zone.objects.alive()
            .filter(tracking_prohibited=False)
            .select_related("floor")
            .order_by("floor__level", "name")
        )
        self.fields["employee"].queryset = Employee.objects.alive().order_by("full_name")
        self.fields["employee"].required = False

        if self.instance.pk:
            scope = getattr(self.instance, "scope", None)
            if scope is not None:
                self.fields["departments"].initial = scope.departments.all()
                self.fields["zones"].initial = scope.zones.all()

    def clean_email(self) -> str:
        email = self.cleaned_data["email"].strip().lower()
        existing = User.objects.filter(email=email)
        if self.instance.pk:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise ValidationError("An account with that email already exists.")
        return email

    def clean(self):
        data = super().clean()
        role = data.get("role")

        if role == Role.EMPLOYEE and not data.get("employee"):
            raise ValidationError(
                {
                    "employee": (
                        "The self-service role needs a linked employee record, "
                        "otherwise the account has nothing to show."
                    )
                }
            )

        # Warn rather than block: an admin may be setting up the account
        # before the departments exist.
        if role in SCOPE_REQUIRED_ROLES:
            if role == Role.HR and not data.get("departments"):
                self.add_warning(
                    "This HR user has no departments assigned, so they will see "
                    "no employees at all until you add some."
                )
            if role == Role.SECURITY_OPERATOR and not data.get("zones"):
                self.add_warning(
                    "This operator has no zones assigned, so their live board "
                    "will be empty."
                )
        if role in MFA_REQUIRED_ROLES:
            self.add_warning(
                f"The {Role(role).label} role requires an authenticator app. "
                "Gated features stay unavailable until the user enrols one."
            )
        return data

    def add_warning(self, message: str) -> None:
        if not hasattr(self, "warnings"):
            self.warnings = []
        self.warnings.append(message)

    def get_warnings(self) -> list[str]:
        return getattr(self, "warnings", [])

    def save_scope(self, user: User) -> None:
        departments = self.cleaned_data.get("departments")
        zones = self.cleaned_data.get("zones")

        if not departments and not zones:
            # No scope wanted: drop any existing one rather than leaving an
            # empty record that reads ambiguously.
            UserScope.objects.filter(user=user).delete()
            return

        scope, _ = UserScope.objects.get_or_create(user=user)
        scope.departments.set(departments or [])
        scope.zones.set(zones or [])


class UserManageMixin(LoginRequiredMixin):
    def check_access(self, request) -> None:
        decision = PermissionService.can(request.user, Action.USER_MANAGE)
        if not decision:
            record("user.denied", reason=decision.reason, success=False)
            raise PermissionDenied(decision.reason)


def role_summary() -> list[dict]:
    """What each role can do, for the picker and the reference table."""
    highlights = {
        Action.HISTORY_ANY: "view anyone's movements",
        Action.HISTORY_DEPARTMENT: "view their department's movements",
        Action.HISTORY_OWN: "view their own movements",
        Action.LIVE_VIEW_ALL: "live board, all zones",
        Action.LIVE_VIEW_ASSIGNED: "live board, assigned zones",
        Action.TRACK_FOLLOW_LIVE: "follow a named person live",
        Action.TRACKREQUEST_APPROVE: "approve tracking requests",
        Action.TRACKREQUEST_CREATE: "request tracking approval",
        Action.EMPLOYEE_MANAGE: "add and edit employees",
        Action.ENROLLMENT_MANAGE: "enrol faces",
        Action.CONSENT_MANAGE: "record consent",
        Action.CAMERA_MANAGE: "configure cameras",
        Action.PIPELINE_CONFIGURE: "change matching thresholds",
        Action.DETECTION_VIEW_RAW: "read raw biometric vectors",
        Action.USER_MANAGE: "manage users and roles",
        Action.PURGE_EXECUTE: "purge footage",
        Action.AUDIT_READ_ALL: "read the full audit log",
    }

    rows = []
    for role in Role.values:
        granted = actions_for_role(role, is_production=True)
        rows.append(
            {
                "value": role,
                "label": Role(role).label,
                "can": [text for action, text in highlights.items() if action in granted],
                "cannot": [
                    text for action, text in highlights.items() if action not in granted
                ],
                "needs_mfa": role in MFA_REQUIRED_ROLES,
                "needs_scope": role in SCOPE_REQUIRED_ROLES,
                "action_count": len(granted),
            }
        )
    return rows


class UserListView(UserManageMixin, View):
    template_name = "accounts/user_list.html"

    def get(self, request):
        self.check_access(request)

        users = (
            User.objects.select_related("employee")
            .prefetch_related("scope__departments", "scope__zones")
            .order_by("role", "email")
        )
        search = request.GET.get("q", "").strip()
        if search:
            users = users.filter(email__icontains=search) | users.filter(
                full_name__icontains=search
            )

        users = list(users)
        for user in users:
            scope = getattr(user, "scope", None)
            user.scope_summary = (
                f"{scope.departments.count()} dept / {scope.zones.count()} zone"
                if scope
                else ("none set - SEES NOTHING" if user.role in SCOPE_REQUIRED_ROLES
                      else "unscoped")
            )
            user.mfa_ok = user.has_confirmed_mfa or user.role not in MFA_REQUIRED_ROLES

        return render(
            request,
            self.template_name,
            {
                "users": users,
                "roles": role_summary(),
                "elevations": AccessElevation.objects.filter(
                    status=AccessElevation.Status.PENDING
                ).select_related("user"),
            },
        )


class UserEditView(UserManageMixin, View):
    template_name = "accounts/user_form.html"

    def get(self, request, user_uuid=None):
        self.check_access(request)
        target = (
            get_object_or_404(User, uuid=user_uuid) if user_uuid else None
        )
        form = UserForm(instance=target, editing=target is not None)
        return render(
            request,
            self.template_name,
            {
                "form": form,
                "target": target,
                "creating": target is None,
                "roles": role_summary(),
                "recent_logins": (
                    LoginEvent.objects.filter(user=target).order_by("-at")[:10]
                    if target
                    else []
                ),
            },
        )

    def post(self, request, user_uuid=None):
        self.check_access(request)
        target = get_object_or_404(User, uuid=user_uuid) if user_uuid else None
        form = UserForm(request.POST, instance=target, editing=target is not None)

        if not form.is_valid():
            return render(
                request,
                self.template_name,
                {
                    "form": form,
                    "target": target,
                    "creating": target is None,
                    "roles": role_summary(),
                    "recent_logins": [],
                },
            )

        creating = target is None
        previous_role = None if creating else target.role

        user = form.save(commit=False)
        temporary_password = None

        if creating:
            # Generated, shown once, and must be changed at first sign-in, so
            # no administrator ever knows a user's standing password.
            temporary_password = get_random_string(14)
            user.set_password(temporary_password)
            user.must_change_password = True

        user.is_staff = False  # the Django admin is not used
        user.save()
        form.save_scope(user)

        # Keep the role group in step, so any remaining model-permission
        # check agrees with the role.
        from django.contrib.auth.models import Group

        user.groups.clear()
        group = Group.objects.filter(name=f"role:{user.role}").first()
        if group:
            user.groups.add(group)

        record(
            "user.created" if creating else "user.updated",
            object_type="accounts.User",
            object_id=user.pk,
            reason=f"{user.email} role={user.role}",
            metadata={
                "email": user.email,
                "role": user.role,
                "role_changed_from": previous_role
                if previous_role and previous_role != user.role
                else None,
                "departments": list(
                    form.cleaned_data.get("departments", []).values_list(
                        "code", flat=True
                    )
                )
                if form.cleaned_data.get("departments")
                else [],
                "zones": list(
                    form.cleaned_data.get("zones", []).values_list("code", flat=True)
                )
                if form.cleaned_data.get("zones")
                else [],
            },
        )

        for warning in form.get_warnings():
            messages.warning(request, warning)

        if creating:
            messages.success(
                request,
                f"Account created for {user.email}. Temporary password: "
                f"{temporary_password} - give it to them directly. They must "
                "change it at first sign-in, and this is the only time it is shown.",
            )
        else:
            if previous_role != user.role:
                messages.warning(
                    request,
                    f"Role changed from {previous_role} to {user.role}. Their "
                    "access changes immediately.",
                )
            messages.success(request, f"{user.email} updated.")

        return redirect(reverse("useradmin:list"))


class UserToggleView(UserManageMixin, View):
    """Disable or re-enable an account."""

    def post(self, request, user_uuid):
        self.check_access(request)
        target = get_object_or_404(User, uuid=user_uuid)

        if target.pk == request.user.pk:
            messages.error(request, "You cannot disable your own account.")
            return redirect(reverse("useradmin:list"))

        remaining = User.objects.filter(
            role=Role.SUPER_ADMIN, is_active=True
        ).exclude(pk=target.pk).count()
        if target.role == Role.SUPER_ADMIN and target.is_active and remaining == 0:
            messages.error(
                request,
                "This is the last active Super Admin. Create another before "
                "disabling this one, or nobody can approve tracking requests "
                "or manage users.",
            )
            return redirect(reverse("useradmin:list"))

        target.is_active = not target.is_active
        target.save(update_fields=["is_active"])

        record(
            "user.enabled" if target.is_active else "user.disabled",
            object_type="accounts.User",
            object_id=target.pk,
            reason=request.POST.get("reason", ""),
            metadata={"email": target.email, "role": target.role},
        )
        messages.info(
            request,
            f"{target.email} "
            + ("re-enabled." if target.is_active else "disabled; all access revoked."),
        )
        return redirect(reverse("useradmin:list"))


class ElevationDecideView(UserManageMixin, View):
    """Approve or reject a break-glass access elevation."""

    def post(self, request, elevation_uuid):
        self.check_access(request)
        import datetime as dt

        from django.utils import timezone

        elevation = get_object_or_404(AccessElevation, uuid=elevation_uuid)

        if elevation.user_id == request.user.pk:
            messages.error(request, "You cannot approve your own elevation.")
            return redirect(reverse("useradmin:list"))

        if request.POST.get("decision") == "approve":
            now = timezone.now()
            hours = min(4, max(1, int(request.POST.get("hours") or 1)))
            elevation.status = AccessElevation.Status.ACTIVE
            elevation.approved_by = request.user
            elevation.approved_at = now
            elevation.starts_at = now
            elevation.expires_at = now + dt.timedelta(hours=hours)
            try:
                elevation.full_clean()
            except ValidationError as exc:
                messages.error(request, "; ".join(exc.messages))
                return redirect(reverse("useradmin:list"))
            elevation.save()
            action, note = "elevation.approved", f"granted for {hours}h"
            messages.warning(
                request,
                f"Elevation granted to {elevation.user.email} for {hours} hour(s). "
                "It expires by itself.",
            )
        else:
            elevation.status = AccessElevation.Status.REJECTED
            elevation.approved_by = request.user
            elevation.approved_at = timezone.now()
            elevation.save()
            action, note = "elevation.rejected", "rejected"
            messages.info(request, "Elevation rejected.")

        record(
            action,
            object_type="accounts.AccessElevation",
            object_id=elevation.pk,
            reason=f"{note}: {elevation.reason}"[:500],
            metadata={"user": elevation.user.email, "actions": elevation.actions},
        )
        return redirect(reverse("useradmin:list"))


class UserMFAResetView(UserManageMixin, View):
    """Remove a user's authenticator and backup codes so they can re-enrol.

    The recovery path for someone who has lost both. Never for oneself: an
    administrator who could reset their own second factor would not have one.
    """

    def post(self, request, user_uuid):
        from django_otp.plugins.otp_static.models import StaticDevice
        from django_otp.plugins.otp_totp.models import TOTPDevice

        self.check_access(request)
        target = get_object_or_404(User, uuid=user_uuid)
        reason = (request.POST.get("reason") or "").strip()

        if target.pk == request.user.pk:
            messages.error(request, "You cannot reset your own authenticator.")
            return redirect(reverse("useradmin:edit", args=[target.uuid]))
        if len(reason) < 10:
            messages.error(request, "Give a reason, including how you confirmed who asked.")
            return redirect(reverse("useradmin:edit", args=[target.uuid]))

        removed = TOTPDevice.objects.filter(user=target).delete()[0]
        removed += StaticDevice.objects.filter(user=target).delete()[0]
        record(
            "user.mfa_reset",
            object_type="accounts.User",
            object_id=target.pk,
            reason=reason[:500],
            metadata={"email": target.email, "devices_removed": removed},
        )
        messages.warning(
            request,
            f"Authenticator reset for {target.email}. They must enrol a new one "
            "at next sign-in.",
        )
        return redirect(reverse("useradmin:edit", args=[target.uuid]))
