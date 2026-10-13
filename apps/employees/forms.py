"""
Employee and consent forms for the HR panel.

The flow these support is deliberately ordered: **add the person, record
their consent, then enrol their face.** That order is not bureaucracy - an
employee with no consent record is not matched by the pipeline at all, so
enrolling faces first produces a gallery that does nothing and an HR user who
believes the system is broken.

The form says so plainly at each step rather than letting someone discover it
later.
"""

from __future__ import annotations

import datetime as dt

from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.employees.models import ConsentRecord, Employee
from apps.org.models import Department, Designation, Zone

# Bumped when the privacy notice materially changes. Consent is to a specific
# notice, so a new purpose needs fresh consent rather than a reused record.
CURRENT_POLICY_VERSION = "2026.1"


class EmployeeForm(forms.ModelForm):
    """Create or edit one employee record."""

    class Meta:
        model = Employee
        fields = [
            "employee_code",
            "full_name",
            "department",
            "designation",
            "date_of_joining",
            "status",
            "home_zone",
            "is_trackable",
        ]
        widgets = {
            "date_of_joining": forms.DateInput(attrs={"type": "date"}),
            "employee_code": forms.TextInput(attrs={"placeholder": "EMP009"}),
            "full_name": forms.TextInput(attrs={"placeholder": "Full legal name"}),
        }
        help_texts = {
            "employee_code": "Must match your HR system. Used in reports and the audit log.",
            "is_trackable": (
                "Uncheck to exclude this person from face matching entirely, "
                "regardless of consent or enrolment."
            ),
            "home_zone": "Usual desk location. Used only to reduce report noise.",
        }

    def __init__(self, *args, scope_departments=None, **kwargs):
        super().__init__(*args, **kwargs)

        # HR is scoped to departments; the form must not let them create a
        # person outside that scope and then lose sight of the record.
        if scope_departments is not None:
            self.fields["department"].queryset = scope_departments
            self.fields["department"].required = True
            self.fields["department"].help_text = (
                "Limited to the departments you administer."
            )
        else:
            self.fields["department"].queryset = Department.objects.all()

        self.fields["designation"].queryset = Designation.objects.all()
        self.fields["designation"].required = False
        self.fields["home_zone"].queryset = Zone.objects.alive().filter(
            tracking_prohibited=False
        ).select_related("floor")
        self.fields["home_zone"].required = False

    def clean_employee_code(self) -> str:
        code = self.cleaned_data["employee_code"].strip().upper()
        existing = Employee.objects.filter(employee_code=code)
        if self.instance.pk:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            match = existing.first()
            state = "deleted" if match.deleted_at else "active"
            raise ValidationError(
                f"Employee code {code} is already used by {match.full_name} "
                f"({state} record)."
            )
        return code

    def clean_full_name(self) -> str:
        name = " ".join(self.cleaned_data["full_name"].split())
        if len(name) < 2:
            raise ValidationError("Give the person's full name.")
        return name

    def clean_date_of_joining(self):
        joined = self.cleaned_data.get("date_of_joining")
        if joined and joined > timezone.localdate() + dt.timedelta(days=365):
            raise ValidationError("That date is more than a year in the future.")
        return joined


class ConsentForm(forms.ModelForm):
    """Record consent to biometric processing.

    This is the form that makes the whole system lawful, so it asks for
    evidence rather than a tick. "We told everyone" is not a record; a signed
    notice is.
    """

    confirm_notice_given = forms.BooleanField(
        required=True,
        label="I confirm this employee was given the privacy notice before consenting",
        help_text=(
            "Consent is only valid if the person was told what is collected, "
            "why, who can see it, and how long it is kept."
        ),
    )

    class Meta:
        model = ConsentRecord
        fields = ["policy_version", "method", "consented_at", "document"]
        widgets = {
            "consented_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
        }
        help_texts = {
            "policy_version": "Version of the privacy notice the person consented to.",
            "document": (
                "The signed form, scanned. Strongly recommended - without it you "
                "have no evidence of consent if it is ever challenged."
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["policy_version"].initial = CURRENT_POLICY_VERSION
        self.fields["consented_at"].initial = timezone.localtime().strftime(
            "%Y-%m-%dT%H:%M"
        )
        self.fields["document"].required = False

    def clean_consented_at(self):
        when = self.cleaned_data.get("consented_at")
        if when and when > timezone.now() + dt.timedelta(minutes=5):
            raise ValidationError("Consent cannot be dated in the future.")
        return when

    def clean(self):
        data = super().clean()
        method = data.get("method")
        document = data.get("document")

        # A paper form with no scan is the case most likely to be disputed
        # later, so it is called out rather than silently accepted.
        if method == ConsentRecord.Method.WRITTEN and not document:
            self.add_warning(
                "A signed paper form was selected but no scan was attached. "
                "Upload it, or the consent record has no supporting evidence."
            )
        return data

    def add_warning(self, message: str) -> None:
        if not hasattr(self, "warnings"):
            self.warnings = []
        self.warnings.append(message)

    def get_warnings(self) -> list[str]:
        return getattr(self, "warnings", [])


class EmployeeSearchForm(forms.Form):
    q = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "Name or employee code"}),
    )
    status = forms.ChoiceField(
        required=False, choices=[("", "Any status"), *Employee.Status.choices]
    )
    readiness = forms.ChoiceField(
        required=False,
        choices=[
            ("", "Any"),
            ("not_matchable", "Not matchable yet"),
            ("no_consent", "Missing consent"),
            ("thin_gallery", "Fewer than 5 images"),
            ("ready", "Fully set up"),
        ],
        label="Setup state",
    )
