"""
Alert inbox and rule configuration.

Two separate permissions, as in the role matrix: ``ALERT_ACK`` to see and
handle alerts, ``ALERTRULE_MANAGE`` to decide what raises them. The inbox is
scoped to the zones a viewer may monitor, so a Security Operator sees alerts
for their own zones only. Who an alert concerns is shown only to viewers
with organisation-wide history access; an operator responding at the door
sees what happened and where, and the evidence image, not a name.
"""

from __future__ import annotations

from types import SimpleNamespace

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.generic import ListView, View

from apps.alerts.engine import clear_rule_cache
from apps.alerts.models import Alert, AlertRule
from apps.common.audit import record
from apps.common.permissions import PermissionService
from apps.common.roles import Action
from apps.common.scoping import visible_zones
from apps.employees.models import Employee
from apps.org.models import Department, Zone
from apps.tracking.views import ActionRequiredMixin


def visible_alerts(user, qs):
    """Alerts in zones this user may monitor. Never-tracked zones never appear."""
    zone_ids = visible_zones(user, Zone.objects.alive()).values_list("pk", flat=True)
    return qs.filter(zone_id__in=zone_ids)


class AlertInboxView(ActionRequiredMixin, ListView):
    template_name = "alerts/inbox.html"
    context_object_name = "alerts"
    paginate_by = 50
    required_action = Action.ALERT_ACK

    def get_queryset(self):
        status = self.request.GET.get("status", "active")
        qs = visible_alerts(
            self.request.user,
            Alert.objects.select_related("zone", "camera", "employee", "rule"),
        )
        if status == "active":
            qs = qs.filter(status__in=[Alert.Status.OPEN, Alert.Status.ACKNOWLEDGED])
        elif status in Alert.Status.values:
            qs = qs.filter(status=status)
        return qs.order_by("status", "-last_seen_at")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        ctx["status"] = self.request.GET.get("status", "active")
        ctx["statuses"] = Alert.Status.choices
        ctx["show_identity"] = bool(PermissionService.can(user, Action.HISTORY_ANY))
        ctx["can_view_snapshots"] = bool(PermissionService.can_view_snapshot(user))
        ctx["can_manage_rules"] = bool(PermissionService.can(user, Action.ALERTRULE_MANAGE))
        named = [a.employee_id for a in ctx["alerts"] if a.employee_id]
        record(
            "alerts.view",
            metadata={"status": ctx["status"], "identities_shown": bool(named and ctx["show_identity"])},
        )
        return ctx


class AlertActionView(ActionRequiredMixin, View):
    """Acknowledge ("someone is on it") or resolve ("dealt with") an alert."""

    required_action = Action.ALERT_ACK

    def post(self, request, alert_uuid):
        alert = get_object_or_404(visible_alerts(request.user, Alert.objects.all()), uuid=alert_uuid)
        verb = request.POST.get("action")
        note = (request.POST.get("note") or "").strip()
        now = timezone.now()

        if verb == "acknowledge" and alert.status == Alert.Status.OPEN:
            alert.status = Alert.Status.ACKNOWLEDGED
            alert.acknowledged_by, alert.acknowledged_at = request.user, now
        elif verb == "resolve" and alert.status != Alert.Status.RESOLVED:
            if not note:
                messages.error(request, "Say how it was resolved.")
                return redirect(reverse("alerts:inbox"))
            alert.status = Alert.Status.RESOLVED
            alert.resolved_by, alert.resolved_at = request.user, now
            if alert.acknowledged_at is None:
                alert.acknowledged_by, alert.acknowledged_at = request.user, now
        else:
            messages.error(request, "That action does not apply to this alert.")
            return redirect(reverse("alerts:inbox"))

        if note:
            alert.note = (alert.note + "\n" + note).strip()
        alert.save()
        record(
            f"alert.{alert.status}",
            target_employee=alert.employee,
            object_type="alerts.Alert",
            object_id=alert.pk,
            reason=note[:500] or alert.message,
        )
        return redirect(reverse("alerts:inbox"))


def alert_snapshot(request, alert_uuid):
    """Evidence image for an alert.

    Operators get it inside their live window only, like any snapshot; the
    zone must be one the viewer monitors. Audited like every image.
    """
    if not request.user.is_authenticated:
        raise PermissionDenied("not authenticated")
    alert = get_object_or_404(
        visible_alerts(request.user, Alert.objects.select_related("employee", "zone")),
        uuid=alert_uuid,
    )
    for decision in (
        PermissionService.can(request.user, Action.ALERT_ACK),
        PermissionService.can_view_snapshot(
            request.user, SimpleNamespace(captured_at=alert.triggered_at)
        ),
    ):
        if not decision:
            record("snapshot.denied", target_employee=alert.employee, reason=decision.reason, success=False)
            raise PermissionDenied(decision.reason)
    if not alert.evidence_snapshot_key:
        raise Http404("No image for this alert")

    from apps.tracking.views import _stream_snapshot

    return _stream_snapshot(
        alert.evidence_snapshot_key,
        filename=f"alert-{str(alert.uuid)[:8]}.jpg",
        target_employee=alert.employee,
        object_type="alerts.Alert",
        object_id=alert.pk,
        metadata={"zone": alert.zone.code if alert.zone else None},
    )


# --------------------------------------------------------------------- rules


class AlertRuleForm(forms.ModelForm):
    class Meta:
        model = AlertRule
        fields = [
            "name",
            "rule_type",
            "severity",
            "is_enabled",
            "zone",
            "authorised_departments",
            "authorised_employees",
            "applies_to_departments",
            "after_hours_start",
            "after_hours_end",
            "weekends_count",
        ]
        widgets = {
            "after_hours_start": forms.TimeInput(attrs={"type": "time"}),
            "after_hours_end": forms.TimeInput(attrs={"type": "time"}),
            "authorised_departments": forms.CheckboxSelectMultiple,
            "applies_to_departments": forms.CheckboxSelectMultiple,
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["zone"].queryset = (
            Zone.objects.alive().filter(tracking_prohibited=False).select_related("floor")
        )
        departments = Department.objects.order_by("name")
        self.fields["authorised_departments"].queryset = departments
        self.fields["applies_to_departments"].queryset = departments
        self.fields["authorised_employees"].queryset = Employee.objects.alive().order_by(
            "full_name"
        )
        self.fields["authorised_employees"].widget.attrs["size"] = 6


class AlertRuleListView(ActionRequiredMixin, ListView):
    template_name = "alerts/rule_list.html"
    context_object_name = "rules"
    required_action = Action.ALERTRULE_MANAGE

    def get_queryset(self):
        return AlertRule.objects.select_related("zone").prefetch_related(
            "authorised_departments", "applies_to_departments"
        )


class AlertRuleEditView(ActionRequiredMixin, View):
    template_name = "alerts/rule_form.html"
    required_action = Action.ALERTRULE_MANAGE

    def get(self, request, pk=None):
        rule = get_object_or_404(AlertRule, pk=pk) if pk else None
        return render(request, self.template_name, {"form": AlertRuleForm(instance=rule), "rule": rule})

    def post(self, request, pk=None):
        rule = get_object_or_404(AlertRule, pk=pk) if pk else None
        form = AlertRuleForm(request.POST, instance=rule)
        if not form.is_valid():
            return render(request, self.template_name, {"form": form, "rule": rule})

        saved = form.save(commit=False)
        if rule is None:
            saved.created_by = request.user
        saved.save()
        form.save_m2m()
        clear_rule_cache()
        record(
            "alertrule." + ("updated" if rule else "created"),
            object_type="alerts.AlertRule",
            object_id=saved.pk,
            reason=str(saved)[:200],
            metadata={"enabled": saved.is_enabled, "type": saved.rule_type},
        )
        messages.success(
            request,
            f"Rule '{saved.name}' saved. The aggregator picks up changes within 30 seconds.",
        )
        return redirect(reverse("alerts:rules"))
