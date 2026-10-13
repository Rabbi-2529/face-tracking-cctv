# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# Django tier: web (ASGI), celery worker, celery beat, realtime aggregator.
# All four run this same image with a different entrypoint command.
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# libpq for psycopg, curl for healthchecks, tini for correct signal handling
# (celery and daphne both need SIGTERM to reach PID 1's children).
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ------------------------------------------------------------------ builder
FROM base AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements/ /tmp/requirements/
ARG REQUIREMENTS=base.txt
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
    && /opt/venv/bin/pip install -r /tmp/requirements/${REQUIREMENTS}

# ------------------------------------------------------------------- final
FROM base AS final

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Run as a non-root user. UID 1000 matches the common host user so bind-mounted
# files stay writable in development.
RUN groupadd -g 1000 app && useradd -u 1000 -g app -m -s /bin/bash app

COPY --chown=app:app . /app

# Static + media live on volumes; create them before dropping privileges.
RUN mkdir -p /app/staticfiles /app/media \
    && chown -R app:app /app/staticfiles /app/media \
    && chmod +x /app/docker/web/entrypoint.sh

USER app

EXPOSE 8000

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["bash", "/app/docker/web/entrypoint.sh", "web"]
