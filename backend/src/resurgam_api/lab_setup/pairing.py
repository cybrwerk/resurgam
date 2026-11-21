"""Pairing a lab node with a one-time code (docs/adr/0027).

The admin clicks "Pair node" in the Lab Compute wizard and copies one command
into the node's shell (Proxmox's own web shell is one click away):

    curl -fsSk -o /root/resurgam-pair.sh '<api>/lab-setup/bootstrap/p/<pairing id>/script' &&
      echo '<sha256>  /root/resurgam-pair.sh' | sha256sum -c - && sh /root/resurgam-pair.sh '<code>'

The first download cannot verify the appliance's TLS certificate yet (Caddy's
`tls internal` CA is unknown to the node), so it goes by the pairing's id --
not a secret -- and the command pins the script's SHA-256: a tampered script
never runs. The script holds no secret either. It carries the appliance's CA
certificate, and only over that verified connection does it use the code
(its argument) -- for the agent package (whose SHA-256 is in the script too)
and the pair call that hands out the agent token. So someone reading the
unverified first download learns nothing they could pair with. The code is
single-use, expires, and only its SHA-256 is stored. The node's PBS token is
asked for on the node and never leaves it.
"""

import hashlib
import secrets
import shlex
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from resurgam_api.lab_setup.agent_install import (
    LAB_AGENT_ENV_FILE_PATH,
    LAB_AGENT_SERVICE,
    PBS_TOKEN_VARIABLE,
    comment_safe_name,
)
from resurgam_api.lab_setup.artifacts import LabArtifact

# Crockford-style alphabet without 0/O/1/I/L: easy to read aloud, 4x4 groups
# = 80 bits.
_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_GROUPS = 4
CODE_GROUP_LENGTH = 4
CA_FILE_PATH = "/etc/resurgam-lab-agent/appliance-ca.pem"
CA_FILE_VARIABLE = "RESURGAM_LAB_AGENT_CA_FILE"
SCRIPT_PATH = "/root/resurgam-pair.sh"


def new_code() -> str:
    groups = ["".join(secrets.choice(_ALPHABET) for _ in range(CODE_GROUP_LENGTH)) for _ in range(CODE_GROUPS)]
    return "-".join(groups)


def normalize_code(code: str) -> str:
    return code.strip().upper()


def hash_code(code: str) -> str:
    return hashlib.sha256(normalize_code(code).encode()).hexdigest()


def read_ca_certificate(path: str) -> str | None:
    """The appliance's TLS CA (Caddy's local root, mounted read-only into the
    backend). None when absent -- a real certificate is then trusted by the
    node's system store and the script needs no CA of its own."""
    try:
        text = Path(path).read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        return None
    if "-----BEGIN CERTIFICATE-----" not in text or len(text) > 64 * 1024:
        return None
    return text.strip() + "\n"


@dataclass(frozen=True)
class BootstrapScript:
    text: str
    sha256: str


def bootstrap_base(api_base: str) -> str:
    return f"{api_base.rstrip('/')}/lab-setup/bootstrap"


def script_url(api_base: str, pairing_id: uuid.UUID) -> str:
    """Where the pair command fetches the script: by the pairing's id, which is
    not a secret (this download is the one without TLS verification)."""
    return f"{bootstrap_base(api_base)}/p/{pairing_id}/script"


def render_bootstrap_script(
    *,
    api_base: str,
    cluster_name: str,
    node: str,
    debian_release: str,
    agent: LabArtifact,
    ca_certificate: str | None,
) -> BootstrapScript:
    """The pairing script. It holds no secret: the code arrives as its first
    argument and is sent only over the verified connection (`$TLS`)."""
    if ca_certificate:
        ca_block = (
            f"mkdir -p {shlex.quote(CA_FILE_PATH.rsplit('/', 1)[0])}\n"
            f"cat > {CA_FILE_PATH} <<'RESURGAM_CA'\n{ca_certificate}RESURGAM_CA\n"
            f"TLS='--cacert {CA_FILE_PATH}'\n"
            f"CA_LINE='{CA_FILE_VARIABLE}={CA_FILE_PATH}'\n"
        )
    else:
        ca_block = "TLS=''\nCA_LINE=''\n"
    service = LAB_AGENT_SERVICE
    text = f"""#!/bin/sh
# RESURGAM: pair this Proxmox VE node with RESURGAM as the lab node of cluster '{comment_safe_name(cluster_name)}'.
# Installs resurgam-lab-agent {agent.version}, registers it and starts it. Run as root on node '{node}':
#   sh {SCRIPT_PATH} <pairing code>
set -eu
API={shlex.quote(bootstrap_base(api_base))}
NODE={shlex.quote(node)}
say() {{ printf '\\033[1m==> %s\\033[0m\\n' "$*"; }}
fail() {{ printf '\\033[31mERROR: %s\\033[0m\\n' "$*" >&2; exit 1; }}

CODE=${{1:-}}
[ -n "$CODE" ] || fail "No pairing code given. Copy the whole command from RESURGAM (Lab Compute setup -> Pair node)."
case "$CODE" in
  *[!A-Za-z0-9-]*) fail "That is not a pairing code. Copy the whole command from RESURGAM again." ;;
esac
[ "$(id -u)" = 0 ] || fail "Run this as root (in the Proxmox web shell you already are)."
command -v qm >/dev/null 2>&1 || fail "This is not a Proxmox VE node (no qm command)."
[ "$(hostname -s)" = "$NODE" ] || fail "This code is for node '$NODE', but this is '$(hostname -s)'. Create a code for this node in RESURGAM."
. /etc/os-release
[ "${{VERSION_CODENAME:-}}" = {shlex.quote(debian_release)} ] || fail "The agent package is built for Debian {debian_release}, this node runs ${{VERSION_CODENAME:-unknown}}. Update the node or RESURGAM."

# From here on every call verifies RESURGAM's TLS certificate ($TLS) -- only then is the code sent.
{ca_block}TMP=$(mktemp -d)
trap 'rm -rf "$TMP"; stty echo 2>/dev/null || true' EXIT

say "Downloading resurgam-lab-agent {agent.version} from RESURGAM"
curl -fsS --retry 3 $TLS -o "$TMP/{agent.filename}" "$API/$CODE/agent.deb" \\
  || fail "Could not download the agent package from $API: the code expired or was replaced (create a new one), or this node cannot reach RESURGAM."
echo "{agent.sha256}  $TMP/{agent.filename}" | sha256sum -c - >/dev/null || fail "The agent package's checksum does not match."

say "Installing it"
DEBIAN_FRONTEND=noninteractive apt-get install -y -q -o Dpkg::Options::=--force-confold "$TMP/{agent.filename}" >/dev/null \\
  || fail "Installing the agent package failed (apt-get's errors are above)."

PBS=$(sed -n 's/^{PBS_TOKEN_VARIABLE}=//p' {LAB_AGENT_ENV_FILE_PATH} 2>/dev/null | tail -n 1 || true)
if [ -z "$PBS" ] && [ -t 0 ]; then
  echo
  echo "Lab runs read backups with this node's own PBS API token (user@realm!name:secret, Datastore.Read)."
  echo "It stays on this node. Paste it now, or press Enter to add it later."
  printf 'PBS token: '
  stty -echo 2>/dev/null || true
  read -r PBS || PBS=""
  stty echo 2>/dev/null || true
  echo
fi
[ -n "$PBS" ] && HAS_PBS=true || HAS_PBS=false

say "Registering with RESURGAM"
# No --retry: the code works once. A retry after a lost answer would only hear "already used".
STATUS=$(curl -sS $TLS -o "$TMP/env" -w '%{{http_code}}' -H 'Content-Type: application/json' \\
  -d "{{\\"node\\":\\"$NODE\\",\\"pbs_token_provided\\":$HAS_PBS}}" "$API/$CODE/pair") \\
  || fail "Could not reach RESURGAM at $API to register. Create a new code and run the command again."
case "$STATUS" in
  200) ;;
  404) fail "RESURGAM refused the pairing (HTTP 404): the code is unknown, was already used or has expired. Create a new one." ;;
  409) fail "RESURGAM refused the pairing (HTTP 409, wrong node): $(head -c 500 "$TMP/env" 2>/dev/null)" ;;
  *) fail "RESURGAM answered HTTP $STATUS: $(head -c 500 "$TMP/env" 2>/dev/null)" ;;
esac
grep -q '^RESURGAM_LAB_AGENT_TOKEN=' "$TMP/env" || fail "RESURGAM's answer holds no agent configuration: $(head -c 500 "$TMP/env")"
umask 077
{{
  cat "$TMP/env"
  echo "{PBS_TOKEN_VARIABLE}=$PBS"
  if [ -n "$CA_LINE" ]; then echo "$CA_LINE"; fi
}} > {LAB_AGENT_ENV_FILE_PATH}.new
chmod 600 {LAB_AGENT_ENV_FILE_PATH}.new
mv {LAB_AGENT_ENV_FILE_PATH}.new {LAB_AGENT_ENV_FILE_PATH}

say "Starting the agent"
systemctl enable {service} >/dev/null 2>&1 || fail "Could not enable the agent service: systemctl enable {service}"
systemctl restart {service} || fail "The agent did not start: journalctl -u {service}"
sleep 2
systemctl is-active --quiet {service} || fail "The agent did not start: journalctl -u {service}"
rm -f "$0" 2>/dev/null || true
say "Done. RESURGAM shows this node as paired in a few seconds."
[ "$HAS_PBS" = true ] || echo "Add the PBS token later: edit {LAB_AGENT_ENV_FILE_PATH}, then systemctl restart {service}"
"""
    return BootstrapScript(text=text, sha256=hashlib.sha256(text.encode()).hexdigest())


def render_pair_command(api_base: str, pairing_id: uuid.UUID, code: str, script_sha256: str) -> str:
    """The one paste: fetch the script by pairing id without TLS verification
    (pinned by SHA-256), then run it with the code as its argument -- the code
    never travels over the unverified download."""
    return (
        f"curl -fsSk -o {SCRIPT_PATH} {shlex.quote(script_url(api_base, pairing_id))} && "
        f"echo '{script_sha256}  {SCRIPT_PATH}' | sha256sum -c - && sh {SCRIPT_PATH} {shlex.quote(code)}"
    )


def render_paired_env(
    *, cluster_name: str, backend_url: str, agent_token: str, overlay_storage_id: str, overlay_dir: str, node: str
) -> str:
    """The env file's RESURGAM part (the script appends the PBS token and CA
    lines locally). Variable names per restore-worker `lab_agent/config.py`."""
    return (
        f"# resurgam-lab-agent for PVE cluster '{comment_safe_name(cluster_name)}' -- paired with RESURGAM "
        "(one-time code).\n"
        "# After editing: systemctl restart resurgam-lab-agent\n"
        f"RESURGAM_BACKEND_URL={backend_url}\n"
        f"RESURGAM_LAB_AGENT_TOKEN={agent_token}\n"
        f"RESURGAM_LAB_AGENT_NODE_NAME={node}\n"
        f"RESURGAM_LAB_AGENT_OVERLAY_STORAGE_ID={overlay_storage_id}\n"
        f"RESURGAM_LAB_AGENT_OVERLAY_DIR={overlay_dir}\n"
    )


def node_shell_url(host: str, port: int, node: str) -> str:
    """Proxmox VE's own web shell for a node (what its "Shell" button opens)."""
    host_part = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"https://{host_part}:{port}/?console=shell&xtermjs=1&vmid=0&vmname=&node={quote(node)}&cmd="
