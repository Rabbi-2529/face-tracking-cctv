"""
ASGI entrypoint. Serves HTTP (Django) and WebSocket (Channels) on one port so
the live occupancy board updates without polling.
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

# The HTTP app must be built before importing anything that touches models,
# otherwise app loading races with the consumer imports below.
django_asgi_app = get_asgi_application()

from channels.auth import AuthMiddlewareStack  # noqa: E402
from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import AllowedHostsOriginValidator  # noqa: E402

from apps.accounts.channels_otp import OTPAuthMiddleware  # noqa: E402
from config.routing import websocket_urlpatterns  # noqa: E402

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        # OTPAuthMiddleware sits inside the auth stack so it sees the
        # resolved user and session: a socket is MFA-verified exactly when
        # the browser session that opened it is.
        "websocket": AllowedHostsOriginValidator(
            AuthMiddlewareStack(OTPAuthMiddleware(URLRouter(websocket_urlpatterns)))
        ),
    }
)
