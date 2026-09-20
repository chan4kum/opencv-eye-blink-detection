# syntax=docker/dockerfile:1.9

# ---- build: resolve and install dependencies into a virtualenv ------------------------------------
FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /usr/local/bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_NO_CACHE=1
WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# ---- runtime libraries shared by the test and runtime stages -----------------------------------------
# MediaPipe's native runtime links against EGL/GLES even for CPU inference.
FROM python:3.12-slim-bookworm AS libs
RUN apt-get update \
 && apt-get install -y --no-install-recommends libegl1 libgles2 \
 && rm -rf /var/lib/apt/lists/*

# ---- test: the full suite in a Linux environment (MediaPipe cannot run in some macOS sandboxes) ------
FROM libs AS test
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /usr/local/bin/
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_NO_CACHE=1 PYTHONDONTWRITEBYTECODE=1 UV_PROJECT_ENVIRONMENT=/opt/venv \
    EB_TEST_REAL_MEDIAPIPE=1
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --all-groups --no-install-project
COPY src ./src
COPY models ./models
COPY tests ./tests
COPY scripts ./scripts
RUN uv sync --frozen --all-groups
ENTRYPOINT ["uv", "run", "--no-sync", "pytest"]

# ---- runtime: minimal image, non-root, no build tools -----------------------------------------------
FROM libs AS runtime

ARG VERSION=dev
ARG REVISION=unknown
LABEL org.opencontainers.image.title="opencv-eye-blink-detection" \
      org.opencontainers.image.description="Production-grade eye-blink detection service (MediaPipe Face Landmarker + FastAPI)" \
      org.opencontainers.image.source="https://github.com/chan4kum/opencv-eye-blink-detection" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"

# pip/setuptools/wheel are not needed at runtime (dependencies are pre-installed in the venv): remove them
# to shrink the attack surface and the vulnerability-scan surface.
RUN python -m pip uninstall -y pip setuptools wheel >/dev/null 2>&1 || true \
 && groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=random \
    EB_MODEL_PATH=/app/models/face_landmarker.task \
    EB_ENVIRONMENT=prod \
    HOME=/tmp \
    MPLCONFIGDIR=/tmp/matplotlib

WORKDIR /app
COPY --from=build --chown=root:root /app/.venv /app/.venv
COPY --chown=root:root models /app/models

USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import sys,urllib.request as u; sys.exit(0 if u.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"]

CMD ["uvicorn", "eye_blink.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--no-server-header", "--no-access-log", "--timeout-graceful-shutdown", "25", "--proxy-headers"]
