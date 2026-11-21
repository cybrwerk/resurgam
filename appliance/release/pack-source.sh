#!/bin/sh
# Packs the source tree the appliance installs (backend/ restore-worker/
# frontend/ appliance/ + VERSION + CHANGELOG.md) into a .tar.gz. Shared by
# packer/build.sh (the image build) and build-bundle.sh (update bundles) so
# both ship exactly the same thing.
#
# Usage: pack-source.sh <repo-root> <output.tar.gz>
#
# appliance/packer is excluded outright: the appliance never needs it, and
# it holds the previous build's multi-GB qcow2 plus the build's private SSH
# key, neither of which may end up on an appliance. .env files (real
# secrets on a dev machine) and build/cache output are excluded too. The
# release signing key never lives in the repo at all (see UPDATE.md).
set -eu

repo_root=$1
out=$2

tar -C "$repo_root" \
  --exclude=appliance/packer \
  --exclude=.git --exclude=.venv --exclude=node_modules \
  --exclude=__pycache__ --exclude=dist --exclude=.env \
  --exclude=.pytest_cache --exclude=.ruff_cache --exclude=.mypy_cache \
  --exclude='*.bundle' --exclude='*.tsbuildinfo' \
  -czf "$out" backend restore-worker frontend appliance VERSION CHANGELOG.md
