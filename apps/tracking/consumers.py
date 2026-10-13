"""
WebSocket consumers for the live board and for approved live-follow sessions.

Both re-check authorisation on connect, because a WebSocket outlives the HTTP
request that opened it. ``TrackSessionConsumer`` additionally re-checks on a
timer: if the authorisation is revoked or expires mid-session, the socket is
closed rather than being allowed to run to the end of the viewer's patience.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.utils import timezone

logger = logging.getLogger("cctv.audit")

# How often an open live-follow socket revalidates its authorisation.
REVALIDATE_SECONDS = 30


class LiveBoardConsumer(AsyncJsonWebsocketConsumer):
    """Pushes zone occupancy counts for the zones this user may see."""

    async def connect(self) -> None:
        user = self.scope.get("user")
        if user is None or not user.is_authenticated:
            await self.close(code=4401)
            return

        if not await self._mfa_ok(user):
            await self.close(code=4403)
            return

        self.zone_ids = await self._permitted_zone_ids(user)
        if not self.zone_ids:
            # Authenticated but scoped to nothing: accept then close with a
            # distinct code so the UI can explain rather than silently fail.
            await self.close(code=4403)
            return

        for zone_id in self.zone_ids:
            await self.channel_layer.group_add(f"zone.{zone_id}", self.channel_name)

        await self.accept()
        await self.send_json({"type": "snapshot", "zones": await self._occupancy()})

    async def disconnect(self, code) -> None:
        for zone_id in getattr(self, "zone_ids", []):
            await self.channel_layer.group_discard(f"zone.{zone_id}", self.channel_name)

    # ---------------------------------------------------- group event hooks

    async def zone_occupancy(self, event) -> None:
        await self.send_json(
            {
                "type": "occupancy",
                "zone_id": event["zone_id"],
                "occupancy": event["occupancy"],
            }
        )

    async def camera_status(self, event) -> None:
        # A camera going offline is a coverage gap, so the board says so
        # explicitly instead of just showing a lower count.
        await self.send_json(
            {
                "type": "camera_status",
                "camera_code": event["camera_code"],
                "zone_id": event["zone_id"],
                "status": event["status"],
                "message": event.get("message", ""),
            }
        )

    # --------------------------------------------------------------- helpers

    @database_sync_to_async
    def _mfa_ok(self, user) -> bool:
        from apps.common.permissions import PermissionService
        from apps.common.roles import Action

        return bool(PermissionService.check_mfa(user, Action.LIVE_VIEW_ASSIGNED))

    @database_sync_to_async
    def _permitted_zone_ids(self, user) -> list[int]:
        from apps.common.scoping import visible_zones
        from apps.org.models import Zone

        qs = visible_zones(user, Zone.objects.alive())
        return list(qs.values_list("pk", flat=True))

    @database_sync_to_async
    def _occupancy(self) -> list[dict]:
        from django.db.models import Count

        from apps.org.models import Zone

        rows = (
            Zone.objects.filter(pk__in=self.zone_ids)
            .annotate(
                occupancy=Count(
                    "presence_intervals",
                    filter=models_open_interval_filter(),
                    distinct=True,
                )
            )
            .values("id", "code", "name", "occupancy", "is_restricted")
        )
        return list(rows)


def models_open_interval_filter():
    from django.db.models import Q

    return Q(presence_intervals__exited_at__isnull=True)


class TrackSessionConsumer(AsyncJsonWebsocketConsumer):
    """Live follow of one subject, under an approved TrackingRequest.

    Opening this socket is itself an auditable event, and it records a
    :class:`watchlist.TrackSession` so that "approved but never used" is
    distinguishable from "approved and watched for two hours".
    """

    async def connect(self) -> None:
        user = self.scope.get("user")
        if user is None or not user.is_authenticated:
            await self.close(code=4401)
            return

        self.request_id = self.scope["url_route"]["kwargs"]["request_id"]
        allowed, subject_id = await self._authorise(user, self.request_id)
        if not allowed:
            await self.close(code=4403)
            return

        self.subject_id = subject_id
        self.group = f"subject.{subject_id}"
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()

        self.session_id = await self._open_session(user)
        await self.send_json({"type": "session_open", "subject_id": subject_id})

        # Revalidate on a server-side timer as well as on client pings. A
        # client that stops pinging - a modified page, a script - must not
        # keep receiving movements after the approval ends.
        self._watchdog = asyncio.create_task(self._revalidate_periodically())

    async def disconnect(self, code) -> None:
        watchdog = getattr(self, "_watchdog", None)
        if watchdog is not None:
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog
        if hasattr(self, "group"):
            await self.channel_layer.group_discard(self.group, self.channel_name)
        if getattr(self, "session_id", None):
            await self._close_session()

    async def receive_json(self, content, **kwargs) -> None:
        # The only client message is a keepalive, which doubles as a point
        # at which the authorisation is revalidated.
        if content.get("type") != "ping":
            return
        if not await self._still_allowed():
            return
        await self.send_json({"type": "pong", "at": timezone.now().isoformat()})

    async def _revalidate_periodically(self) -> None:
        while True:
            await asyncio.sleep(REVALIDATE_SECONDS)
            if not await self._still_allowed():
                return

    async def _still_allowed(self) -> bool:
        user = self.scope.get("user")
        allowed, _ = await self._authorise(user, self.request_id)
        if not allowed:
            await self._end("authorisation_ended")
        return allowed

    async def _end(self, reason: str) -> None:
        with contextlib.suppress(Exception):
            await self.send_json({"type": reason})
        await self.close(code=4403)

    async def session_terminate(self, event) -> None:
        """Sent to this channel when the approval is revoked or expires."""
        await self._end("authorisation_ended")

    async def subject_movement(self, event) -> None:
        await self.send_json({"type": "movement", **{k: v for k, v in event.items() if k != "type"}})

    # --------------------------------------------------------------- helpers

    @database_sync_to_async
    def _authorise(self, user, request_uuid) -> tuple[bool, int | None]:
        from apps.common.audit import record
        from apps.common.permissions import PermissionService
        from apps.watchlist.models import TrackingRequest

        request = (
            TrackingRequest.objects.select_related("employee")
            .filter(uuid=request_uuid)
            .first()
        )
        if request is None:
            return False, None

        if not request.is_active:
            record(
                "track.follow_denied",
                actor=user,
                target_employee=request.employee,
                reason=f"request status={request.status}",
                success=False,
            )
            return False, None

        decision = PermissionService.can_follow_live(user, request.employee)
        if not decision:
            record(
                "track.follow_denied",
                actor=user,
                target_employee=request.employee,
                reason=decision.reason,
                success=False,
            )
            return False, None

        # Only the requester or the approver may open the session.
        if user.pk not in {request.requested_by_id, request.approved_by_id}:
            record(
                "track.follow_denied",
                actor=user,
                target_employee=request.employee,
                reason="not the requester or approver of this authorisation",
                success=False,
            )
            return False, None

        return True, request.employee_id

    @database_sync_to_async
    def _open_session(self, user) -> int | None:
        from apps.common.audit import record
        from apps.watchlist.models import TrackingRequest, TrackSession

        request = TrackingRequest.objects.filter(uuid=self.request_id).first()
        if request is None:
            return None

        client = self.scope.get("client") or (None, None)
        session = TrackSession.objects.create(
            request=request,
            opened_by=user,
            channel_name=self.channel_name,
            ip_address=client[0],
        )
        record(
            "track.follow_opened",
            actor=user,
            target_employee=request.employee,
            object_type="watchlist.TrackSession",
            object_id=session.pk,
            reason=request.reason[:500],
        )
        return session.pk

    @database_sync_to_async
    def _close_session(self) -> None:
        from apps.watchlist.models import TrackSession

        TrackSession.objects.filter(pk=self.session_id, ended_at__isnull=True).update(
            ended_at=timezone.now()
        )
