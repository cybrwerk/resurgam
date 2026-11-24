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
    """Deprecated (ADR 0031): the keyfile goes into the key store
    (`/encryption-keys`) by its fingerprint."""

    keyfile_content: str = Field(
        min_length=1,
        description=(
            "Full content (JSON) of the encryption keyfile the backups on this datastore were encrypted with. For "
            "backups made by PVE this is `/etc/pve/priv/storage/<STORAGE-ID>.enc` on a PVE node. Stored encrypted; "
            "never returned by this endpoint. Added to the key store by its fingerprint; uploading another key "
            "adds it next to the first. Password-protected keys are not supported yet."
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
            "Deprecated (ADR 0031): whether this datastore still has a key of its own from before the key store. "
            "The collector moves such keys into the key store, so this becomes false by itself. Use "
            "`key_fingerprints_in_use` and `keys_missing` instead."
        ),
    )
    key_fingerprints_in_use: list[str] = Field(
        default_factory=list,
        description=(
            "Every encryption key fingerprint used by a current backup in this datastore (ADR 0031), sorted. "
            "Empty when no backup is encrypted or the inventory has not been synced since this was added."
        ),
    )
    keys_missing: list[str] = Field(
        default_factory=list,
        description=(
            "Fingerprints from `key_fingerprints_in_use` that are not in the key store (`/encryption-keys`): the "
            "backups encrypted with them cannot be indexed, browsed or restored by RESURGAM until the key is uploaded."
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
