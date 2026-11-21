#!/bin/sh
# RESURGAM guest-exec-live hookscript (ADR 0021 Sec.4). Fixed and
# project-authored -- never operator- or customer-editable, matching
# every other executed-on-a-guest command this project runs (the
# enumeration commands themselves, Recovery Lab's own check types).
#
# This is a vzdump backup-phase hook (referenced from /etc/vzdump.conf's
# own node-level `script:` option -- see debian/resurgam-lab-agent.default
# for the one-time setup step), NOT PVE's per-VM `hookscript` property.
# Confirmed live (2026-09-24) that the per-VM property only fires on VM
# start/stop, never on a backup at all -- an earlier version of this
# feature wired the wrong PVE mechanism entirely.
#
# vzdump calls a backup-phase hook as "<script> <phase> <mode> <vmid>"
# -- confirmed live against a real backup, and against PVE's own
# reference implementation
# (/usr/share/doc/pve-manager/examples/vzdump-hook-script.pl, whose
# `backup-*`/`log-end`/`pre-stop`/`pre-restart`/`post-restart` branch
# does `my $mode = shift; my $vmid = shift;` right after the phase
# itself). $1=backup-end fires once this VM's own data is already safely
# on PBS -- deliberately the only phase this script does anything for,
# and deliberately the only thing it does: drop a small JSON file and
# exit immediately. vzdump waits for this script, so it must never
# block, never make a network call, and never invoke `qm`/`pvesh` itself
# -- all of that happens later, out-of-band, in resurgam-lab-agent's own
# poll loop (poll_backup_triggers in daemon.py), which is what actually
# relays this to the RESURGAM backend.

[ "$1" = "backup-end" ] || exit 0

vmid="$3"
# A real bug found live (2026-09-24): $HOSTNAME in vzdump's own hook
# environment is the *guest's* name (matches --notes-template
# '{{guestname}}'), not the physical PVE node -- a real backup's trigger
# carried the VM's own name here, which the backend then (wrongly)
# treated as a PVE node to run `pvesh`/`qm` commands against, and every
# subsequent guest-exec call failed with a DNS lookup error. The actual
# OS `hostname` command (this script's own host, i.e. the PVE node
# itself -- vzdump hooks run on the node, not in the guest) is the only
# reliable source; no env var substitutes for it correctly here.
node="$(hostname)"

triggers_dir="/var/run/resurgam-lab-agent/backup-triggers"
mkdir -p "$triggers_dir"

f="$triggers_dir/$vmid-$$"
at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

echo "{\"vmid\":$vmid,\"node\":\"$node\",\"at\":\"$at\"}" > "$f.tmp" && mv "$f.tmp" "$f.json"
