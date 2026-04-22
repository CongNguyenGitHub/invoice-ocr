# Single image used for api / worker / init.
# Entrypoint is overridden per service in docker-compose.yml.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# System deps — Pillow needs libjpeg/zlib at runtime.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        build-essential \
        libjpeg-dev \
        zlib1g-dev \
        libpq-dev \
        curl \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
RUN pip install --upgrade pip \
 && pip install -e .[dev]

COPY src ./src
COPY migrations ./migrations
COPY whitelists ./whitelists

# Default = nothing — compose sets the entrypoint per service.
CMD ["python", "-c", "print('override CMD in docker-compose.yml')"]
