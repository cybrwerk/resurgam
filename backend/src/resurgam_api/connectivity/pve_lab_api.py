"""PVE API helpers for the Recovery Lab compute setup wizard
(`GET /pve-clusters/{id}/lab-readiness`, the wizard's pickers and its two
safe auto-fixes). Kept out of `pve_api.py` (already ~1100 lines) but built
on its request plumbing.

Every endpoint, parameter and privilege below was checked against the PVE
API viewer schema (pve-docs/api-viewer/apidoc.js, 2026-10-06) and, where
the schema is vague, the PVE source (git.proxmox.com):

- `GET /cluster/sdn/zones` -- lists zones the token has `SDN.Audit` or
  `SDN.Allocate` on; each entry has `zone`, `type` (enum evpn, faucet,
  qinq, simple, vlan, vxlan) and optional `nodes`.
- `GET /nodes/{node}/network?type=any_local_bridge` -- Linux bridges and
  OVS bridges only, never SDN VNets (pve-manager `PVE/API2/Network.pm`:
  `any_local_bridge` matches `bridge`/`OVSBridge`; `any_bridge` would add
  the node's SDN VNets, which must never be the appliance's uplink).
- `GET /storage` -- storage.cfg entries the token has `Datastore.Audit` or
  `Datastore.AllocateSpace` on (full config per entry: `storage`, `type`,
  `content`, `nodes`, `path`, `shared`, `disable`; confirmed live for
  `pve_api.list_storages`).
- `GET /storage/{storage}` -- one entry incl. `digest`; needs
  `Datastore.Allocate` on `/storage/{storage}`.
- `POST /storage` / `PUT /storage/{storage}` -- need `Datastore.Allocate`
  on `/storage`. `content` is a comma-separated content list that
  *replaces* the current one, so `add_storage_content` always sends the
  full existing list plus the new type, guarded by `digest`.
- `GET /cluster/resources?type=vm` -- every guest with `vmid`, `node`,
  `type` (qemu/lxc), `name` and `template` (boolean, "Determines if the
  guest is a template").
- `GET /cluster/status` (needs `Sys.Audit` on `/`) -- one entry per node
  (`type` "node") with `name` and `ip`, "[node] IP of the resolved
  nodename" (apidoc.js, checked 2026-10-07): the default SSH address for
  the guided agent install.
- `GET /nodes/{node}/network` entries also carry `bridge_vids` ("Specify
  the allowed VLANs ... Only used if the bridge is VLAN aware").
"""

from dataclasses import dataclass

from resurgam_api.connectivity.models import TrustConfig
from resurgam_api.connectivity.pve_api import (
    CLUSTER_RESOURCES_PATH,
    SDN_ZONES_PATH,
    STORAGE_CONFIG_PATH,
    _get_data,
    _post_data,
    _put_data,
)

# Storage types whose plugin supports the `snippets` content type -- pve-docs
# "Proxmox VE Storage" chapter, the per-backend "Storage features" tables
# (dir, nfs, cifs, cephfs, btrfs list `snippets`; block storages such as
# lvm/lvmthin/zfspool/rbd/iscsi do not). glusterfs is kept for older PVE
# releases that still ship it.
SNIPPET_CAPABLE_STORAGE_TYPES = frozenset({"dir", "nfs", "cifs", "cephfs", "btrfs", "glusterfs"})


class PveApiError(Exception):
    """A PVE call made by this module failed -- the message is PVE's own
    error (HTTP status + body excerpt) or a response-shape problem."""


@dataclass(frozen=True)
class SdnZone:
    zone: str
    type: str | None
    nodes: list[str] | None
    pending: bool


@dataclass(frozen=True)
class NodeBridge:
    iface: str
    type: str
    active: bool | None
    vlan_aware: bool | None
    comments: str | None
    # `bridge_vids` of a VLAN-aware Linux bridge, e.g. "2-4094" (PVE's
    # default when VLAN-aware is ticked) -- None if PVE reports none.
    vlan_ids: str | None = None


@dataclass(frozen=True)
class StorageConfig:
    storage: str
    type: str
    content: list[str]
    nodes: list[str] | None
    path: str | None
    shared: bool
    disabled: bool
    digest: str | None = None


@dataclass(frozen=True)
class GuestResource:
    vmid: int
    node: str
    guest_type: str
    name: str | None
    template: bool


def _split_list(raw: object) -> list[str] | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item)]
    return [item.strip() for item in str(raw).split(",") if item.strip()]


def _check(data, error):
    if error is not None:
        raise PveApiError(error)
    return data


def list_sdn_zones(host: str, port: int, trust: TrustConfig, token: str, timeout: float) -> list[SdnZone]:
    data = _check(*_get_data(host, port, trust, token, SDN_ZONES_PATH, timeout))
    try:
        return [
            SdnZone(
                zone=entry["zone"],
                type=entry.get("type"),
                nodes=_split_list(entry.get("nodes")),
                # `state` is "new"/"changed"/"deleted" for staged, not yet
                # applied (`PUT /cluster/sdn`) changes.
                pending=bool(entry.get("state")),
            )
            for entry in data
        ]
    except (TypeError, KeyError) as exc:
        raise PveApiError(f"unexpected response shape: {exc}") from exc


def list_node_bridges(
    host: str, port: int, trust: TrustConfig, token: str, timeout: float, *, node: str
) -> list[NodeBridge]:
    data = _check(
        *_get_data(
            host, port, trust, token, f"/api2/json/nodes/{node}/network", timeout, params={"type": "any_local_bridge"}
        )
    )
    try:
        bridges = [
            NodeBridge(
                iface=entry["iface"],
                type=entry.get("type", ""),
                active=bool(entry["active"]) if "active" in entry else None,
                vlan_aware=bool(entry["bridge_vlan_aware"]) if "bridge_vlan_aware" in entry else None,
                comments=(entry.get("comments") or "").strip() or None,
                vlan_ids=(str(entry.get("bridge_vids") or "").strip() or None),
            )
            for entry in data
        ]
    except (TypeError, KeyError) as exc:
        raise PveApiError(f"unexpected response shape: {exc}") from exc
    # Defensive: the filter is server-side, but never offer anything else.
    return sorted((b for b in bridges if b.type in ("bridge", "OVSBridge")), key=lambda b: b.iface)


def _storage_from_entry(entry: dict) -> StorageConfig:
    return StorageConfig(
        storage=entry["storage"],
        type=entry.get("type", ""),
        content=_split_list(entry.get("content")) or [],
        nodes=_split_list(entry.get("nodes")),
        path=entry.get("path"),
        shared=bool(entry.get("shared")),
        disabled=bool(entry.get("disable")),
        digest=entry.get("digest"),
    )


def list_storage_config(host: str, port: int, trust: TrustConfig, token: str, timeout: float) -> list[StorageConfig]:
    data = _check(*_get_data(host, port, trust, token, STORAGE_CONFIG_PATH, timeout))
    try:
        return [_storage_from_entry(entry) for entry in data]
    except (TypeError, KeyError) as exc:
        raise PveApiError(f"unexpected response shape: {exc}") from exc


def get_storage_config(
    host: str, port: int, trust: TrustConfig, token: str, timeout: float, *, storage_id: str
) -> StorageConfig | None:
    """One storage.cfg entry incl. its `digest`, or `None` if it doesn't
    exist (PVE answers a missing storage id with HTTP 500 "storage '<id>'
    does not exist", same shape as a missing SDN zone)."""
    data, error = _get_data(host, port, trust, token, f"{STORAGE_CONFIG_PATH}/{storage_id}", timeout)
    if error is not None:
        if error.startswith("HTTP 404") or "does not exist" in error:
            return None
        raise PveApiError(error)
    if not isinstance(data, dict):
        raise PveApiError(f"unexpected response shape: {data!r}")
    return _storage_from_entry({"storage": storage_id, **data})


def create_dir_storage(
    host: str,
    port: int,
    trust: TrustConfig,
    token: str,
    timeout: float,
    *,
    storage_id: str,
    path: str,
    nodes: list[str],
    content: str = "images",
) -> None:
    """`POST /storage` with `type=dir`. `create-base-path`/`create-subdirs`
    are left at their PVE defaults (yes): PVE creates the directory and its
    `images/` sub-directory on each listed node when the storage is
    activated there."""
    payload = {"storage": storage_id, "type": "dir", "path": path, "content": content}
    if nodes:
        payload["nodes"] = ",".join(nodes)
    _check(*_post_data(host, port, trust, token, STORAGE_CONFIG_PATH, timeout, data=payload))


def add_storage_content(
    host: str,
    port: int,
    trust: TrustConfig,
    token: str,
    timeout: float,
    *,
    storage: StorageConfig,
    content_type: str,
) -> list[str]:
    """`PUT /storage/{storage}` with `content` = every existing content
    type plus `content_type`. Never drops an existing type: the full list
    read moments ago is sent back, and `digest` makes PVE reject the write
    if storage.cfg changed in between. Returns the content list sent."""
    new_content = list(storage.content)
    if content_type not in new_content:
        new_content.append(content_type)
    payload = {"content": ",".join(new_content)}
    if storage.digest:
        payload["digest"] = storage.digest
    _check(*_put_data(host, port, trust, token, f"{STORAGE_CONFIG_PATH}/{storage.storage}", timeout, data=payload))
    return new_content


def list_guest_resources(host: str, port: int, trust: TrustConfig, token: str, timeout: float) -> list[GuestResource]:
    data = _check(*_get_data(host, port, trust, token, CLUSTER_RESOURCES_PATH, timeout, params={"type": "vm"}))
    try:
        return [
            GuestResource(
                vmid=int(entry["vmid"]),
                node=entry["node"],
                guest_type=entry.get("type", ""),
                name=entry.get("name"),
                template=bool(entry.get("template")),
            )
            for entry in data
        ]
    except (TypeError, KeyError, ValueError) as exc:
        raise PveApiError(f"unexpected response shape: {exc}") from exc


CLUSTER_STATUS_PATH = "/api2/json/cluster/status"


def get_node_addresses(host: str, port: int, trust: TrustConfig, token: str, timeout: float) -> dict[str, str]:
    """Node name -> IP as PVE resolves it (`GET /cluster/status`, entries of
    `type` "node"). Nodes without an `ip` are left out."""
    data = _check(*_get_data(host, port, trust, token, CLUSTER_STATUS_PATH, timeout))
    try:
        return {
            str(entry["name"]): str(entry["ip"])
            for entry in data
            if entry.get("type") == "node" and entry.get("ip") and entry.get("name")
        }
    except (TypeError, KeyError) as exc:
        raise PveApiError(f"unexpected response shape: {exc}") from exc


def vlan_ids_include(vlan_ids: str | None, tag: int) -> bool | None:
    """Whether a `bridge_vids` list ("2 4 100-200", also comma-separated)
    includes `tag`; None when no list is known."""
    if not vlan_ids:
        return None
    for part in vlan_ids.replace(",", " ").split():
        try:
            if "-" in part:
                low, high = part.split("-", 1)
                if int(low) <= tag <= int(high):
                    return True
            elif int(part) == tag:
                return True
        except ValueError:
            return None
    return False
