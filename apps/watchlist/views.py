"""
The tracking-request workflow.

This is the control that makes the rest of the system defensible: following a
named individual requires a written reason, a different person's approval, a
time limit, and a record. Everything here exists to make that unavoidable
rather than optional.
"""

from __future__ import annotations

import datetime as dt

from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.generic import ListView, View

from apps.common.audit import record
from apps.common.permissions import PermissionService
from apps.common.roles import Action
from apps.employees.models import Employee
from apps.watchlist.models import TrackingRequest


class TrackingRequestForm(forms.ModelForm):
    # Approvers need to see a concrete window, not "ongoing".
    window_hours = forms.IntegerField(
        min_value=1,
        max_value=168,
        initial=48,
        help_text="How long the authorisation should last once approved "
        "(maximum 168 hours / 7 days).",
    )

    class Meta:
        model = TrackingRequest
        fields = [
            "employee",
            "purpose",
            "reason",
            "scope",
            "data_from",
            "data_to",
            "notification_suppressed",
            "suppression_reason",
        ]
        widgets = {
            "reason": forms.Textarea(attrs={"rows": 4}),
            "suppression_reason": forms.Textarea(attrs={"rows": 2}),
            "data_from": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "data_to": forms.DateTimeInput(attrs={"type": "datetime-local"}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        # Any active employee may be named as a subject, deliberately *not*
        # just those already in the requester's directory scope. The whole
        # purpose of this form is to reach someone outside that scope; it
        # would be useless if it only offered people whose history the
        # requester can already read.
        #
        # What is gated is access to movement history, not the fact that a
        # colleague exists - and the approver sees exactly who was named and
        # why before anything is unlocked.
        self.fields["employee"].queryset = (
            Employee.objects.alive()
            .filter(status=Employee.Status.ACTIVE)
            .select_related("department")
            .order_by("full_name")
        )


class TrackingRequestListView(LoginRequiredMixin, ListView):
    template_name = "watchlist/request_list.html"
    context_object_name = "requests"
    paginate_by = 50

    def get_queryset(self):
        user = self.request.user
        granted = PermissionService.granted_actions(user)
        qs = TrackingRequest.objects.select_related(
            "employee", "requested_by", "approved_by"
        )

        if Action.TRACKREQUEST_APPROVE in granted:
            # Approvers see the whole queue - that is the job.
            pass
        elif Action.TRACKREQUEST_CREATE in granted:
            qs = qs.filter(Q(requested_by=user) | Q(approved_by=user))
        elif user.employee_id:
            # Subjects can see requests about themselves, unless notice has
            # been formally suppressed for an active investigation.
            qs = qs.filter(employee_id=user.employee_id, notification_suppressed=False)
        else:
            qs = qs.none()

        status = self.request.GET.get("status")
        if status:
            qs = qs.filter(status=status)
        return qs.order_by("-requested_at")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["can_approve"] = bool(
            PermissionService.can(self.request.user, Action.TRACKREQUEST_APPROVE)
        )
        ctx["can_create"] = bool(
            PermissionService.can(self.request.user, Action.TRACKREQUEST_CREATE)
        )
        ctx["statuses"] = TrackingRequest.Status.choices
        return ctx


class TrackingRequestCreateView(LoginRequiredMixin, View):
    template_name = "watchlist/request_form.html"

    def _check(self):
        decision = PermissionService.can(self.request.user, Action.TRACKREQUEST_CREATE)
        if not decision:
            raise PermissionDenied(decision.reason)

    def get(self, request):
        self._check()
        initial = {}
        employee_uuid = request.GET.get("employee")
        if employee_uuid:
            employee = Employee.objects.alive().filter(uuid=employee_uuid).first()
            if employee:
                initial["employee"] = employee
        # Default the data window to the last 7 days - a concrete, bounded
        # range rather than an open-ended one the approver has to narrow.
        now = timezone.localtime()
        initial.setdefault("data_from", (now - dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M"))
        initial.setdefault("data_to", now.strftime("%Y-%m-%dT%H:%M"))
        form = TrackingRequestForm(user=request.user, initial=initial)
        return render(request, self.template_name, {"form": form})

    def post(self, request):
        self._check()
        form = TrackingRequestForm(request.POST, user=request.user)
        if not form.is_valid():
            return render(request, self.template_name, {"form": form})

        tracking_request = form.save(commit=False)
        tracking_request.requested_by = request.user
        tracking_request.status = TrackingRequest.Status.PENDING
        try:
            tracking_request.full_clean(exclude=["requested_by"])
        except ValidationError as exc:
            form.add_error(None, exc)
            return render(request, self.template_name, {"form": form})
        tracking_request.save()

        record(
            "trackrequest.created",
            target_employee=tracking_request.employee,
            object_type="watchlist.TrackingRequest",
            object_id=tracking_request.pk,
            reason=tracking_request.reason[:1000],
            metadata={
                "purpose": tracking_request.purpose,
                "scope": tracking_request.scope,
                "window_hours": form.cleaned_data["window_hours"],
            },
        )
        messages.success(
            request,
            "Request submitted. An administrator must approve it before you can "
            "view this employee's movements.",
        )
        return redirect(reverse("watchlist:list"))


class TrackingRequestDecideView(LoginRequiredMixin, View):
    """Approve, reject or revoke. Approver must not be the requester."""

    def post(self, request, request_uuid):
        decision_allowed = PermissionService.can(
            request.user, Action.TRACKREQUEST_APPROVE
        )
        if not decision_allowed:
            record(
                "trackrequest.decide_denied",
                reason=decision_allowed.reason,
                success=False,
            )
            raise PermissionDenied(decision_allowed.reason)

        tracking_request = get_object_or_404(
            TrackingRequest.objects.select_related("employee"), uuid=request_uuid
        )
        verb = request.POST.get("decision")
        note = request.POST.get("note", "").strip()

        try:
            if verb == "approve":
                hours = int(request.POST.get("window_hours") or 48)
                tracking_request.approve(request.user, window_hours=hours, note=note)
                audit_action = "trackrequest.approved"
                messages.success(
                    request,
                    f"Approved for {hours}h. It expires automatically at "
                    f"{timezone.localtime(tracking_request.end_at):%Y-%m-%d %H:%M}.",
                )
            elif verb == "reject":
                tracking_request.reject(request.user, note=note)
                audit_action = "trackrequest.rejected"
                messages.info(request, "Request rejected.")
            elif verb == "revoke":
                tracking_request.revoke(request.user, note=note)
                audit_action = "trackrequest.revoked"
                messages.info(request, "Authorisation revoked; any live session is closed.")
            else:
                messages.error(request, "Unknown decision.")
                return redirect(reverse("watchlist:list"))
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
            return redirect(reverse("watchlist:list"))

        record(
            audit_action,
            target_employee=tracking_request.employee,
            object_type="watchlist.TrackingRequest",
            object_id=tracking_request.pk,
            reason=note or tracking_request.reason[:500],
            metadata={"requested_by": tracking_request.requested_by.email},
        )
        return redirect(reverse("watchlist:list"))


class TrackingFollowView(LoginRequiredMixin, View):
    """Live follow of one subject, under an approved request.

    The page itself is the third gate, after the approval and the socket:
    it checks the same conditions the socket will, so a viewer who cannot
    follow is refused here, on record, instead of being shown a page that
    silently never updates. The socket re-checks every ping, so a revoked or
    expired approval ends the session within half a minute.
    """

    template_name = "watchlist/follow.html"

    def get(self, request, request_uuid):
        tracking_request = get_object_or_404(
            TrackingRequest.objects.select_related("employee", "employee__department"),
            uuid=request_uuid,
        )
        employee = tracking_request.employee

        refusal = ""
        if not tracking_request.is_active:
            refusal = "this authorisation is not active"
        elif tracking_request.scope not in (
            TrackingRequest.Scope.LIVE,
            TrackingRequest.Scope.BOTH,
        ):
            refusal = "this authorisation covers history only, not live follow"
        elif request.user.pk not in {
            tracking_request.requested_by_id,
            tracking_request.approved_by_id,
        }:
            refusal = "only the requester or the approver may use this authorisation"
        else:
            decision = PermissionService.can_follow_live(request.user, employee)
            if not decision:
                refusal = decision.reason

        if refusal:
            record(
                "track.follow_denied",
                target_employee=employee,
                object_type="watchlist.TrackingRequest",
                object_id=tracking_request.pk,
                reason=refusal,
                success=False,
            )
            raise PermissionDenied(refusal)

        from apps.tracking.models import MovementEvent, PresenceInterval

        # Live authority starts when the approval did. Anything earlier is
        # history, which a live-only request does not cover - so the page
        # opens with nothing from before the approval, not with today so far.
        today = timezone.localdate()
        day_start = timezone.make_aware(
            dt.datetime.combine(today, dt.time.min), timezone.get_current_timezone()
        )
        since = max(day_start, tracking_request.start_at)
        current = (
            PresenceInterval.objects.filter(
                employee=employee, zone__tracking_prohibited=False, last_seen_at__gte=since
            )
            .select_related("zone")
            .order_by("-last_seen_at")
            .first()
        )
        movements = list(
            MovementEvent.objects.filter(employee=employee, occurred_at__gte=since)
            .select_related("from_zone", "to_zone")
            .order_by("-occurred_at")[:20]
        )

        record(
            "track.follow_page",
            target_employee=employee,
            object_type="watchlist.TrackingRequest",
            object_id=tracking_request.pk,
            reason=tracking_request.reason[:500],
        )
        return render(
            request,
            self.template_name,
            {
                "tr": tracking_request,
                "employee": employee,
                "current": current,
                "movements": movements,
            },
        )
