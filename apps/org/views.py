"""
Site structure: buildings, floors, zones, the topology graph, departments.

Zones and their adjacency are the two things most worth a purpose-built
screen rather than generic model forms:

* A **zone** carries ``tracking_prohibited``, which is a hard block on
  monitoring. That decision deserves a form that explains what it does and
  warns when cameras would be orphaned by it, not a bare checkbox.
* The **adjacency graph** is what separates a real movement from a
  mis-identification. Missing edges are the main cause of movements being
  flagged implausible, so the screen shows unconnected zones and the current
  review backlog instead of leaving the operator to guess.
"""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.generic import View

from apps.common.audit import record
from apps.common.permissions import PermissionService
from apps.common.roles import Action
from apps.org.models import (
    Building,
    Department,
    Designation,
    Floor,
    Zone,
    ZoneAdjacency,
)


class ZoneManageMixin(LoginRequiredMixin):
    def check_access(self, request, action: str = Action.ZONE_MANAGE) -> None:
        decision = PermissionService.can(request.user, action)
        if not decision:
            record(f"org.denied:{action}", reason=decision.reason, success=False)
            raise PermissionDenied(decision.reason)


# ---------------------------------------------------------------------- forms


class ZoneForm(forms.ModelForm):
    class Meta:
        model = Zone
        fields = [
            "name",
            "code",
            "floor",
            "zone_type",
            "is_restricted",
            "tracking_prohibited",
            "expected_occupancy",
        ]
        help_texts = {
            "code": "Short stable identifier, e.g. 'mr-2'. Used in logs and reports.",
            "is_restricted": "Entry by an unauthorised person raises an alert.",
            "tracking_prohibited": (
                "HARD BLOCK. Toilets, changing rooms, prayer rooms, medical "
                "rooms. Ingest refuses cameras here, the aggregator refuses "
                "their events, and the zone is hidden from every monitoring "
                "view including an administrator's."
            ),
            "expected_occupancy": "Optional. Used for occupancy anomaly alerts.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["floor"].queryset = Floor.objects.select_related("building")
        self.fields["expected_occupancy"].required = False

    def clean_code(self) -> str:
        code = self.cleaned_data["code"].strip().lower()
        existing = Zone.objects.filter(code=code)
        if self.instance.pk:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise ValidationError(f"A zone with code '{code}' already exists.")
        return code

    def clean(self):
        data = super().clean()

        if data.get("zone_type") == Zone.ZoneType.PRIVATE:
            # The model forces this too; setting it here makes the form honest
            # about what it is about to do.
            data["tracking_prohibited"] = True

        if data.get("tracking_prohibited") and self.instance.pk:
            active = self.instance.cameras.filter(
                is_enabled=True, deleted_at__isnull=True
            )
            if active.exists():
                codes = ", ".join(c.code for c in active[:5])
                raise ValidationError(
                    {
                        "tracking_prohibited": (
                            f"{active.count()} camera(s) are still enabled in this "
                            f"zone ({codes}). Disable them first - marking the "
                            "zone prohibited while cameras run would leave them "
                            "configured but silently ignored."
                        )
                    }
                )
        return data


class AdjacencyForm(forms.ModelForm):
    class Meta:
        model = ZoneAdjacency
        fields = [
            "from_zone",
            "to_zone",
            "bidirectional",
            "min_transit_seconds",
            "typical_transit_seconds",
        ]
        help_texts = {
            "min_transit_seconds": (
                "Fastest a person could plausibly cover this. A transition "
                "faster than this is flagged as physically impossible."
            ),
            "typical_transit_seconds": "Normal walking time. Used for reports.",
            "bidirectional": "Uncheck for one-way routes such as a turnstile.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        trackable = Zone.objects.alive().filter(
            tracking_prohibited=False
        ).select_related("floor")
        self.fields["from_zone"].queryset = trackable
        self.fields["to_zone"].queryset = trackable

    def clean(self):
        data = super().clean()
        source, target = data.get("from_zone"), data.get("to_zone")

        if source and target:
            if source == target:
                raise ValidationError({"to_zone": "A zone cannot connect to itself."})

            duplicate = ZoneAdjacency.objects.filter(
                Q(from_zone=source, to_zone=target)
                | Q(from_zone=target, to_zone=source, bidirectional=True)
            )
            if self.instance.pk:
                duplicate = duplicate.exclude(pk=self.instance.pk)
            if duplicate.exists():
                raise ValidationError(
                    "These two zones are already connected. Edit the existing "
                    "connection instead of adding a second one."
                )

        minimum = data.get("min_transit_seconds")
        typical = data.get("typical_transit_seconds")
        if minimum is not None and typical is not None and typical < minimum:
            raise ValidationError(
                {
                    "typical_transit_seconds": (
                        "Typical time cannot be below the minimum."
                    )
                }
            )
        return data


class SimpleNameForm(forms.ModelForm):
    """Shared form for the small lookup tables."""

    class Meta:
        model = Department
        fields = ["name", "code"]


class DesignationForm(forms.ModelForm):
    class Meta:
        model = Designation
        fields = ["title", "level"]
        help_texts = {"level": "Optional seniority ordering, lowest first."}


class BuildingForm(forms.ModelForm):
    class Meta:
        model = Building
        fields = ["name", "address", "timezone"]
        widgets = {"address": forms.Textarea(attrs={"rows": 2})}
        help_texts = {
            "timezone": "Used for after-hours alert rules at this site.",
        }


class FloorForm(forms.ModelForm):
    class Meta:
        model = Floor
        fields = ["building", "name", "level", "plan_image"]
        help_texts = {
            "level": "0 for ground, -1 for basement. Used to order the live board.",
            "plan_image": (
                "Optional floor plan (PNG or JPEG), shown behind the live board "
                "and the movement map. After uploading, draw each zone's "
                "outline on it from the site page."
            ),
        }

    def save(self, commit: bool = True):
        floor = super().save(commit=False)
        # Zone outlines are stored in plan-pixel coordinates, so the plan's
        # size is recorded whenever the image changes.
        if "plan_image" in self.changed_data:
            if floor.plan_image:
                from django.core.files.images import get_image_dimensions

                width, height = get_image_dimensions(floor.plan_image)
                floor.plan_width_px, floor.plan_height_px = width, height
            else:
                floor.plan_width_px = floor.plan_height_px = None
        if commit:
            floor.save()
        return floor


# ---------------------------------------------------------------------- views


class SiteOverview(ZoneManageMixin, View):
    """Everything about the site in one place, with the gaps called out."""

    template_name = "org/overview.html"

    def get(self, request):
        # ZONE_MANAGE, not CAMERA_VIEW: this page is site configuration and
        # shows the whole estate - every zone, the full topology, and
        # department headcounts. A Security Operator is scoped to a handful of
        # zones and has no business seeing the rest of the building here.
        self.check_access(request, Action.ZONE_MANAGE)

        zones = (
            Zone.objects.alive()
            .select_related("floor", "floor__building")
            .annotate(
                camera_count=Count("cameras", filter=Q(cameras__deleted_at__isnull=True)),
                out_edges=Count("adjacencies_out", distinct=True),
                in_edges=Count("adjacencies_in", distinct=True),
            )
            .order_by("floor__building__name", "floor__level", "name")
        )
        zones = list(zones)

        for zone in zones:
            zone.edge_count = zone.out_edges + zone.in_edges

        # The two gaps that actually degrade tracking quality.
        orphans = [
            z for z in zones if not z.tracking_prohibited and z.edge_count == 0
        ]
        no_camera = [
            z for z in zones if not z.tracking_prohibited and z.camera_count == 0
        ]

        from apps.tracking.models import MovementEvent

        pending_review = MovementEvent.objects.filter(
            is_plausible=False, review_status=MovementEvent.ReviewStatus.PENDING
        ).count()

        return render(
            request,
            self.template_name,
            {
                "zones": zones,
                "buildings": Building.objects.annotate(
                    floor_count=Count("floors")
                ).order_by("name"),
                "floors": Floor.objects.select_related("building").order_by(
                    "building__name", "level"
                ),
                "adjacencies": ZoneAdjacency.objects.select_related(
                    "from_zone", "to_zone"
                ).order_by("from_zone__name"),
                "departments": Department.objects.annotate(
                    employee_count=Count("employees")
                ).order_by("name"),
                "designations": Designation.objects.order_by("level", "title"),
                "orphan_zones": orphans,
                "zones_without_cameras": no_camera,
                "pending_review": pending_review,
                "can_manage": bool(
                    PermissionService.can(request.user, Action.ZONE_MANAGE)
                ),
            },
        )


class ModelEditView(ZoneManageMixin, View):
    """Generic add/edit for the site-structure models.

    One view rather than six near-identical ones. Each entry in ``KINDS``
    names the form, the lookup field and the labels.
    """

    template_name = "org/model_form.html"

    KINDS = {
        "zone": {
            "form": ZoneForm,
            "model": Zone,
            "label": "Zone",
            "lookup": "code",
            "blurb": (
                "A zone is the unit of tracking: a room, corridor or entry "
                "point. Movement means appearing in one zone after another."
            ),
        },
        "adjacency": {
            "form": AdjacencyForm,
            "model": ZoneAdjacency,
            "label": "Zone connection",
            "lookup": "pk",
            "blurb": (
                "A walkable connection between two zones. Every real door "
                "should be an edge here - missing edges are the main cause of "
                "movements being flagged as impossible."
            ),
        },
        "building": {
            "form": BuildingForm,
            "model": Building,
            "label": "Building",
            "lookup": "pk",
            "blurb": "A site. Its timezone drives after-hours rules.",
        },
        "floor": {
            "form": FloorForm,
            "model": Floor,
            "label": "Floor",
            "lookup": "pk",
            "blurb": "A level within a building. Zones belong to floors.",
        },
        "department": {
            "form": SimpleNameForm,
            "model": Department,
            "label": "Department",
            "lookup": "pk",
            "blurb": (
                "Departments scope HR access: an HR user sees only the "
                "departments assigned to them."
            ),
        },
        "designation": {
            "form": DesignationForm,
            "model": Designation,
            "label": "Job title",
            "lookup": "pk",
            "blurb": "Optional job titles for employee records.",
        },
    }

    def _kind(self, kind: str) -> dict:
        spec = self.KINDS.get(kind)
        if spec is None:
            raise PermissionDenied(f"Unknown settings section '{kind}'.")
        return spec

    def _instance(self, spec: dict, identifier):
        if identifier is None:
            return None
        if spec["lookup"] == "code":
            return get_object_or_404(spec["model"], code=identifier)
        return get_object_or_404(spec["model"], pk=identifier)

    def get(self, request, kind, identifier=None):
        self.check_access(request)
        spec = self._kind(kind)
        instance = self._instance(spec, identifier)
        form = spec["form"](instance=instance)
        return render(
            request,
            self.template_name,
            {
                "form": form,
                "kind": kind,
                "spec": spec,
                "instance": instance,
                "creating": instance is None,
            },
        )

    def post(self, request, kind, identifier=None):
        self.check_access(request)
        spec = self._kind(kind)
        instance = self._instance(spec, identifier)
        form = spec["form"](request.POST, request.FILES, instance=instance)

        if not form.is_valid():
            return render(
                request,
                self.template_name,
                {
                    "form": form,
                    "kind": kind,
                    "spec": spec,
                    "instance": instance,
                    "creating": instance is None,
                },
            )

        saved = form.save()
        record(
            f"org.{kind}." + ("updated" if instance else "created"),
            object_type=f"org.{spec['model'].__name__}",
            object_id=saved.pk,
            reason=str(saved)[:200],
        )

        messages.success(
            request, f"{spec['label']} '{saved}' saved."
        )

        if kind == "adjacency":
            messages.info(
                request,
                "New connections can clear previously flagged movements. "
                "Re-check the review queue if it was full.",
            )
        return redirect(reverse("org:overview") + f"#{kind}")


class AdjacencyDeleteView(ZoneManageMixin, View):
    """Remove one connection from the topology graph."""

    def post(self, request, identifier):
        self.check_access(request)
        edge = get_object_or_404(ZoneAdjacency, pk=identifier)
        label = str(edge)
        edge.delete()

        record(
            "org.adjacency.deleted",
            object_type="org.ZoneAdjacency",
            object_id=identifier,
            reason=label,
        )
        messages.info(
            request,
            f"Connection {label} removed. Movements along that route will now "
            "be flagged as implausible.",
        )
        return redirect(reverse("org:overview") + "#adjacency")


# --------------------------------------------------------------- floor plans


class FloorPlanImageView(LoginRequiredMixin, View):
    """Serve a floor's plan image.

    Plans are uploaded media with no public URL (see STORAGES in settings),
    so they come through here. A plan is site layout rather than personal
    data, so any signed-in user may load one - the live board and the
    movement map both draw on it.
    """

    def get(self, request, pk):
        from django.http import FileResponse, Http404

        floor = get_object_or_404(Floor, pk=pk)
        if not floor.plan_image:
            raise Http404("This floor has no plan image.")
        try:
            handle = floor.plan_image.open("rb")
        except FileNotFoundError as exc:
            raise Http404("The plan image file is missing.") from exc

        import mimetypes

        content_type = mimetypes.guess_type(floor.plan_image.name)[0] or "image/png"
        response = FileResponse(handle, content_type=content_type)
        response["Cache-Control"] = "private, max-age=300"
        response["X-Content-Type-Options"] = "nosniff"
        return response


class FloorPlanEditorView(ZoneManageMixin, View):
    """Draw zone outlines on a floor plan.

    Click points on the plan to outline the selected zone; saving replaces
    that zone's ``plan_polygon``. Prohibited zones are listed too, because
    somebody has to be able to place them on the map - they are simply never
    drawn on a monitoring view.
    """

    template_name = "org/floor_plan_editor.html"

    def get(self, request, pk):
        self.check_access(request)
        from apps.org.plans import has_plan

        floor = get_object_or_404(Floor.objects.select_related("building"), pk=pk)
        zones = list(floor.zones.alive().order_by("name"))
        outlines = {
            zone.pk: zone.plan_polygon for zone in zones if zone.plan_polygon
        }
        return render(
            request,
            self.template_name,
            {
                "floor": floor,
                "zones": zones,
                "has_plan": has_plan(floor),
                "outlines": outlines,
                "image_url": reverse("org:floor_plan_image", args=[floor.pk]),
            },
        )

    def post(self, request, pk):
        import json

        from django.http import JsonResponse

        from apps.org.plans import valid_polygon

        self.check_access(request)
        floor = get_object_or_404(Floor, pk=pk)

        try:
            payload = json.loads(request.body or b"{}")
        except json.JSONDecodeError:
            return JsonResponse({"error": "Malformed request."}, status=400)

        zone = get_object_or_404(floor.zones.alive(), pk=payload.get("zone"))
        points = payload.get("points")

        if points in (None, []):
            polygon: list = []
        else:
            polygon = valid_polygon(points)
            if polygon is None:
                return JsonResponse(
                    {"error": "An outline needs at least three points."}, status=400
                )
            width, height = floor.plan_width_px or 0, floor.plan_height_px or 0
            if width and height and any(
                not (0 <= x <= width and 0 <= y <= height) for x, y in polygon
            ):
                return JsonResponse(
                    {"error": "Every point must lie on the plan image."}, status=400
                )

        zone.plan_polygon = polygon
        zone.save(update_fields=["plan_polygon", "updated_at"])
        record(
            "org.zone.outline_" + ("cleared" if not polygon else "saved"),
            object_type="org.Zone",
            object_id=zone.pk,
            reason=f"{zone.code} on {floor}",
            metadata={"points": len(polygon)},
        )
        return JsonResponse({"zone": zone.pk, "points": polygon})
