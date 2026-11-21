"""Leftovers of Recovery Lab runs on this node, cleaned up once their VM is gone.

Found live 2026-11-21: after a lab run's cleanup (the backend destroys the
member and appliance VMs through the PVE API) two things stayed on the node
for good, because only this agent can remove them:

- the member's backup mapping (`proxmox-backup-client map` process + loop
  device) -- unmapped so far only when the member's start failed;
- the probe appliance's cloud-init snippet in the snippet storage.

So the agent records both when it creates them, in a small registry on the
node, and every poll removes the ones whose VM no longer exists:

- a mapping once `/etc/pve/qemu-server/<vmid>.conf` of its member is gone;
- a snippet once no VM config on this node references it any more (after a
  grace period: the backend creates the appliance VM only after the agent
  confirmed the snippet).

Only entries this agent recorded itself are ever touched -- never an Instant
Recovery or Disk Mounts mapping, never another snippet.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import time
from collections.abc import Callable

from resurgam_worker.pbs_client.errors import PbsClientError
from resurgam_worker.pbs_client.map import unmap

logger = logging.getLogger("resurgam_worker.lab_agent")

REGISTRY_PATH = "/var/lib/resurgam-lab-agent/lab-cleanup.json"
QEMU_CONF_DIR = "/etc/pve/qemu-server"
SNIPPET_GRACE_SECONDS = 600
# An unmap that keeps failing (device already gone by other means, e.g. a
# node reboot) is dropped after this many attempts instead of retried forever.
MAX_UNMAP_ATTEMPTS = 3


def _load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("mappings", [])
    data.setdefault("snippets", [])
    return data


def _save(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = f"{path}.tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    os.replace(temp, path)


def record_mapping(device: str, vmid: int, *, path: str = REGISTRY_PATH, now: Callable[[], float] = time.time) -> None:
    """A lab member's backup mapping, recorded once its VM booted."""
    data = _load(path)
    data["mappings"].append({"device": device, "vmid": vmid, "recorded_at": now(), "attempts": 0})
    _save(path, data)


def record_snippet(snippet_path: str, *, path: str = REGISTRY_PATH, now: Callable[[], float] = time.time) -> None:
    """A probe appliance's cloud-init snippet, recorded when written."""
    data = _load(path)
    data["snippets"].append({"path": snippet_path, "recorded_at": now()})
    _save(path, data)


def _referenced(snippet_path: str, conf_dir: str) -> bool:
    name = os.path.basename(snippet_path)
    for conf in glob.glob(os.path.join(conf_dir, "*.conf")):
        try:
            with open(conf, encoding="utf-8", errors="replace") as handle:
                if name in handle.read():
                    return True
        except OSError:
            return True  # can't tell -- keep the snippet
    return False


def sweep(
    token: str,
    *,
    path: str = REGISTRY_PATH,
    conf_dir: str = QEMU_CONF_DIR,
    now: Callable[[], float] = time.time,
    unmap_fn: Callable[..., None] = unmap,
) -> None:
    """One pass: unmap the mappings whose member VM is gone, delete the
    snippets nothing references any more."""
    data = _load(path)
    if not data["mappings"] and not data["snippets"]:
        return
    if not os.path.isdir(conf_dir):
        return  # not on a PVE node (or pmxcfs down) -- can't tell what is gone

    changed = False
    kept_mappings = []
    for entry in data["mappings"]:
        if os.path.exists(os.path.join(conf_dir, f"{entry['vmid']}.conf")):
            kept_mappings.append(entry)
            continue
        changed = True
        try:
            unmap_fn(entry["device"], token=token)
            logger.info("lab-agent: unmapped %s of the removed lab member VM %s", entry["device"], entry["vmid"])
        except PbsClientError:
            entry["attempts"] = int(entry.get("attempts", 0)) + 1
            if entry["attempts"] < MAX_UNMAP_ATTEMPTS:
                kept_mappings.append(entry)
            logger.warning("lab-agent: unmapping %s failed", entry["device"], exc_info=True)

    kept_snippets = []
    for entry in data["snippets"]:
        if now() - float(entry.get("recorded_at", 0)) < SNIPPET_GRACE_SECONDS or _referenced(entry["path"], conf_dir):
            kept_snippets.append(entry)
            continue
        changed = True
        try:
            os.remove(entry["path"])
            logger.info("lab-agent: removed the unused lab snippet %s", entry["path"])
        except FileNotFoundError:
            pass
        except OSError:
            kept_snippets.append(entry)
            logger.warning("lab-agent: removing %s failed", entry["path"], exc_info=True)

    if changed:
        _save(path, {"mappings": kept_mappings, "snippets": kept_snippets})
