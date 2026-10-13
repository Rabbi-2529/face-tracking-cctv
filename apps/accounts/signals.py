"""Login / logout signal handlers.

Sign-in history is kept deliberately: in a system where one account can look
up anybody's movements, knowing when and from where that account was used is
part of the control, not an ops nicety.
"""

from __future__ import annotations

from django.contrib.auth.signals import (
    user_logged_in,
    user_logged_out,
    user_login_failed,
)
from django.dispatch import receiver

from apps.common.audit import record


def _ip(request) -> str | None:
    if request is None:
        return None
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def _agent(request) -> str:
    return (request.META.get("HTTP_USER_AGENT", "") if request else "")[:512]


@receiver(user_logged_in)
def on_login(sender, request, user, **kwargs):
    from apps.accounts.models import LoginEvent

    ip = _ip(request)
    LoginEvent.objects.create(
        email_attempted=user.email,
        user=user,
        succeeded=True,
        ip_address=ip,
        user_agent=_agent(request),
    )
    if ip:
        type(user).objects.filter(pk=user.pk).update(last_login_ip=ip)
    record("auth.login", actor=user, metadata={"role": user.role})


@receiver(user_logged_out)
def on_logout(sender, request, user, **kwargs):
    if user is not None:
        record("auth.logout", actor=user)


@receiver(user_login_failed)
def on_login_failed(sender, credentials, request=None, **kwargs):
    from apps.accounts.models import LoginEvent

    email = (credentials or {}).get("username", "") or ""
    LoginEvent.objects.create(
        email_attempted=email[:254],
        succeeded=False,
        ip_address=_ip(request),
        user_agent=_agent(request),
        failure_reason="invalid_credentials",
    )
    # No actor: the credentials did not authenticate anybody.
    record("auth.login_failed", metadata={"email": email[:254]}, success=False)
