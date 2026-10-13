"""
Review queues: the places where the system's mistakes get looked at.

Three queues, one gate (``HISTORY_ANY``). Each one needs a reviewer who can
open the timelines and evidence involved:

* **Movements** the adjacency graph says could not have happened.
* **Corrections** - "that was not me" reports, which are both a legal right
  (rectification) and the only labelled error data the system will get.
* **Unknown faces** - clusters of repeat sightings of someone not enrolled,
  to label as a visitor, contractor, staff member or false detection.

Every decision is audited. None of them deletes anything: a rejected movement
stays in the record marked as a bad match, and an accepted correction marks
the entry rather than erasing it, so the history of the mistake survives.
"""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.generic import ListView, View

from apps.common.audit import record
from apps.common.roles import Action
from apps.employees.models import Employee, IdentityCorrection
from apps.tracking.models import MovementEvent, UnknownFaceCluster
from apps.tracking.views import ActionRequiredMixin


def _counts() -> dict:
    """Queue sizes for the tabs shown on every review page."""
    return {
        "movements": MovementEvent.objects.filter(
            is_plausible=False, review_status=MovementEvent.ReviewStatus.PENDING
        ).count(),
        "corrections": IdentityCorrection.objects.filter(
            status=IdentityCorrection.Status.OPEN
        ).count(),
        "unknown": UnknownFaceCluster.objects.filter(
            resolution=UnknownFaceCluster.Resolution.UNRESOLVED
        ).count(),
    }


class ReviewMixin(ActionRequiredMixin):
    required_action = Action.HISTORY_ANY
    review_tab = ""

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["queue_counts"] = _counts()
        ctx["review_tab"] = self.review_tab
        return ctx


# ------------------------------------------------------------------ movements


class MovementReviewView(ReviewMixin, ListView):
    """Implausible transitions awaiting review.

    This queue is the system's own honesty check: if it fills up, matching or
    the adjacency graph needs attention, and the timelines being shown to
    people are less reliable than they look.
    """

    template_name = "tracking/movement_review.html"
    context_object_name = "events"
    paginate_by = 50
    review_tab = "movements"

    def get_queryset(self):
        return (
            MovementEvent.objects.filter(
                is_plausible=False, review_status=MovementEvent.ReviewStatus.PENDING
            )
            .select_related("employee", "from_zone", "to_zone")
            .order_by("-occurred_at")
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["recent"] = (
            MovementEvent.objects.filter(
                review_status__in=[
                    MovementEvent.ReviewStatus.CONFIRMED,
                    MovementEvent.ReviewStatus.REJECTED,
                ]
            )
            .select_related("employee", "from_zone", "to_zone")
            .order_by("-updated_at")[:15]
        )
        return ctx


class MovementDecideView(ActionRequiredMixin, View):
    """Confirm an implausible movement as real, or reject it as a bad match."""

    required_action = Action.HISTORY_ANY

    def post(self, request, pk):
        event = get_object_or_404(
            MovementEvent.objects.select_related("employee", "from_zone", "to_zone"),
            pk=pk,
            review_status=MovementEvent.ReviewStatus.PENDING,
        )
        verdict = request.POST.get("decision")
        if verdict == "confirm":
            event.review_status = MovementEvent.ReviewStatus.CONFIRMED
        elif verdict == "reject":
            event.review_status = MovementEvent.ReviewStatus.REJECTED
        else:
            messages.error(request, "Unknown decision.")
            return redirect(reverse("tracking:movement_review"))

        event.save(update_fields=["review_status", "updated_at"])
        record(
            f"movement.review_{verdict}ed",
            target_employee=event.employee,
            object_type="tracking.MovementEvent",
            object_id=event.pk,
            reason=(request.POST.get("note") or event.implausible_reason)[:500],
        )
        messages.success(
            request,
            "Marked as a real movement - the adjacency graph may be missing a door."
            if verdict == "confirm"
            else "Marked as a bad match. It stays in the record, flagged.",
        )
        return redirect(reverse("tracking:movement_review"))


# ---------------------------------------------------------------- corrections


class CorrectionQueueView(ReviewMixin, ListView):
    template_name = "tracking/correction_queue.html"
    context_object_name = "corrections"
    paginate_by = 50
    review_tab = "corrections"

    def get_queryset(self):
        return (
            IdentityCorrection.objects.filter(status=IdentityCorrection.Status.OPEN)
            .select_related(
                "was_employee",
                "should_be_employee",
                "reported_by",
                "presence_interval",
                "presence_interval__zone",
            )
            .order_by("created_at")
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["recent"] = (
            IdentityCorrection.objects.exclude(status=IdentityCorrection.Status.OPEN)
            .select_related("was_employee", "resolved_by")
            .order_by("-resolved_at")[:15]
        )
        return ctx


class CorrectionDecideView(ActionRequiredMixin, View):
    """Accept or reject a reported mis-identification."""

    required_action = Action.HISTORY_ANY

    def post(self, request, pk):
        correction = get_object_or_404(
            IdentityCorrection.objects.select_related("was_employee"),
            pk=pk,
            status=IdentityCorrection.Status.OPEN,
        )
        verdict = request.POST.get("decision")
        note = (request.POST.get("note") or "").strip()

        if verdict not in ("accept", "reject"):
            messages.error(request, "Unknown decision.")
            return redirect(reverse("tracking:correction_queue"))
        if verdict == "reject" and not note:
            # The person who reported it is entitled to know why.
            messages.error(request, "Rejecting a correction needs a note explaining why.")
            return redirect(reverse("tracking:correction_queue"))

        correction.status = (
            IdentityCorrection.Status.ACCEPTED
            if verdict == "accept"
            else IdentityCorrection.Status.REJECTED
        )
        correction.resolved_by = request.user
        correction.resolved_at = timezone.now()
        correction.resolution_note = note
        correction.save(
            update_fields=["status", "resolved_by", "resolved_at", "resolution_note", "updated_at"]
        )
        record(
            f"correction.{verdict}ed",
            target_employee=correction.was_employee,
            object_type="employees.IdentityCorrection",
            object_id=correction.pk,
            reason=note[:500] or correction.reason[:500],
            metadata={"interval": correction.presence_interval_id},
        )
        messages.success(
            request,
            "Correction accepted. The entry now shows as not this person."
            if verdict == "accept" and correction.presence_interval_id
            else "Correction accepted."
            if verdict == "accept"
            else "Correction rejected; the original entry stands.",
        )
        return redirect(reverse("tracking:correction_queue"))


# -------------------------------------------------------------- unknown faces


class ClusterResolveForm(forms.ModelForm):
    class Meta:
        model = UnknownFaceCluster
        fields = ["resolution", "label", "resolved_as_employee", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["resolved_as_employee"].queryset = Employee.objects.alive().order_by(
            "full_name"
        )
        self.fields["resolved_as_employee"].required = False
        self.fields["resolved_as_employee"].label = "Is this employee (if known)"

    def clean(self):
        data = super().clean()
        resolution = data.get("resolution")
        if resolution == UnknownFaceCluster.Resolution.EMPLOYEE and not data.get(
            "resolved_as_employee"
        ):
            self.add_error(
                "resolved_as_employee",
                "Pick the employee, so their enrolment can be checked.",
            )
        if resolution == UnknownFaceCluster.Resolution.STAFF_NON_CONSENTING:
            # Someone who has not consented must not be linked to a record
            # that would make their face searchable.
            data["resolved_as_employee"] = None
        return data


class UnknownFaceQueueView(ReviewMixin, ListView):
    template_name = "tracking/unknown_faces.html"
    context_object_name = "clusters"
    paginate_by = 30
    review_tab = "unknown"

    def get_queryset(self):
        shown = self.request.GET.get("show", "unresolved")
        qs = UnknownFaceCluster.objects.prefetch_related("zones_seen").annotate(
            restricted_hits=Count("zones_seen", filter=Q(zones_seen__is_restricted=True))
        )
        if shown == "unresolved":
            qs = qs.filter(resolution=UnknownFaceCluster.Resolution.UNRESOLVED)
        # Seen in a restricted area first: that is the case worth acting on.
        return qs.order_by("-restricted_hits", "-last_seen")

    def get_context_data(self, **kwargs):
        from apps.common.permissions import PermissionService

        ctx = super().get_context_data(**kwargs)
        ctx["show"] = self.request.GET.get("show", "unresolved")
        ctx["form"] = ClusterResolveForm()
        ctx["can_view_snapshots"] = bool(PermissionService.can_view_snapshot(self.request.user))
        return ctx


class UnknownFaceResolveView(ActionRequiredMixin, View):
    required_action = Action.HISTORY_ANY

    def post(self, request, cluster_uuid):
        cluster = get_object_or_404(UnknownFaceCluster, uuid=cluster_uuid)
        form = ClusterResolveForm(request.POST, instance=cluster)
        if not form.is_valid():
            for field, errors in form.errors.items():
                messages.error(request, f"{field}: {'; '.join(errors)}")
            return redirect(reverse("tracking:unknown_faces"))

        cluster = form.save(commit=False)
        cluster.resolved_by = request.user
        cluster.save()
        record(
            "unknown.resolved",
            target_employee=cluster.resolved_as_employee,
            object_type="tracking.UnknownFaceCluster",
            object_id=cluster.pk,
            reason=f"{cluster.resolution}: {cluster.label}"[:500],
            metadata={"sightings": cluster.sighting_count},
        )
        messages.success(request, f"Cluster marked as {cluster.get_resolution_display().lower()}.")
        return redirect(reverse("tracking:unknown_faces"))
