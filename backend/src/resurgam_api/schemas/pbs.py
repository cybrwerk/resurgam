from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from resurgam_api.models.pbs import ConnectionHealthStatus, TlsTrustMode
from resurgam_api.schemas.connectivity import (
    API_TOKEN_DEPRECATED_DESCRIPTION,
    TOKEN_ID_DESCRIPTION,
    TOKEN_SECRET_DESCRIPTION,
    TokenLoginCredentials,
    combined_token,
    resolve_token_input,
)

_PBS_TOKEN_ID_DESCRIPTION = TOKEN_ID_DESCRIPTION.format(
    example="resurgam@pbs!resurgam", gui="PBS", location="Configuration > Access Control > API Token"
)


class PbsInstanceCreateRequest(BaseModel):
    """Register a PBS instance. Give the API token either as `token_id` +
    `token_secret`, or as `credentials` (RESURGAM logs in once, creates its
    own token `<user>!resurgam` and grants it the required roles), or via
    the deprecated combined `api_token`. The UI runs `POST
    /pbs-instances/{id}/test-connection` right after creating.
    """

    display_name: str = Field(min_length=1, description="Name shown in RESURGAM, e.g. `PBS Zurich 1`.")
    location: str | None = Field(default=None, description="Optional site/location label, e.g. `DC1`.")
    host: str = Field(min_length=1, description="Host name or IP address of the PBS (no scheme, no port).")
    port: int = Field(default=8007, gt=0, le=65535, description="PBS API port, 1-65535 (PBS default 8007).")
    tls_trust_mode: TlsTrustMode = Field(
        description="`ca`: trust the system CA store. `fingerprint`: pin the certificate's SHA-256 fingerprint "
        "(needed for PBS's default self-signed certificate)."
    )
    tls_fingerprint_sha256: str | None = Field(
        default=None,
        description="Confirmed SHA-256 fingerprint (from `POST /pbs-instances/probe-tls`); required for "
        "`fingerprint` mode. Re-verified against the live host on save.",
    )
    token_id: str | None = Field(default=None, description=_PBS_TOKEN_ID_DESCRIPTION)
    token_secret: str | None = Field(default=None, description=TOKEN_SECRET_DESCRIPTION)
    credentials: TokenLoginCredentials | None = Field(
        default=None,
        description="Credentials mode instead of a token: RESURGAM creates and stores its own token, the "
        "password is discarded.",
    )
    api_token: str | None = Field(
        default=None,
        json_schema_extra={"deprecated": True},
        description=API_TOKEN_DEPRECATED_DESCRIPTION.format(sep=":"),
    )

    @model_validator(mode="after")
    def _fingerprint_required_when_pinned(self) -> "PbsInstanceCreateRequest":
        if self.tls_trust_mode == TlsTrustMode.FINGERPRINT and not self.tls_fingerprint_sha256:
            raise ValueError("tls_fingerprint_sha256 is required when tls_trust_mode is 'fingerprint'")
        resolve_token_input(
            "pbs",
            token_id=self.token_id,
            token_secret=self.token_secret,
            api_token=self.api_token,
            has_credentials=self.credentials is not None,
            required=True,
        )
        return self

    def stored_token(self) -> str | None:
        """Combined token to store (`user@realm!name:secret`), or None in credentials mode."""
        return combined_token("pbs", token_id=self.token_id, token_secret=self.token_secret, api_token=self.api_token)


class PbsInstanceUpdateRequest(BaseModel):
    """Partial update -- an omitted field leaves the current value
    unchanged. There is no way to clear `location` back to null through
    this endpoint. Token fields: leave them out to keep the stored token;
    `token_secret` alone keeps the stored token ID, `token_id` alone keeps
    the stored secret; `credentials` makes RESURGAM create a new token. The
    token secret is never returned by any endpoint. The UI re-runs the
    connection test after a change to host, port, TLS or token.
    """

    display_name: str | None = Field(default=None, min_length=1, description="New display name.")
    location: str | None = Field(default=None, description="New site/location label.")
    host: str | None = Field(default=None, min_length=1, description="New host name or IP address.")
    port: int | None = Field(default=None, gt=0, le=65535, description="New API port, 1-65535.")
    tls_trust_mode: TlsTrustMode | None = Field(default=None, description="New TLS trust mode (`ca`/`fingerprint`).")
    tls_fingerprint_sha256: str | None = Field(
        default=None, description="New confirmed SHA-256 fingerprint; re-verified against the live host."
    )
    token_id: str | None = Field(default=None, description=_PBS_TOKEN_ID_DESCRIPTION + " Omit to keep the stored one.")
    token_secret: str | None = Field(
        default=None, description=TOKEN_SECRET_DESCRIPTION + " Omit (or leave blank) to keep the stored one."
    )
    credentials: TokenLoginCredentials | None = Field(
        default=None, description="Let RESURGAM create a new token with these credentials (replaces the stored one)."
    )
    api_token: str | None = Field(
        default=None,
        json_schema_extra={"deprecated": True},
        description=API_TOKEN_DEPRECATED_DESCRIPTION.format(sep=":"),
    )

    @model_validator(mode="after")
    def _token_fields_valid(self) -> "PbsInstanceUpdateRequest":
        resolve_token_input(
            "pbs",
            token_id=self.token_id,
            token_secret=self.token_secret,
            api_token=self.api_token,
            has_credentials=self.credentials is not None,
            required=False,
        )
        return self


class PbsInstanceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    display_name: str
    location: str | None
    host: str
    port: int
    tls_trust_mode: TlsTrustMode
    tls_fingerprint_sha256: str | None
    token_id: str | None = Field(
        default=None,
        description="ID of the stored API token (`user@realm!tokenname`), derived from the stored token. Not "
        "secret; the secret itself is never returned.",
    )
    token_secret_set: bool = Field(
        default=True,
        description="False when no API token secret is stored -- typically a connection imported from a "
        "configuration backup (secrets are never exported). Such a connection reports `auth_failed` (secret not "
        "set) and is skipped by the Collector until the secret is entered via edit (`token_secret`).",
    )
    version: str | None
    last_health_check_at: datetime | None = Field(
        description="When the last connection check (test or heartbeat) ran -- the 'last heartbeat'."
    )
    last_health_status: ConnectionHealthStatus = Field(
        description="Classified result of the last connection check: ok, unreachable, tls_mismatch, auth_failed, "
        "missing_privileges, or unknown (never checked)."
    )
    last_collector_sync_at: datetime | None
    server_timezone: str | None = Field(
        default=None,
        description="The PBS server's own timezone (IANA name, e.g. `Europe/Zurich`), read by every Collector "
        "sync. PBS prune rules count days and hours in this zone, so scheduled File Indexing uses it to pick the "
        "snapshot `keep-daily` will keep. Null until the first successful sync; read-only.",
    )
    disabled: bool
    instant_recoveries_reading: list[str] = Field(
        default_factory=list,
        description=(
            "Datastores of this PBS that VMs run straight from right now (Instant Recovery, ADR 0030), one entry "
            "per running VM. While non-empty, stopping this PBS, its network or a datastore gives those VMs I/O "
            "errors. Read-only."
        ),
    )


DatastoreMaintenanceModeLiteral = Literal["read-only", "offline", "delete", "unmount", "s3-refresh"]


class DatastoreMaintenanceModeOut(BaseModel):
    mode: DatastoreMaintenanceModeLiteral | None
    message: str | None


class SetDatastoreMaintenanceModeRequest(BaseModel):
    mode: DatastoreMaintenanceModeLiteral | None = Field(
        default=None, description="`read-only`, `offline`, or null to end maintenance mode."
    )
    message: str | None = Field(default=None, description="Shown by PBS while the datastore is in maintenance.")
    confirm_instant_recovery: bool = Field(
        default=False,
        description=(
            "Required (true) to set a maintenance mode while an Instant Recovery reads from this datastore "
            "(ADR 0030 §6): `offline` cuts the VM running from the backup off its disks. Without it the call is "
            "refused with 409 `instant_recovery_active`, listing the affected VMs."
        ),
    )


class DatastoreGcScheduleOut(BaseModel):
    gc_schedule: str | None


class SetDatastoreGcScheduleRequest(BaseModel):
    gc_schedule: str | None = Field(
        default=None,
        description=(
            "PBS calendar event for garbage collection on this datastore, e.g. `daily` or `sat 03:00`. "
            "`null`, an empty string or omitting it removes the schedule, so garbage collection only runs when "
            "started manually."
        ),
    )

    @model_validator(mode="after")
    def _blank_means_clear(self) -> "SetDatastoreGcScheduleRequest":
        if self.gc_schedule is not None and not self.gc_schedule.strip():
            self.gc_schedule = None
        elif self.gc_schedule is not None:
            self.gc_schedule = self.gc_schedule.strip()
        return self


class PbsCapabilitiesOut(BaseModel):
    """Whether the PBS instance's own configured token can actually
    read/write a given job category, computed live from `GET
    /access/permissions` -- never assumed (docs/adr/0023-backup-and-maintenance-job-administration,
    mirroring `pve.PveCapabilitiesOut`'s identical reasoning for PVE).
    """

    can_read: bool = Field(description="True if the PBS token holds every privilege needed to list this job category.")
    can_write: bool = Field(
        description="True if the PBS token holds every privilege needed to create, edit and delete jobs of this category."
    )
    missing_write_privileges: list[str] = Field(
        default_factory=list,
        description=(
            "PBS privileges (checked on path `/`) the token lacks for writing this job category, e.g. "
            '`["Datastore.Modify"]`. Empty when `can_write` is true. Grant these to the API token (or its user) '
            "in PBS under Configuration > Access Control > Permissions to enable editing."
        ),
    )


class PruneJobOut(BaseModel):
    id: str
    store: str
    schedule: str | None = None
    ns: str | None = None
    keep_last: int | None = None
    keep_hourly: int | None = None
    keep_daily: int | None = None
    keep_weekly: int | None = None
    keep_monthly: int | None = None
    keep_yearly: int | None = None
    disable: bool = False
    comment: str | None = None


class PruneJobCreateRequest(BaseModel):
    schedule: str | None = None
    ns: str | None = None
    keep_last: int | None = None
    keep_hourly: int | None = None
    keep_daily: int | None = None
    keep_weekly: int | None = None
    keep_monthly: int | None = None
    keep_yearly: int | None = None
    disable: bool = False
    comment: str | None = None


_CLEAR_HINT = "Omit to leave unchanged; send `null` (or an empty string) to remove the value on PBS."


class PruneJobUpdateRequest(BaseModel):
    """Partial update. An omitted field is left unchanged; a field sent as
    `null` (or `""`) is removed from the job on PBS via PBS's own `delete`
    parameter (`PUT /config/prune/{id}`, verified against the PBS API
    viewer). `schedule` cannot be removed -- PBS requires one for every
    prune job -- so `schedule: null` is rejected with 400. A body with no
    fields at all is rejected with 400 `no_changes`.
    """

    schedule: str | None = Field(
        default=None,
        description="PBS calendar event, e.g. `daily` or `21:00`. Required by PBS for prune jobs: cannot be cleared.",
    )
    ns: str | None = Field(default=None, description=f"Namespace to prune (empty = root). {_CLEAR_HINT}")
    keep_last: int | None = Field(default=None, ge=1, description=f"Keep the N most recent backups. {_CLEAR_HINT}")
    keep_hourly: int | None = Field(default=None, ge=1, description=f"Hourly backups to keep. {_CLEAR_HINT}")
    keep_daily: int | None = Field(default=None, ge=1, description=f"Daily backups to keep. {_CLEAR_HINT}")
    keep_weekly: int | None = Field(default=None, ge=1, description=f"Weekly backups to keep. {_CLEAR_HINT}")
    keep_monthly: int | None = Field(default=None, ge=1, description=f"Monthly backups to keep. {_CLEAR_HINT}")
    keep_yearly: int | None = Field(default=None, ge=1, description=f"Yearly backups to keep. {_CLEAR_HINT}")
    disable: bool | None = Field(
        default=None, description="True pauses the job's schedule, false re-enables it. `null` resets to PBS's default."
    )
    comment: str | None = Field(default=None, max_length=128, description=f"Free-text comment. {_CLEAR_HINT}")


class VerifyJobOut(BaseModel):
    """No `disable` field -- confirmed live 2026-09-27 that PBS's
    `/config/verify` schema rejects it outright on both create and
    update, unlike prune jobs (see `PruneJobOut`'s sibling, which
    genuinely has one).
    """

    id: str
    store: str
    schedule: str | None = None
    ns: str | None = None
    outdated_after: int | None = None
    ignore_verified: bool | None = None
    comment: str | None = None


class VerifyJobCreateRequest(BaseModel):
    schedule: str | None = None
    ns: str | None = None
    outdated_after: int | None = None
    ignore_verified: bool | None = None
    comment: str | None = None


class VerifyJobUpdateRequest(BaseModel):
    """Partial update. An omitted field is left unchanged; a field sent as
    `null` (or `""`) is removed from the job on PBS via PBS's `delete`
    parameter (`PUT /config/verify/{id}`, verified against the PBS API
    viewer). Verify jobs have no `disable` flag on PBS -- clearing
    `schedule` turns the job into a manual-only (run on demand) job.
    """

    schedule: str | None = Field(
        default=None,
        description=f"PBS calendar event. {_CLEAR_HINT} Without a schedule the job only runs when started manually.",
    )
    ns: str | None = Field(default=None, description=f"Namespace to verify (empty = root). {_CLEAR_HINT}")
    outdated_after: int | None = Field(
        default=None, ge=0, description=f"Days after which a verification counts as outdated. {_CLEAR_HINT}"
    )
    ignore_verified: bool | None = Field(
        default=None, description="Skip snapshots whose verification is not outdated. `null` resets to PBS's default."
    )
    comment: str | None = Field(default=None, max_length=128, description=f"Free-text comment. {_CLEAR_HINT}")


class RemoteOut(BaseModel):
    """`password` is deliberately absent -- PBS never returns it (write-only
    on the wire) and RESURGAM never persists it either (docs/adr/0023-backup-and-maintenance-job-administration).
    """

    name: str
    host: str
    auth_id: str | None = None
    port: int | None = None
    fingerprint: str | None = None
    comment: str | None = None


class RemoteCreateRequest(BaseModel):
    name: str
    host: str
    auth_id: str
    password: str
    port: int | None = None
    fingerprint: str | None = None
    comment: str | None = None


class RemoteUpdateRequest(BaseModel):
    """Every field optional. `password`, if given, rotates the remote's
    stored credential on *that* server via a plain pass-through PUT --
    RESURGAM still never persists it (see `RemoteOut`).
    """

    host: str | None = None
    auth_id: str | None = None
    password: str | None = None
    port: int | None = None
    fingerprint: str | None = None
    comment: str | None = None


class SyncJobOut(BaseModel):
    """No `disable` field -- confirmed live 2026-09-27 that PBS's
    `/config/sync` schema rejects it outright on both create and update.
    A sync job can only be removed entirely, not toggled off.
    """

    job_id: str
    local_store: str
    remote_store: str
    remote_name: str | None = None
    ns: str | None = None
    schedule: str | None = None
    remove_vanished: bool | None = None
    owner: str | None = None
    comment: str | None = None


class SyncJobCreateRequest(BaseModel):
    remote_store: str
    remote_name: str | None = None
    ns: str | None = None
    schedule: str | None = None
    remove_vanished: bool | None = None
    owner: str | None = None
    comment: str | None = None


class SyncJobUpdateRequest(BaseModel):
    """Partial update. An omitted field is left unchanged; a field sent as
    `null` (or `""`) is removed from the job on PBS via PBS's `delete`
    parameter (`PUT /config/sync/{id}`, verified against the PBS API
    viewer). `remote_store` cannot be removed (PBS requires it). PBS sync
    jobs have no `disable` flag; clearing `schedule` is PBS's own way to
    stop a sync job from running automatically -- it then only runs when
    started manually.
    """

    remote_store: str | None = Field(
        default=None, description="Datastore name on the remote PBS to pull from. Required by PBS: cannot be cleared."
    )
    remote_name: str | None = Field(
        default=None, description=f"Name of the configured PBS Remote to pull from. {_CLEAR_HINT}"
    )
    ns: str | None = Field(default=None, description=f"Local target namespace (empty = root). {_CLEAR_HINT}")
    schedule: str | None = Field(
        default=None,
        description=f"PBS calendar event. {_CLEAR_HINT} Without a schedule the job only runs when started manually.",
    )
    remove_vanished: bool | None = Field(
        default=None,
        description="Delete local snapshots that no longer exist on the remote. `null` resets to PBS's default (off).",
    )
    owner: str | None = Field(
        default=None, description=f"PBS user or token that owns the synced backups, e.g. `root@pam`. {_CLEAR_HINT}"
    )
    comment: str | None = Field(default=None, max_length=128, description=f"Free-text comment. {_CLEAR_HINT}")
