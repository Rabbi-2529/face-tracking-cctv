"""Health endpoints for container orchestration."""

from __future__ import annotations

from django.db import connection
from django.http import JsonResponse


def healthz(request) -> JsonResponse:
    """Liveness. Cheap, no dependencies - answers "is the process alive".

    Deliberately does not touch the database: a DB blip should not cause the
    orchestrator to kill healthy web containers.
    """
    return JsonResponse({"status": "ok"})


def readyz(request) -> JsonResponse:
    """Readiness. Checks the dependencies needed to serve traffic."""
    checks: dict[str, str] = {}
    ok = True

    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {type(exc).__name__}"
        ok = False

    try:
        from django.core.cache import cache

        cache.set("readyz", "1", 10)
        checks["redis"] = "ok" if cache.get("readyz") == "1" else "error: no round-trip"
        ok = ok and checks["redis"] == "ok"
    except Exception as exc:
        checks["redis"] = f"error: {type(exc).__name__}"
        ok = False

    return JsonResponse(
        {"status": "ok" if ok else "degraded", "checks": checks},
        status=200 if ok else 503,
    )
