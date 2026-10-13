"""
Second-factor state for WebSocket connections.

django-otp's ``OTPMiddleware`` gives every HTTP request user an
``is_verified()`` method, read from the session. Channels has no equivalent,
so without this a socket's user has no ``is_verified`` at all - and the live
board and live follow would be reachable on a password alone. This
middleware reads the same session key, so a socket is verified exactly when
the browser session that opened it is.
"""

from __future__ import annotations

import functools

from channels.db import database_sync_to_async
from channels.middleware import BaseMiddleware
from django_otp import DEVICE_ID_SESSION_KEY
from django_otp.middleware import is_verified


def device_for_session(user, session) -> object | None:
    """The verified OTP device recorded in this session, if it is the user's."""
    from django_otp.models import Device

    persistent_id = session.get(DEVICE_ID_SESSION_KEY) if session is not None else None
    if not persistent_id:
        return None
    device = Device.from_persistent_id(persistent_id)
    if device is None or device.user_id != user.pk:
        return None
    return device


def mark_verification(user, device) -> None:
    user.otp_device = device
    user.is_verified = functools.partial(is_verified, user)


class OTPAuthMiddleware(BaseMiddleware):
    """Install ``is_verified()`` on ``scope["user"]``. Goes inside AuthMiddlewareStack."""

    async def __call__(self, scope, receive, send):
        user = scope.get("user")
        if user is not None and getattr(user, "is_authenticated", False):
            device = await database_sync_to_async(device_for_session)(
                user, scope.get("session")
            )
            mark_verification(user, device)
        return await super().__call__(scope, receive, send)
