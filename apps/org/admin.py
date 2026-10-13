from __future__ import annotations

from django.contrib import admin

from apps.org.models import (
    Building,
    Department,
    Designation,
    Floor,
    Zone,
    ZoneAdjacency,
)


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "head", "employee_count")
    search_fields = ("name", "code")

    @admin.display(description="Employees")
    def employee_count(self, obj) -> int:
        return obj.employees.count()


@admin.register(Designation)
class DesignationAdmin(admin.ModelAdmin):
    list_display = ("title", "level")
    ordering = ("level", "title")


class FloorInline(admin.TabularInline):
    model = Floor
    extra = 0


@admin.register(Building)
class BuildingAdmin(admin.ModelAdmin):
    list_display = ("name", "timezone", "floor_count")
    inlines = [FloorInline]

    @admin.display(description="Floors")
    def floor_count(self, obj) -> int:
        return obj.floors.count()


@admin.register(Floor)
class FloorAdmin(admin.ModelAdmin):
    list_display = ("name", "building", "level", "zone_count")
    list_filter = ("building",)

    @admin.display(description="Zones")
    def zone_count(self, obj) -> int:
        return obj.zones.count()


class ZoneAdjacencyOutInline(admin.TabularInline):
    model = ZoneAdjacency
    fk_name = "from_zone"
    extra = 1
    verbose_name = "connection from this zone"
    verbose_name_plural = "connections from this zone"
    autocomplete_fields = ("to_zone",)


@admin.register(Zone)
class ZoneAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "code",
        "floor",
        "zone_type",
        "is_restricted",
        "tracking_prohibited",
        "camera_count",
        "connection_count",
    )
    list_filter = ("floor__building", "floor", "zone_type", "is_restricted", "tracking_prohibited")
    search_fields = ("name", "code")
    inlines = [ZoneAdjacencyOutInline]

    fieldsets = (
        (None, {"fields": ("name", "code", "floor", "zone_type")}),
        (
            "Monitoring policy",
            {
                "fields": ("is_restricted", "tracking_prohibited"),
                "description": "Tracking prohibited is a hard block enforced in "
                "code: ingest refuses cameras in the zone, the aggregator "
                "refuses its events, and the zone is hidden from every "
                "monitoring view. Use it for toilets, changing rooms, prayer "
                "rooms and medical rooms.",
            },
        ),
        ("Floor plan", {"classes": ("collapse",), "fields": ("plan_polygon", "expected_occupancy")}),
    )

    @admin.display(description="Cameras")
    def camera_count(self, obj) -> int:
        return obj.cameras.count()

    @admin.display(description="Connections")
    def connection_count(self, obj) -> int:
        return obj.adjacencies_out.count() + obj.adjacencies_in.count()


@admin.register(ZoneAdjacency)
class ZoneAdjacencyAdmin(admin.ModelAdmin):
    """The topology graph.

    Every real door should be an edge here. Missing edges are the main cause
    of movements being flagged implausible, so this list is worth reviewing
    whenever the review queue grows.
    """

    list_display = (
        "from_zone",
        "to_zone",
        "bidirectional",
        "min_transit_seconds",
        "typical_transit_seconds",
    )
    list_filter = ("bidirectional", "from_zone__floor")
    autocomplete_fields = ("from_zone", "to_zone")
    search_fields = ("from_zone__name", "to_zone__name", "from_zone__code", "to_zone__code")
