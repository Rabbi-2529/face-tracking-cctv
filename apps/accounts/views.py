"""Authentication, forced password change, and MFA enrolment."""

from __future__ import annotations

from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.generic import TemplateView, View
from django_otp import login as otp_login

from apps.common.roles import MFA_REQUIRED_ROLES


class LoginView(auth_views.LoginView):
    template_name = "accounts/login.html"
    redirect_authenticated_user = True

    def form_valid(self, form):
        response = super().form_valid(form)
        user = self.request.user
        if user.has_confirmed_mfa:
            # Password alone is the first factor. Anyone with an enrolled
            # authenticator proves the second before reaching anything gated;
            # until then PermissionService treats the session as unverified.
            target = reverse("accounts:mfa_verify")
            next_url = self.get_redirect_url()
            if next_url:
                target += "?" + urlencode({"next": next_url})
            return redirect(target)
        if user.must_change_password:
            messages.warning(
                self.request, "You must change your password before continuing."
            )
            return redirect(reverse("accounts:password_change"))
        if user.role in MFA_REQUIRED_ROLES and not user.has_confirmed_mfa:
            messages.warning(
                self.request,
                "Your role requires an authenticator app. Gated features stay "
                "unavailable until you enrol one.",
            )
            return redirect(reverse("accounts:mfa_setup"))
        return response


class LogoutView(auth_views.LogoutView):
    next_page = reverse_lazy("accounts:login")


class PasswordChangeView(auth_views.PasswordChangeView):
    template_name = "accounts/password_change.html"
    success_url = reverse_lazy("tracking:dashboard")

    def form_valid(self, form):
        from apps.common.audit import record

        response = super().form_valid(form)
        # set_password() clears must_change_password; persist it.
        self.request.user.must_change_password = False
        self.request.user.save(
            update_fields=["must_change_password", "password_changed_at"]
        )
        record("auth.password_changed")
        messages.success(self.request, "Password updated.")
        return response


class LockoutView(TemplateView):
    """Shown by django-axes after too many failed attempts."""

    template_name = "accounts/lockout.html"

    def dispatch(self, request, *args, **kwargs):
        response = super().dispatch(request, *args, **kwargs)
        response.status_code = 429
        return response


class MFASetupView(LoginRequiredMixin, TemplateView):
    """Enrol a TOTP device and show the provisioning QR code."""

    template_name = "accounts/mfa_setup.html"

    def get_context_data(self, **kwargs):
        from django_otp.plugins.otp_totp.models import TOTPDevice

        ctx = super().get_context_data(**kwargs)
        user = self.request.user

        device = TOTPDevice.objects.filter(user=user, confirmed=False).first()
        if device is None and not user.has_confirmed_mfa:
            device = TOTPDevice.objects.create(
                user=user, name=f"{user.email} authenticator", confirmed=False
            )

        ctx["already_enrolled"] = user.has_confirmed_mfa
        ctx["device"] = device
        # config_url is the otpauth:// URI the authenticator app scans.
        ctx["config_url"] = device.config_url if device else ""
        return ctx

    def post(self, request, *args, **kwargs):
        from django_otp.plugins.otp_totp.models import TOTPDevice

        from apps.common.audit import record

        token = request.POST.get("token", "").strip()
        device = TOTPDevice.objects.filter(user=request.user, confirmed=False).first()
        if device is None:
            messages.error(request, "No pending device. Reload to start again.")
            return redirect(reverse("accounts:mfa_setup"))

        if device.verify_token(token):
            device.confirmed = True
            device.save(update_fields=["confirmed"])
            # Proving the code just now is a verification in its own right,
            # so this session does not have to sign in again to use it.
            otp_login(request, device)
            codes = _issue_backup_codes(request.user)
            record("auth.mfa_enrolled", metadata={"backup_codes": len(codes)})
            return render(
                request,
                "accounts/mfa_backup_codes.html",
                {"codes": codes, "just_enrolled": True},
            )

        record("auth.mfa_enrol_failed", success=False)
        messages.error(request, "That code did not verify. Try the next one.")
        return redirect(reverse("accounts:mfa_setup"))


BACKUP_CODE_COUNT = 10


def _issue_backup_codes(user) -> list[str]:
    """Replace the user's one-time backup codes and return the new ones.

    Shown once, never stored in readable form anywhere else. They exist so
    a lost phone means a short delay rather than an administrator reset.
    """
    from django_otp.plugins.otp_static.models import StaticDevice, StaticToken

    StaticDevice.objects.filter(user=user).delete()
    device = StaticDevice.objects.create(user=user, name="Backup codes", confirmed=True)
    codes = [StaticToken.random_token() for _ in range(BACKUP_CODE_COUNT)]
    StaticToken.objects.bulk_create([StaticToken(device=device, token=c) for c in codes])
    return codes


class MFAVerifyView(LoginRequiredMixin, TemplateView):
    """The second sign-in step: an authenticator code or a backup code.

    Tokens go through django-otp's device verification, which applies its
    own escalating delay after failures; every attempt is also audited.
    """

    template_name = "accounts/mfa_verify.html"

    def _next(self) -> str:
        candidate = self.request.POST.get("next") or self.request.GET.get("next") or ""
        if candidate and url_has_allowed_host_and_scheme(
            candidate,
            allowed_hosts={self.request.get_host()},
            require_https=self.request.is_secure(),
        ):
            return candidate
        return reverse("tracking:dashboard")

    def get(self, request, *args, **kwargs):
        if not request.user.has_confirmed_mfa:
            return redirect(reverse("accounts:mfa_setup"))
        if request.user.is_verified():
            return redirect(self._next())
        return render(request, self.template_name, {"next": self._next()})

    def post(self, request, *args, **kwargs):
        from django_otp import match_token

        from apps.common.audit import record

        token = "".join(request.POST.get("token", "").split())
        device = match_token(request.user, token) if token else None
        if device is None:
            record("auth.mfa_failed", success=False)
            messages.error(
                request,
                "That code did not verify. Codes change every 30 seconds; "
                "repeated failures add a delay before the next try.",
            )
            return render(request, self.template_name, {"next": self._next()})

        otp_login(request, device)
        record(
            "auth.mfa_verified",
            metadata={"method": "backup_code" if "Static" in type(device).__name__ else "totp"},
        )
        if request.user.must_change_password:
            messages.warning(request, "You must change your password before continuing.")
            return redirect(reverse("accounts:password_change"))
        return redirect(self._next())


class BackupCodesView(LoginRequiredMixin, View):
    """Issue a fresh set of backup codes. Needs a verified session."""

    def post(self, request):
        from apps.common.audit import record

        if not request.user.has_confirmed_mfa or not request.user.is_verified():
            messages.error(request, "Verify your authenticator first.")
            return redirect(reverse("accounts:mfa_verify"))
        codes = _issue_backup_codes(request.user)
        record("auth.backup_codes_regenerated")
        return render(
            request, "accounts/mfa_backup_codes.html", {"codes": codes, "just_enrolled": False}
        )


@login_required
def my_account(request):
    """Self-service: own role, scope, sign-in history, consent, own movements.

    Giving every employee visibility of their own record is both a legal
    requirement in most jurisdictions and the cheapest trust-building feature
    in the system.
    """
    from apps.accounts.models import LoginEvent
    from apps.common.permissions import PermissionService

    employee = request.user.employee
    scope = getattr(request.user, "scope", None)

    return render(
        request,
        "accounts/my_account.html",
        {
            "granted_actions": sorted(PermissionService.granted_actions(request.user)),
            "logins": LoginEvent.objects.filter(user=request.user).order_by("-at")[:20],
            "employee": employee,
            "consent": employee.active_consent if employee else None,
            "scope_departments": (
                list(scope.departments.all()) if scope else []
            ),
            "scope_zones": list(scope.zones.all()) if scope else [],
            "has_mfa": request.user.has_confirmed_mfa,
            "mfa_required": request.user.role in MFA_REQUIRED_ROLES,
            "backup_codes_left": _backup_codes_left(request.user),
            "enrollments": (
                list(employee.enrollments.filter(is_active=True).order_by("-created_at"))
                if employee
                else []
            ),
            "tracking_requests_about_me": _requests_about(employee),
            "elevations": request.user.elevations.order_by("-requested_at")[:10],
            "can_request_elevation": bool(_elevatable_actions(request.user)),
        },
    )


def _backup_codes_left(user) -> int:
    from django_otp.plugins.otp_static.models import StaticToken

    return StaticToken.objects.filter(device__user=user).count()


def _requests_about(employee):
    """Tracking requests naming this person, unless notice is formally suppressed."""
    if employee is None:
        return []
    from apps.watchlist.models import TrackingRequest

    return list(
        TrackingRequest.objects.filter(employee=employee, notification_suppressed=False)
        .select_related("requested_by", "approved_by")
        .order_by("-requested_at")[:10]
    )


def _elevatable_actions(user) -> list[str]:
    """Actions this user may ask break-glass elevation for.

    Break-glass exists for one case: a Developer in production who needs
    the identity access they would have in staging. Everyone else who needs
    to look at a person uses a tracking request, which names the person.
    Never authority over people, and never what the user already holds.
    """
    from apps.common.permissions import PermissionService
    from apps.common.roles import DEVELOPER_NONPROD_EXTRA, NEVER_ELEVATABLE, Role

    if user.role != Role.DEVELOPER:
        return []
    held = PermissionService.granted_actions(user)
    return sorted(DEVELOPER_NONPROD_EXTRA - held - NEVER_ELEVATABLE)


class ElevationRequestView(LoginRequiredMixin, View):
    """Ask for time-boxed break-glass access. Someone else approves it."""

    template_name = "accounts/elevation_request.html"

    def _context(self, request, **extra) -> dict:
        from apps.accounts.models import AccessElevation

        return {
            "actions": _elevatable_actions(request.user),
            "max_hours": int(AccessElevation.MAX_DURATION.total_seconds() // 3600),
            **extra,
        }

    def get(self, request):
        if not _elevatable_actions(request.user):
            messages.info(request, "There is nothing break-glass could add to your access.")
            return redirect(reverse("accounts:my_account"))
        return render(request, self.template_name, self._context(request))

    def post(self, request):
        from apps.accounts.models import AccessElevation
        from apps.common.audit import record

        allowed = set(_elevatable_actions(request.user))
        chosen = [a for a in request.POST.getlist("actions") if a in allowed]
        reason = (request.POST.get("reason") or "").strip()

        errors = []
        if not chosen:
            errors.append("Choose at least one capability.")
        if len(reason) < 20:
            errors.append("Explain why, in at least 20 characters. The approver reads this.")
        if AccessElevation.objects.filter(
            user=request.user, status=AccessElevation.Status.PENDING
        ).exists():
            errors.append("You already have a request waiting for a decision.")
        if errors:
            for error in errors:
                messages.error(request, error)
            return render(
                request,
                self.template_name,
                self._context(request, chosen=chosen, reason=reason),
            )

        elevation = AccessElevation.objects.create(
            user=request.user, actions=chosen, reason=reason
        )
        record(
            "elevation.requested",
            object_type="accounts.AccessElevation",
            object_id=elevation.pk,
            reason=reason[:500],
            metadata={"actions": chosen},
        )
        messages.success(
            request,
            "Requested. A Super Admin must approve it; it then lasts at most "
            f"{self._context(request)['max_hours']} hours and ends by itself.",
        )
        return redirect(reverse("accounts:my_account"))


@login_required
def own_enrollment_image(request, enrollment_uuid):
    """One of the signed-in person's own enrolled face images.

    Strictly self-service: the image is served only to the person it shows.
    Seeing exactly what the system holds about you is a right under privacy
    law, and it is the most direct way to spot a wrong photo in the gallery.
    """
    from django.core.exceptions import PermissionDenied
    from django.http import FileResponse, Http404

    from apps.common.audit import record
    from apps.employees.models import FaceEnrollment

    enrollment = FaceEnrollment.objects.filter(uuid=enrollment_uuid).first()
    if enrollment is None:
        raise Http404("No such image")
    if request.user.employee_id is None or enrollment.employee_id != request.user.employee_id:
        record("enrollment.self_view_denied", success=False)
        raise PermissionDenied("You can only view your own enrolled images.")

    try:
        handle = enrollment.image.open("rb")
    except FileNotFoundError as exc:
        raise Http404("The image file is missing") from exc

    record(
        "enrollment.self_view",
        target_employee=enrollment.employee,
        object_type="employees.FaceEnrollment",
        object_id=enrollment.pk,
    )
    response = FileResponse(handle, content_type="image/jpeg")
    response["Cache-Control"] = "no-store, private"
    response["X-Content-Type-Options"] = "nosniff"
    return response
