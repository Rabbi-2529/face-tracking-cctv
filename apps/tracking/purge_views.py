"""Screens for the dual-authorisation purge. Super Admins only (PURGE_EXECUTE)."""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.generic import View

from apps.common.roles import Action
from apps.employees.models import Employee
from apps.tracking import purge
from apps.tracking.models import PurgeRequest
from apps.tracking.views import ActionRequiredMixin


class PurgeForm(forms.Form):
    employee = forms.ModelChoiceField(
        queryset=Employee.objects.none(),
        required=False,
        empty_label="Everyone in the date range",
        help_text="One person, or leave blank to purge everyone's data in the date range.",
    )
    date_from = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}), label="From (inclusive)")
    date_to = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}), label="To (inclusive)")
    kinds = forms.MultipleChoiceField(
        choices=PurgeRequest.Kind.choices,
        widget=forms.CheckboxSelectMultiple,
        label="What to delete",
    )
    reason = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "e.g. Erasure request from the employee, ticket DP-42, approved by the DPO."}),
        help_text="Read by the approving Super Admin and kept with the record forever.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Soft-deleted people included: an erasure often follows someone leaving.
        self.fields["employee"].queryset = Employee.objects.order_by("full_name")

    def clean(self):
        data = super().clean()
        try:
            purge.validate(
                data.get("employee"), data.get("date_from"), data.get("date_to"),
                data.get("kinds") or [], data.get("reason") or "",
            )
        except ValidationError as exc:
            for field, errors in exc.message_dict.items():
                self.add_error(field if field in self.fields else None, errors)
        return data


class PurgeListView(ActionRequiredMixin, View):
    template_name = "tracking/purge_list.html"
    required_action = Action.PURGE_EXECUTE

    def get(self, request):
        purge.expire_stale()
        requests = PurgeRequest.objects.select_related("requested_by", "decided_by", "employee")
        return render(request, self.template_name, {
            "pending": requests.filter(status=PurgeRequest.Status.PENDING),
            "decided": requests.exclude(status=PurgeRequest.Status.PENDING)[:50],
        })


class PurgeCreateView(ActionRequiredMixin, View):
    template_name = "tracking/purge_form.html"
    required_action = Action.PURGE_EXECUTE

    def get(self, request):
        return render(request, self.template_name, {"form": PurgeForm()})

    def post(self, request):
        form = PurgeForm(request.POST)
        if not form.is_valid():
            return render(request, self.template_name, {"form": form})
        data = form.cleaned_data
        if request.POST.get("action") == "preview":
            counts = purge.preview(data["employee"], data["date_from"], data["date_to"], data["kinds"])
            return render(request, self.template_name, {"form": form, "preview": counts})
        created = purge.create(
            request.user, employee=data["employee"], date_from=data["date_from"],
            date_to=data["date_to"], kinds=data["kinds"], reason=data["reason"],
        )
        messages.success(
            request,
            "Purge requested. Nothing is deleted until a different Super Admin approves it "
            f"(within {PurgeRequest.EXPIRY_DAYS} days).",
        )
        return redirect(reverse("tracking:purge_detail", args=[created.uuid]))


class PurgeDetailView(ActionRequiredMixin, View):
    template_name = "tracking/purge_detail.html"
    required_action = Action.PURGE_EXECUTE

    def get(self, request, request_uuid):
        purge.expire_stale()
        item = get_object_or_404(PurgeRequest.objects.select_related("requested_by", "decided_by"), uuid=request_uuid)
        current = None
        if item.status == PurgeRequest.Status.PENDING:
            current = purge.preview(item.employee, item.date_from, item.date_to, item.kinds)
        return render(request, self.template_name, {"item": item, "current": current})

    def post(self, request, request_uuid):
        item = get_object_or_404(PurgeRequest, uuid=request_uuid)
        action = request.POST.get("action")
        note = request.POST.get("note", "")
        try:
            if action == "approve":
                if request.POST.get("confirm") != "DELETE":
                    raise ValidationError('Type DELETE to confirm. A purge cannot be undone.')
                purge.approve_and_execute(item, request.user, note)
                messages.warning(request, "Purge executed. The record of it is kept below and in the audit log.")
            elif action == "reject":
                purge.reject(item, request.user, note)
                messages.info(request, "Purge rejected; nothing was deleted.")
            elif action == "cancel":
                purge.cancel(item, request.user)
                messages.info(request, "Purge request cancelled; nothing was deleted.")
            else:
                raise ValidationError("Unknown action.")
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        return redirect(reverse("tracking:purge_detail", args=[item.uuid]))
