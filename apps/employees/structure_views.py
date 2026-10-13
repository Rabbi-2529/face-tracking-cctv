"""
Departments and designations: the lists the employee form chooses from.

Managed here, next to the people they describe, rather than only under site
settings. Two different permissions, on purpose:

* **Designations** (job titles) are descriptive. Anyone who manages
  employees may add and edit them.
* **Departments** decide access: an HR user sees exactly the employees in the
  departments assigned to them. Adding, renaming or removing one changes who
  can see whom, so it needs USER_MANAGE - the same authority that assigns
  those scopes.

Both offer a quick-add endpoint, so the employee form can create a missing
entry without leaving the page. Every change is audited; nothing in use can
be deleted.
"""

from __future__ import annotations

import json

from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.generic import View

from apps.common.audit import record
from apps.common.permissions import PermissionService
from apps.common.roles import Action
from apps.org.models import Department, Designation

DEPARTMENT_ACTION = Action.USER_MANAGE
DESIGNATION_ACTION = Action.EMPLOYEE_MANAGE


class DepartmentForm(forms.ModelForm):
    class Meta:
        model = Department
        fields = ["name", "code", "head"]
        help_texts = {
            "code": "Short stable code, e.g. 'FIN'. Used in reports and exports.",
            "head": "Optional.",
        }

    def __init__(self, *args, **kwargs):
        from apps.employees.models import Employee

        super().__init__(*args, **kwargs)
        self.fields["head"].queryset = Employee.objects.alive().order_by("full_name")
        self.fields["head"].required = False

    def clean_name(self) -> str:
        return " ".join(self.cleaned_data["name"].split())

    def clean_code(self) -> str:
        code = self.cleaned_data["code"].strip().upper()
        clash = Department.objects.filter(code=code).exclude(pk=self.instance.pk or 0)
        if clash.exists():
            raise ValidationError(f"Code {code} is already used by {clash.first().name}.")
        return code


class DesignationForm(forms.ModelForm):
    class Meta:
        model = Designation
        fields = ["title", "level"]
        help_texts = {"level": "Optional seniority order, lowest first. Used only for sorting."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Optional, so the employee form's quick-add can send just a title.
        self.fields["level"].required = False

    def clean_level(self) -> int:
        return self.cleaned_data.get("level") or 0

    def clean_title(self) -> str:
        title = " ".join(self.cleaned_data["title"].split())
        clash = Designation.objects.filter(title__iexact=title).exclude(pk=self.instance.pk or 0)
        if clash.exists():
            raise ValidationError(f"'{clash.first().title}' already exists.")
        return title


KINDS = {
    "department": {
        "model": Department,
        "form": DepartmentForm,
        "action": DEPARTMENT_ACTION,
        "label": "Department",
        "blurb": (
            "Departments decide access: an HR user sees the employees of the "
            "departments assigned to them under Users & roles."
        ),
    },
    "designation": {
        "model": Designation,
        "form": DesignationForm,
        "action": DESIGNATION_ACTION,
        "label": "Designation",
        "blurb": "Job titles shown on employee records.",
    },
}


class StructureMixin(LoginRequiredMixin):
    def kind(self, kind: str) -> dict:
        spec = KINDS.get(kind)
        if spec is None:
            raise PermissionDenied("Unknown list.")
        return spec

    def require(self, request, action: str) -> None:
        decision = PermissionService.can(request.user, action)
        if not decision:
            record(f"structure.denied:{action}", reason=decision.reason, success=False)
            raise PermissionDenied(decision.reason)


class StructureView(StructureMixin, View):
    """Both lists on one page, with usage counts."""

    template_name = "employees/structure.html"

    def get(self, request):
        granted = PermissionService.granted_actions(request.user)
        can_departments = DEPARTMENT_ACTION in granted
        can_designations = DESIGNATION_ACTION in granted
        if not (can_departments or can_designations or Action.EMPLOYEE_VIEW in granted):
            self.require(request, Action.EMPLOYEE_VIEW)

        departments = Department.objects.annotate(
            employee_count=Count("employees", filter=Q(employees__deleted_at__isnull=True), distinct=True),
            scoped_user_count=Count("scoped_users", distinct=True),
        ).select_related("head").order_by("name")
        designations = Designation.objects.annotate(
            employee_count=Count("employees", filter=Q(employees__deleted_at__isnull=True), distinct=True),
        ).order_by("level", "title")
        return render(
            request,
            self.template_name,
            {
                "departments": departments,
                "designations": designations,
                "can_departments": bool(PermissionService.can(request.user, DEPARTMENT_ACTION)),
                "can_designations": bool(PermissionService.can(request.user, DESIGNATION_ACTION)),
            },
        )


class StructureEditView(StructureMixin, View):
    template_name = "employees/structure_form.html"

    def _render(self, request, spec, form, instance):
        return render(
            request,
            self.template_name,
            {"form": form, "spec": spec, "instance": instance, "kind": spec["label"].lower()},
        )

    def get(self, request, kind, pk=None):
        spec = self.kind(kind)
        self.require(request, spec["action"])
        instance = get_object_or_404(spec["model"], pk=pk) if pk else None
        return self._render(request, spec, spec["form"](instance=instance), instance)

    def post(self, request, kind, pk=None):
        spec = self.kind(kind)
        self.require(request, spec["action"])
        instance = get_object_or_404(spec["model"], pk=pk) if pk else None
        form = spec["form"](request.POST, instance=instance)
        if not form.is_valid():
            return self._render(request, spec, form, instance)
        saved = form.save()
        record(
            f"{kind}.{'updated' if instance else 'created'}",
            object_type=f"org.{spec['model'].__name__}",
            object_id=saved.pk,
            reason=str(saved),
        )
        messages.success(request, f"{spec['label']} '{saved}' saved.")
        return redirect(reverse("employees:structure") + f"#{kind}")


class StructureDeleteView(StructureMixin, View):
    """Delete an entry nothing refers to. In-use entries are refused."""

    def post(self, request, kind, pk):
        spec = self.kind(kind)
        self.require(request, spec["action"])
        instance = get_object_or_404(spec["model"], pk=pk)

        in_use = instance.employees.filter(deleted_at__isnull=True).count()
        scoped = instance.scoped_users.count() if kind == "department" else 0
        if in_use or scoped:
            parts = []
            if in_use:
                parts.append(f"{in_use} employee(s)")
            if scoped:
                parts.append(f"{scoped} user access scope(s)")
            messages.error(
                request,
                f"'{instance}' is still used by {' and '.join(parts)}. Move them first.",
            )
            return redirect(reverse("employees:structure") + f"#{kind}")

        label = str(instance)
        instance.delete()
        record(f"{kind}.deleted", object_type=f"org.{spec['model'].__name__}", object_id=pk, reason=label)
        messages.info(request, f"{spec['label']} '{label}' deleted.")
        return redirect(reverse("employees:structure") + f"#{kind}")


class QuickAddView(StructureMixin, View):
    """Create an entry from the employee form without leaving it. JSON in and out."""

    def post(self, request, kind):
        spec = self.kind(kind)
        decision = PermissionService.can(request.user, spec["action"])
        if not decision:
            record(f"structure.denied:{spec['action']}", reason=decision.reason, success=False)
            return JsonResponse({"error": decision.reason}, status=403)

        try:
            payload = json.loads(request.body or b"{}")
        except json.JSONDecodeError:
            return JsonResponse({"error": "Malformed request."}, status=400)

        form = spec["form"](payload)
        if not form.is_valid():
            first = next(iter(form.errors.values()))[0]
            return JsonResponse({"error": first, "errors": form.errors}, status=400)
        saved = form.save()
        record(
            f"{kind}.created",
            object_type=f"org.{spec['model'].__name__}",
            object_id=saved.pk,
            reason=str(saved),
            metadata={"via": "quick_add"},
        )
        return JsonResponse({"id": saved.pk, "text": str(saved)})
