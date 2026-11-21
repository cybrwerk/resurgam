from typing import Literal

from pydantic import BaseModel, Field

UpdateAction = Literal["verify", "apply"]
UpdateState = Literal["queued", "running", "succeeded", "failed", "rolled_back"]


class UpdatesOverviewOut(BaseModel):
    available: bool = Field(
        description="True when the shared updates directory exists and is writable, i.e. this is an appliance "
        "with the host-side updater installed. False in local dev; every other /updates endpoint then answers 503."
    )
    unavailable_reason: str | None = Field(
        default=None, description="Why updates are unavailable (and how to fix it), when `available` is false."
    )
    current_version: str = Field(description="Running RESURGAM release version, same as GET /version.")
    max_bundle_bytes: int = Field(
        description="Largest bundle upload accepted, in bytes (setting UPDATES_MAX_BUNDLE_BYTES)."
    )
    active_apply: "UpdateRequestStatusOut | None" = Field(
        default=None,
        description="The apply request that is currently queued or running, if any -- lets the UI resume showing "
        "progress after a page reload or the backend restart that happens during an update.",
    )


class UpdateBundleOut(BaseModel):
    name: str = Field(
        description="File name inside the updates `incoming/` directory; pass it as `bundle` in requests."
    )
    size_bytes: int = Field(description="Bundle size in bytes.")
    uploaded_at: str = Field(description="When the file landed in `incoming/` (ISO 8601, UTC).")
    version: str | None = Field(
        default=None,
        description="Version from the bundle's manifest.json, read without verifying the signature (the host updater "
        "does that). Null when the manifest cannot be read.",
    )
    problem: str | None = Field(
        default=None,
        description="Why the file does not look like a usable bundle (missing manifest/signature), null when it does.",
    )
    signature_prechecked: bool = Field(
        default=False,
        description="True when the upload's signature was already checked against the release key baked into the "
        "backend image (fast check, no component hashes). False when no key is available here; the host updater's "
        "verify is authoritative in both cases.",
    )


class UpdateManifestSummary(BaseModel):
    version: str | None = Field(default=None, description="Release version the bundle installs.")
    changelog: str | None = Field(default=None, description="Release notes from the manifest, plain text/Markdown.")
    min_upgrade_from: str | None = Field(
        default=None, description="Oldest running version this bundle may upgrade from."
    )


class UpdateRequestCreate(BaseModel):
    action: UpdateAction = Field(
        description="`verify` checks the signature, every component hash and the version rules without changing "
        "anything. `apply` verifies, takes a database backup, installs the release and rolls back automatically on "
        "failure."
    )
    bundle: str = Field(
        description="File name of an uploaded bundle (from POST /updates/bundles or GET /updates/bundles).",
        examples=["3f2c9a0e8b1d4c7f9e6a5b4c3d2e1f00.bundle"],
    )


class UpdateRequestStatusOut(BaseModel):
    request_id: str = Field(description="Id of the request (32 hex characters).")
    action: UpdateAction | None = Field(default=None, description="What was requested.")
    bundle: str | None = Field(default=None, description="Bundle file the request refers to.")
    requested_by: str | None = Field(default=None, description="Who requested it (email of the RESURGAM user).")
    requested_at: str | None = Field(default=None, description="When it was requested (ISO 8601, UTC).")
    state: UpdateState = Field(
        description="`queued` until the host updater picks the request up (no status file yet), then `running`, and "
        "finally `succeeded`, `failed` (nothing changed or verification rejected the bundle) or `rolled_back` (the "
        "update failed after the backup and the previous version was restored)."
    )
    step: str | None = Field(
        default=None,
        description="Current/last step: verify, backup, stop, load_images, install, host_setup (since 0.2.2: the new "
        "release's host-setup hook, e.g. installing host packages from the bundle), migrate, start, health, rollback, "
        "done.",
    )
    from_version: str | None = Field(default=None, description="Version running when the request started.")
    to_version: str | None = Field(default=None, description="Version the bundle installs.")
    message: str | None = Field(
        default=None, description="Human-readable result or failure reason (e.g. why verification rejected the bundle)."
    )
    log: list[str] = Field(default_factory=list, description="Progress log lines written by the host updater.")
    manifest: UpdateManifestSummary | None = Field(
        default=None, description="Manifest summary, once the updater has read the bundle."
    )
    backup_file: str | None = Field(default=None, description="Pre-update database dump taken by an apply.")
    started_at: str | None = Field(default=None, description="When the updater started the run (ISO 8601).")
    finished_at: str | None = Field(default=None, description="When the run finished (ISO 8601).")


UpdatesOverviewOut.model_rebuild()


class UpdateHistoryEntryOut(BaseModel):
    """One entry of the updater's history.json. Unknown extra keys the
    updater writes are passed through untouched."""

    model_config = {"extra": "allow"}

    request_id: str | None = Field(default=None, description="Apply request that produced this entry.")
    state: str | None = Field(default=None, description="Outcome: succeeded, failed or rolled_back.")
    from_version: str | None = Field(default=None, description="Version before the update.")
    to_version: str | None = Field(default=None, description="Version the update installed (or tried to).")
    message: str | None = Field(default=None, description="Result or failure/rollback reason.")
    backup_file: str | None = Field(default=None, description="Database dump taken before the update.")
    requested_by: str | None = Field(default=None, description="Who requested the update.")
    started_at: str | None = Field(default=None, description="When the update started (ISO 8601).")
    finished_at: str | None = Field(default=None, description="When it finished (ISO 8601).")


class UpdateBackupOut(BaseModel):
    name: str = Field(description="Dump file name in the backups directory (pre-<version>-<timestamp>.dump).")
    size_bytes: int = Field(description="Dump size in bytes.")
    created_at: str = Field(description="File modification time (ISO 8601, UTC).")
