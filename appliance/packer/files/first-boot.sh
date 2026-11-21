#!/bin/sh
# Runs once, on the real appliance's first real boot (resurgam-first-boot.
# service) -- the only thing that still differs between a cloned image and
# a real deployment. Generates real secrets (nothing is baked into the
# image, see packer/README.md), brings up the Compose stack, and starts
# the four native workers. After this, the operator finishes setup
# through the browser-based setup wizard (SetupWizard.tsx) -- this script
# only gets the machine to the point where that wizard is reachable.
set -eu

cd /opt/resurgam/appliance

if [ -f .env ]; then
  # Re-run (e.g. a reboot before the unit's own Condition re-evaluated,
  # or a manual systemctl start) -- already done, nothing to do.
  exit 0
fi

PYTHON=/opt/venvs/resurgam-backend/bin/python3

SESSION_SECRET=$(openssl rand -base64 48)
SECRETS_LOCAL_ENCRYPTION_KEY=$("$PYTHON" -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
POSTGRES_PASSWORD=$(openssl rand -base64 24)

# Caddy's certificate, the CORS origin and the WebAuthn RP ID all key off
# RESURGAM_DOMAIN -- "localhost" would leave the appliance unreachable from
# any other machine, so default it to this machine's own address (the NIC
# has a lower interface index than Docker's bridges, so it's listed first).
address=$(hostname -I 2>/dev/null | awk '{print $1}')

# The installed release (picks the image tags in docker-compose.yml); the
# host updater (resurgam-update) rewrites this line on every update.
RESURGAM_VERSION=$(tr -d ' \n' < /opt/resurgam/current/VERSION)

cat > .env <<EOF
RESURGAM_VERSION=${RESURGAM_VERSION}
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
SESSION_SECRET=${SESSION_SECRET}
SECRETS_LOCAL_ENCRYPTION_KEY=${SECRETS_LOCAL_ENCRYPTION_KEY}
# Breakglass starts disabled -- the setup wizard (reachable at this
# machine's address with no login at all until a real admin exists) is
# the intended way in. Enable this later if you want the emergency
# fallback ADR 0004 describes.
BREAKGLASS_ENABLED=false
BREAKGLASS_USERNAME=admin
BREAKGLASS_PASSWORD_HASH=
# A bare IP works for now; swap in a real hostname once you have one and
# re-run 'docker compose up -d' here -- WebAuthn in particular refuses to
# work against a bare IP.
RESURGAM_DOMAIN=${address:-localhost}
EOF
chmod 600 .env

docker compose up -d

for name in resurgam-collector-scheduler resurgam-file-index-worker \
            resurgam-file-restore-worker resurgam-recovery-lab-scheduler; do
  cat > "/etc/default/${name}" <<EOF
DATABASE_URL=postgresql+psycopg://resurgam:${POSTGRES_PASSWORD}@127.0.0.1:5432/resurgam
SESSION_SECRET=${SESSION_SECRET}
SECRETS_LOCAL_ENCRYPTION_KEY=${SECRETS_LOCAL_ENCRYPTION_KEY}
EOF
  chmod 600 "/etc/default/${name}"
done

systemctl daemon-reload
systemctl enable --now \
  resurgam-collector-scheduler resurgam-file-index-worker \
  resurgam-file-restore-worker resurgam-recovery-lab-scheduler

{
  echo "RESURGAM first-boot setup complete."
  echo "Browse to https://${address:-this-machine}/ to run the setup wizard and create the first admin account."
  echo "Once you have a real hostname: edit RESURGAM_DOMAIN in /opt/resurgam/appliance/.env, then 'cd /opt/resurgam/appliance && docker compose up -d'."
  echo "Updates: System Settings -> Updates in the web UI, or 'sudo resurgam-update' here (see /opt/resurgam/current/appliance/UPDATE.md)."
} | tee /etc/resurgam-first-boot.log >> /etc/motd
