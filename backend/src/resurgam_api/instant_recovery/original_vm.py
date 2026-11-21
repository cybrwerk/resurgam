"""Where is the VM this backup came from, and is it running? (ADR 0030 §4)

Instant Recovery must never put a second copy of a running machine on the same
network. Before a plan is created, every PVE cluster that could hold the
original is asked live: the clusters linked to the backup's PBS instance, or --
when none is linked -- every enabled cluster. VMIDs are only unique per cluster
(ADR 0009), so a VM with the same VMID elsewhere counts as "the original": that
can only make the check stricter, never let a running original through.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from resurgam_api.config import Settings
from resurgam_api.connectivity.models import TrustConfig
from resurgam_api.connectivity.pve_api import list_guests
from resurgam_api.models.datastore import Datastore
from resurgam_api.models.inventory import BackupGroup
from resurgam_api.models.pbs import TlsTrustMode
from resurgam_api.models.pve import PveCluster, PveClusterPbsInstance
from resurgam_api.models.secrets import SecretReference
from resurgam_api.secrets.local_provider import LocalEncryptedSecretsProvider

RUNNING = "running"
STOPPED = "stopped"
ABSENT = "absent"
UNKNOWN = "unknown"


@dataclass
class OriginalVmState:
    status: str
    cluster_name: str | None = None
    node: str | None = None
    unreachable_clusters: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "cluster_name": self.cluster_name,
            "node": self.node,
            "unreachable_clusters": list(self.unreachable_clusters),
        }


def _candidate_clusters(db: Session, group: BackupGroup) -> list[PveCluster]:
    datastore = db.get(Datastore, group.datastore_id)
    linked_ids = [
        link.pve_cluster_id
        for link in db.query(PveClusterPbsInstance).filter_by(pbs_instance_id=datastore.pbs_instance_id).all()
    ]
    query = db.query(PveCluster).filter(PveCluster.disabled.is_(False))
    if linked_ids:
        query = query.filter(PveCluster.id.in_(linked_ids))
    return query.order_by(PveCluster.display_name).all()


def find_original_vm(db: Session, settings: Settings, group: BackupGroup) -> OriginalVmState:
    """`running` wins over everything; else `unknown` if any cluster could not
    be asked; else `stopped` (found, not running) or `absent`."""
    try:
        vmid = int(group.backup_id)
    except ValueError:
        return OriginalVmState(status=ABSENT)

    found_stopped: tuple[str, str] | None = None
    unreachable: list[str] = []
    provider = LocalEncryptedSecretsProvider(db, settings.secrets_local_encryption_key)
    for cluster in _candidate_clusters(db, group):
        secret_ref = db.get(SecretReference, cluster.api_token_secret_ref_id)
        token = provider.retrieve(secret_ref.reference_key)
        trust = TrustConfig(
            trust_mode=TlsTrustMode(cluster.tls_trust_mode), fingerprint_sha256=cluster.tls_fingerprint_sha256
        )
        result = list_guests(cluster.host, cluster.port, trust, token, timeout=settings.connectivity_timeout_seconds)
        if not result.reachable:
            unreachable.append(cluster.display_name)
            continue
        for guest in result.guests or []:
            if guest.vmid != vmid or guest.guest_type != "qemu":
                continue
            if guest.status == RUNNING:
                return OriginalVmState(
                    status=RUNNING, cluster_name=cluster.display_name, node=guest.node, unreachable_clusters=unreachable
                )
            found_stopped = found_stopped or (cluster.display_name, guest.node)

    if unreachable:
        return OriginalVmState(status=UNKNOWN, unreachable_clusters=unreachable)
    if found_stopped:
        return OriginalVmState(status=STOPPED, cluster_name=found_stopped[0], node=found_stopped[1])
    return OriginalVmState(status=ABSENT)
