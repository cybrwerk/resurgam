from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

DatastoreRoleLiteral = Literal["primary", "offsite"]


class RemoteDatastoreOut(BaseModel):
    name: str
    path: str | None
    comment: str | None


class ImportDatastoreRequest(BaseModel):
    name: str


class AssignCustomerRequest(BaseModel):
    customer_id: UUID
    role: DatastoreRoleLiteral = "primary"


class SetDatastoreEncryptionKeyRequest(BaseModel):
    """The raw content of a PBS keyfile (as produced by `proxmox-backup-client
    key create`/`key show`) -- plain JSON text, never parsed or validated by
    RESURGAM itself, just stored and later handed back to `proxmox-backup-client`
    as-is via `--keyfile`. See docs/adr/0022-pbs-client-side-encryption.md.
    """

    keyfile_content: str = Field(
        min_length=1,
        description=(
            "Full content (JSON) of the encryption keyfile the backups on this datastore were encrypted with. For "
            "backups made by PVE this is `/etc/pve/priv/storage/<STORAGE-ID>.enc` on a PVE node. Stored encrypted; "
            "never returned by any API call. Setting a key again replaces the previous one. Password-protected "
            "keys are not supported (same as in PVE itself)."
        ),
    )


class DatastoreOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    pbs_instance_id: UUID
    name: str
    path: str | None
    comment: str | None
    created_at: datetime
    customer_id: UUID | None = None
    role: DatastoreRoleLiteral | None = None
    # Never the key content or even its secret reference -- matching PVE's
    # own write-only convention for a storage's encryption key (confirmed
    # live: PVE::Storage::Plugin::sensitive_properties never returns it via
    # any API read either). Just enough for the UI to show "configured" vs
    # "not configured" and offer set/clear actions.
    encryption_key_configured: bool = Field(
        default=False,
        description=(
            "Whether a PBS encryption keyfile is stored for this datastore (set with POST, removed with DELETE "
            "`/datastores/{id}/encryption-key`). RESURGAM needs it to index, browse or restore files from "
            "client-side encrypted backups. The key itself is never returned."
        ),
    )
    key_fingerprints_in_use: list[str] = Field(
        default_factory=list,
        description=(
            "Every encryption key fingerprint used by a current backup in this datastore (ADR 0031), sorted. "
            "Empty when no backup is encrypted or the inventory has not been synced since this was added."
        ),
    )
    encrypted_snapshot_count: int = Field(
        default=0,
        description="How many current backups in this datastore are client-side encrypted.",
    )
    removed_from_pbs_at: datetime | None = Field(
        default=None,
        description=(
            "When the Collector's automatic datastore discovery last found this datastore missing from PBS's "
            "datastore configuration (GET /config/datastore). Null while PBS still lists it. Nothing is deleted "
            "on removal -- customer assignment, inventory and usage history stay; the mark is cleared "
            "automatically if the datastore reappears on PBS. Removed datastores are skipped by the inventory sync."
        ),
    )
