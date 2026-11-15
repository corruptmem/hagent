# The hagent app image (amd64).
#
# Build context: a folder that scripts/package-app.sh creates. It holds this
# file, rootfs/ and source/ (a copy of the repository sources).
#
# Base: the HA Debian base image (glibc, s6-overlay, bashio). sqlite-vec
# publishes manylinux wheels only, so the HA Alpine image cannot install it
# (spike task-1).
# Python: a uv-managed CPython (python-build-standalone). Its SQLite has
# extension loading and FTS5 turned on.
#
# The Supervisor builds a local app without build.yaml, so it passes no
# BUILD_FROM argument and the default below applies.
ARG BUILD_FROM=ghcr.io/home-assistant/base-debian:trixie-2026.11.0

# --- Stage 1: the management UI -------------------------------------------
FROM node:24.22.3-trixie-slim AS ui
WORKDIR /src/frontend
COPY source/frontend/package.json source/frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY source/frontend/ ./
RUN npm run build

# --- Stage 2: uv ---------------------------------------------------------
FROM ghcr.io/astral-sh/uv:0.13.0 AS uv

# --- Stage 3: the app ----------------------------------------------------
FROM ${BUILD_FROM}

ARG PYTHON_VERSION=3.14.8

ENV UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:${PATH} \
    PYTHONUNBUFFERED=1

COPY --from=uv /uv /usr/local/bin/uv

RUN uv python install "${PYTHON_VERSION}"

# /opt/hagent keeps the layout of the repository. The app finds the UI in
# frontend/dist next to app/, the same path as in a source checkout.
WORKDIR /opt/hagent

# Dependencies first, so that a source change does not reinstall them.
COPY source/pyproject.toml source/uv.lock source/.python-version ./
COPY source/app/pyproject.toml app/pyproject.toml
RUN uv sync --frozen --no-dev --package hagent-app --no-install-workspace \
        --python "${PYTHON_VERSION}"

COPY source/app/src app/src
RUN uv sync --frozen --no-dev --package hagent-app --python "${PYTHON_VERSION}"

COPY --from=ui /src/frontend/dist frontend/dist
COPY rootfs /

# s6 runs the service as root. The app stores all data in /data.
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import socket; socket.create_connection(('127.0.0.1', 8099), 5).close()"]

LABEL io.hass.type="app" \
      io.hass.arch="amd64"
