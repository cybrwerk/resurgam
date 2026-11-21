from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class LabAgentTokenOut(BaseModel):
    """`POST /pve-clusters/{id}/lab-agent-token`'s response -- the
    plaintext token is shown exactly once, at generation time; only its
    hash is ever persisted (`PveCluster.lab_agent_token_hash`).
    """

    token: str


class LabAgentJobOut(BaseModel):
    """One `RestoreTestPlanMember` ready for `resurgam-lab-agent` to pick
    up. Carries the PBS coordinates to map (host/port/datastore/
    namespace/snapshot), not the standing PBS credential itself -- the
    agent uses its own locally-configured PBS API token (ADR 0019: that
    standing credential is never distributed over this API, matching
    `restore-worker`'s own local-config precedent), and RESURGAM doesn't
    hardcode an archive/disk name here either -- a VM can have several
    disks, and the agent has full PBS access to enumerate a snapshot's
    real contents itself rather than trusting a guess made without ever
    having looked at the archive.

    `encryption_keyfile` is a deliberate, narrow exception to the
    "no secrets over this API" principle above (docs/adr/0022): a PBS
    backup-encryption key is job-scoped and ephemeral (used once, for
    one snapshot, then discarded), not a broad standing credential like
    the token -- and unlike the token, per-host static config can't
    cleanly support multiple datastores/keys on one cluster. Populated
    only when the snapshot's own `Datastore` has one configured.
    """

    member_id: UUID
    pbs_host: str
    pbs_port: int
    pbs_datastore: str
    pbs_namespace: str
    backup_type: str
    backup_id: str
    snapshot_time: datetime
    target_vmid: int
    target_node: str
    target_storage: str
    vnet_name: str
    pool: str
    encryption_keyfile: str | None = None


class LabAgentStatusUpdate(BaseModel):
    status: Literal["booted", "failed"]
    error: str | None = None


class LabAgentFileIndexJobOut(BaseModel):
    """One `FileIndexJob` (ADR 0021) ready for `resurgam-lab-agent` to
    process -- covers both opt-in guest-exec methods, the agent branches
    on `file_index_method`. `guest_exec_ephemeral`-only fields (the PBS
    map/boot coordinates and `target_storage`) are null for
    `guest_exec_live`, whose target is already a running VM the agent
    reaches directly via `qm guest exec` -- no boot needed.
    """

    id: UUID
    file_index_method: Literal["guest_exec_live", "guest_exec_ephemeral"]
    target_vmid: int
    target_node: str
    since: datetime | None = None
    # A real gap found live (2026-09-24): the guest-side enumeration
    # command had no awareness of the group's own exclude patterns at
    # all -- it walked (and transferred back) the *entire* drive,
    # including paths already excluded by default for exactly this
    # reason (`Windows/WinSxS` etc., ADR 0016) -- confirmed live to
    # produce a 95MB listing and a guest_exec call that ran 9+ minutes
    # unattended against a real production VM before being killed.
    # Sent here so the agent can prune excluded subtrees during
    # enumeration itself, not just filter the results after a full,
    # unfiltered walk and transfer.
    exclude_patterns: list[str] = Field(default_factory=list)

    # guest_exec_ephemeral only -- same PBS coordinates as LabAgentJobOut,
    # for the agent's map+boot machinery (Recovery Lab, ADR 0019/0020).
    target_storage: str | None = None
    pbs_host: str | None = None
    pbs_port: int | None = None
    pbs_datastore: str | None = None
    pbs_namespace: str | None = None
    backup_type: str | None = None
    backup_id: str | None = None
    snapshot_time: datetime | None = None
    pool: str | None = None
    # See `LabAgentJobOut.encryption_keyfile`'s docstring for why this is
    # pushed per-job rather than left to local agent config -- applies to
    # both methods here (guest_exec_live needs it to read the already-
    # running VM's own backup; guest_exec_ephemeral needs it before it
    # can even map the snapshot to boot from).
    encryption_keyfile: str | None = None


class LabAgentFileIndexEntryIn(BaseModel):
    path: str
    filename: str
    type: Literal["file", "directory", "symlink", "other"]
    size_bytes: int | None = None
    mtime: datetime | None = None


class LabAgentFileIndexEntriesRequest(BaseModel):
    entries: list[LabAgentFileIndexEntryIn]


class LabAgentFileIndexStatusUpdate(BaseModel):
    status: Literal["completed", "failed"]
    error: str | None = None


class LabAgentFileIndexClaimOut(BaseModel):
    since: datetime | None = Field(
        description="Cutoff for the enumeration command's -newermt/LastWriteTime filter -- null means "
        "walk everything (this group's first guest-exec run, or a periodic full reconciliation)."
    )
    is_full_reindex: bool


class LabAgentBackupCompletedRequest(BaseModel):
    """Relayed by the agent from its local trigger-file poll loop
    (ADR 0021 §4) -- the hookscript itself just drops `{vmid, node, at}`
    as a file; `node` comes from the OS `hostname` command run on the
    PVE node itself (a vzdump hook runs on the node, not in the guest).
    A real bug found live: `$HOSTNAME` in vzdump's own hook environment
    is the *guest's* name (matches `--notes-template '{{guestname}}'`),
    not the physical node -- an earlier version trusted that env var and
    every real trigger carried the wrong value, which this endpoint then
    (wrongly) treated as a PVE node to route `guest_exec_target_node` to.
    """

    vmid: int
    node: str
    at: datetime
