from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from resurgam_api.auth.step_up import StepUpConfirmation


class EncryptionKeyOut(BaseModel):
    id: UUID = Field(description="This key's own id.")
    fingerprint: str = Field(
        description="The key's fingerprint as PBS prints it ('ad:a6:9b:...'). Backups carry the same fingerprint; "
        "RESURGAM uses this key for exactly those backups."
    )
    label: str = Field(description="Free text to recognize the key, e.g. which PVE storage uses it.")
    customer_id: UUID | None = Field(description="The tenant this key is assigned to, or null for none.")
    customer_name: str | None = Field(default=None, description="Display name of `customer_id`.")
    kdf: str = Field(description="How the keyfile itself is protected: 'none' (no passphrase, as PVE uses keys).")
    source: str = Field(
        description="Where the key came from: 'upload', 'import' (from a key export) or 'datastore' (moved over from "
        "the earlier per-datastore key)."
    )
    key_created_at: datetime | None = Field(description="When the key was created, as written in the keyfile.")
    created_at: datetime = Field(description="When the key was added to RESURGAM.")
    copy_confirmed_at: datetime | None = Field(
        description="When an admin confirmed that a copy of this key is stored outside RESURGAM (key sheet or "
        "downloaded keyfile). Null: not confirmed yet -- an alarm reminds you."
    )
    backup_count: int = Field(description="How many current backups are encrypted with this key.")
    can_download: bool = Field(
        description="Whether the caller may download this key (platform admins always; tenant admins only for their "
        "own tenant's keys and only when a platform admin allowed it for that tenant)."
    )


class EncryptionKeyUploadRequest(BaseModel):
    keyfile_content: str = Field(
        min_length=2,
        max_length=16384,
        description="The PBS keyfile's full content (JSON, as `proxmox-backup-client key create` or PVE's "
        "`encryption-key=autogen` produce it). Keys with a passphrase are not accepted yet.",
    )
    label: str = Field(default="", max_length=200, description="Free text to recognize the key.")
    customer_id: UUID | None = Field(default=None, description="Assign the key to this tenant (optional).")


class EncryptionKeyUpdateRequest(BaseModel):
    label: str | None = Field(default=None, max_length=200, description="New label; omit to keep it.")
    customer_id: UUID | None = Field(default=None, description="New tenant; send `clear_customer` to remove it.")
    clear_customer: bool = Field(default=False, description="True removes the tenant assignment.")


class EncryptionKeyDownloadRequest(BaseModel):
    confirmation: StepUpConfirmation = Field(
        default_factory=StepUpConfirmation,
        description="Fresh proof that you are signed in yourself: password, or an authenticator code. SSO-only "
        "accounts need a sign-in no older than 10 minutes and can send an empty object.",
    )


class EncryptionKeyExportRequest(EncryptionKeyDownloadRequest):
    passphrase: str = Field(
        min_length=12,
        max_length=1024,
        description="Passphrase the export file is encrypted with (scrypt + AES-256-GCM). Needed again to import it; "
        "RESURGAM does not keep it.",
    )


class EncryptionKeyImportRequest(BaseModel):
    export_content: str = Field(
        min_length=2, max_length=10_000_000, description="The full content of a RESURGAM encryption key export file."
    )
    passphrase: str = Field(min_length=1, max_length=1024, description="The passphrase the export was made with.")


class EncryptionKeyImportOut(BaseModel):
    imported: list[str] = Field(description="Fingerprints of the keys added.")
    skipped: list[str] = Field(description="Fingerprints already stored, left unchanged.")


class TenantKeyDownloadPermissionRequest(BaseModel):
    allowed: bool = Field(
        description="True lets this tenant's admins download the encryption keys assigned to their tenant."
    )


class TenantKeyDownloadPermissionOut(BaseModel):
    customer_id: UUID = Field(description="The tenant.")
    allowed: bool = Field(description="Whether this tenant's admins may download their own tenant's keys.")
