# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# Vision worker image. Builds for CPU or CUDA from the same Dockerfile:
#
#   CPU (default, works anywhere):
#     BASE_IMAGE=python:3.12-slim-bookworm
#     ONNX_PACKAGE=onnxruntime==1.21.0
#
#   GPU (docker-compose.gpu.yml sets these):
#     BASE_IMAGE=nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04
#     ONNX_PACKAGE=onnxruntime-gpu==1.21.0
# ---------------------------------------------------------------------------
ARG BASE_IMAGE=python:3.12-slim-bookworm
FROM ${BASE_IMAGE} AS base

ARG ONNX_PACKAGE=onnxruntime==1.21.0

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive \
    OPENCV_FFMPEG_CAPTURE_OPTIONS="rtsp_transport;tcp|stimeout;5000000"

# The CUDA base images have no Python; the slim-bookworm base has no ffmpeg.
# Install whatever is missing for the selected base.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libpq5 \
        libgl1 \
        libglib2.0-0 \
        tini \
        ca-certificates \
    && if ! command -v python3 >/dev/null 2>&1; then \
         apt-get install -y --no-install-recommends python3 python3-pip python3-venv; \
       fi \
    && rm -rf /var/lib/apt/lists/* \
    && (command -v python >/dev/null 2>&1 || ln -s "$(command -v python3)" /usr/local/bin/python) \
    && (command -v pip >/dev/null 2>&1 || ln -s "$(command -v pip3)" /usr/local/bin/pip)

WORKDIR /app

COPY requirements/vision.txt /tmp/vision.txt
RUN pip install --break-system-packages --upgrade pip \
    && pip install --break-system-packages -r /tmp/vision.txt \
    && pip install --break-system-packages "${ONNX_PACKAGE}"

# Model weights live on a named volume, populated by scripts/download_models.py.
RUN mkdir -p /models

RUN groupadd -g 1000 app 2>/dev/null || true; \
    useradd -u 1000 -g 1000 -m -s /bin/bash app 2>/dev/null || true; \
    chown -R 1000:1000 /models /app

COPY --chown=1000:1000 vision/ /app/vision/
COPY --chown=1000:1000 scripts/ /app/scripts/

USER 1000

# tini reaps the per-camera child processes the supervisor forks.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "vision.supervisor"]
