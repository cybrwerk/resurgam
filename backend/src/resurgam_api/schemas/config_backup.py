"""Configuration export/import document (ADR 0024, "Configuration
export/import").

The document is RESURGAM's *configuration* -- what an operator would
otherwise have to re-create by hand on a fresh appliance -- never runtime
or history data, and never a secret. Every cross-reference uses natural
keys (customer slug, PBS/PVE host + port, datastore name, user email,
policy name), never internal UUIDs, so a document can be imported into a
database that has completely different ids.

What each section contains (and deliberately leaves out) is spelled out in
the field descriptions below; they are what `/docs` shows.
"""

from datetime import datetime, time
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from resurgam_api.models.identity import RoleKey, ScopeType
from resurgam_api.models.pbs import TlsTrustMode

CONFIG_BACKUP_FORMAT = 1


# --- natural-key references ---------------------------------------------------


class ConnectionRef(BaseModel):
    host: str = Field(description="Host name or IP of the PBS instance / PVE cluster, exactly as configured.")
    port: int = Field(description="API port of the PBS instance (usually 8007) / PVE cluster (usually 8006).")


class DatastoreRef(BaseModel):
    pbs_host: str = Field(description="Host of the PBS instance the datastore belongs to.")
    pbs_port: int = Field(description="API port of that PBS instance.")
    datastore: str = Field(description="The datastore's name on that PBS instance.")


class BackupGroupRef(DatastoreRef):
    namespace: str = Field(default="", description="PBS namespace of the backup group ('' = root namespace).")
    backup_type: str = Field(description="PBS backup type: vm, ct or host.")
    backup_id: str = Field(description="PBS backup id (e.g. the VMID).")


class PolicyRef(BaseModel):
    customer_slug: str = Field(description="Slug of the customer owning the protection policy.")
    name: str = Field(description="The protection policy's name (unique per customer).")


class RoleScopeRef(BaseModel):
    """Exactly one field is set, matching the assignment's `scope_type`."""

    customer_slug: str | None = Field(default=None, description="For scope_type 'customer': the customer's slug.")
    pbs_instance: ConnectionRef | None = Field(default=None, description="For scope_type 'pbs_instance'.")
    datastore: DatastoreRef | None = Field(default=None, description="For scope_type 'datastore'.")
    pve_cluster: ConnectionRef | None = Field(default=None, description="For scope_type 'pve_cluster'.")


# --- sections -----------------------------------------------------------------


class InstanceSettingsSection(BaseModel):
    min_report_retention_days: int = Field(ge=1, description="Platform-wide floor for report retention, in days.")
    collector_poll_interval_minutes: int = Field(ge=1, description="Collector scheduler poll interval, in minutes.")
    require_mfa_all_local_users: bool | None = Field(
        default=None,
        description="Users & Roles -> 'Require MFA for all local users'. Absent or null (older export files) "
        "leaves the current value unchanged on import.",
    )


class BrandingSection(BaseModel):
    primary_color: str | None = Field(default=None, description="Primary color (#RRGGBB) or null for the default.")
    accent_color: str | None = Field(default=None, description="Accent color (#RRGGBB) or null for the default.")
    logo_base64: str | None = Field(
        default=None,
        description="The logo image, base64-encoded -- only when it is at most 512 KiB; larger logos are left out "
        "(see logo_omitted_reason) and must be uploaded again after an import.",
    )
    logo_content_type: str | None = Field(default=None, description="MIME type of the logo (e.g. image/png).")
    logo_omitted_reason: str | None = Field(
        default=None, description="Set when a configured logo was not embedded (too large); null otherwise."
    )
    app_name: str | None = Field(
        default=None,
        max_length=60,
        description="White-label app title shown instead of 'RESURGAM' (System Settings -> Branding); null = the "
        "default name. Absent (older export files) leaves the current title unchanged on import.",
    )


class UpdateSourceSection(BaseModel):
    provider: Literal["github", "gitlab"] = Field(description="Where the release repository lives.")
    repository_url: str = Field(min_length=1, max_length=500, description="The repository's web address.")
    tls_verify: bool = Field(default=True, description="Verify the release host's TLS certificate.")
    include_prereleases: bool = Field(default=False, description="Offer pre-releases too.")
    auto_check: bool = Field(default=True, description="Check for new releases automatically.")
    check_interval_hours: int = Field(default=24, ge=1, le=720, description="Hours between automatic checks.")
    token_set: bool = Field(
        default=False,
        description="Whether an access token was configured. The token itself is never exported -- if true it "
        "must be re-entered after an import (System Settings -> Updates).",
    )


class MailSettingsSection(BaseModel):
    smtp_host: str = Field(description="SMTP relay host.")
    smtp_port: int = Field(description="SMTP port.")
    smtp_username: str | None = Field(default=None, description="SMTP user name, or null for no authentication.")
    use_tls: bool = Field(description="Whether STARTTLS/TLS is used.")
    from_address: str = Field(description="Sender address.")
    from_name: str | None = Field(default=None, description="Sender display name.")
    enabled: bool = Field(description="Whether outbound mail is switched on.")
    smtp_password_set: bool = Field(
        description="Whether an SMTP password was configured. The password itself is never exported -- if true "
        "it must be re-entered after an import."
    )


class OidcSettingsSection(BaseModel):
    issuer: str = Field(description="OIDC issuer URL.")
    client_id: str = Field(description="OIDC client id (not secret).")
    redirect_uri: str = Field(description="OIDC redirect URI.")
    enabled: bool = Field(description="Whether the DB-stored OIDC configuration is active.")
    client_secret_set: bool = Field(
        description="Whether a client secret was configured. The secret itself is never exported -- if true it "
        "must be re-entered after an import."
    )


class NotificationSettingsEntry(BaseModel):
    report_notification_email: str | None = Field(default=None, description="Report recipient.")
    report_notifications_enabled: bool = Field(default=False, description="Whether report mails are sent.")
    alarm_notification_email: str | None = Field(default=None, description="Alarm recipients, comma-separated.")
    alarm_notifications_enabled: bool = Field(default=False, description="Whether alarm mails are sent.")
    billing_notification_email: str | None = Field(default=None, description="Billing recipient.")
    billing_notifications_enabled: bool = Field(default=False, description="Whether billing mails are sent.")
    alarm_min_severity: Literal["warning", "critical"] = Field(
        default="warning", description="Alert mailer: lowest alarm severity that is mailed."
    )
    alarm_notify_resolved: bool = Field(default=True, description="Alert mailer: also mail when an alarm clears.")
    alarm_reminder_hours: int | None = Field(
        default=None, description="Alert mailer: re-send open alarms every N hours; null = no reminders."
    )


class FileIndexSettingsEntry(BaseModel):
    enabled: bool = Field(description="Whether file indexing is active for this customer.")
    retention_days: int = Field(ge=1, description="How long index entries are kept, in days.")
    schedule_enabled: bool = Field(default=False, description="Whether indexing runs on a schedule.")
    schedule_interval_hours: int | None = Field(default=None, description="Hours between scheduled index runs.")
    window_start_time: time | None = Field(default=None, description="Daily allowed window start (UTC).")
    window_end_time: time | None = Field(default=None, description="Daily allowed window end (UTC).")
    guest_exec_pve_cluster: ConnectionRef | None = Field(
        default=None, description="PVE cluster for guest_exec_ephemeral indexing clones."
    )
    guest_exec_target_node: str | None = Field(default=None, description="Node for those clones.")
    guest_exec_target_storage: str | None = Field(default=None, description="Storage for those clones.")
    full_reindex_every_n_runs: int | None = Field(default=None, description="Force a full re-index every N runs.")


class LegApplicabilityEntry(BaseModel):
    leg: Literal["copies", "media_types", "offsite", "immutable", "zero_errors"] = Field(
        description="The 3-2-1-1-0 leg."
    )
    applicable: bool = Field(description="False = this leg is out of contractual scope for the customer.")


class CustomerEntry(BaseModel):
    name: str = Field(min_length=1, max_length=200, description="Display name.")
    slug: str = Field(
        pattern=r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$",
        max_length=100,
        description="Natural key: the customer's slug (matched on import).",
    )
    debitor_number: str = Field(min_length=1, max_length=50, description="Debitor number (unique).")
    match_code: str = Field(pattern=r"^[A-Z]{3}$", description="Three-letter match code, uppercase (unique).")
    disabled: bool = Field(default=False, description="Whether the customer is disabled.")
    report_retention_days: int | None = Field(default=None, description="Customer-specific report retention.")
    notification_settings: NotificationSettingsEntry | None = Field(
        default=None, description="Notification recipients/toggles, or null if never configured."
    )
    file_index_settings: FileIndexSettingsEntry | None = Field(
        default=None, description="File-index opt-in and settings, or null if never opted in."
    )
    leg_applicability: list[LegApplicabilityEntry] = Field(
        default_factory=list, description="Stored 3-2-1-1-0 leg scoping (legs not listed are applicable)."
    )


class DatastoreEntry(BaseModel):
    name: str = Field(description="Datastore name on the PBS instance (natural key together with the instance).")
    path: str | None = Field(default=None, description="Datastore path as last reported by PBS.")
    comment: str | None = Field(default=None, description="Datastore comment as last reported by PBS.")
    encryption_key_set: bool = Field(
        default=False,
        description="Whether this datastore held client-side encrypted backups (or, before the key store, had a "
        "key stored). Keys are never part of a configuration backup -- bring them over with an encryption key "
        "export (Administration -> Encryption keys).",
    )


class PbsInstanceEntry(BaseModel):
    display_name: str = Field(description="Display name.")
    location: str | None = Field(default=None, description="Free-text location.")
    host: str = Field(description="Natural key (with port): host name or IP.")
    port: int = Field(description="Natural key (with host): API port.")
    tls_trust_mode: TlsTrustMode = Field(description="'ca' or 'fingerprint'.")
    tls_fingerprint_sha256: str | None = Field(default=None, description="Pinned certificate fingerprint.")
    token_id: str | None = Field(
        default=None,
        description="API token ID (user@realm!tokenname) -- not secret. The token secret is never exported; an "
        "imported instance is marked 'secret not set' until it is entered.",
    )
    disabled: bool = Field(default=False, description="Whether the instance is disabled.")
    datastores: list[DatastoreEntry] = Field(
        default_factory=list, description="Datastores known on this instance (removed ones are left out)."
    )


class BackupJobCustomerAssignmentEntry(BaseModel):
    job_id: str = Field(description="PVE vzdump job id.")
    customer_slug: str = Field(description="Customer the job is explicitly scoped to.")


class PveClusterEntry(BaseModel):
    display_name: str = Field(description="Display name.")
    host: str = Field(description="Natural key (with port): host name or IP.")
    port: int = Field(description="Natural key (with host): API port.")
    tls_trust_mode: TlsTrustMode = Field(description="'ca' or 'fingerprint'.")
    tls_fingerprint_sha256: str | None = Field(default=None, description="Pinned certificate fingerprint.")
    token_id: str | None = Field(
        default=None,
        description="API token ID (user@realm!tokenname) -- not secret. The token secret is never exported.",
    )
    disabled: bool = Field(default=False, description="Whether the cluster is disabled.")
    allowed_target_storages: list | None = Field(default=None, description="Allowed restore target storages.")
    restore_network_rules: dict | None = Field(default=None, description="Restore network rules.")
    require_restore_approval: bool = Field(default=False, description="Four-eyes approval for restores.")
    is_lab_compute: bool = Field(default=False, description="Usable as Recovery Lab compute.")
    lab_sdn_zone_name: str | None = Field(default=None, description="Admin-configured Lab SDN zone.")
    lab_appliance_template_vmid: int | None = Field(default=None, description="Lab probe appliance template VMID.")
    lab_snippet_storage_id: str | None = Field(default=None, description="Storage for Lab cloud-init snippets.")
    lab_appliance_uplink_bridge: str | None = Field(default=None, description="Uplink bridge of the probe appliance.")
    lab_appliance_uplink_vlan_tag: int | None = Field(default=None, description="Uplink VLAN tag (1-4094) or null.")
    lab_appliance_uplink_ip_mode: str = Field(default="dhcp", description="Uplink IPv4 mode: dhcp or static_range.")
    lab_appliance_uplink_ip_range_start: str | None = Field(default=None, description="Static uplink range start.")
    lab_appliance_uplink_ip_range_end: str | None = Field(default=None, description="Static uplink range end.")
    lab_appliance_uplink_ip_prefix_length: int | None = Field(default=None, description="Static uplink prefix length.")
    lab_appliance_uplink_gateway: str | None = Field(default=None, description="Static uplink gateway.")
    lab_appliance_uplink_dns_servers: list[str] | None = Field(default=None, description="Static uplink DNS servers.")
    lab_agent_token_set: bool = Field(
        default=False,
        description="Whether a lab-agent token had been generated. Only its hash is stored, so it can't be "
        "exported -- generate a new one after an import and reconfigure resurgam-lab-agent.",
    )
    linked_pbs_instances: list[ConnectionRef] = Field(
        default_factory=list, description="PBS instances this cluster restores from."
    )
    linked_datastores: list[DatastoreRef] = Field(
        default_factory=list, description="Datastores whose backups come from this cluster (name resolution)."
    )
    backup_job_customer_assignments: list[BackupJobCustomerAssignmentEntry] = Field(
        default_factory=list, description="Explicit customer scope overrides for vzdump jobs."
    )


class DatastoreAssignmentEntry(DatastoreRef):
    customer_slug: str = Field(description="Customer the datastore is assigned to.")
    role: Literal["primary", "offsite"] = Field(default="primary", description="Billing role of the datastore.")


class ProtectionPolicyEntry(BaseModel):
    customer_slug: str = Field(description="Owning customer (natural key with name).")
    name: str = Field(description="Policy name (natural key with customer).")
    expected_interval_hours: int = Field(gt=0, description="Expected backup interval.")
    max_time_since_last_backup_hours: int = Field(gt=0, description="Max hours since the last backup.")
    expected_verify_deadline_hours: int | None = Field(default=None, description="Verify deadline.")
    min_snapshot_count: int = Field(default=1, ge=0, description="Minimum snapshot count.")
    sync_dr_required: bool = Field(default=False, description="Whether Sync/DR is required.")


class PricingPlanEntry(BaseModel):
    customer_slug: str | None = Field(
        description="Customer of a per-customer plan, or null for the global default plan (natural key)."
    )
    name: str = Field(description="Plan name.")
    price_per_vm_month: Decimal = Field(ge=0, description="Per-VM monthly price.")
    price_per_ct_month: Decimal = Field(default=Decimal(0), ge=0, description="Per-container monthly price.")
    price_per_host_month: Decimal = Field(default=Decimal(0), ge=0, description="Per-host monthly price.")
    price_per_gb_month: Decimal = Field(ge=0, description="Per-GB monthly price.")
    currency: str = Field(default="CHF", min_length=3, max_length=3, description="ISO currency code.")


class RoleAssignmentEntry(BaseModel):
    role: RoleKey = Field(description="Role key.")
    scope_type: ScopeType = Field(description="Scope type of the assignment.")
    scope: RoleScopeRef | None = Field(default=None, description="The scoped object (null for 'global').")
    source: Literal["manual", "oidc"] = Field(default="manual", description="Where the assignment came from.")


class UserEntry(BaseModel):
    email: str = Field(description="Natural key: the user's email.")
    display_name: str = Field(description="Display name.")
    external_subject: str | None = Field(default=None, description="OIDC subject (lets SSO adopt the account).")
    is_breakglass: bool = Field(default=False, description="Whether this is the break-glass user row.")
    disabled: bool = Field(default=False, description="Whether the account is disabled.")
    has_local_password: bool = Field(
        default=False,
        description="Whether the user had a local password. Password hashes are never exported -- after an import "
        "such users sign in via SSO or an admin sets a new password (POST /users/{id}/set-password).",
    )
    mfa_required: bool = Field(
        default=False,
        description="Whether an administrator required MFA for the account. Second factors themselves are never "
        "exported -- after an import such a user sets one up at the next login. Absent in older exports, which "
        "then leave an existing user's setting unchanged.",
    )
    role_assignments: list[RoleAssignmentEntry] = Field(default_factory=list, description="Role assignments.")


class BackupGroupSettingsEntry(BackupGroupRef):
    protection_policy: PolicyRef | None = Field(default=None, description="Assigned protection policy.")
    customer_slug: str | None = Field(default=None, description="Explicit customer override for the group.")
    file_index_enabled: bool = Field(default=False, description="Whether file indexing is on for this group.")
    file_index_exclude_patterns: list[str] | None = Field(
        default=None, description="Exclude patterns (null = keep the defaults)."
    )
    file_index_method: str = Field(default="external_mount", description="File index method.")
    guest_exec_live_pve_cluster: ConnectionRef | None = Field(
        default=None, description="PVE cluster for guest_exec_live indexing."
    )


class ApplicationGroupMemberEntry(BackupGroupRef):
    startup_order: int = Field(default=0, description="Boot order in a Lab run.")
    check_types: list[str] = Field(default_factory=list, description="Lab checks for this member.")
    expected_ip: str | None = Field(default=None, description="Known static IP, or null.")


class ApplicationGroupEntry(BaseModel):
    customer_slug: str = Field(description="Owning customer (natural key with name).")
    name: str = Field(description="Group name (natural key with customer).")
    lab_network_mode: str = Field(default="dhcp", description="'dhcp' or 'static'.")
    lab_subnet_cidr: str | None = Field(default=None, description="Lab subnet.")
    lab_gateway_ip: str | None = Field(default=None, description="Lab gateway IP.")
    lab_dns_ip: str | None = Field(default=None, description="Lab DNS IP.")
    lab_dhcp_range_start: str | None = Field(default=None, description="Lab DHCP range start.")
    lab_dhcp_range_end: str | None = Field(default=None, description="Lab DHCP range end.")
    lab_schedule_enabled: bool = Field(default=False, description="Recurring Lab runs on.")
    lab_schedule_interval_hours: int | None = Field(default=None, description="Hours between Lab runs.")
    lab_schedule_pve_cluster: ConnectionRef | None = Field(default=None, description="Lab compute cluster.")
    lab_schedule_target_node: str | None = Field(default=None, description="Lab target node.")
    lab_schedule_target_storage: str | None = Field(default=None, description="Lab target storage.")
    members: list[ApplicationGroupMemberEntry] = Field(default_factory=list, description="Group members.")


class ConfigBackupSections(BaseModel):
    instance_settings: InstanceSettingsSection | None = Field(
        default=None, description="Platform settings (report retention floor, collector interval); null = defaults."
    )
    branding: BrandingSection | None = Field(
        default=None, description="Colors, app title and (small) logo; null = defaults."
    )
    update_source: UpdateSourceSection | None = Field(
        default=None,
        description="Release update source (repository URL, provider, check flags) without its access token; null "
        "or absent = not configured in the export, the importing instance's update source stays unchanged.",
    )
    mail_settings: MailSettingsSection | None = Field(
        default=None, description="SMTP relay settings without the password; null = not configured."
    )
    oidc_settings: OidcSettingsSection | None = Field(
        default=None, description="OIDC (SSO) settings without the client secret; null = not configured."
    )
    customers: list[CustomerEntry] = Field(
        default_factory=list,
        description="Customers with notification, file-index and 3-2-1-1-0 scoping settings. Not included: "
        "reports, usage, billing history, compliance attestations (append-only evidence).",
    )
    pbs_instances: list[PbsInstanceEntry] = Field(
        default_factory=list,
        description="PBS connections (no secrets, no health/version state) with their datastores.",
    )
    pve_clusters: list[PveClusterEntry] = Field(
        default_factory=list,
        description="PVE connections (no secrets, no lab-agent token) with links and Lab settings.",
    )
    datastore_assignments: list[DatastoreAssignmentEntry] = Field(
        default_factory=list, description="Which customer each datastore belongs to."
    )
    protection_policies: list[ProtectionPolicyEntry] = Field(default_factory=list, description="Protection policies.")
    pricing_plans: list[PricingPlanEntry] = Field(default_factory=list, description="Pricing plans.")
    users: list[UserEntry] = Field(
        default_factory=list,
        description="Users and role assignments. Not included: password hashes, TOTP/WebAuthn credentials, "
        "recovery codes, sessions.",
    )
    backup_group_settings: list[BackupGroupSettingsEntry] = Field(
        default_factory=list,
        description="Per-backup-group settings an operator made (policy, customer override, file indexing) -- "
        "only groups with non-default settings. The groups themselves come from the Collector: on import, "
        "entries whose group hasn't been synced yet are skipped (re-import after the first sync).",
    )
    application_groups: list[ApplicationGroupEntry] = Field(
        default_factory=list,
        description="Recovery Lab application groups. Members reference backup groups by identity; members not "
        "synced yet are skipped (re-import after the first sync). Lab runs/history are not included.",
    )


class ConfigBackupDocument(BaseModel):
    format: Literal[1] = Field(description="Document format version. This RESURGAM version reads format 1.")
    exported_at: datetime = Field(description="When the document was exported (UTC).")
    resurgam_version: str = Field(description="RESURGAM version that produced the document.")
    instance_name: str | None = Field(default=None, description="Host name of the exporting RESURGAM instance.")
    sections: ConfigBackupSections = Field(description="The configuration, per section.")


# --- import result ------------------------------------------------------------

ImportAction = Literal["create", "update", "skip", "conflict"]


class ConfigImportItem(BaseModel):
    key: str = Field(description="Human-readable natural key of the object (e.g. 'acme', 'pbs1.example:8007').")
    action: ImportAction = Field(
        description="create = new object, update = existing object changed, skip = already identical or not "
        "applicable yet, conflict = cannot be applied (see reason)."
    )
    reason: str | None = Field(default=None, description="Why it was skipped / is a conflict, or what changed.")


class ConfigImportSectionResult(BaseModel):
    section: str = Field(description="Section name, as in the document's `sections`.")
    create: int = Field(description="Number of objects created.")
    update: int = Field(description="Number of objects updated.")
    skip: int = Field(description="Number of objects skipped.")
    conflict: int = Field(description="Number of conflicts.")
    items: list[ConfigImportItem] = Field(description="Per-object outcome.")


class ReEnterItem(BaseModel):
    kind: Literal[
        "pbs_token_secret",
        "pve_token_secret",
        "datastore_encryption_key",
        "smtp_password",
        "oidc_client_secret",
        "lab_agent_token",
        "user_password",
        "branding_logo",
        "update_source_token",
    ] = Field(description="What has to be re-entered.")
    target: str = Field(description="Which object it belongs to (display name / natural key).")
    target_id: str | None = Field(
        default=None, description="Internal id of that object after the import (null in a dry run)."
    )
    page: str = Field(description="Frontend route where it is entered, e.g. /infrastructure/pbs-instances.")
    detail: str = Field(description="What exactly to do.")


class ConfigImportResult(BaseModel):
    dry_run: bool = Field(description="True: nothing was written; the result shows what an import would do.")
    applied: bool = Field(description="True when the changes were committed (not a dry run).")
    source_resurgam_version: str = Field(description="RESURGAM version that exported the document.")
    source_exported_at: datetime = Field(description="When the document was exported.")
    source_instance_name: str | None = Field(description="Host name of the exporting instance.")
    sections: list[ConfigImportSectionResult] = Field(description="Outcome per section, in import order.")
    to_reenter: list[ReEnterItem] = Field(
        description="Secrets and credentials a configuration backup never contains and that must be entered "
        "again (for a dry run: what would have to be entered)."
    )
    notes: list[str] = Field(description="General remarks (e.g. how imported users sign in).")
