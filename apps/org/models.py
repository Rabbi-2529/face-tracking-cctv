"""
Physical and organisational structure.

A :class:`Zone` is a room, corridor or entry point - the unit of tracking.
Cameras belong to zones; "movement" means a subject appeared in one zone
after another. :class:`ZoneAdjacency` is the topology graph that makes it
possible to tell a real transition from a bad face match.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models

from apps.common.models import SoftDeleteModel, TimeStampedModel


class Department(TimeStampedModel):
    name = models.CharField(max_length=100, unique=True)
    code = models.CharField(max_length=20, unique=True)
    head = models.ForeignKey(
        "employees.Employee",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="heads_departments",
    )

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Designation(TimeStampedModel):
    title = models.CharField(max_length=100, unique=True)
    level = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["level", "title"]

    def __str__(self) -> str:
        return self.title


class Building(TimeStampedModel):
    name = models.CharField(max_length=100, unique=True)
    address = models.TextField(blank=True)
    timezone = models.CharField(
        max_length=64,
        default="Asia/Kolkata",
        help_text="Used for after-hours alert rules at this site.",
    )

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Floor(TimeStampedModel):
    building = models.ForeignKey(
        Building, on_delete=models.CASCADE, related_name="floors"
    )
    name = models.CharField(max_length=50)
    level = models.SmallIntegerField(help_text="0 = ground, -1 = basement.")
    # Served to the browser as the background of the live board.
    plan_image = models.ImageField(upload_to="floorplans/", null=True, blank=True)
    plan_width_px = models.PositiveIntegerField(null=True, blank=True)
    plan_height_px = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["building", "level"]
        constraints = [
            models.UniqueConstraint(
                fields=["building", "level"], name="uniq_floor_per_building"
            )
        ]

    def __str__(self) -> str:
        return f"{self.building.name} / {self.name}"


class Zone(SoftDeleteModel, TimeStampedModel):
    """A trackable space. One zone may hold several cameras."""

    class ZoneType(models.TextChoices):
        ENTRY = "entry", "Entry / exit"
        RECEPTION = "reception", "Reception"
        ROOM = "room", "Office / room"
        MEETING = "meeting", "Meeting room"
        CORRIDOR = "corridor", "Corridor"
        STAIRWELL = "stairwell", "Stairwell / lift lobby"
        PANTRY = "pantry", "Pantry / cafeteria"
        SERVER = "server", "Server / equipment room"
        RESTRICTED = "restricted", "Restricted area"
        PRIVATE = "private", "Private area (never tracked)"

    name = models.CharField(max_length=100)
    code = models.SlugField(
        max_length=40, unique=True, help_text="Short stable identifier, e.g. 'mr-2'."
    )
    floor = models.ForeignKey(Floor, on_delete=models.PROTECT, related_name="zones")
    zone_type = models.CharField(
        max_length=20, choices=ZoneType.choices, default=ZoneType.ROOM
    )

    is_restricted = models.BooleanField(
        default=False,
        help_text="Entry by an unauthorised person raises an alert.",
    )
    tracking_prohibited = models.BooleanField(
        default=False,
        help_text="Hard block. Toilets, changing rooms, prayer rooms, medical "
        "rooms. Ingest refuses to process cameras in this zone and the zone is "
        "hidden from every monitoring view - enforced in code, not policy.",
    )

    # Polygon on the floor plan, as [[x, y], ...] in plan pixel coordinates.
    plan_polygon = models.JSONField(
        default=list, blank=True, help_text="Outline on the floor plan image."
    )

    expected_occupancy = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="Used for occupancy anomaly alerts."
    )

    class Meta:
        ordering = ["floor", "name"]
        indexes = [
            models.Index(fields=["zone_type"]),
            models.Index(fields=["tracking_prohibited"]),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.floor})"

    def clean(self):
        # A PRIVATE zone must always be untracked; prevent a misconfiguration
        # that would quietly start processing a toilet camera.
        if self.zone_type == self.ZoneType.PRIVATE and not self.tracking_prohibited:
            raise ValidationError(
                {"tracking_prohibited": "Private zones must have tracking prohibited."}
            )

    def save(self, *args, **kwargs):
        if self.zone_type == self.ZoneType.PRIVATE:
            self.tracking_prohibited = True
        super().save(*args, **kwargs)


class ZoneAdjacency(TimeStampedModel):
    """A physically walkable connection between two zones.

    This is what separates a real movement from a mis-identification. If the
    pipeline claims someone went from a 3rd-floor meeting room to a ground
    floor pantry in four seconds, and no adjacency permits it, the event is
    flagged implausible and surfaced for review instead of being written up
    as fact.
    """

    from_zone = models.ForeignKey(
        Zone, on_delete=models.CASCADE, related_name="adjacencies_out"
    )
    to_zone = models.ForeignKey(
        Zone, on_delete=models.CASCADE, related_name="adjacencies_in"
    )
    min_transit_seconds = models.PositiveIntegerField(
        default=2,
        help_text="Fastest a person could plausibly cover this. Transitions "
        "faster than this are flagged implausible.",
    )
    typical_transit_seconds = models.PositiveIntegerField(default=10)
    bidirectional = models.BooleanField(
        default=True, help_text="Uncheck for one-way routes such as a turnstile."
    )

    class Meta:
        verbose_name_plural = "zone adjacencies"
        constraints = [
            models.UniqueConstraint(
                fields=["from_zone", "to_zone"], name="uniq_zone_adjacency"
            ),
            models.CheckConstraint(
                condition=~models.Q(from_zone=models.F("to_zone")),
                name="no_self_adjacency",
            ),
        ]

    def __str__(self) -> str:
        arrow = "<->" if self.bidirectional else "->"
        return f"{self.from_zone.code} {arrow} {self.to_zone.code}"
