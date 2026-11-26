export type TlsTrustMode = "ca" | "fingerprint";
export type ConnectionHealthStatus =
  | "ok"
  | "degraded"
  | "unreachable"
  | "unknown"
  | "tls_mismatch"
  | "auth_failed"
  | "missing_privileges";

/** Credentials mode: RESURGAM logs in once, creates its own API token and discards the password. */
export interface TokenLoginCredentials {
  username: string;
  password: string;
  token_name?: string;
}

/** Token input shared by PBS-instance and PVE-cluster create/update requests. */
export interface TokenInputFields {
  /** user@realm!tokenname */
  token_id?: string;
  token_secret?: string;
  credentials?: TokenLoginCredentials;
  /** @deprecated combined user@realm!tokenname:secret (PBS) / =secret (PVE) */
  api_token?: string;
}

export interface RequiredPrivilegeOut {
  name: string;
  path: string;
  reason: string;
}

export interface RequiredPrivilegesOut {
  privileges: RequiredPrivilegeOut[];
  roles: string[];
  manual_commands: string[];
  default_token_name: string;
}
export type VerifyStatus = "ok" | "failed" | "none" | "unknown";
export type TaskStatus = "ok" | "error" | "running" | "unknown";
export type ProtectionStatus = "ok" | "warning" | "critical" | "unknown";
export type ScopeType = "global" | "customer" | "pbs_instance" | "datastore" | "pve_cluster";

export interface RoleAssignmentOut {
  role: string;
  scope_type: ScopeType;
  scope_id: string | null;
}

export interface WhoAmIResponse {
  user_id: string;
  email: string;
  display_name: string;
  is_breakglass: boolean;
  assignments: RoleAssignmentOut[];
}

export interface BreakglassLoginResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
}

export interface PbsInstanceOut {
  id: string;
  display_name: string;
  location: string | null;
  host: string;
  port: number;
  tls_trust_mode: TlsTrustMode;
  tls_fingerprint_sha256: string | null;
  /** ID of the stored token (user@realm!tokenname); never the secret. */
  token_id?: string | null;
  /** False when no API token secret is stored (e.g. imported from a configuration backup). */
  token_secret_set?: boolean;
  version: string | null;
  last_health_check_at: string | null;
  last_health_status: ConnectionHealthStatus;
  last_collector_sync_at: string | null;
  /** PBS server's own timezone (IANA name); PBS prune counts days in it. Null until the first sync. */
  server_timezone?: string | null;
  disabled: boolean;
  /** Datastores VMs run straight from right now (Instant Recovery), one entry per VM. */
  instant_recoveries_reading?: string[];
}

export interface PbsInstanceCreateRequest extends TokenInputFields {
  display_name: string;
  location?: string | null;
  host: string;
  port: number;
  tls_trust_mode: TlsTrustMode;
  tls_fingerprint_sha256?: string | null;
}

export interface PbsInstanceUpdateRequest extends TokenInputFields {
  display_name?: string;
  location?: string | null;
  host?: string;
  port?: number;
  tls_trust_mode?: TlsTrustMode;
  tls_fingerprint_sha256?: string | null;
}

export interface TlsProbeResponse {
  fingerprint_sha256: string;
}

export interface TestConnectionResponse {
  reachable: boolean;
  status: ConnectionHealthStatus;
  version: string | null;
  error: string | null;
  missing_privileges: string[];
  permissions_checked: boolean;
  resolved_ip: string | null;
  checked_at: string | null;
}

export interface TaskSyncOut {
  reachable: boolean;
  tasks_created: number;
  tasks_updated: number;
  error: string | null;
}

export interface DatastoreSyncOut {
  datastore_id: string;
  datastore_name: string;
  reachable: boolean;
  groups_created: number;
  groups_updated: number;
  snapshots_created: number;
  snapshots_updated: number;
  error: string | null;
}

export interface SyncJobSyncOut {
  reachable: boolean;
  sync_jobs_created: number;
  sync_jobs_updated: number;
  sync_jobs_removed?: number;
  error: string | null;
}

export interface DatastoreDiscoveryOut {
  reachable: boolean;
  created: string[];
  updated: string[];
  removed: string[];
  restored: string[];
  error: string | null;
}

export interface PbsInstanceSyncOut {
  tasks: TaskSyncOut;
  sync_jobs: SyncJobSyncOut;
  datastores: DatastoreSyncOut[];
  discovery?: DatastoreDiscoveryOut | null;
}

export interface SyncJobOut {
  id: string;
  pbs_instance_id: string;
  job_id: string;
  remote_name: string;
  remote_store: string;
  local_store_name: string;
  local_datastore_id: string | null;
  schedule: string | null;
  remove_vanished: boolean | null;
  comment: string | null;
  last_synced_at: string;
}

export interface RestoreNetworkRules {
  production_bridges?: string[];
  safe_bridges?: string[];
  default_bridge?: string;
}

export interface PveClusterOut {
  id: string;
  display_name: string;
  host: string;
  port: number;
  tls_trust_mode: TlsTrustMode;
  tls_fingerprint_sha256: string | null;
  allowed_target_storages: string[] | null;
  restore_network_rules: RestoreNetworkRules | null;
  require_restore_approval: boolean;
  /** Restore plans on this cluster may use Instant Recovery (ADR 0030). */
  instant_recovery_enabled?: boolean;
  /** Hours a VM may run from its backup before an alarm. */
  instant_recovery_alarm_hours?: number;
  /** ID of the stored token (user@realm!tokenname); never the secret. */
  token_id?: string | null;
  /** False when no API token secret is stored (e.g. imported from a configuration backup). */
  token_secret_set?: boolean;
  version: string | null;
  last_health_check_at: string | null;
  last_health_status: ConnectionHealthStatus;
  disabled: boolean;
  linked_pbs_instance_ids: string[];
  /** Datastores linked to this cluster for workload-name resolution provenance only (not restore capability). */
  linked_datastore_ids: string[];
  is_lab_compute: boolean;
  lab_sdn_zone_name: string | null;
  lab_appliance_template_vmid: number | null;
  /** A PVE storage (content type 'snippets') resurgam-lab-agent writes each run's probe appliance cloud-init config to. */
  lab_snippet_storage_id: string | null;
  /** An existing, already-connected PVE bridge (e.g. 'vmbr0') for the probe appliance's own uplink NIC. */
  lab_appliance_uplink_bridge: string | null;
  /** When resurgam-lab-agent last checked in with this cluster's token; null = never. */
  lab_agent_last_seen_at?: string | null;
  /** Node the lab agent was paired on (ADR 0027). */
  lab_agent_node?: string | null;
  /** Probe appliance uplink: optional VLAN tag (1-4094) on its uplink NIC; null = untagged. */
  lab_appliance_uplink_vlan_tag?: number | null;
  /** How the uplink gets its IPv4 address: DHCP, or one address per active lab run from a static range. */
  lab_appliance_uplink_ip_mode?: LabUplinkIpMode;
  lab_appliance_uplink_ip_range_start?: string | null;
  lab_appliance_uplink_ip_range_end?: string | null;
  lab_appliance_uplink_ip_prefix_length?: number | null;
  lab_appliance_uplink_gateway?: string | null;
  lab_appliance_uplink_dns_servers?: string[];
}

export type LabUplinkIpMode = "dhcp" | "static_range";

/** The uplink fields shared by the PVE cluster create/update requests. */
export interface LabUplinkFields {
  lab_appliance_uplink_vlan_tag?: number | null;
  lab_appliance_uplink_ip_mode?: LabUplinkIpMode;
  lab_appliance_uplink_ip_range_start?: string | null;
  lab_appliance_uplink_ip_range_end?: string | null;
  lab_appliance_uplink_ip_prefix_length?: number | null;
  lab_appliance_uplink_gateway?: string | null;
  lab_appliance_uplink_dns_servers?: string[];
}

export interface PveNodeOut {
  node: string;
  status: string | null;
  cpu: number | null;
  maxmem: number | null;
  uptime: number | null;
}

export interface PveStorageOut {
  storage: string;
  type: string;
  content: string[];
  active: boolean | null;
  shared: boolean | null;
  avail_bytes: number | null;
  used_bytes: number | null;
  total_bytes: number | null;
  pbs_server: string | null;
  pbs_datastore: string | null;
  pbs_namespace: string | null;
}

export interface PveGuestOut {
  vmid: number;
  node: string;
  guest_type: string;
  name: string | null;
  status: string | null;
}

export interface PvePoolOut {
  poolid: string;
  comment: string | null;
}

export interface PveVmidCheckOut {
  vmid: number;
  free: boolean;
}

/** `prune_keep_*` mirrors `PbsPruneJobOut`'s own field names on purpose
 * -- the intended, primary home for retention is still a PBS Prune Job,
 * not this, but real vzdump jobs on this environment were found live
 * (2026-09-27) to already carry their own alongside separately-existing
 * PBS Prune Jobs. Cross-check against the target storage's real PBS
 * Prune Job(s) rather than treat this as the only mechanism. */
export interface VzdumpJobOut {
  id: string;
  enabled: boolean;
  schedule: string | null;
  storage: string | null;
  node: string | null;
  comment: string | null;
  all_guests: boolean;
  vmid: string | null;
  pool: string | null;
  exclude: string | null;
  mode: string | null;
  compress: string | null;
  notes_template: string | null;
  mailnotification: string | null;
  mailto: string | null;
  prune_keep_last: number | null;
  prune_keep_hourly: number | null;
  prune_keep_daily: number | null;
  prune_keep_weekly: number | null;
  prune_keep_monthly: number | null;
  prune_keep_yearly: number | null;
  /** An explicit override only -- a job's default, in-design scope is
   * inherited from its target datastore/namespace, not stored here.
   * `null` means no override is set, not "no customer." */
  customer_id: string | null;
  /** When RESURGAM enables this disabled job again automatically ("disable
   * for N hours", ADR 0018 addendum). `null`/absent: no timer. */
  reenable_at?: string | null;
  /** Last failed automatic re-enable attempt; retried every minute. */
  reenable_last_error?: string | null;
}

/** The common, practically useful field subset for authoring a vzdump job
 * (docs/adr/0023). */
export interface VzdumpJobCreateRequest {
  schedule?: string | null;
  storage?: string | null;
  node?: string | null;
  comment?: string | null;
  all_guests?: boolean;
  vmid?: string | null;
  pool?: string | null;
  exclude?: string | null;
  mode?: string | null;
  compress?: string | null;
  notes_template?: string | null;
  mailnotification?: string | null;
  mailto?: string | null;
  enabled?: boolean;
  prune_keep_last?: number | null;
  prune_keep_hourly?: number | null;
  prune_keep_daily?: number | null;
  prune_keep_weekly?: number | null;
  prune_keep_monthly?: number | null;
  prune_keep_yearly?: number | null;
}

/** `prune_keep_*` is a single property-string field on PVE's wire, not
 * independently-settable sub-keys -- the backend always sends the
 * complete encoded set together whenever any one is touched, confirmed
 * live 2026-09-27 (a partial send would silently drop the others).
 * `clear_prune_backups` removes the field entirely and is mutually
 * exclusive with setting any `prune_keep_*` field in the same request. */
export interface VzdumpJobUpdateRequest extends VzdumpJobCreateRequest {
  clear_prune_backups?: boolean;
}

/** Whether the calling token can actually read/write a given job
 * category, computed live from that system's own `GET
 * /access/permissions` -- never assumed (docs/adr/0023). Shared shape
 * for both the PVE and PBS sides. */
export interface JobCapabilitiesOut {
  can_read: boolean;
  can_write: boolean;
  /** PBS-side only: privileges (on `/`) the token lacks for writing this
   * job category, e.g. ["Datastore.Modify"]. Empty/absent when writable. */
  missing_write_privileges?: string[];
}

export type RestorePlanVmidIntent = "new" | "existing";
/** `full`: copy, then the guest exists. `instant`: run the VM from the backup (ADR 0030). */
export type RestorePlanMethod = "full" | "instant";
export type RestorePlanNetworkMode = "none" | "isolated" | "original" | "custom";

export interface RestorePlanCreateRequest {
  backup_snapshot_id: string;
  pve_cluster_id: string;
  target_node: string;
  target_storage: string;
  vmid_intent: RestorePlanVmidIntent;
  target_vmid: number;
  temp_name?: string | null;
  network_mode: RestorePlanNetworkMode;
  network_bridge?: string | null;
  start_after_restore?: boolean;
  network_danger_acknowledged?: boolean;
  method?: RestorePlanMethod;
}

export interface RestorePlanCheckEntry {
  check: string;
  status: "pass" | "warn" | "fail" | "unknown";
  message: string;
}

export interface RestorePlanOut {
  id: string;
  backup_snapshot_id: string;
  backup_group_id: string;
  customer_id: string | null;
  pve_cluster_id: string;
  target_node: string;
  target_storage: string;
  vmid_intent: RestorePlanVmidIntent;
  target_vmid: number;
  temp_name: string;
  network_mode: RestorePlanNetworkMode;
  network_bridge: string | null;
  start_after_restore: boolean;
  network_danger_acknowledged: boolean;
  checks_result: RestorePlanCheckEntry[];
  checks_performed_at: string;
  generated_command: string;
  created_by_user_id: string | null;
  created_at: string;

  // EPIC 9 (manifest §11.2): execution + four-eyes approval state, all
  // null until acted on.
  execution_status: RestorePlanExecutionStatus | null;
  upid: string | null;
  executed_at: string | null;
  execution_started_by_user_id: string | null;
  execution_exit_status: string | null;
  execution_completed_at: string | null;
  execution_error: string | null;
  execution_last_confirmed_at: string | null;
  approval_status: RestorePlanApprovalStatus | null;
  approved_by_user_id: string | null;
  approved_at: string | null;
  approval_notes: string | null;
  method?: RestorePlanMethod;
  /** Only for `method === "instant"`. */
  instant_recovery?: InstantRecoveryState | null;
}

export interface InstantRecoveryOriginalVm {
  /** `running`, `stopped`, `absent` or `unknown`, at plan creation / execute. */
  status: "running" | "stopped" | "absent" | "unknown";
  cluster_name: string | null;
  node: string | null;
  unreachable_clusters: string[];
}

export interface InstantRecoveryMoveDisk {
  slot: string;
  status: "pending" | "running" | "done" | "failed";
  upid: string | null;
  error: string | null;
}

export interface InstantRecoveryState {
  original_vm: InstantRecoveryOriginalVm | null;
  keep_mac: boolean;
  disks: string[];
  imported_volumes: string[];
  skipped_devices: string[];
  running_since: string | null;
  last_heartbeat_at: string | null;
  overlay_bytes: number | null;
  overlay_storage_total_bytes: number | null;
  overlay_storage_free_bytes: number | null;
  move_disks: InstantRecoveryMoveDisk[];
  move_bwlimit_kib: number | null;
  last_error: string | null;
  ended_at: string | null;
}

export type RestorePlanExecutionStatus =
  | "pending_approval"
  | "approved"
  | "rejected"
  | "restore_requested"
  | "restore_running"
  | "restore_failed"
  | "finalizing"
  | "completed"
  | "finalize_failed"
  // Instant Recovery (ADR 0030)
  | "queued"
  | "mapping"
  | "starting"
  | "running_from_backup"
  | "moving_disks"
  | "finishing"
  | "discarding"
  | "discarded"
  | "failed";

export type RestorePlanApprovalStatus = "pending" | "approved" | "rejected";

export interface PveClusterCreateRequest extends TokenInputFields, LabUplinkFields {
  display_name: string;
  host: string;
  port: number;
  tls_trust_mode: TlsTrustMode;
  tls_fingerprint_sha256?: string | null;
  linked_pbs_instance_ids?: string[];
  linked_datastore_ids?: string[];
  is_lab_compute?: boolean;
  lab_sdn_zone_name?: string | null;
  lab_appliance_template_vmid?: number | null;
  lab_snippet_storage_id?: string | null;
  lab_appliance_uplink_bridge?: string | null;
}

export interface PveClusterUpdateRequest extends TokenInputFields, LabUplinkFields {
  display_name?: string;
  host?: string;
  port?: number;
  tls_trust_mode?: TlsTrustMode;
  tls_fingerprint_sha256?: string | null;
  linked_pbs_instance_ids?: string[];
  linked_datastore_ids?: string[];
  is_lab_compute?: boolean;
  lab_sdn_zone_name?: string | null;
  lab_appliance_template_vmid?: number | null;
  lab_snippet_storage_id?: string | null;
  lab_appliance_uplink_bridge?: string | null;
  instant_recovery_enabled?: boolean;
  instant_recovery_alarm_hours?: number;
}

export interface LabAgentTokenOut {
  token: string;
}

// --- Recovery Lab compute setup wizard (GET /pve-clusters/{id}/lab-readiness etc.)
export type LabReadinessStatus = "ok" | "missing" | "warning" | "unknown";
export type LabReadinessKey =
  | "privileges"
  | "sdn_zone"
  | "uplink_bridge"
  | "uplink_network"
  | "snippet_storage"
  | "overlay_storage"
  | "appliance_template"
  | "lab_agent"
  | "backend_url";
export type LabAutoFix = "create_overlay_storage" | "add_snippets_content" | "add_images_content";

export interface LabReadinessItemOut {
  key: LabReadinessKey;
  status: LabReadinessStatus;
  /** Machine-readable reason within the item; the UI translates key + code. */
  code: string;
  message: string;
  fix_hint: string | null;
  auto_fix: LabAutoFix | null;
  details: Record<string, unknown>;
}

export interface LabReadinessOut {
  cluster_id: string;
  ready: boolean;
  lab_nodes: string[];
  lab_nodes_source: "query" | "overlay_storage" | "all_online_nodes";
  overlay_storage_id: string;
  checked_at: string;
  items: LabReadinessItemOut[];
}

export interface PveSdnZoneOut {
  zone: string;
  type: string | null;
  nodes: string[] | null;
  pending: boolean;
  usable_for_lab: boolean;
}

export interface PveBridgeOut {
  iface: string;
  type: string;
  active: boolean | null;
  vlan_aware: boolean | null;
  comments: string | null;
  /** Allowed VLANs of a VLAN-aware Linux bridge (bridge_vids), e.g. "2-4094". */
  vlan_ids?: string | null;
}

export interface PveStorageConfigOut {
  storage: string;
  type: string;
  content: string[];
  nodes: string[] | null;
  path: string | null;
  shared: boolean;
  disabled: boolean;
  snippets_capable: boolean;
}

export interface PveTemplateOut {
  vmid: number;
  name: string | null;
  node: string;
  guest_type: string;
}

export interface LabSetupFixOut {
  action: string;
  storage_id: string;
  content: string[];
  item: LabReadinessItemOut;
}

export interface LabAgentInstallOut {
  package_name: string;
  package_version: string;
  package_source: string;
  /** API path (below /api/v1) of the shipped .deb; null when this installation ships none. */
  package_download_path?: string | null;
  package_sha256?: string | null;
  package_filename?: string;
  /** Empty when the package is shipped with RESURGAM. */
  build_steps: string[];
  install_steps: string[];
  env_file_path: string;
  env_file: string;
  token_placeholder: string;
  backend_url: string | null;
  overlay_storage_id: string;
  overlay_dir: string;
}

// --- Guided lab provisioning (one-time SSH: agent install, template build)
export interface LabArtifactOut {
  name: string;
  version: string;
  architecture: string;
  filename: string;
  sha256: string;
  size: number;
  download_path: string;
  sha256_path: string;
}

export interface LabArtifactsOut {
  release_version: string;
  /** Debian codename the packages were built on; the lab node must match. */
  debian_release: string;
  artifacts: LabArtifactOut[];
}

export interface SshTargetOut {
  node: string;
  host: string;
  host_source: "cluster_status" | "cluster_host";
  port: number;
  username: string;
}

export interface SshHostKeyOut {
  host: string;
  port: number;
  key_type: string;
  fingerprint_sha256: string;
  verify_command: string;
}

/** One-time SSH login. Never stored by the frontend beyond the open form. */
export interface SshLoginRequest {
  node: string;
  host: string;
  port: number;
  username: string;
  auth_method: "password" | "private_key";
  password?: string | null;
  private_key?: string | null;
  passphrase?: string | null;
  confirmed_fingerprint: string;
}

export interface LabAgentInstallJobRequest {
  ssh: SshLoginRequest;
  overlay_storage_id?: string;
  overlay_dir?: string;
  pbs_token?: string | null;
}

export type LabTemplateImageSource = "download" | "node_image";

export interface LabTemplateBuildJobRequest {
  ssh: SshLoginRequest;
  storage: string;
  vmid?: number | null;
  image_source: LabTemplateImageSource;
  node_image?: string | null;
}

export type LabSetupJobStepStatus = "running" | "ok" | "warning" | "failed" | "skipped";

export interface LabSetupJobStepOut {
  key: string;
  status: LabSetupJobStepStatus;
  message: string;
  output: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface LabSetupJobOut {
  id: string;
  cluster_id: string;
  kind: "agent_install" | "template_build" | "share_helper_template_build";
  node: string;
  status: "running" | "succeeded" | "failed";
  started_at: string;
  finished_at: string | null;
  steps: LabSetupJobStepOut[];
  result: Record<string, unknown>;
  error_code: string | null;
  error_message: string | null;
  /** `agent`: run by the paired lab agent (ADR 0027); `ssh`: over a one-time SSH login. */
  runner?: "ssh" | "agent";
}

/** Lab node pairing with a one-time code (ADR 0027). */
export interface LabNodePairingOut {
  id: string;
  node: string;
  /** Only in the create response. */
  code: string | null;
  /** The command to paste into the node's root shell; only in the create response. */
  command: string | null;
  script_sha256: string;
  /** Proxmox VE's own web shell for the node. */
  shell_url: string;
  expires_at: string;
  state: "waiting" | "paired" | "expired" | "revoked";
  consumed_at: string | null;
  pbs_token_provided: boolean | null;
  agent_last_seen_at: string | null;
}

export interface LabAgentSetupJobCreate {
  kind: "template_build" | "share_helper_template_build";
  storage: string;
  vmid?: number | null;
  image_source?: LabTemplateImageSource;
  node_image?: string | null;
}

export interface LabTemplateBuildScriptOut {
  node: string;
  vmid: number;
  storage: string;
  prober_filename: string;
  prober_download_path: string;
  script: string;
}

// Manifest §22.6's typed checks -- no arbitrary scripts, deliberately.
// A 3-tier escalating hierarchy, not a flat bag: guest_agent (Level 1,
// cheapest, no network needed) -> icmp (Level 2, needs IP info) ->
// everything else (Level 3, a real service-level test).
export type CheckType =
  | "guest_agent" | "icmp" | "arp" | "tcp_port" | "http" | "https" | "dns" | "tls_handshake" | "ssh_banner" | "nmap_scan";

export interface ApplicationGroupMemberIn {
  backup_group_id: string;
  startup_order: number;
  check_types: CheckType[];
  /** This member's known IP, if static -- skips guest-agent-based IP discovery. Null resolves dynamically once the member boots. */
  expected_ip?: string | null;
}

export interface ApplicationGroupMemberOut {
  id: string;
  backup_group_id: string;
  startup_order: number;
  check_types: string[];
  expected_ip: string | null;
  /** True if this member's backup source (VMID slot) was reused for a different guest since it was added -- still points at the frozen, closed-out generation. */
  backup_group_superseded: boolean;
}

/** Whether a group's probe appliance serves DHCP on its own Lab-VNet subnet ('dhcp') or every member has a fixed static IP ('static'). */
export type LabNetworkMode = "dhcp" | "static";

export interface ApplicationGroupCreateRequest {
  customer_id: string;
  name: string;
  members: ApplicationGroupMemberIn[];
  lab_network_mode?: LabNetworkMode;
  lab_subnet_cidr?: string | null;
  lab_gateway_ip?: string | null;
  lab_dns_ip?: string | null;
  lab_dhcp_range_start?: string | null;
  lab_dhcp_range_end?: string | null;
  lab_schedule_enabled?: boolean;
  lab_schedule_interval_hours?: number | null;
  lab_schedule_pve_cluster_id?: string | null;
  lab_schedule_target_node?: string | null;
  lab_schedule_target_storage?: string | null;
}

export interface ApplicationGroupUpdateRequest {
  name?: string;
  members?: ApplicationGroupMemberIn[];
  lab_network_mode?: LabNetworkMode;
  lab_subnet_cidr?: string | null;
  lab_gateway_ip?: string | null;
  lab_dns_ip?: string | null;
  lab_dhcp_range_start?: string | null;
  lab_dhcp_range_end?: string | null;
  lab_schedule_enabled?: boolean;
  lab_schedule_interval_hours?: number | null;
  lab_schedule_pve_cluster_id?: string | null;
  lab_schedule_target_node?: string | null;
  lab_schedule_target_storage?: string | null;
}

export interface ApplicationGroupOut {
  id: string;
  customer_id: string;
  name: string;
  created_at: string;
  members: ApplicationGroupMemberOut[];
  lab_network_mode: string;
  lab_subnet_cidr: string | null;
  lab_gateway_ip: string | null;
  lab_dns_ip: string | null;
  lab_dhcp_range_start: string | null;
  lab_dhcp_range_end: string | null;
  lab_schedule_enabled: boolean;
  lab_schedule_interval_hours: number | null;
  lab_next_scheduled_run_at: string | null;
  lab_schedule_pve_cluster_id: string | null;
  lab_schedule_target_node: string | null;
  lab_schedule_target_storage: string | null;
}

export interface RestoreTestPlanCreateRequest {
  application_group_id: string;
  pve_cluster_id: string;
  target_node: string;
  target_storage: string;
}

export interface ProbeResultEntry {
  check_type: string;
  status: "pass" | "fail";
  detail: string | null;
  checked_at: string;
}

export type RestoreTestPlanMemberStatus =
  | "pending"
  | "queued"
  | "mounting"
  | "restore_requested"
  | "restore_running"
  | "booting"
  | "probing"
  | "passed"
  | "failed";

export interface RestoreTestPlanMemberOut {
  id: string;
  application_group_member_id: string;
  backup_snapshot_id: string;
  target_vmid: number;
  target_node: string;
  target_storage: string;
  upid: string | null;
  member_status: string;
  probe_results: ProbeResultEntry[];
  /** This member's real IP once resolved (static expected_ip, or learned via the guest agent) -- null until Level 1 succeeds and IP discovery runs. */
  resolved_ip: string | null;
  started_at: string | null;
  completed_at: string | null;
  error: string | null;
}

export type RestoreTestPlanStatus = "planned" | "running" | "passed" | "partial" | "failed" | "cleaning_up" | "completed";

export interface RestoreTestPlanOut {
  id: string;
  created_at: string;
  application_group_id: string;
  customer_id: string | null;
  pve_cluster_id: string;
  vnet_name: string;
  appliance_vmid: number | null;
  status: string;
  created_by_user_id: string | null;
  requested_at: string | null;
  completed_at: string | null;
  error: string | null;
  members: RestoreTestPlanMemberOut[];
}

export interface RestoreTestPlanExecuteOut {
  plan: RestoreTestPlanOut;
  appliance_run_token: string;
}

export interface CustomerOut {
  id: string;
  name: string;
  slug: string;
  debitor_number: string;
  match_code: string;
  disabled: boolean;
  created_at: string;
  report_retention_days: number | null;
}

export interface CustomerUpdateRequest {
  name?: string;
  debitor_number?: string;
  match_code?: string;
  // `null` clears the override back to the platform floor; omitting the
  // key entirely leaves the current value unchanged -- these are
  // distinct on the wire (the backend checks model_fields_set), so
  // callers must be deliberate about including this key at all.
  report_retention_days?: number | null;
}

export interface CustomerImportRowError {
  row: number;
  message: string;
}

export interface CustomerImportResult {
  created: number;
  updated: number;
  errors: CustomerImportRowError[];
}

export interface CustomerCreateRequest {
  name: string;
  slug: string;
  debitor_number: string;
  match_code: string;
}

export interface RemoteDatastoreOut {
  name: string;
  path: string | null;
  comment: string | null;
}

export type DatastoreRole = "primary" | "offsite";

export type DatastoreMaintenanceMode = "read-only" | "offline" | "delete" | "unmount" | "s3-refresh";

export interface DatastoreMaintenanceModeOut {
  mode: DatastoreMaintenanceMode | null;
  message: string | null;
}

export interface SetDatastoreMaintenanceModeRequest {
  mode: DatastoreMaintenanceMode | null;
  message?: string | null;
  /** Needed while an Instant Recovery reads from the datastore (409 `instant_recovery_active` otherwise). */
  confirm_instant_recovery?: boolean;
}

export interface DatastoreGcScheduleOut {
  gc_schedule: string | null;
}

export interface SetDatastoreGcScheduleRequest {
  gc_schedule?: string | null;
}

/** A PBS Prune Job (`/config/prune`) -- the intended home for retention
 * policy in this project's design (docs/adr/0023), never a vzdump job
 * property. */
export interface PbsPruneJobOut {
  id: string;
  store: string;
  schedule: string | null;
  ns: string | null;
  keep_last: number | null;
  keep_hourly: number | null;
  keep_daily: number | null;
  keep_weekly: number | null;
  keep_monthly: number | null;
  keep_yearly: number | null;
  disable: boolean;
  comment: string | null;
}

export interface PbsPruneJobCreateRequest {
  schedule?: string | null;
  ns?: string | null;
  keep_last?: number | null;
  keep_hourly?: number | null;
  keep_daily?: number | null;
  keep_weekly?: number | null;
  keep_monthly?: number | null;
  keep_yearly?: number | null;
  disable?: boolean;
  comment?: string | null;
}

/** Partial update: an omitted field stays unchanged on PBS, a field sent
 * as `null` is removed (PBS `delete` parameter). `schedule` can't be
 * cleared -- PBS requires one for prune jobs. */
export interface PbsPruneJobUpdateRequest extends Omit<PbsPruneJobCreateRequest, "disable"> {
  disable?: boolean | null;
}

/** A PBS Verify Job (`/config/verify`). No `disable` field -- confirmed
 * live (2026-09-27) that PBS's real schema rejects it on both create and
 * update, unlike Prune jobs (docs/adr/0023). */
export interface PbsVerifyJobOut {
  id: string;
  store: string;
  schedule: string | null;
  ns: string | null;
  outdated_after: number | null;
  ignore_verified: boolean | null;
  comment: string | null;
}

export interface PbsVerifyJobCreateRequest {
  schedule?: string | null;
  ns?: string | null;
  outdated_after?: number | null;
  ignore_verified?: boolean | null;
  comment?: string | null;
}

/** Partial update: omitted = unchanged, `null` = removed on PBS. Clearing
 * `schedule` makes the verify job manual-only. */
export type PbsVerifyJobUpdateRequest = PbsVerifyJobCreateRequest;

/** A PBS "Remote" (another PBS server this instance can pull from/push
 * to for sync jobs). `password` is write-only -- never returned by a GET,
 * and RESURGAM never persists it either (docs/adr/0023). Instance-scoped,
 * not tied to one datastore. */
export interface PbsRemoteOut {
  name: string;
  host: string;
  auth_id: string | null;
  port: number | null;
  fingerprint: string | null;
  comment: string | null;
}

export interface PbsRemoteCreateRequest {
  name: string;
  host: string;
  auth_id: string;
  password: string;
  port?: number | null;
  fingerprint?: string | null;
  comment?: string | null;
}

export interface PbsRemoteUpdateRequest {
  host?: string | null;
  auth_id?: string | null;
  password?: string | null;
  port?: number | null;
  fingerprint?: string | null;
  comment?: string | null;
}

/** A PBS Sync Job (`/config/sync`, pull-based replication referencing an
 * existing Remote). No `disable` field -- confirmed live (2026-09-27)
 * that PBS's real schema rejects it on both create and update; a sync
 * job can only be removed entirely, not toggled off. */
export interface PbsSyncJobOut {
  job_id: string;
  local_store: string;
  remote_store: string;
  remote_name: string | null;
  ns: string | null;
  schedule: string | null;
  remove_vanished: boolean | null;
  owner: string | null;
  comment: string | null;
}

export interface PbsSyncJobCreateRequest {
  remote_store: string;
  remote_name?: string | null;
  ns?: string | null;
  schedule?: string | null;
  remove_vanished?: boolean | null;
  owner?: string | null;
  comment?: string | null;
}

export interface PbsSyncJobUpdateRequest {
  remote_store?: string | null;
  remote_name?: string | null;
  ns?: string | null;
  schedule?: string | null;
  remove_vanished?: boolean | null;
  owner?: string | null;
  comment?: string | null;
}

export interface DatastoreOut {
  id: string;
  pbs_instance_id: string;
  name: string;
  path: string | null;
  comment: string | null;
  created_at: string;
  customer_id: string | null;
  role: DatastoreRole | null;
  /** Whether a PBS encryption keyfile is stored for this datastore (the key itself is never returned). */
  encryption_key_configured?: boolean;
  /** Set by the Collector's automatic discovery when PBS no longer lists this datastore; null while present. */
  removed_from_pbs_at?: string | null;
  /** Encryption key fingerprints used by current backups in this datastore (ADR 0031). */
  key_fingerprints_in_use?: string[];
  /** Fingerprints from key_fingerprints_in_use that are not in the key store. */
  keys_missing?: string[];
  /** How many current backups in this datastore are client-side encrypted. */
  encrypted_snapshot_count?: number;
}

/** Client-side encryption of a backup as PBS reports it (ADR 0031); null = not known yet. */
export type CryptMode = "none" | "encrypt" | "sign-only" | "mixed";

export interface BackupGroupOut {
  id: string;
  datastore_id: string;
  namespace: string;
  backup_type: string;
  backup_id: string;
  resolved_name: string | null;
  owner: string | null;
  last_backup_at: string | null;
  last_verify_status: VerifyStatus;
  protection_policy_id: string | null;
  protection_status: ProtectionStatus;
  customer_id: string | null;
  effective_customer_id: string | null;
  file_index_enabled: boolean;
  file_index_exclude_patterns: string[];
  /** How this group is indexed: "external_mount" (default, PBS file-restore/catalog tools), "guest_exec_live" (hookscript indexes the live production VM at backup completion), or "guest_exec_ephemeral" (boots a disposable clone, indexes it, destroys it). VM-only in practice. */
  file_index_method: FileIndexMethod;
  /** Required only for "guest_exec_live": the PVE cluster hosting this group's production VM. */
  guest_exec_live_pve_cluster_id: string | null;
  last_synced_at: string;
  /** Set when this VMID slot's guest identity changed (a reused VMID) and this row was closed out in favor of a new generation. Null means current. */
  superseded_at: string | null;
  /** Set when the Collector's most recent sync no longer found this backup on PBS at all (deleted/pruned, not renamed). History is preserved; cleared automatically if PBS reports it again later. */
  removed_from_pbs_at: string | null;
  /** Encryption of the group's newest backup (ADR 0031). */
  latest_crypt_mode?: CryptMode | null;
  latest_key_fingerprint?: string | null;
  /** Every key fingerprint any backup of this group used, oldest first. */
  key_fingerprints?: string[];
  /** The newest backup uses a different key (or none) than the one before it. */
  key_changed?: boolean;
}

export type FileIndexMethod = "external_mount" | "guest_exec_live" | "guest_exec_ephemeral";

export interface ProtectionPolicyOut {
  id: string;
  customer_id: string;
  name: string;
  expected_interval_hours: number;
  max_time_since_last_backup_hours: number;
  expected_verify_deadline_hours: number | null;
  min_snapshot_count: number;
  sync_dr_required: boolean;
  created_at: string;
}

export interface ProtectionPolicyCreateRequest {
  customer_id: string;
  name: string;
  expected_interval_hours: number;
  max_time_since_last_backup_hours: number;
  expected_verify_deadline_hours?: number | null;
  min_snapshot_count: number;
  sync_dr_required?: boolean;
}

export interface UsageMeasurementOut {
  id: string;
  datastore_id: string;
  captured_at: string;
  used_bytes: number;
  available_bytes: number;
  total_bytes: number;
  logical_bytes: number;
  usage_percent: number;
  group_count_vm: number;
  group_count_ct: number;
  group_count_host: number;
  snapshot_count: number;
  oldest_snapshot_at: string | null;
  newest_snapshot_at: string | null;
  last_successful_gc_at: string | null;
}

export type CapacityForecastStatus = "growing" | "not_growing" | "insufficient_history";

export interface CapacityForecastOut {
  status: CapacityForecastStatus;
  basis_days: number;
  growth_bytes_per_day: number | null;
  days_until_full: number | null;
  full_at: string | null;
}

export interface DatastoreUsageOut {
  latest: UsageMeasurementOut | null;
  history: UsageMeasurementOut[];
  daily_change_bytes: number | null;
  monthly_change_bytes: number | null;
  forecast: CapacityForecastOut;
}

export interface BackupSnapshotOut {
  id: string;
  backup_group_id: string;
  snapshot_time: string;
  verify_status: VerifyStatus;
  size_bytes: number | null;
  crypt_mode?: CryptMode | null;
  /** Fingerprint of the key this backup was encrypted with; null if unencrypted. */
  key_fingerprint?: string | null;
  last_synced_at: string;
}

export type FileEntryType = "file" | "directory" | "symlink" | "other";

export interface FileEntryOut {
  path: string;
  name: string;
  type: FileEntryType;
  size: number | null;
  mtime: string | null;
}

export type CapabilityStatus = "ok" | "degraded" | "missing";

/** Where a check ran: natively on the appliance host (as the workers) or in the API's own process (dev). */
export type CapabilityLocation = "appliance_host" | "local";

export interface CapabilityCheckOut {
  name: string;
  status: CapabilityStatus;
  message: string;
  location: CapabilityLocation;
}

export interface BackupTypeCapabilityOut {
  backup_type: "vm" | "ct" | "host";
  available: boolean;
  location: CapabilityLocation;
  missing: string[];
  message: string;
}

export interface CapabilityReportOut {
  checks: CapabilityCheckOut[];
  backup_types: BackupTypeCapabilityOut[];
  location: CapabilityLocation;
  checked_at: string | null;
}

/** `archive` = several files and/or directories of one snapshot as ONE ZIP (folder download,
 * multi-selection download). */
export type FileRestoreJobType = "list" | "download" | "archive";
export type FileRestoreJobStatus = "pending" | "running" | "completed" | "failed" | "expired" | "cancelled";

export interface FileRestoreJobCreateRequest {
  job_type: FileRestoreJobType;
  /** `list`/`download`. */
  path?: string;
  /** `archive`: the selected snapshot paths. */
  paths?: string[];
}

export interface ArchiveSkippedEntry {
  path: string;
  reason: string;
}

export interface ArchiveRenamedEntry {
  path: string;
  archive_name: string;
}

/** What a completed `archive` job's ZIP contains (backend `ArchiveReportOut`). */
export interface ArchiveReportOut {
  file_count: number;
  directory_count: number;
  symlink_count: number;
  total_bytes: number;
  skipped_count: number;
  renamed_count: number;
  skipped: ArchiveSkippedEntry[];
  renamed: ArchiveRenamedEntry[];
}

export interface FileRestoreJobProgressOut {
  phase: "extracting" | "packing";
  entries_done: number;
  entries_total: number;
  bytes_done: number;
}

/** Polled (ADR 0013) via `refetchInterval` until `status` reaches a
 * terminal value. `result` is only populated for a completed `list` job;
 * a completed `download`/`archive` job's content is fetched separately via
 * `fileRestoreApi.saveJobContent`, not embedded here. */
export interface FileRestoreJobOut {
  id: string;
  job_type: FileRestoreJobType;
  status: FileRestoreJobStatus;
  /** The backup group's resolved name and VMID/CTID (e.g. "Office-PC (116)"), resolved server-side. */
  workload_label: string;
  /** `archive`: the selection's common parent directory. */
  path: string;
  paths?: string[] | null;
  result: FileEntryOut[] | null;
  result_filename: string | null;
  result_size_bytes?: number | null;
  content_available?: boolean;
  archive_report?: ArchiveReportOut | null;
  progress?: FileRestoreJobProgressOut | null;
  error_code: string | null;
  error_message: string | null;
  expires_at: string;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
}

export interface DownloadTicketOut {
  /** Relative to the API base; works without an Authorization header for a short time. */
  download_url: string;
  expires_at: string;
}

export interface TaskRunOut {
  id: string;
  pbs_instance_id: string;
  upid: string;
  task_type: string;
  worker_id: string | null;
  status: TaskStatus;
  started_at: string;
  ended_at: string | null;
  task_user: string | null;
  last_synced_at: string;
}

export interface TaskLogLineOut {
  n: number;
  text: string;
}

export interface TaskLogOut {
  lines: TaskLogLineOut[];
  truncated: boolean;
}

export interface PricingPlanOut {
  id: string;
  customer_id: string | null;
  name: string;
  price_per_vm_month: string;
  price_per_ct_month: string;
  price_per_host_month: string;
  price_per_gb_month: string;
  currency: string;
  created_at: string;
}

export interface PricingPlanCreateRequest {
  customer_id?: string | null;
  name: string;
  price_per_vm_month: string;
  price_per_ct_month?: string;
  price_per_host_month?: string;
  price_per_gb_month: string;
  currency?: string;
}

export interface PricingPlanUpdateRequest {
  name?: string;
  price_per_vm_month?: string;
  price_per_ct_month?: string;
  price_per_host_month?: string;
  price_per_gb_month?: string;
  currency?: string;
}

export type AlarmSeverity = "critical" | "warning";

export interface AlarmOut {
  type: string;
  severity: AlarmSeverity;
  customer_id: string | null;
  customer_name: string | null;
  customer_match_code: string | null;
  message: string;
  entity_type: string;
  entity_id: string;
}

export type PricingPlanSource = "customer" | "global";

export interface CustomerBillingOut {
  customer_id: string;
  customer_name: string;
  customer_match_code: string;
  vm_count: number;
  ct_count: number;
  host_count: number;
  estimated_physical_bytes: number;
  physical_bytes_estimate_complete: boolean;
  pricing_plan_id: string | null;
  pricing_plan_source: PricingPlanSource | null;
  price_per_vm_month: string | null;
  price_per_ct_month: string | null;
  price_per_host_month: string | null;
  price_per_gb_month: string | null;
  currency: string | null;
  estimated_monthly_cost: string | null;
}

// --- First-boot setup wizard (manifest §5.1/§17 Milestone 5) ---

export interface SetupStatusOut {
  needs_setup: boolean;
  kvm_available: boolean;
  kvm_detail: string;
}

export interface SetupCompleteRequest {
  email: string;
  display_name: string;
  password: string;
}

// --- Local users, password auth, and MFA (not from the manifest) ---

export interface UserOut {
  id: string;
  email: string;
  display_name: string;
  is_breakglass: boolean;
  disabled: boolean;
  has_password: boolean;
  created_at: string;
  /** Every role assignment this user currently holds, resolved server-side. */
  roles: UserRoleAssignmentOut[];
  /** An administrator requires MFA for this user (local accounts only). */
  mfa_required: boolean;
  /** Own flag or the global "Require MFA for all local users". */
  mfa_required_effective?: boolean;
  /** Has a confirmed authenticator app or a security key. */
  mfa_enrolled: boolean;
  /** Reset MFA has something to remove (also an unconfirmed TOTP or recovery codes). */
  mfa_reset_possible?: boolean;
}

export interface UserCreateRequest {
  email: string;
  display_name: string;
  password: string;
  mfa_required?: boolean;
}

export interface UserRoleAssignmentOut {
  id: string;
  role: string;
  scope_type: string;
  scope_id: string | null;
  source: string;
}

export interface AssignRoleRequest {
  role: string;
  scope_type: string;
  scope_id?: string | null;
}

export interface LocalLoginResponse {
  mfa_required: boolean;
  access_token: string | null;
  token_type: string;
  expires_in: number | null;
  challenge_token: string | null;
  available_methods: string[];
  /** An admin requires MFA and the account has none yet: set one up with
   * `enrollment_token`, then POST /auth/mfa/enrollment/complete. */
  mfa_enrollment_required?: boolean;
  enrollment_token?: string | null;
}

export interface MfaSessionResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
}

export interface MfaStatusOut {
  totp_enabled: boolean;
  webauthn_credentials: { id: string; nickname: string; created_at: string }[];
  recovery_codes_remaining: number;
  /** An administrator requires MFA: the last factor cannot be removed. */
  mfa_required?: boolean;
}

export interface TotpEnrollResponse {
  secret: string;
  otpauth_uri: string;
}

export interface RecoveryCodesOut {
  codes: string[];
}

export interface WebauthnCeremonyOptionsOut {
  options: Record<string, unknown>;
  ceremony_token: string;
}

// --- File Indexing (manifest §13, ADR 0016) ---

export type FileIndexJobStatus = "pending" | "running" | "paused" | "completed" | "failed" | "cancelled";
export type FileIndexJobTrigger = "manual" | "bulk" | "scheduled" | "guest_exec_hook";

export interface FileIndexJobOut {
  id: string;
  status: FileIndexJobStatus;
  trigger: FileIndexJobTrigger;
  file_index_method: FileIndexMethod;
  /** For a guest-exec method: the VMID indexing actually ran against -- a freshly allocated clone for "guest_exec_ephemeral", or the production VM's own real VMID for "guest_exec_live". */
  guest_exec_target_vmid: number | null;
  /** The PVE node guest_exec_target_vmid runs on. */
  guest_exec_target_node: string | null;
  backup_group_id: string;
  /** The backup group's resolved name and VMID/CTID (e.g. "Office-PC (116)"), resolved server-side. */
  workload_label: string;
  backup_snapshot_id: string;
  entries_indexed: number;
  current_path: string | null;
  cancel_requested: boolean;
  error_code: string | null;
  error_message: string | null;
  paused_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
}

export interface BulkFileIndexJobResult {
  created_job_ids: string[];
  skipped_already_indexed: number;
}

export interface FileIndexEntryOut {
  id: string;
  backup_snapshot_id: string;
  backup_group_id: string;
  archive: string;
  path: string;
  filename: string;
  type: FileEntryType;
  size_bytes: number | null;
  mtime: string | null;
}

/** The regex patterns every newly synced backup group starts with -- see
 * `BackupGroup.file_index_exclude_patterns`'s default on the backend.
 * Fetched, never hardcoded here, so this can't drift from the backend's
 * own list. */
export interface FileIndexDefaultExcludePatternsOut {
  patterns: string[];
}

export interface FileIndexSearchResponse {
  items: FileIndexEntryOut[];
  has_more: boolean;
}

export interface SnapshotIndexStatusOut {
  indexed_snapshot_ids: string[];
}

export interface CustomerFileIndexSettingsOut {
  customer_id: string;
  enabled: boolean;
  retention_days: number;
  schedule_enabled: boolean;
  schedule_interval_hours: number | null;
  window_start_time: string | null;
  window_end_time: string | null;
  next_scheduled_run_at: string | null;
  /** Default lab-enabled PVE cluster a "guest_exec_ephemeral" job's disposable clone boots on. */
  guest_exec_pve_cluster_id: string | null;
  /** Default PVE node for "guest_exec_ephemeral" boots. */
  guest_exec_target_node: string | null;
  /** Default PVE storage for "guest_exec_ephemeral" boots. */
  guest_exec_target_storage: string | null;
  /** Every Nth guest-exec index for a group forces a full walk instead of incremental, reconciling deletions/renames. Null disables periodic full reconciliation. */
  full_reindex_every_n_runs: number | null;
}

export interface CustomerFileIndexSettingsCreateRequest {
  enabled?: boolean;
  retention_days: number;
  schedule_enabled?: boolean;
  schedule_interval_hours?: number | null;
  window_start_time?: string | null;
  window_end_time?: string | null;
  guest_exec_pve_cluster_id?: string | null;
  guest_exec_target_node?: string | null;
  guest_exec_target_storage?: string | null;
  full_reindex_every_n_runs?: number | null;
}

export interface CustomerFileIndexSettingsUpdateRequest {
  enabled?: boolean;
  retention_days?: number;
  schedule_enabled?: boolean;
  schedule_interval_hours?: number | null;
  window_start_time?: string | null;
  window_end_time?: string | null;
  guest_exec_pve_cluster_id?: string | null;
  guest_exec_target_node?: string | null;
  guest_exec_target_storage?: string | null;
  full_reindex_every_n_runs?: number | null;
}

// --- Audit log (manifest §8) ---

export interface AuditEventOut {
  id: string;
  occurred_at: string;
  actor_user_id: string | null;
  /** Actor's display name and email, snapshotted at write time -- stays correct even after the user
   * account is deleted. Null only for events recorded before this field existed, or with no actor at all. */
  actor_label: string | null;
  /** Whether this action was taken via the emergency break-glass credential, snapshotted at write time. */
  actor_is_breakglass: boolean;
  action: string;
  target_type: string | null;
  target_id: string | null;
  correlation_id: string | null;
  detail: Record<string, unknown> | null;
  customer_id: string | null;
}

// --- Recovery Evidence compliance reporting (Reporting) ---

export interface FrameworkControlRef {
  framework: string;
  control: string;
  title: string;
}

export interface BackupHistoryEntryOut {
  backup_group_id: string;
  workload_label: string;
  backup_type: string;
  namespace: string;
  snapshot_time: string;
  size_bytes: number | null;
  verify_status: string;
}

export interface PolicyAdherenceEntryOut {
  backup_group_id: string;
  workload_label: string;
  policy_name: string | null;
  status: string;
  last_backup_at: string | null;
  last_verify_status: string;
}

export interface VerificationSummaryOut {
  ok: number;
  failed: number;
  none: number;
  unknown: number;
}

export interface VerificationResultsOut {
  summary: VerificationSummaryOut;
  entries: BackupHistoryEntryOut[];
}

export interface FailedJobEntryOut {
  source: "file_index_job" | "file_restore_job" | "pbs_task";
  id: string;
  label: string;
  status: string;
  occurred_at: string;
  error_message: string | null;
}

export interface AuditTrailEntryOut {
  id: string;
  occurred_at: string;
  actor_user_id: string | null;
  action: string;
  target_type: string | null;
  target_id: string | null;
}

export interface PbsInstanceAttestationOut {
  pbs_instance_id: string;
  display_name: string;
  tls_trust_mode: string;
  tls_fingerprint_masked: string | null;
}

export interface EncryptionAttestationOut {
  instances: PbsInstanceAttestationOut[];
}

export interface RestoreTestEvidenceEntryOut {
  application_group_name: string;
  backup_group_id: string;
  workload_label: string;
  run_status: string;
  member_status: string;
  tested_at: string | null;
}

export interface AccessControlEntryOut {
  user_display_name: string;
  role: string;
  scope: "customer" | "global";
  granted_at: string;
}

/** A PBS-native ACL grant that applies to one of this customer's
 * datastores -- access granted directly in PBS, outside RESURGAM's own
 * roles (AccessControlEntryOut above). Synced, not fetched live. */
export interface PbsDatastoreAccessEntryOut {
  datastore_name: string;
  path: string;
  role: string;
  subject: string;
  subject_type: string;
}

export interface ManagementSummaryOut {
  traffic_light: "green" | "amber" | "red";
  backup_success_rate: number;
  verification_pass_rate: number;
  restore_test_pass_rate: number | null;
  policy_adherence_rate: number;
  unresolved_failed_jobs: number;
  four_eyes_approval_rate: number | null;
  period_timeline_svg: string;
  kpi_bars_svg: string;
}

export type ProtectionLevelTier =
  | "backup_only"
  | "backup_verified"
  | "backup_verified_restore_tested"
  | "backup_verified_lab_tested";

export interface ProtectionLevelWorkloadOut {
  workload_label: string;
  tier: ProtectionLevelTier;
}

export interface ProtectionLevelOut {
  tier: ProtectionLevelTier;
  label: string;
  workloads: ProtectionLevelWorkloadOut[];
  tier_distribution_svg: string;
}

export interface OffsiteStatusEntryOut {
  backup_group_id: string;
  workload_label: string;
  has_offsite_copy: boolean;
  offsite_method: "datastore_role" | "sync_job" | "none";
  sync_schedule: string | null;
  last_synced_at: string | null;
}

export type ThreeTwoOneOneZeroLeg = "copies" | "media_types" | "offsite" | "immutable" | "zero_errors";

export interface ThreeTwoOneOneZeroOut {
  leg: ThreeTwoOneOneZeroLeg;
  status: "met" | "not_met" | "not_attested" | "not_applicable";
  detail: string;
  attested_by: string | null;
  attested_at: string | null;
}

export interface LegApplicabilityOut {
  leg: ThreeTwoOneOneZeroLeg;
  applicable: boolean;
  updated_by_display_name: string | null;
  updated_at: string | null;
}

export interface NotificationSettingsOut {
  report_notification_email: string | null;
  report_notifications_enabled: boolean;
  alarm_notification_email: string | null;
  alarm_notifications_enabled: boolean;
  billing_notification_email: string | null;
  billing_notifications_enabled: boolean;
}

/** Alarm fields are optional: omitted = left unchanged (the Alert Mailer panel owns them). */
export interface UpdateNotificationSettingsRequest {
  report_notification_email: string | null;
  report_notifications_enabled: boolean;
  alarm_notification_email?: string | null;
  alarm_notifications_enabled?: boolean;
  billing_notification_email: string | null;
  billing_notifications_enabled: boolean;
}

export type AlarmMinSeverity = "warning" | "critical";

export interface AlertMailerSettingsOut {
  enabled: boolean;
  recipients: string[];
  min_severity: AlarmMinSeverity;
  notify_resolved: boolean;
  reminder_hours: number | null;
  open_alarm_count: number;
  notified_alarm_count: number;
  last_attempt_at: string | null;
  last_sent_at: string | null;
  last_error: string | null;
}

export interface UpdateAlertMailerSettingsRequest {
  enabled: boolean;
  recipients: string[];
  min_severity: AlarmMinSeverity;
  notify_resolved: boolean;
  reminder_hours: number | null;
}

export interface SendTestAlertResponse {
  sent: boolean;
  recipients: string[];
  error: string | null;
}

export interface RecoveryEvidenceReportOut {
  customer_id: string;
  customer_name: string;
  customer_match_code: string;
  date_from: string;
  date_to: string;
  generated_at: string;
  generated_by_user_id: string | null;
  backup_history: BackupHistoryEntryOut[];
  policy_adherence: PolicyAdherenceEntryOut[];
  verification_results: VerificationResultsOut;
  failed_jobs: FailedJobEntryOut[];
  audit_trail: AuditTrailEntryOut[];
  encryption_attestation: EncryptionAttestationOut;
  restore_test_evidence: RestoreTestEvidenceEntryOut[];
  management_summary: ManagementSummaryOut;
  access_control: AccessControlEntryOut[];
  pbs_datastore_access: PbsDatastoreAccessEntryOut[];
  protection_level: ProtectionLevelOut;
  offsite_status: OffsiteStatusEntryOut[];
  three_two_one_one_zero: ThreeTwoOneOneZeroOut[];
  framework: boolean;
  section_controls: Record<string, FrameworkControlRef[]>;
}

// --- Compliance attestations (3-2-1-1-0 self-assessment) ---

export type ComplianceAttestationValue = boolean | number | string[];

export interface ComplianceAttestationOut {
  id: string;
  scope_type: "datastore" | "customer";
  scope_id: string;
  dimension: string;
  value: ComplianceAttestationValue;
  notes: string | null;
  asserted_by_user_id: string;
  asserted_by_display_name: string;
  created_at: string;
}

// --- Report history / instance settings (Report Center) ---

export interface ReportHistoryOut {
  id: string;
  report_type: string;
  customer_id: string | null;
  date_from: string;
  date_to: string;
  framework: boolean;
  generated_by_user_id: string | null;
  sha256_checksum: string;
  created_at: string;
}

export interface InstanceSettingsOut {
  min_report_retention_days: number;
  collector_poll_interval_minutes: number;
}

export interface MailSettingsOut {
  smtp_host: string;
  smtp_port: number;
  smtp_username: string | null;
  smtp_password_configured: boolean;
  use_tls: boolean;
  from_address: string;
  from_name: string | null;
  enabled: boolean;
}

export interface UpdateMailSettingsRequest {
  smtp_host: string;
  smtp_port: number;
  smtp_username?: string | null;
  // Omit to keep the current password; null clears it; a string rotates it.
  smtp_password?: string | null;
  use_tls: boolean;
  from_address: string;
  from_name?: string | null;
  enabled: boolean;
}

export interface SendTestEmailResponse {
  sent: boolean;
  error: string | null;
}

// --- Credentials (Administration) ---

export interface CredentialOut {
  secret_reference_id: string;
  description: string;
  owner_type: "pbs_instance" | "pve_cluster";
  owner_id: string;
  owner_label: string;
  created_at: string;
  /** When this token's value was last set -- equal to created_at until the first rotation. */
  last_rotated_at: string;
}

// --- Integrations (Administration, manifest §6.3) ---

export interface OidcSettingsOut {
  issuer: string;
  client_id: string;
  redirect_uri: string;
  enabled: boolean;
  client_secret_configured: boolean;
  source: "database" | "environment";
}

export interface UpdateOidcSettingsRequest {
  issuer: string;
  client_id: string;
  client_secret?: string;
  redirect_uri: string;
  enabled: boolean;
}

export interface OidcDiscoveryTestResponse {
  reachable: boolean;
  error: string | null;
}

/** Backup Jobs -> Run now: one PVE vzdump task per node. */
export interface BackupRunTaskOut {
  node: string;
  upid: string;
  vmids: number[] | null;
}

/** A VMID of the job this run does not back up: gone, or on another node than a node-bound job's. */
export interface BackupRunSkippedOut {
  vmid: number;
  reason: "missing" | "other_node";
}

export interface BackupRunOut {
  job_id: string;
  tasks: BackupRunTaskOut[];
  skipped: BackupRunSkippedOut[];
}

export interface PveTaskProgressOut {
  node: string;
  upid: string;
  status: string | null;
  exitstatus: string | null;
  lines: { n: number; t: string }[];
  next_start: number;
  /** A full page came back -- poll again right away, even after `stopped`. */
  more: boolean;
}

export interface BrandingSettingsOut {
  logo_configured: boolean;
  primary_color: string | null;
  accent_color: string | null;
  /** White-label product name; null = "RESURGAM". */
  app_name?: string | null;
}

// --- Appliance updates (ADR 0024) -------------------------------------------

export interface VersionResponse {
  version: string;
}

export type UpdateAction = "verify" | "apply";
export type UpdateState = "queued" | "running" | "succeeded" | "failed" | "rolled_back";
export type UpdateStep =
  | "verify"
  | "backup"
  | "stop"
  | "load_images"
  | "install"
  | "host_setup"
  | "migrate"
  | "start"
  | "health"
  | "rollback"
  | "done";

export interface UpdateManifestSummary {
  version: string | null;
  changelog: string | null;
  min_upgrade_from: string | null;
}

export interface UpdateRequestStatusOut {
  request_id: string;
  action: UpdateAction | null;
  bundle: string | null;
  requested_by: string | null;
  requested_at: string | null;
  state: UpdateState;
  step: string | null;
  from_version: string | null;
  to_version: string | null;
  message: string | null;
  log: string[];
  manifest: UpdateManifestSummary | null;
  backup_file: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface UpdatesOverviewOut {
  available: boolean;
  unavailable_reason: string | null;
  current_version: string;
  max_bundle_bytes: number;
  active_apply: UpdateRequestStatusOut | null;
}

export interface UpdateBundleOut {
  name: string;
  size_bytes: number;
  uploaded_at: string;
  version: string | null;
  problem: string | null;
  signature_prechecked: boolean;
}

export interface UpdateHistoryEntryOut {
  request_id: string | null;
  state: string | null;
  from_version: string | null;
  to_version: string | null;
  message: string | null;
  backup_file: string | null;
  requested_by: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface UpdateBackupOut {
  name: string;
  size_bytes: number;
  created_at: string;
}

// --- Release update source (ADR 0026) ---

export type UpdateSourceProvider = "github" | "gitlab";
export type UpdateSourceDownloadState = "running" | "succeeded" | "failed";

export interface UpdateSourceRelease {
  version: string;
  tag_name: string;
  name: string | null;
  released_at: string | null;
  notes: string | null;
  web_url: string | null;
  prerelease: boolean;
  bundle_name: string | null;
  bundle_size: number | null;
}

export interface UpdateSourceDownloadOut {
  download_id: string;
  version: string;
  source_asset: string | null;
  state: UpdateSourceDownloadState;
  bytes_done: number;
  bytes_total: number | null;
  bundle: string | null;
  signature_prechecked: boolean;
  message: string | null;
  requested_by: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface UpdateSourceOut {
  configured: boolean;
  provider: UpdateSourceProvider;
  repository_url: string;
  token_configured: boolean;
  tls_verify: boolean;
  include_prereleases: boolean;
  auto_check: boolean;
  check_interval_hours: number;
  current_version: string;
  last_checked_at: string | null;
  last_check_error: string | null;
  latest_version: string | null;
  available_releases: UpdateSourceRelease[];
  active_download: UpdateSourceDownloadOut | null;
  /** Only in the PUT answer: the save moved to another provider/API host
   * without a new token, so the stored token was removed. */
  token_cleared?: boolean;
}

export interface UpdateSourceSettingsRequest {
  provider: UpdateSourceProvider;
  repository_url: string;
  /** Omit to keep the stored token, null to remove it. */
  token?: string | null;
  tls_verify: boolean;
  include_prereleases: boolean;
  auto_check: boolean;
  check_interval_hours: number;
}

export interface UpdateSourceTestRequest {
  provider: UpdateSourceProvider;
  repository_url: string;
  /** Omit to test with the stored token. */
  token?: string | null;
  tls_verify: boolean;
}

export interface UpdateSourceTestOut {
  ok: boolean;
  repository: string | null;
  private: boolean | null;
  releases_found: number | null;
  latest_release: string | null;
  error: string | null;
  /** The stored token belongs to another provider/API host and was not sent. */
  stored_token_withheld?: boolean;
}

// --- Configuration export/import (ADR 0024) ---
// The exported document itself is treated as opaque JSON by the UI: it is
// downloaded as-is and posted back unchanged. Only the envelope is typed.
export interface ConfigBackupDocument {
  format: number;
  exported_at: string;
  resurgam_version: string;
  instance_name: string | null;
  sections: Record<string, unknown>;
}

export type ConfigImportAction = "create" | "update" | "skip" | "conflict";

export interface ConfigImportItem {
  key: string;
  action: ConfigImportAction;
  reason: string | null;
}

export interface ConfigImportSectionResult {
  section: string;
  create: number;
  update: number;
  skip: number;
  conflict: number;
  items: ConfigImportItem[];
}

export type ConfigReEnterKind =
  | "pbs_token_secret"
  | "pve_token_secret"
  | "datastore_encryption_key"
  | "smtp_password"
  | "oidc_client_secret"
  | "lab_agent_token"
  | "user_password"
  | "branding_logo"
  | "update_source_token";

export interface ConfigReEnterItem {
  kind: ConfigReEnterKind;
  target: string;
  target_id: string | null;
  page: string;
  detail: string;
}

export interface ConfigImportResult {
  dry_run: boolean;
  applied: boolean;
  source_resurgam_version: string;
  source_exported_at: string;
  source_instance_name: string | null;
  sections: ConfigImportSectionResult[];
  to_reenter: ConfigReEnterItem[];
  notes: string[];
}

// --- Disk Mounts (ADR 0025) ---------------------------------------------------

export type DiskMountStatus =
  | "pending_approval"
  | "queued"
  | "provisioning"
  | "ready"
  | "stopping"
  | "stopped"
  | "expired"
  | "failed"
  | "rejected";
export type DiskMountProtocol = "smb" | "nfs";
export type DiskMountIpMode = "dhcp" | "static" | "cluster_range";

export interface DiskMountVolumeOut {
  disk_index: number;
  archive: string;
  partition: string;
  fstype: string | null;
  size_bytes: number | null;
  name: string | null;
  mounted: boolean;
  reason: string | null;
  detail: string | null;
}

export interface DiskMountConnectionOut {
  host: string | null;
  smb_unc: string | null;
  smb_url: string | null;
  smb_username: string | null;
  nfs_exports: string[];
}

export interface DiskMountOut {
  id: string;
  status: DiskMountStatus;
  teardown_reason: string | null;
  customer_id: string | null;
  customer_name: string | null;
  backup_group_id: string;
  backup_snapshot_id: string;
  snapshot_time: string | null;
  workload_label: string | null;
  disk_archives: string[];
  pve_cluster_id: string;
  pve_cluster_name: string | null;
  target_node: string;
  bridge: string;
  vlan_tag: number | null;
  ip_mode: DiskMountIpMode;
  ip_address_cidr: string | null;
  gateway: string | null;
  protocols: DiskMountProtocol[];
  allowed_clients: string[];
  smb_username: string | null;
  lifetime_hours: number;
  created_at: string;
  created_by_user_id: string | null;
  approval_required: boolean;
  approved_by_user_id: string | null;
  approved_at: string | null;
  approval_notes: string | null;
  ready_at: string | null;
  expires_at: string | null;
  ended_at: string | null;
  error: string | null;
  helper_vmid: number | null;
  volumes: DiskMountVolumeOut[];
  connection: DiskMountConnectionOut;
}

export interface DiskMountCreatedOut {
  mount: DiskMountOut;
  /** Shown exactly once; null for an NFS-only mount. */
  share_password: string | null;
}

export interface DiskMountCreateRequest {
  backup_snapshot_id: string;
  disk_archives: string[];
  pve_cluster_id: string;
  target_node: string;
  target_storage?: string | null;
  bridge?: string | null;
  vlan_tag?: number | null;
  ip_mode: DiskMountIpMode;
  ip_address_cidr?: string | null;
  gateway?: string | null;
  protocols: DiskMountProtocol[];
  allowed_clients: string[];
  lifetime_hours: number;
}

export interface SnapshotDiskOut {
  archive: string;
  slot: string;
  size_bytes: number | null;
  crypt_mode: string | null;
}

export interface DiskMountPlacementOut {
  pve_cluster_id: string;
  display_name: string;
  ready: boolean;
  problems: string[];
  require_approval: boolean;
  helper_template_vmid: number | null;
  lab_agent_last_seen_at: string | null;
  lab_agent_node?: string | null;
  uplink_bridge: string | null;
  uplink_vlan_tag: number | null;
  uplink_static_range: boolean;
}

/** Personal API tokens (docs/adr/0029). */
export interface ApiTokenGrant {
  role: string;
  scope_type: string;
  scope_id: string | null;
  scope_name?: string | null;
}

export interface ApiTokenOut {
  id: string;
  name: string;
  token_prefix: string;
  read_only: boolean;
  grants: ApiTokenGrant[];
  allowed_cidrs: string[] | null;
  created_at: string;
  expires_at: string;
  last_used_at: string | null;
  last_used_ip: string | null;
  status: "active" | "expired";
  user_id: string;
  user_email: string;
  user_display_name: string;
}

/** Returned once, at creation: the only time the token itself is visible. */
export interface ApiTokenCreatedOut extends ApiTokenOut {
  token: string;
}

export interface ApiTokenCreateRequest {
  name: string;
  expires_in_days: number;
  read_only: boolean;
  grants: { role: string; scope_type: string; scope_id: string | null }[];
  allowed_cidrs: string[] | null;
}

export interface ApiTokenGrantableOut {
  can_grant_any: boolean;
  options: ApiTokenGrant[];
  max_lifetime_days: number;
}

/** A PBS encryption key in RESURGAM's key store (ADR 0031). Never carries key material. */
export interface EncryptionKeyOut {
  id: string;
  fingerprint: string;
  label: string;
  customer_id: string | null;
  customer_name: string | null;
  kdf: string;
  source: "upload" | "import" | "datastore" | string;
  key_created_at: string | null;
  created_at: string;
  copy_confirmed_at: string | null;
  backup_count: number;
  can_download: boolean;
}

/** Fresh proof of identity for a key download/export: password or authenticator code. */
export interface StepUpConfirmation {
  password?: string | null;
  totp_code?: string | null;
}

export interface EncryptionKeyImportOut {
  imported: string[];
  skipped: string[];
}

export interface TenantKeyDownloadPermissionOut {
  customer_id: string;
  allowed: boolean;
}

/** GET /zabbix/setup -- what the Zabbix monitoring wizard shows. */
export interface ZabbixSetupOut {
  appliance_url: string;
  template_name: string;
  template_version: string;
  min_zabbix_version: string;
  ca_certificate_available: boolean;
  datastores: number;
  datastores_with_usage: number;
  job_types: number;
}
