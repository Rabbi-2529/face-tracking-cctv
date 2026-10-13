"""
API serializers.

Field-level discipline matters as much as endpoint-level permission here:

* No serializer ever exposes ``embedding``. A biometric template must not
  leave the system through a JSON API.
* No serializer exposes ``rtsp_url`` or camera credentials.
* Snapshot keys are exposed as *endpoint links*, not as URLs - fetching one
  is a separate permission check and a separate audit row.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.cameras.models import Camera
from apps.employees.models import Employee
from apps.org.models import Zone
from apps.tracking.models import MovementEvent, PresenceInterval
from apps.watchlist.models import TrackingRequest


class ZoneSerializer(serializers.ModelSerializer):
    floor_name = serializers.CharField(source="floor.name", read_only=True)
    building_name = serializers.CharField(source="floor.building.name", read_only=True)
    occupancy = serializers.IntegerField(read_only=True, required=False)

    class Meta:
        model = Zone
        fields = [
            "id",
            "code",
            "name",
            "zone_type",
            "is_restricted",
            "floor_name",
            "building_name",
            "occupancy",
        ]


class CameraSerializer(serializers.ModelSerializer):
    """Note the absence of rtsp_url, username and password - by design."""

    zone_code = serializers.CharField(source="zone.code", read_only=True)
    is_identity_capable = serializers.BooleanField(read_only=True)

    class Meta:
        model = Camera
        fields = [
            "id",
            "code",
            "name",
            "zone_code",
            "vendor",
            "purpose",
            "status",
            "is_enabled",
            "sample_fps",
            "measured_face_width_px",
            "is_identity_capable",
        ]


class EmployeeSerializer(serializers.ModelSerializer):
    department_name = serializers.CharField(source="department.name", read_only=True)
    enrollment_count = serializers.IntegerField(read_only=True)
    is_processable = serializers.SerializerMethodField()

    class Meta:
        model = Employee
        fields = [
            "uuid",
            "employee_code",
            "full_name",
            "department_name",
            "status",
            "is_trackable",
            "enrollment_count",
            "is_processable",
        ]

    def get_is_processable(self, obj) -> bool:
        return obj.is_processable()


class PresenceIntervalSerializer(serializers.ModelSerializer):
    employee_code = serializers.CharField(source="employee.employee_code", read_only=True)
    employee_name = serializers.CharField(source="employee.full_name", read_only=True)
    zone_code = serializers.CharField(source="zone.code", read_only=True)
    zone_name = serializers.CharField(source="zone.name", read_only=True)
    duration_seconds = serializers.IntegerField(read_only=True)

    class Meta:
        model = PresenceInterval
        fields = [
            "id",
            "employee_code",
            "employee_name",
            "zone_code",
            "zone_name",
            "entered_at",
            "exited_at",
            "last_seen_at",
            "duration_seconds",
            "detection_count",
            # Exposed on purpose: a consumer of this API must be able to tell
            # a confirmed match from a probable one.
            "confidence",
            "is_inferred",
        ]


class MovementEventSerializer(serializers.ModelSerializer):
    employee_code = serializers.CharField(source="employee.employee_code", read_only=True)
    from_zone_code = serializers.CharField(
        source="from_zone.code", read_only=True, default=None
    )
    to_zone_code = serializers.CharField(source="to_zone.code", read_only=True)

    class Meta:
        model = MovementEvent
        fields = [
            "id",
            "employee_code",
            "from_zone_code",
            "to_zone_code",
            "occurred_at",
            "transit_seconds",
            "prev_dwell_seconds",
            "confidence",
            "is_plausible",
            "implausible_reason",
            "review_status",
        ]


class TrackingRequestSerializer(serializers.ModelSerializer):
    employee_code = serializers.CharField(source="employee.employee_code", read_only=True)
    requested_by_email = serializers.CharField(
        source="requested_by.email", read_only=True
    )
    approved_by_email = serializers.CharField(
        source="approved_by.email", read_only=True, default=None
    )
    is_active = serializers.BooleanField(read_only=True)
    seconds_remaining = serializers.IntegerField(read_only=True)

    class Meta:
        model = TrackingRequest
        fields = [
            "uuid",
            "employee_code",
            "purpose",
            "reason",
            "scope",
            "data_from",
            "data_to",
            "status",
            "requested_by_email",
            "requested_at",
            "approved_by_email",
            "approved_at",
            "start_at",
            "end_at",
            "is_active",
            "seconds_remaining",
            "decision_note",
        ]
        read_only_fields = [
            "status",
            "requested_by_email",
            "approved_by_email",
            "approved_at",
            "start_at",
            "end_at",
            "decision_note",
        ]
