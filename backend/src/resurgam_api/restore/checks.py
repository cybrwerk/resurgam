from typing import Literal, TypedDict

from resurgam_api.connectivity.models import RemoteGuest, RemoteStorage, VmidAvailabilityResult
from resurgam_api.models.restore import RestorePlanNetworkMode, RestorePlanVmidIntent

CheckStatus = Literal["pass", "warn", "fail", "unknown"]


class CheckEntry(TypedDict):
    check: str
    status: CheckStatus
    message: str


def _entry(check: str, status: CheckStatus, message: str) -> CheckEntry:
    return {"check": check, "status": status, "message": message}


def check_vmid(
    guests: list[RemoteGuest],
    candidate: int,
    intent: RestorePlanVmidIntent,
    nextid_result: VmidAvailabilityResult,
) -> CheckEntry:
    """Manifest §11.1: "freie VMID" -- confirm the chosen VMID matches the
    operator's stated intent (a genuinely new VMID isn't already taken; an
    "existing" VMID actually exists to be overwritten into).
    """
    if not nextid_result.reachable:
        return _entry("vmid", "unknown", f"Could not verify VMID {candidate}: {nextid_result.error}")

    taken = nextid_result.free is False or any(g.vmid == candidate for g in guests)

    if intent == RestorePlanVmidIntent.NEW:
        if taken:
            return _entry("vmid", "fail", f"VMID {candidate} is already in use — pick a free VMID for a new guest.")
        return _entry("vmid", "pass", f"VMID {candidate} is free.")

    # EXISTING
    if not taken:
        return _entry("vmid", "fail", f"VMID {candidate} does not exist — nothing to restore into.")
    return _entry("vmid", "pass", f"VMID {candidate} exists and will be overwritten by this restore.")


def check_storage_capacity(
    storages: list[RemoteStorage], storage_id: str, min_free_bytes: int | None = None
) -> CheckEntry:
    """Manifest §11.1: "Zielkapazität" -- the target storage exists, is
    active, and (when a minimum is known) has enough free space.
    """
    storage = next((s for s in storages if s.storage == storage_id), None)
    if storage is None:
        return _entry("storage_capacity", "fail", f"Storage '{storage_id}' was not found on this node.")
    if storage.active is False:
        return _entry("storage_capacity", "fail", f"Storage '{storage_id}' is not active.")
    if storage.avail_bytes is None:
        return _entry("storage_capacity", "unknown", f"Storage '{storage_id}' did not report available space.")
    if min_free_bytes is not None and storage.avail_bytes < min_free_bytes:
        return _entry(
            "storage_capacity",
            "fail",
            f"Storage '{storage_id}' has insufficient free space "
            f"({storage.avail_bytes} bytes available, {min_free_bytes} bytes required).",
        )
    return _entry("storage_capacity", "pass", f"Storage '{storage_id}' is active with sufficient space.")


def check_pbs_storage_visibility(
    storages: list[RemoteStorage],
    source_datastore_name: str,
    source_namespace: str,
    source_pbs_host: str,
    is_linked: bool,
) -> CheckEntry:
    """Manifest §11.1: "PBS-Storage-Sichtbarkeit auf dem PVE-Ziel" -- cross-
    references the admin-entered `PveClusterPbsInstance` link (`is_linked`)
    against what the PVE cluster's own live storage config actually
    reports, rather than trusting the link alone. A PBS-backed PVE storage
    is scoped to exactly one namespace (confirmed live against a real PVE
    9.2 cluster), so the snapshot's own namespace must match too.
    """
    if not is_linked:
        return _entry(
            "pbs_storage_visibility",
            "warn",
            "This PVE cluster is not linked to the snapshot's PBS instance — the restore command may still work "
            "if a matching PVE storage exists, but the link should be configured for traceability.",
        )

    matching = [s for s in storages if s.type == "pbs" and s.pbs_datastore == source_datastore_name]
    if not matching:
        return _entry(
            "pbs_storage_visibility",
            "fail",
            f"PBS store not configured on this PVE node — please configure a PBS storage for datastore "
            f"'{source_datastore_name}' in PVE before restoring here.",
        )

    namespace_matching = [s for s in matching if (s.pbs_namespace or "") == (source_namespace or "")]
    if not namespace_matching:
        return _entry(
            "pbs_storage_visibility",
            "fail",
            f"PBS store not configured on this PVE node — a storage exists for datastore "
            f"'{source_datastore_name}', but none is scoped to namespace '{source_namespace or '/'}'; "
            f"please configure it in PVE before restoring here.",
        )

    server_matching = [s for s in namespace_matching if s.pbs_server == source_pbs_host]
    if not server_matching:
        return _entry(
            "pbs_storage_visibility",
            "warn",
            f"A matching PBS storage was found for datastore '{source_datastore_name}' and namespace "
            f"'{source_namespace or '/'}', but its configured server does not match the snapshot's PBS instance "
            f"host ('{source_pbs_host}') — double-check it's the same PBS.",
        )

    return _entry(
        "pbs_storage_visibility",
        "pass",
        f"Storage '{server_matching[0].storage}' backs datastore '{source_datastore_name}' "
        f"(namespace '{source_namespace or '/'}') on this node.",
    )


def check_name_conflict(guests: list[RemoteGuest], proposed_name: str) -> CheckEntry:
    """Manifest §11.1: "Namens-/ID-Konflikte" -- the proposed temp name."""
    conflict = next((g for g in guests if g.name == proposed_name), None)
    if conflict is not None:
        return _entry(
            "name_conflict", "warn", f"Another guest (VMID {conflict.vmid}) already uses the name '{proposed_name}'."
        )
    return _entry("name_conflict", "pass", f"No existing guest uses the name '{proposed_name}'.")


def classify_network_risk(rules: dict | None, mode: RestorePlanNetworkMode, bridge: str | None) -> CheckEntry:
    """Manifest §11.1/§26.7: "Netzwerkgefahr" -- the safe default is no
    production network connectivity. `PveCluster.restore_network_rules`'s
    JSONB shape is defined here for the first time:
    `{"production_bridges": [...], "safe_bridges": [...], "default_bridge": "..."}`.
    An unconfigured cluster (`rules is None`) must never look safe by
    default -- same discipline as protection status: no policy means
    UNKNOWN, not a pass.
    """
    if mode in (RestorePlanNetworkMode.NONE, RestorePlanNetworkMode.ISOLATED):
        if mode == RestorePlanNetworkMode.ISOLATED and rules is None:
            return _entry(
                "network_risk",
                "unknown",
                "Isolated network was requested, but this PVE cluster has no restore network rules configured "
                "to confirm a safe bridge exists — acknowledgment required.",
            )
        return _entry("network_risk", "pass", "No production network connectivity requested.")

    if mode == RestorePlanNetworkMode.ORIGINAL:
        return _entry(
            "network_risk",
            "warn",
            "Restoring with the snapshot's original network configuration risks reconnecting to production "
            "networks (e.g. duplicate IP/MAC) — acknowledgment required.",
        )

    # CUSTOM
    if rules is None:
        return _entry(
            "network_risk",
            "unknown",
            f"Custom bridge '{bridge}' was requested, but this PVE cluster has no restore network rules "
            "configured to classify it — acknowledgment required.",
        )
    if bridge in (rules.get("production_bridges") or []):
        return _entry(
            "network_risk", "warn", f"Bridge '{bridge}' is configured as a production bridge — acknowledgment required."
        )
    if bridge in (rules.get("safe_bridges") or []):
        return _entry("network_risk", "pass", f"Bridge '{bridge}' is configured as a safe bridge.")
    return _entry(
        "network_risk",
        "unknown",
        f"Bridge '{bridge}' is not classified in this cluster's restore network rules — acknowledgment required.",
    )
