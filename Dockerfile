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
# Upgrade pip AND setuptools/wheel before installing the project.
# setuptools vendors jaraco.context + wheel, which carry HIGH CVEs in older
# versions (CVE-2026-23949 jaraco.context path-traversal, CVE-2026-24049 wheel
# priv-esc).  Pinning minimums keeps Trivy green.
RUN pip install --upgrade pip 'setuptools>=78.1.1' 'wheel>=0.46.2' \
 && pip install -e .[dev]

COPY src ./src
COPY migrations ./migrations
COPY whitelists ./whitelists

# Default = nothing — compose sets the entrypoint per service.
CMD ["python", "-c", "print('override CMD in docker-compose.yml')"]
