"""
Audit trail.

Rule for this project: **if an identity read is not audited, it is not a
feature.** Every path that resolves a face to a named person, views a
snapshot, follows someone live, or exports data must leave a row here.

``AuditContextMiddleware`` stashes the actor and request metadata in a
context variable so service-layer code can write a complete row without
having to thread ``request`` through every call.
"""

from __future__ import annotations

import contextvars
import logging
from dataclasses import dataclass, field
from functools import wraps
from typing import Any

from django.apps import apps

logger = logging.getLogger("cctv.audit")


@dataclass
class AuditContext:
    user: Any = None
    ip: str | None = None
    user_agent: str = ""
    request_path: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


_context: contextvars.ContextVar[AuditContext | None] = contextvars.ContextVar(
    "audit_context", default=None
)


def current_context() -> AuditContext:
    return _context.get() or AuditContext()


def set_context(ctx: AuditContext):
    return _context.set(ctx)


def reset_context(token) -> None:
    _context.reset(token)


def _client_ip(request) -> str | None:
    # Behind nginx/traefik the real address is the first X-Forwarded-For hop.
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


class AuditContextMiddleware:
    """Populates the audit context for the lifetime of a request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = set_context(
            AuditContext(
                user=getattr(request, "user", None),
                ip=_client_ip(request),
                user_agent=request.META.get("HTTP_USER_AGENT", "")[:512],
                request_path=request.path[:255],
            )
        )
        try:
            return self.get_response(request)
        finally:
            reset_context(token)


def record(
    action: str,
    *,
    actor=None,
    target_employee=None,
    object_type: str = "",
    object_id: str = "",
    reason: str = "",
    metadata: dict[str, Any] | None = None,
    success: bool = True,
) -> None:
    """Write one audit row. Never raises - a logging failure must not break
    the user's request, but it is logged loudly so it cannot pass unnoticed.
    """
    ctx = current_context()
    resolved_actor = actor or ctx.user
    if resolved_actor is not None and not getattr(
        resolved_actor, "is_authenticated", False
    ):
        resolved_actor = None

    try:
        AuditLog = apps.get_model("accounts", "AuditLog")
        AuditLog.objects.create(
            actor=resolved_actor,
            action=action,
            object_type=object_type,
            object_id=str(object_id)[:64],
            target_employee=target_employee,
            reason=reason[:1000],
            ip_address=ctx.ip,
            user_agent=ctx.user_agent,
            request_path=ctx.request_path,
            metadata=metadata or {},
            success=success,
        )
    except Exception:  # pragma: no cover - audit must not break the request
        logger.exception(
            "AUDIT WRITE FAILED action=%s actor=%s target=%s",
            action,
            getattr(resolved_actor, "pk", None),
            getattr(target_employee, "pk", None),
        )


def audited(action: str, *, object_type: str = ""):
    """Decorator for view functions and service methods.

    Records success or failure, so a denied attempt is as visible as an
    allowed one - a run of denials is exactly the signal you want.
    """

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                result = func(*args, **kwargs)
            except Exception as exc:
                record(
                    action,
                    object_type=object_type,
                    success=False,
                    metadata={"error": type(exc).__name__},
                )
                raise
            record(action, object_type=object_type, success=True)
            return result

        return wrapper

    return decorator
