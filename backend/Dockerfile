# Manifest §5.3: "API, UI, PostgreSQL und optional Queue als Container
# über Docker Compose" -- this image is the API container. The
# privileged workers (file-restore, file-index, collector scheduler,
# recovery-lab scheduler) are deliberately NOT built here: per the
# user's own end-state decision (2026-09-27), they run natively as
# systemd services on the appliance VM, not in a container, since they
# need root + the real Proxmox CLI tooling (proxmox-file-restore, qm,
# pvesm) a container can't cleanly grant access to for VM-path restores
# (nested QEMU, ADR 0011).
#
# Built from the *repository root* as build context (see
# appliance/docker-compose.yml's `context: ..`), not from backend/ alone
# -- resurgam_api genuinely imports resurgam_worker at runtime
# (filerestore/browse.py, filerestore/sessions.py, restore_test/
# execution.py), which `backend/pyproject.toml` declares as a local path
# dependency on the sibling ../restore-worker package. That's a real
# import the API process itself needs, not just the standalone worker
# scripts, so restore-worker/ has to be in the build context even though
# this container never calls proxmox-file-restore directly.
#
# Recovery Lab packages (ADR 0019/0020): resurgam-lab-agent (installed on
# the lab PVE node) and resurgam-lab-prober (baked into the probe appliance
# template), built from this same source tree so every image -- and every
# signed update bundle carrying it (ADR 0024) -- ships exactly the versions
# that match its backend. The wizard serves them for download, installs the
# agent over a one-time SSH session and builds the template from them
# (resurgam_api/lab_setup/).
#
# Built on debian:trixie on purpose: dh-virtualenv's venv links to the
# *build host's* system Python, so the .debs only run on the same Debian
# release (ADR 0020 -- a trixie build fails at import time on bookworm).
# Debian 13 = PVE 9, and the probe appliance template is Debian 13 too.
# The manifest step fails the build if debian/changelog's upstream version
# differs from VERSION (restore-worker/README.md, "Versioning").
FROM debian:trixie AS lab-packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    debhelper \
    dh-virtualenv \
    virtualenv \
    dpkg-dev \
    python3-dev \
    python3-venv \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY restore-worker /build/restore-worker
COPY VERSION /build/VERSION
COPY appliance/release/lab_artifacts_manifest.py /build/lab_artifacts_manifest.py
WORKDIR /build/restore-worker
RUN dpkg-buildpackage -us -uc -b \
    && mkdir -p /artifacts \
    && mv ../*.deb /artifacts/ \
    && python3 /build/lab_artifacts_manifest.py /artifacts /build/VERSION

# Debian 13 (trixie) base, matching the appliance's own OS target.
FROM python:3.12-slim-trixie AS base

# WeasyPrint (Recovery Evidence PDF reports) needs Pango/Cairo/GDK-Pixbuf
# at runtime -- not bundled in its wheel, confirmed via its own install
# docs. psycopg[binary] ships its own libpq, so no separate libpq-dev.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libcairo2 \
    libgdk-pixbuf-2.0-0 \
    libffi8 \
    shared-mime-info \
    fonts-liberation \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /uvx /usr/local/bin/

WORKDIR /app

COPY restore-worker /restore-worker

# Dependencies first, layer-cached independently of application code.
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY backend/alembic.ini backend/README.md ./
COPY backend/alembic ./alembic
COPY backend/src ./src
RUN uv sync --frozen --no-dev

# The release version (repo-root VERSION, ADR 0024) and the public half of
# the release signing key -- the API uses the key only for a quick
# signature pre-check on bundle uploads; the authoritative verification
# runs in the host updater (resurgam-update) against the host's own copy.
COPY VERSION /app/VERSION
COPY appliance/release/release-signing.pub /etc/resurgam/release-signing.pub
ARG RESURGAM_VERSION=unknown
LABEL org.opencontainers.image.version="${RESURGAM_VERSION}"

# The Recovery Lab .debs + manifest.json from the lab-packages stage above
# (Settings.lab_artifacts_dir).
COPY --from=lab-packages /artifacts /app/artifacts

COPY backend/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENV PATH="/app/.venv/bin:${PATH}"
EXPOSE 8001

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "resurgam_api.main:app", "--host", "0.0.0.0", "--port", "8001"]
