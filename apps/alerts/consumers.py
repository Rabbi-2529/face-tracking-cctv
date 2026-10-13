"""Live alert feed for the inbox page."""

from __future__ import annotations

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer


class AlertConsumer(AsyncJsonWebsocketConsumer):
    """Forwards new alerts in the zones this viewer may monitor.

    Every alert goes to one group; the filter is here, per connection, from
    the same scoping helper the inbox page uses.
    """

    async def connect(self) -> None:
        user = self.scope.get("user")
        if user is None or not user.is_authenticated:
            await self.close(code=4401)
            return
        self.zone_ids = await self._permitted_zone_ids(user)
        if self.zone_ids is None:
            await self.close(code=4403)
            return
        await self.channel_layer.group_add("alerts", self.channel_name)
        await self.accept()

    async def disconnect(self, code) -> None:
        if getattr(self, "zone_ids", None) is not None:
            await self.channel_layer.group_discard("alerts", self.channel_name)

    async def alert_raised(self, event) -> None:
        if event.get("zone_id") in self.zone_ids:
            await self.send_json({"type": "alert", **{k: v for k, v in event.items() if k != "type"}})

    @database_sync_to_async
    def _permitted_zone_ids(self, user) -> set[int] | None:
        from apps.common.permissions import PermissionService
        from apps.common.roles import Action
        from apps.common.scoping import visible_zones
        from apps.org.models import Zone

        if not PermissionService.can(user, Action.ALERT_ACK):
            return None
        return set(visible_zones(user, Zone.objects.alive()).values_list("pk", flat=True))
