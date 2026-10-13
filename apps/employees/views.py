"""
Face enrolment screens.

Enrolment gives immediate, specific feedback rather than queueing a job. HR
is standing there with a photo; "too dark, avoid shooting into shadow" while
they can still take another one is worth far more than the same message in an
inbox ten minutes later.

Employee records and consent live in :mod:`apps.employees.record_views` -
different job, different permission.
"""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.generic import View

from apps.common.audit import record
from apps.common.permissions import PermissionService
from apps.common.roles import Action
from apps.common.scoping import visible_employees
from apps.employees.enrollment import (
    RECOMMENDED_IMAGES,
    deactivate_enrollment,
    enroll_image,
    gallery_coverage,
    resolve_conflict,
)
from apps.employees.models import Employee, FaceEnrollment


class MultipleFileInput(forms.ClearableFileInput):
    """Django refuses `multiple` on the stock widget unless opted in."""

    allow_multiple_selected = True


class MultipleImageField(forms.FileField):
    """Validates every file in a multi-file upload, not just the first."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput(attrs={"accept": "image/*"}))
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single = super().clean
        if isinstance(data, list | tuple):
            return [single(item, initial) for item in data]
        return [single(data, initial)]


class EnrollmentUploadForm(forms.Form):
    images = MultipleImageField(
        label="Face images",
        help_text=(
            f"Upload {RECOMMENDED_IMAGES} or more: one frontal, a couple turned "
            "slightly left and right, and with and without glasses if worn. "
            "One face per image."
        ),
    )


class EnrollmentActionMixin(LoginRequiredMixin):
    """Requires ENROLLMENT_MANAGE and limits access to employees in scope."""

    def get_employee(self, request, employee_uuid) -> Employee:
        decision = PermissionService.can(request.user, Action.ENROLLMENT_MANAGE)
        if not decision:
            record(
                "enrollment.denied", reason=decision.reason, success=False
            )
            raise PermissionDenied(decision.reason)

        employee = get_object_or_404(
            visible_employees(request.user, Employee.objects.alive()),
            uuid=employee_uuid,
        )
        return employee


class EnrollmentView(EnrollmentActionMixin, View):
    """Upload and review the gallery for one employee."""

    template_name = "employees/enroll.html"

    def _context(self, request, employee) -> dict:
        enrollments = employee.enrollments.select_related("conflict_with").order_by(
            "-is_active", "-quality_score"
        )
        return {
            "employee": employee,
            "form": EnrollmentUploadForm(),
            "coverage": gallery_coverage(employee),
            "enrollments": enrollments,
            "recommended": RECOMMENDED_IMAGES,
        }

    def get(self, request, employee_uuid):
        employee = self.get_employee(request, employee_uuid)
        return render(request, self.template_name, self._context(request, employee))

    def post(self, request, employee_uuid):
        employee = self.get_employee(request, employee_uuid)

        uploads = request.FILES.getlist("images")
        if not uploads:
            messages.error(request, "Choose at least one image.")
            return redirect(reverse("employees:enroll", args=[employee.uuid]))

        accepted = 0
        results = []
        for upload in uploads:
            result = enroll_image(
                employee,
                upload.read(),
                filename=upload.name,
                enrolled_by=request.user,
            )
            results.append((upload.name, result))
            if result.accepted:
                accepted += 1

        for name, result in results:
            if result.accepted:
                messages.success(request, f"{name}: {result.message}")
            elif result.needs_review:
                messages.warning(request, f"{name}: {result.message}")
            else:
                messages.error(request, f"{name}: {result.message}")

        if accepted and not employee.has_active_consent:
            messages.warning(
                request,
                "This employee has no active consent record. They will not be "
                "matched until one is recorded, regardless of enrolment.",
            )

        context = self._context(request, employee)
        context["last_results"] = results
        return render(request, self.template_name, context)


class EnrollmentDeactivateView(EnrollmentActionMixin, View):
    """Remove one image from the live gallery."""

    def post(self, request, employee_uuid, enrollment_uuid):
        employee = self.get_employee(request, employee_uuid)
        enrollment = get_object_or_404(
            FaceEnrollment, uuid=enrollment_uuid, employee=employee
        )
        deactivate_enrollment(
            enrollment,
            actor=request.user,
            reason=request.POST.get("reason", "removed by HR"),
        )
        messages.info(
            request,
            "Image removed from the gallery. Matching stops using it within "
            "about 30 seconds.",
        )
        return redirect(reverse("employees:enroll", args=[employee.uuid]))


class EnrollmentConflictView(EnrollmentActionMixin, View):
    """Resolve an image flagged as matching a different employee."""

    def post(self, request, employee_uuid, enrollment_uuid):
        employee = self.get_employee(request, employee_uuid)
        enrollment = get_object_or_404(
            FaceEnrollment, uuid=enrollment_uuid, employee=employee
        )

        activate = request.POST.get("resolution") == "accept"
        note = request.POST.get("note", "").strip()

        if activate and not note:
            messages.error(
                request,
                "Accepting a conflicting image needs a note explaining why "
                "(for example: confirmed identical twins).",
            )
            return redirect(reverse("employees:enroll", args=[employee.uuid]))

        resolve_conflict(enrollment, activate=activate, actor=request.user, note=note)
        if activate:
            messages.warning(
                request,
                "Image activated despite the conflict. Expect lower confidence "
                "for both people, and watch the correction queue.",
            )
        else:
            messages.info(request, "Image rejected and left out of the gallery.")
        return redirect(reverse("employees:enroll", args=[employee.uuid]))
