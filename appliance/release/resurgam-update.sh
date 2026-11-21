#!/bin/sh
# /usr/local/sbin/resurgam-update -- the appliance updater CLI (ADR 0024,
# see /opt/resurgam/current/appliance/UPDATE.md).
#
# Resolves /opt/venvs/resurgam-backend ONCE, here, and runs the updater from
# that versioned venv (resurgam-backend-<version>): an apply re-points the
# link to the new release's venv while this process keeps running, and it
# must keep importing only the old, complete release's code. -I (isolated
# mode) keeps the current directory and PYTHON* variables out of sys.path.
set -eu
venv=$(readlink -f /opt/venvs/resurgam-backend)
exec "$venv/bin/python" -I -m resurgam_api.updates.updater "$@"
