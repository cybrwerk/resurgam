#!/bin/sh
# Runs migrations once before starting the API -- safe for the
# One-In-All appliance's single backend replica (no horizontal scaling,
# per the user's own end-state deployment decision), so there's no
# concurrent-migration race to guard against.
set -eu

echo "docker-entrypoint: running database migrations..."
alembic upgrade head

echo "docker-entrypoint: starting: $*"
exec "$@"
