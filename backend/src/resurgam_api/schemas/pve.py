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

_PVE_TOKEN_ID_DESCRIPTION = TOKEN_ID_DESCRIPTION.format(
    example="resurgam@pve!resurgam", gui="PVE", location="Datacenter > Permissions > API Tokens"
)

_UPLINK_VLAN_DESCRIPTION = (
    "Optional 802.1Q VLAN tag (1-4094) for the probe appliance's uplink NIC, sent to PVE as `tag=` on the netX "
    "device. `null` = untagged. The bridge must carry that VLAN (VLAN-aware Linux bridge, OVS bridge, or PVE's "
    "traditional VLAN method on a bridge with a physical port) -- the lab readiness check reports it."
)
_UPLINK_IP_MODE_DESCRIPTION = (
    "How the probe appliance's uplink gets its IPv4 address: `dhcp` (default -- the uplink bridge's LAN hands one "
    "out) or `static_range` (RESURGAM gives every active lab run its own address from range start..end, with the "
    "prefix length, gateway and DNS servers below, via cloud-init). Static needs all of those fields."
)
_UPLINK_RANGE_START_DESCRIPTION = "First IPv4 address RESURGAM may hand out (static_range only), e.g. `192.168.10.50`."
_UPLINK_RANGE_END_DESCRIPTION = (
    "Last IPv4 address RESURGAM may hand out (static_range only); same subnet as the start, not before it. One "
    "address per active lab run (kept until the run is cleaned up)."
)
_UPLINK_PREFIX_DESCRIPTION = "Subnet prefix length of the uplink network (static_range only), 8-30, e.g. `24`."
_UPLINK_GATEWAY_DESCRIPTION = (
    "Default gateway of the uplink network (static_range only); inside the subnet, outside the range."
)
# The name ends up in comments of files RESURGAM writes onto nodes (the lab
# agent's env file, the pairing script): a line break there would start a
# new shell line.
_DISPLAY_NAME_PATTERN = r"^[^\x00-\x1f\x7f]+$"

_UPLINK_DNS_DESCRIPTION = (
    "1-3 IPv4 DNS servers for the probe appliance (static_range only; required there so it can resolve the "
    "RESURGAM backend's host name). Sent to PVE as cloud-init `nameserver`."
)


class PveClusterCreateRequest(BaseModel):
    """Register a PVE cluster. Give the API token either as `token_id` +
    `token_secret`, or as `credentials` (RESURGAM logs in once, creates its
    own token `<user>!resurgam`, creates/updates the role `RESURGAM` and
    grants it to the token at `/`), or via the deprecated combined
    `api_token`. The UI runs `POST /pve-clusters/{id}/test-connection` right
    after creating.
    """

    display_name: str = Field(
        min_length=1,
        pattern=_DISPLAY_NAME_PATTERN,
        description="Name shown in RESURGAM, e.g. `PVE Cluster Zurich`. One line: no control characters "
        "(line breaks, tabs).",
    )
    host: str = Field(min_length=1, description="Host name or IP address of any PVE node (no scheme, no port).")
    port: int = Field(default=8006, gt=0, le=65535, description="PVE API port, 1-65535 (PVE default 8006).")
    tls_trust_mode: TlsTrustMode = Field(
        description="`ca`: trust the system CA store. `fingerprint`: pin the certificate's SHA-256 fingerprint "
        "(needed for PVE's default self-signed certificate)."
    )
    tls_fingerprint_sha256: str | None = Field(
        default=None,
        description="Confirmed SHA-256 fingerprint (from `POST /pve-clusters/probe-tls`); required for "
        "`fingerprint` mode. Re-verified against the live host on save.",
    )
    token_id: str | None = Field(default=None, description=_PVE_TOKEN_ID_DESCRIPTION)
    token_secret: str | None = Field(default=None, description=TOKEN_SECRET_DESCRIPTION)
    credentials: TokenLoginCredentials | None = Field(
        default=None,
        description="Credentials mode instead of a token: RESURGAM creates and stores its own token, the "
        "password is discarded.",
    )
    api_token: str | None = Field(
        default=None,
        json_schema_extra={"deprecated": True},
        description=API_TOKEN_DEPRECATED_DESCRIPTION.format(sep="="),
    )
    allowed_target_storages: list[str] | None = None
    restore_network_rules: dict | None = None
    require_restore_approval: bool = False
    linked_pbs_instance_ids: list[UUID] = Field(default_factory=list)
    linked_datastore_ids: list[UUID] = Field(
        default_factory=list,
        description=(
            "Datastores whose backups actually come from this cluster, used only to "
            "resolve real workload/guest names when a backup group's PBS notes are "
            "empty -- distinct from linked_pbs_instance_ids, which governs restore "
            "capability, not name resolution."
        ),
    )
    is_lab_compute: bool = False
    lab_sdn_zone_name: str | None = None
    lab_appliance_template_vmid: int | None = None
    lab_snippet_storage_id: str | None = Field(
        default=None,
        description="A PVE storage (content type 'snippets') resurgam-lab-agent writes each run's probe appliance cloud-init config to -- required before a run's appliance can ever report back.",
    )
    lab_appliance_uplink_bridge: str | None = Field(
        default=None,
        description="An existing, already-connected PVE bridge (e.g. 'vmbr0') for the probe appliance's own uplink NIC -- PVE never provisions a subnet on the isolated Lab VNet itself; the appliance is dual-homed and reaches the backend via this bridge.",
    )
    lab_appliance_uplink_vlan_tag: int | None = Field(default=None, ge=1, le=4094, description=_UPLINK_VLAN_DESCRIPTION)
    lab_appliance_uplink_ip_mode: Literal["dhcp", "static_range"] = Field(
        default="dhcp", description=_UPLINK_IP_MODE_DESCRIPTION
    )
    lab_appliance_uplink_ip_range_start: str | None = Field(
        default=None, max_length=45, description=_UPLINK_RANGE_START_DESCRIPTION
    )
    lab_appliance_uplink_ip_range_end: str | None = Field(
        default=None, max_length=45, description=_UPLINK_RANGE_END_DESCRIPTION
    )
    lab_appliance_uplink_ip_prefix_length: int | None = Field(
        default=None, ge=8, le=30, description=_UPLINK_PREFIX_DESCRIPTION
    )
    lab_appliance_uplink_gateway: str | None = Field(
        default=None, max_length=45, description=_UPLINK_GATEWAY_DESCRIPTION
    )
    lab_appliance_uplink_dns_servers: list[str] = Field(
        default_factory=list, max_length=3, description=_UPLINK_DNS_DESCRIPTION
    )

    @model_validator(mode="after")
    def _fingerprint_required_when_pinned(self) -> "PveClusterCreateRequest":
        if self.tls_trust_mode == TlsTrustMode.FINGERPRINT and not self.tls_fingerprint_sha256:
            raise ValueError("tls_fingerprint_sha256 is required when tls_trust_mode is 'fingerprint'")
        resolve_token_input(
            "pve",
            token_id=self.token_id,
            token_secret=self.token_secret,
            api_token=self.api_token,
            has_credentials=self.credentials is not None,
            required=True,
        )
        return self

    def stored_token(self) -> str | None:
        """Combined token to store (`user@realm!name=secret`), or None in credentials mode."""
        return combined_token("pve", token_id=self.token_id, token_secret=self.token_secret, api_token=self.api_token)


class PveClusterUpdateRequest(BaseModel):
    """Partial update — an omitted field leaves the current value
    unchanged. Token fields: leave them out to keep the stored token;
    `token_secret` alone keeps the stored token ID, `token_id` alone keeps
    the stored secret; `credentials` makes RESURGAM create a new token.
    `linked_pbs_instance_ids`/`linked_datastore_ids`, if given,
    each *replace* their own full set of links (not a merge) — omit
    either entirely to leave that link set untouched. The UI re-runs the
    connection test after a change to host, port, TLS or token.
    """

    display_name: str | None = Field(
        default=None,
        min_length=1,
        pattern=_DISPLAY_NAME_PATTERN,
        description="New display name. One line: no control characters (line breaks, tabs).",
    )
    host: str | None = Field(default=None, min_length=1, description="New host name or IP address.")
    port: int | None = Field(default=None, gt=0, le=65535, description="New API port, 1-65535.")
    tls_trust_mode: TlsTrustMode | None = Field(default=None, description="New TLS trust mode (`ca`/`fingerprint`).")
    tls_fingerprint_sha256: str | None = Field(
        default=None, description="New confirmed SHA-256 fingerprint; re-verified against the live host."
    )
    token_id: str | None = Field(default=None, description=_PVE_TOKEN_ID_DESCRIPTION + " Omit to keep the stored one.")
    token_secret: str | None = Field(
        default=None, description=TOKEN_SECRET_DESCRIPTION + " Omit (or leave blank) to keep the stored one."
    )
    credentials: TokenLoginCredentials | None = Field(
        default=None, description="Let RESURGAM create a new token with these credentials (replaces the stored one)."
    )
    api_token: str | None = Field(
        default=None,
        json_schema_extra={"deprecated": True},
        description=API_TOKEN_DEPRECATED_DESCRIPTION.format(sep="="),
    )
    allowed_target_storages: list[str] | None = None
    restore_network_rules: dict | None = None
    require_restore_approval: bool | None = None
    instant_recovery_enabled: bool | None = Field(
        default=None,
        description=(
            "Allow Instant Recovery on this cluster (ADR 0030): restore plans with `method=instant` run a VM "
            "straight from its PBS backup through the cluster's paired resurgam-lab-agent. Omit to leave unchanged."
        ),
    )
    instant_recovery_alarm_hours: int | None = Field(
        default=None,
        ge=1,
        le=720,
        description="Hours a VM may run from its backup before an alarm asks to move or discard it (default 24).",
    )
    linked_pbs_instance_ids: list[UUID] | None = None
    linked_datastore_ids: list[UUID] | None = Field(
        default=None,
        description=(
            "Datastores whose backups actually come from this cluster, used only to "
            "resolve real workload/guest names when a backup group's PBS notes are "
            "empty. Replaces the full set when given; omit to leave unchanged."
        ),
    )
    is_lab_compute: bool | None = None
    lab_sdn_zone_name: str | None = None
    lab_appliance_template_vmid: int | None = None
    lab_snippet_storage_id: str | None = None
    lab_appliance_uplink_bridge: str | None = None
    lab_appliance_uplink_vlan_tag: int | None = Field(
        default=None, ge=1, le=4094, description=_UPLINK_VLAN_DESCRIPTION + " Send `null` to remove the tag."
    )
    lab_appliance_uplink_ip_mode: Literal["dhcp", "static_range"] | None = Field(
        default=None, description=_UPLINK_IP_MODE_DESCRIPTION + " Omit to leave unchanged."
    )
    lab_appliance_uplink_ip_range_start: str | None = Field(
        default=None, max_length=45, description=_UPLINK_RANGE_START_DESCRIPTION
    )
    lab_appliance_uplink_ip_range_end: str | None = Field(
        default=None, max_length=45, description=_UPLINK_RANGE_END_DESCRIPTION
    )
    lab_appliance_uplink_ip_prefix_length: int | None = Field(
        default=None, ge=8, le=30, description=_UPLINK_PREFIX_DESCRIPTION
    )
    lab_appliance_uplink_gateway: str | None = Field(
        default=None, max_length=45, description=_UPLINK_GATEWAY_DESCRIPTION
    )
    lab_appliance_uplink_dns_servers: list[str] | None = Field(
        default=None, max_length=3, description=_UPLINK_DNS_DESCRIPTION + " Omit to leave unchanged."
    )

    @model_validator(mode="after")
    def _token_fields_valid(self) -> "PveClusterUpdateRequest":
        resolve_token_input(
            "pve",
            token_id=self.token_id,
            token_secret=self.token_secret,
            api_token=self.api_token,
            has_credentials=self.credentials is not None,
            required=False,
        )
        return self


class PveClusterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    display_name: str
    host: str
    port: int
    tls_trust_mode: TlsTrustMode
    tls_fingerprint_sha256: str | None
    allowed_target_storages: list[str] | None
    restore_network_rules: dict | None
    require_restore_approval: bool
    instant_recovery_enabled: bool = Field(
        default=False,
        description="Whether restore plans on this cluster may use Instant Recovery (`method=instant`, ADR 0030).",
    )
    instant_recovery_alarm_hours: int = Field(
        default=24, description="Hours a VM may run from its backup before the `instant_recovery_running_long` alarm."
    )
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
    disabled: bool
    linked_pbs_instance_ids: list[UUID]
    linked_datastore_ids: list[UUID] = Field(
        description=(
            "Datastores linked to this cluster for workload-name resolution provenance "
            "(see linked_pbs_instance_ids for the separate restore-capability link)."
        )
    )
    is_lab_compute: bool
    lab_sdn_zone_name: str | None
    lab_appliance_template_vmid: int | None
    lab_snippet_storage_id: str | None = Field(
        default=None,
        description="A PVE storage (content type 'snippets') resurgam-lab-agent writes each run's probe appliance cloud-init config to -- required before a run's appliance can ever report back.",
    )
    lab_appliance_uplink_bridge: str | None = Field(
        default=None,
        description="An existing, already-connected PVE bridge (e.g. 'vmbr0') for the probe appliance's own uplink NIC -- PVE never provisions a subnet on the isolated Lab VNet itself; the appliance is dual-homed and reaches the backend via this bridge.",
    )
    lab_appliance_uplink_vlan_tag: int | None = Field(default=None, description=_UPLINK_VLAN_DESCRIPTION)
    lab_appliance_uplink_ip_mode: Literal["dhcp", "static_range"] = Field(
        default="dhcp", description=_UPLINK_IP_MODE_DESCRIPTION
    )
    lab_appliance_uplink_ip_range_start: str | None = Field(default=None, description=_UPLINK_RANGE_START_DESCRIPTION)
    lab_appliance_uplink_ip_range_end: str | None = Field(default=None, description=_UPLINK_RANGE_END_DESCRIPTION)
    lab_appliance_uplink_ip_prefix_length: int | None = Field(default=None, description=_UPLINK_PREFIX_DESCRIPTION)
    lab_appliance_uplink_gateway: str | None = Field(default=None, description=_UPLINK_GATEWAY_DESCRIPTION)
    lab_appliance_uplink_dns_servers: list[str] = Field(default_factory=list, description=_UPLINK_DNS_DESCRIPTION)
    lab_agent_node: str | None = Field(
        default=None,
        description="The node the lab agent was paired on with a one-time code (ADR 0027) -- where agent-run "
        "template builds happen. `null` when no agent was paired (an SSH or manual install also leaves it unset).",
    )
    lab_agent_last_seen_at: datetime | None = Field(
        default=None,
        description="When resurgam-lab-agent last authenticated with this cluster's agent token (any /lab-agent/* "
        "call, recorded at 30-second resolution); `null` if it never checked in. See "
        "`GET /pve-clusters/{id}/lab-readiness` for the full Lab Compute checklist.",
    )


class PveNodeOut(BaseModel):
    node: str
    status: str | None = None
    cpu: float | None = None
    maxmem: int | None = None
    uptime: int | None = None


class PveStorageOut(BaseModel):
    storage: str
    type: str
    content: list[str]
    active: bool | None = None
    shared: bool | None = None
    avail_bytes: int | None = None
    used_bytes: int | None = None
    total_bytes: int | None = None
    pbs_server: str | None = None
    pbs_datastore: str | None = None
    pbs_namespace: str | None = None


class PveGuestOut(BaseModel):
    vmid: int
    node: str
    guest_type: str
    name: str | None = None
    status: str | None = None


class PvePoolOut(BaseModel):
    poolid: str
    comment: str | None = None


class PveVmidCheckOut(BaseModel):
    vmid: int
    free: bool


class VzdumpJobOut(BaseModel):
    """`prune_keep_*` mirrors `PruneJobOut`'s own field names on purpose
    -- see `connectivity.models.VzdumpJob`'s docstring for why this
    exists (real vzdump-level retention found live on this environment,
    2026-09-27) and how the frontend is meant to cross-check it against
    the target storage's real PBS Prune Job(s) rather than treat it as
    the only mechanism.
    """

    id: str
    enabled: bool
    schedule: str | None = None
    storage: str | None = None
    node: str | None = None
    comment: str | None = None
    all_guests: bool = False
    vmid: str | None = None
    pool: str | None = None
    exclude: str | None = None
    mode: str | None = None
    compress: str | None = None
    notes_template: str | None = None
    mailnotification: str | None = None
    mailto: str | None = None
    prune_keep_last: int | None = None
    prune_keep_hourly: int | None = None
    prune_keep_daily: int | None = None
    prune_keep_weekly: int | None = None
    prune_keep_monthly: int | None = None
    prune_keep_yearly: int | None = None
    # An explicit override only -- a job's default, in-design scope is
    # inherited from its target datastore/namespace, not stored here.
    # `None` means no override is set, not "no customer."
    customer_id: UUID | None = None
    reenable_at: datetime | None = Field(
        default=None,
        description=(
            "When RESURGAM will automatically enable this disabled job again (UTC), set by "
            "'disable for N hours'. Null when the job is enabled or disabled without a time limit. "
            "The collector scheduler enables the job within about a minute after this time."
        ),
    )
    reenable_last_error: str | None = Field(
        default=None,
        description=(
            "Error from the last failed automatic re-enable attempt (e.g. PVE unreachable). The attempt "
            "is retried every minute until it succeeds or the timer is cancelled. Null if no attempt failed."
        ),
    )


REENABLE_HOURS_DESCRIPTION = (
    "Enable the job again automatically after this many hours (1-168, i.e. up to one week). "
    "Omit or set to null to disable the job until someone enables it again."
)


class DisableBackupJobRequest(BaseModel):
    """Optional body for disabling a backup job. Without a body (or with
    `reenable_after_hours` null) the job stays disabled indefinitely."""

    reenable_after_hours: int | None = Field(default=None, ge=1, le=168, description=REENABLE_HOURS_DESCRIPTION)


class BackupJobReenableScheduleRequest(BaseModel):
    """Sets (or replaces) the automatic re-enable timer of an already
    disabled backup job, without changing the job in PVE now."""

    reenable_after_hours: int = Field(
        ge=1, le=168, description="Enable the job again automatically after this many hours from now (1-168)."
    )


class AssignBackupJobCustomerRequest(BaseModel):
    customer_id: UUID


class VzdumpJobCreateRequest(BaseModel):
    """The common, practically useful field subset for creating a vzdump
    job (docs/adr/0023-backup-and-maintenance-job-administration). The
    intended, primary home for retention is still PBS's own Prune Job,
    not this -- but `prune_keep_*` is offered too, since real vzdump jobs
    on this environment already set their own (2026-09-27). At least one
    of `all_guests`/`vmid`/`pool` should be set so the job actually
    targets something, but this isn't enforced here -- PVE's own real
    error on an empty target set is clearer than a guessed validation
    rule would be.
    """

    schedule: str | None = None
    storage: str | None = None
    node: str | None = None
    comment: str | None = None
    all_guests: bool = False
    vmid: str | None = None
    pool: str | None = None
    exclude: str | None = None
    mode: str | None = None
    compress: str | None = None
    notes_template: str | None = None
    mailnotification: str | None = None
    mailto: str | None = None
    enabled: bool = True
    prune_keep_last: int | None = None
    prune_keep_hourly: int | None = None
    prune_keep_daily: int | None = None
    prune_keep_weekly: int | None = None
    prune_keep_monthly: int | None = None
    prune_keep_yearly: int | None = None


class VzdumpJobUpdateRequest(BaseModel):
    """Every field optional and `None`-means-unsent, matching PVE's own
    confirmed-live partial-update semantics for `PUT /cluster/backup/{id}`
    (ADR 0018) -- only fields an operator actually changed are ever sent.

    `prune_keep_*` is the exception to "one field, one change": PVE's
    `prune-backups` is a single property-string field on the wire (not
    independently-settable sub-keys), so the API layer always sends the
    *complete* set together whenever any one of them is touched -- see
    `api.v1.pve_clusters._vzdump_update_payload`. `clear_prune_backups`
    removes the field entirely (`delete=prune-backups`), for reverting to
    "no vzdump-level retention, PBS Prune Job only" -- mutually exclusive
    with setting any `prune_keep_*` field in the same request.
    """

    schedule: str | None = None
    storage: str | None = None
    node: str | None = None
    comment: str | None = None
    all_guests: bool | None = None
    vmid: str | None = None
    pool: str | None = None
    exclude: str | None = None
    mode: str | None = None
    compress: str | None = None
    notes_template: str | None = None
    mailnotification: str | None = None
    mailto: str | None = None
    enabled: bool | None = None
    prune_keep_last: int | None = None
    prune_keep_hourly: int | None = None
    prune_keep_daily: int | None = None
    prune_keep_weekly: int | None = None
    prune_keep_monthly: int | None = None
    prune_keep_yearly: int | None = None
    clear_prune_backups: bool = False


class PveCapabilitiesOut(BaseModel):
    """Whether the cluster's own configured PVE token can actually read/
    write vzdump job definitions -- computed live from `GET
    /access/permissions`, never assumed (docs/adr/0023-backup-and-maintenance-job-administration:
    ADR 0018 already found live that schema-documented privileges alone
    don't always predict a real write's success).
    """

    can_read: bool
    can_write: bool
