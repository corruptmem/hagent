# Spike image for task-1: Python 3.14 with sqlite-vec and FTS5.
#
# Base: the HA Debian base image (glibc). sqlite-vec publishes manylinux
# wheels only, so the HA Alpine (musl) Python image cannot install it.
# Python: a uv-managed CPython (python-build-standalone). Its SQLite has
# extension loading and FTS5 turned on.
#
# Build: docker build --platform linux/amd64 -t hagent-spike-1 .
# Run:   docker run --rm hagent-spike-1
ARG BUILD_FROM=ghcr.io/home-assistant/base-debian:trixie-2026.11.0
FROM ghcr.io/astral-sh/uv:0.13.0 AS uv
FROM ${BUILD_FROM}

ARG PYTHON_VERSION=3.14.8
ARG SQLITE_VEC_VERSION=0.1.9

ENV UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:${PATH}

COPY --from=uv /uv /usr/local/bin/uv

RUN uv python install "${PYTHON_VERSION}" \
    && uv venv --python "${PYTHON_VERSION}" /opt/venv \
    && uv pip install "sqlite-vec==${SQLITE_VEC_VERSION}"

COPY smoke.py /opt/spike/smoke.py

# The HA base image starts s6-overlay through /init. The spike keeps it, so
# the image behaves like a real app image. `docker run --rm hagent-spike-1`
# runs the smoke test under s6. `--entrypoint python` skips s6.
CMD ["python", "/opt/spike/smoke.py"]
