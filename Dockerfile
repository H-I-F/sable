# syntax=docker/dockerfile:1

# ============================ stage 1: build ============================ #
# A wheel is built in isolation so that the runtime image contains neither
# pip metadata, nor a compiler toolchain, nor this repository.
FROM python:3.11-slim AS builder

WORKDIR /src

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

# Only the packaging inputs: changing a test must not invalidate this layer.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN python -m pip install --upgrade --no-cache-dir pip \
 && python -m pip install --no-cache-dir --no-deps --prefix=/install .

# ============================ stage 2: runtime =========================== #
FROM python:3.11-slim AS runtime

LABEL org.opencontainers.image.title="sable" \
      org.opencontainers.image.description="Crash-safe pure-Python LSM-tree key-value storage engine" \
      org.opencontainers.image.source="https://github.com/H-I-F/sable" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.vendor="H.I.F"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SABLE_HOME=/data/sable-data

# Unprivileged by default: the entry point writes to one directory and needs
# nothing else, so there is no reason for the container to run as root.
RUN groupadd --gid 10001 sable \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin sable \
 && mkdir -p /data \
 && chown 10001:10001 /data

COPY --from=builder /install /usr/local

WORKDIR /data
VOLUME ["/data"]
USER 10001:10001

ENTRYPOINT ["sable"]
CMD ["--help"]
