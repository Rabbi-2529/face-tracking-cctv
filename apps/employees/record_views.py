"""
Employee record screens: add, edit, setup checklist, consent.

Kept separate from :mod:`apps.employees.views` (which handles face enrolment)
because these are different jobs with different permissions: EMPLOYEE_MANAGE
creates people, CONSENT_MANAGE establishes the lawful basis, and
ENROLLMENT_MANAGE handles biometrics.

The three steps are presented in a fixed order on purpose. An employee with
no consent record is not matched by the pipeline at all, so enrolling faces
first produces a gallery that does nothing and an HR user who concludes the
system is broken. :class:`EmployeeSetupView` makes the order and the current
state visible rather than leaving it to be discovered.
"""

from __future__ import annotations

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
from apps.employees.enrollment import RECOMMENDED_IMAGES, gallery_coverage
from apps.employees.forms import ConsentForm, EmployeeForm
from apps.employees.models import Employee
from apps.org.models import Department


class EmployeeManageMixin(LoginRequiredMixin):
    """Requires EMPLOYEE_MANAGE and keeps HR inside its department scope."""

    def check_access(self, request) -> None:
        decision = PermissionService.can(request.user, Action.EMPLOYEE_MANAGE)
        if not decision:
            record("employee.denied", reason=decision.reason, success=False)
            raise PermissionDenied(decision.reason)

    def scope_departments(self, request):
        """Departments this user may file people under.

        Returns None for roles with organisation-wide reach, which the form
        reads as "no restriction".
        """
        granted = PermissionService.granted_actions(request.user)
        if Action.HISTORY_ANY in granted:
            return None

        scope = getattr(request.user, "scope", None)
        if scope is None:
            # Fail closed: an unscoped HR user gets an empty list and a clear
            # message, never the whole organisation.
            return Department.objects.none()
        return scope.departments.all()

    def get_employee(self, request, employee_uuid) -> Employee:
        return get_object_or_404(
            visible_employees(request.user, Employee.objects.alive()),
            uuid=employee_uuid,
        )


class EmployeeCreateView(EmployeeManageMixin, View):
    """Add one employee, then go straight to the consent step."""

    template_name = "employees/employee_form.html"

    def get(self, request):
        self.check_access(request)
        departments = self.scope_departments(request)

        if departments is not None and not departments.exists():
            messages.error(
                request,
                "You have no departments assigned, so you cannot create "
                "employees. Ask an administrator to set your access scope.",
            )
            return redirect(reverse("tracking:employee_list"))

        form = EmployeeForm(scope_departments=departments)
        return render(request, self.template_name, {"form": form, "creating": True})

    def post(self, request):
        self.check_access(request)
        departments = self.scope_departments(request)
        form = EmployeeForm(request.POST, scope_departments=departments)

        if not form.is_valid():
            return render(
                request, self.template_name, {"form": form, "creating": True}
            )

        employee = form.save()
        record(
            "employee.created",
            target_employee=employee,
            object_type="employees.Employee",
            object_id=employee.pk,
            reason=f"{employee.employee_code} {employee.full_name}",
            metadata={
                "department": employee.department.code if employee.department else None
            },
        )
        messages.success(
            request,
            f"{employee.full_name} added. Next: record their consent. Until that "
            "exists they will not be matched, however many faces you enrol.",
        )
        return redirect(reverse("employees:setup", args=[employee.uuid]))


class EmployeeEditView(EmployeeManageMixin, View):
    template_name = "employees/employee_form.html"

    def get(self, request, employee_uuid):
        self.check_access(request)
        employee = self.get_employee(request, employee_uuid)
        form = EmployeeForm(
            instance=employee, scope_departments=self.scope_departments(request)
        )
        return render(
            request,
            self.template_name,
            {"form": form, "employee": employee, "creating": False},
        )

    def post(self, request, employee_uuid):
        self.check_access(request)
        employee = self.get_employee(request, employee_uuid)
        form = EmployeeForm(
            request.POST,
            instance=employee,
            scope_departments=self.scope_departments(request),
        )

        if not form.is_valid():
            return render(
                request,
                self.template_name,
                {"form": form, "employee": employee, "creating": False},
            )

        was_trackable = employee.is_trackable
        saved = form.save()

        record(
            "employee.updated",
            target_employee=saved,
            object_type="employees.Employee",
            object_id=saved.pk,
            reason=f"edited {saved.employee_code}",
            metadata={"changed": sorted(form.changed_data)},
        )

        if was_trackable and not saved.is_trackable:
            messages.warning(
                request,
                f"{saved.full_name} is no longer trackable. Matching stops "
                "within about 30 seconds.",
            )
        else:
            messages.success(request, f"{saved.full_name} updated.")
        return redirect(reverse("employees:setup", args=[saved.uuid]))


class EmployeeSetupView(LoginRequiredMixin, View):
    """One employee: record, consent, gallery, readiness.

    Answers the only question HR actually has - "is this person set up?" -
    on one page, instead of requiring three unrelated screens to be compared.
    """

    template_name = "employees/employee_setup.html"

    def get(self, request, employee_uuid):
        decision = PermissionService.can(request.user, Action.EMPLOYEE_VIEW)
        if not decision:
            raise PermissionDenied(decision.reason)

        employee = get_object_or_404(
            visible_employees(request.user, Employee.objects.alive()).select_related(
                "department", "designation", "home_zone"
            ),
            uuid=employee_uuid,
        )

        coverage = gallery_coverage(employee)
        consent = employee.active_consent

        steps = [
            {
                "key": "record",
                "label": "Employee record",
                "done": True,
                "detail": f"{employee.employee_code} in "
                + (
                    employee.department.name
                    if employee.department
                    else "no department"
                ),
                "url": reverse("employees:edit", args=[employee.uuid]),
                "action": "Edit",
            },
            {
                "key": "consent",
                "label": "Consent to biometric processing",
                "done": consent is not None,
                "detail": (
                    f"Version {consent.policy_version}, "
                    f"{consent.get_method_display().lower()}, "
                    f"{consent.consented_at:%d %b %Y}"
                    if consent
                    else "Not recorded. This person will NOT be matched."
                ),
                "url": reverse("employees:consent", args=[employee.uuid]),
                "action": "Record consent" if consent is None else "Manage",
            },
            {
                "key": "faces",
                "label": "Enrolled face images",
                "done": coverage["active_count"] >= RECOMMENDED_IMAGES,
                "detail": (
                    f"{coverage['active_count']} of {RECOMMENDED_IMAGES} recommended"
                    + (
                        ", poses: " + ", ".join(coverage["by_pose"])
                        if coverage["by_pose"]
                        else ""
                    )
                ),
                "url": reverse("employees:enroll", args=[employee.uuid]),
                "action": "Enrol faces",
            },
        ]

        return render(
            request,
            self.template_name,
            {
                "employee": employee,
                "coverage": coverage,
                "consent": consent,
                "consent_history": employee.consent_records.order_by("-consented_at"),
                "steps": steps,
                "all_done": coverage["is_processable"],
                "can_manage": bool(
                    PermissionService.can(request.user, Action.EMPLOYEE_MANAGE)
                ),
                "can_enroll": bool(
                    PermissionService.can(request.user, Action.ENROLLMENT_MANAGE)
                ),
                "can_consent": bool(
                    PermissionService.can(request.user, Action.CONSENT_MANAGE)
                ),
            },
        )


class ConsentView(LoginRequiredMixin, View):
    """Record or withdraw consent for one employee."""

    template_name = "employees/consent_form.html"

    def _check(self, request) -> None:
        decision = PermissionService.can(request.user, Action.CONSENT_MANAGE)
        if not decision:
            record("consent.denied", reason=decision.reason, success=False)
            raise PermissionDenied(decision.reason)

    def _employee(self, request, employee_uuid) -> Employee:
        return get_object_or_404(
            visible_employees(request.user, Employee.objects.alive()),
            uuid=employee_uuid,
        )

    def _context(self, employee, form) -> dict:
        return {
            "employee": employee,
            "form": form,
            "current": employee.active_consent,
            "history": employee.consent_records.order_by("-consented_at"),
        }

    def get(self, request, employee_uuid):
        self._check(request)
        employee = self._employee(request, employee_uuid)
        return render(request, self.template_name, self._context(employee, ConsentForm()))

    def post(self, request, employee_uuid):
        self._check(request)
        employee = self._employee(request, employee_uuid)

        if request.POST.get("withdraw"):
            return self._withdraw(request, employee)

        form = ConsentForm(request.POST, request.FILES)
        if not form.is_valid():
            return render(request, self.template_name, self._context(employee, form))

        consent = form.save(commit=False)
        consent.employee = employee
        consent.recorded_by = request.user
        consent.save()

        record(
            "consent.recorded",
            target_employee=employee,
            object_type="employees.ConsentRecord",
            object_id=consent.pk,
            reason=f"version {consent.policy_version} via {consent.method}",
            metadata={
                "policy_version": consent.policy_version,
                "method": consent.method,
                "has_document": bool(consent.document),
            },
        )

        for warning in form.get_warnings():
            messages.warning(request, warning)

        messages.success(
            request,
            "Consent recorded. This person can be matched once at least one "
            "face image is enrolled.",
        )
        return redirect(reverse("employees:setup", args=[employee.uuid]))

    def _withdraw(self, request, employee):
        """Withdraw consent and stop processing.

        Deactivates the gallery immediately; the vision workers drop the
        person on their next refresh, about 30 seconds later.
        """
        consent = employee.active_consent
        if consent is None:
            messages.error(request, "There is no active consent to withdraw.")
            return redirect(reverse("employees:setup", args=[employee.uuid]))

        note = request.POST.get("note", "").strip()
        consent.withdraw(note=note or f"withdrawn by {request.user.email}")

        record(
            "consent.withdrawn",
            target_employee=employee,
            object_type="employees.ConsentRecord",
            object_id=consent.pk,
            reason=note,
        )
        messages.warning(
            request,
            f"Consent withdrawn for {employee.full_name}. Their enrolled images "
            "are deactivated and matching stops within about 30 seconds. "
            "Historical records are unaffected and age out under the retention "
            "policy.",
        )
        return redirect(reverse("employees:setup", args=[employee.uuid]))
