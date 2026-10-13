"""WebSocket URL routing."""

from django.urls import path

from apps.alerts.consumers import AlertConsumer
from apps.tracking.consumers import LiveBoardConsumer, TrackSessionConsumer

websocket_urlpatterns = [
    # New alerts, filtered to the zones the connecting user may monitor.
    path("ws/alerts/", AlertConsumer.as_asgi()),
    # Live room occupancy. Scoped to the zones the connecting user may see.
    path("ws/live/", LiveBoardConsumer.as_asgi()),
    # Live follow of one employee. Requires an approved, unexpired
    # TrackingRequest; the consumer rejects the connection otherwise.
    path("ws/track/<uuid:request_id>/", TrackSessionConsumer.as_asgi()),
]
