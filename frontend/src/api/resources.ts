import {
  API_BASE_URL,
  apiRequest,
  downloadFile,
  postDownload,
  putFormData,
  startBrowserDownload,
  uploadFile,
  uploadRawWithProgress,
} from "./client";
import type {
  EncryptionKeyImportOut,
  EncryptionKeyOut,
  StepUpConfirmation,
  TenantKeyDownloadPermissionOut,
  DiskMountCreatedOut,
  DiskMountCreateRequest,
  DiskMountOut,
  DiskMountPlacementOut,
  SnapshotDiskOut,
  ConfigBackupDocument,
  ConfigImportResult,
  UpdateAction,
  UpdateBackupOut,
  UpdateBundleOut,
  UpdateHistoryEntryOut,
  UpdateRequestStatusOut,
  UpdatesOverviewOut,
  UpdateSourceDownloadOut,
  UpdateSourceOut,
  UpdateSourceSettingsRequest,
  UpdateSourceTestOut,
  UpdateSourceTestRequest,
  VersionResponse,
  AlarmOut,
  ApplicationGroupCreateRequest,
  ApplicationGroupOut,
  ApplicationGroupUpdateRequest,
  AssignRoleRequest,
  AuditEventOut,
  BackupGroupOut,
  BackupSnapshotOut,
  BrandingSettingsOut,
  BreakglassLoginResponse,
  BulkFileIndexJobResult,
  CapabilityReportOut,
  ComplianceAttestationOut,
  ComplianceAttestationValue,
  CredentialOut,
  CustomerBillingOut,
  CustomerCreateRequest,
  CustomerFileIndexSettingsCreateRequest,
  CustomerFileIndexSettingsOut,
  CustomerFileIndexSettingsUpdateRequest,
  CustomerImportResult,
  CustomerOut,
  CustomerUpdateRequest,
  DatastoreGcScheduleOut,
  DatastoreMaintenanceModeOut,
  DatastoreOut,
  DatastoreRole,
  DatastoreUsageOut,
  JobCapabilitiesOut,
  FileIndexDefaultExcludePatternsOut,
  FileIndexEntryOut,
  FileIndexJobOut,
  FileIndexMethod,
  FileIndexJobStatus,
  FileIndexJobTrigger,
  FileIndexSearchResponse,
  DownloadTicketOut,
  FileRestoreJobOut,
  InstanceSettingsOut,
  SnapshotIndexStatusOut,
  LabAgentInstallJobRequest,
  LabAgentInstallOut,
  LabArtifactsOut,
  LabAgentSetupJobCreate,
  LabNodePairingOut,
  LabSetupJobOut,
  LabTemplateBuildJobRequest,
  LabTemplateBuildScriptOut,
  SshHostKeyOut,
  SshTargetOut,
  LabAgentTokenOut,
  LabReadinessOut,
  LabSetupFixOut,
  PveBridgeOut,
  PveSdnZoneOut,
  PveStorageConfigOut,
  PveTemplateOut,
  LegApplicabilityOut,
  LocalLoginResponse,
  MailSettingsOut,
  MfaSessionResponse,
  MfaStatusOut,
  NotificationSettingsOut,
  AlertMailerSettingsOut,
  UpdateAlertMailerSettingsRequest,
  SendTestAlertResponse,
  OidcDiscoveryTestResponse,
  OidcSettingsOut,
  PbsInstanceCreateRequest,
  PbsInstanceOut,
  PbsInstanceSyncOut,
  PbsInstanceUpdateRequest,
  PbsPruneJobCreateRequest,
  PbsPruneJobOut,
  PbsPruneJobUpdateRequest,
  PbsRemoteCreateRequest,
  PbsRemoteOut,
  PbsRemoteUpdateRequest,
  PbsSyncJobCreateRequest,
  PbsSyncJobOut,
  PbsSyncJobUpdateRequest,
  PbsVerifyJobCreateRequest,
  PbsVerifyJobOut,
  PbsVerifyJobUpdateRequest,
  PricingPlanCreateRequest,
  PricingPlanOut,
  PricingPlanUpdateRequest,
  ProtectionPolicyCreateRequest,
  ProtectionPolicyOut,
  PveClusterCreateRequest,
  PveClusterOut,
  PveClusterUpdateRequest,
  PveGuestOut,
  PveNodeOut,
  PvePoolOut,
  PveStorageOut,
  PveVmidCheckOut,
  RecoveryCodesOut,
  RecoveryEvidenceReportOut,
  RemoteDatastoreOut,
  ReportHistoryOut,
  RestorePlanCreateRequest,
  RestorePlanOut,
  RestoreTestPlanCreateRequest,
  RestoreTestPlanExecuteOut,
  RestoreTestPlanOut,
  SendTestEmailResponse,
  SetDatastoreGcScheduleRequest,
  SetDatastoreMaintenanceModeRequest,
  SetupCompleteRequest,
  SetupStatusOut,
  SyncJobOut,
  TaskLogOut,
  TaskRunOut,
  RequiredPrivilegesOut,
  TestConnectionResponse,
  TlsProbeResponse,
  TotpEnrollResponse,
  UpdateMailSettingsRequest,
  UpdateNotificationSettingsRequest,
  UpdateOidcSettingsRequest,
  UserCreateRequest,
  UserOut,
  UserRoleAssignmentOut,
  VzdumpJobCreateRequest,
  VzdumpJobOut,
  BackupRunOut,
  PveTaskProgressOut,
  VzdumpJobUpdateRequest,
  WebauthnCeremonyOptionsOut,
  WhoAmIResponse,
  ApiTokenCreatedOut,
  ApiTokenCreateRequest,
  ApiTokenGrantableOut,
  ApiTokenOut,
} from "./types";

export const setupApi = {
  status: () => apiRequest<SetupStatusOut>("/setup/status"),
  complete: (body: SetupCompleteRequest) => apiRequest<UserOut>("/setup/complete", { method: "POST", body }),
};

export const authApi = {
  breakglassLogin: (username: string, password: string) =>
    apiRequest<BreakglassLoginResponse>("/auth/breakglass-login", {
      method: "POST",
      body: { username, password },
    }),
  localLogin: (email: string, password: string) =>
    apiRequest<LocalLoginResponse>("/auth/local-login", { method: "POST", body: { email, password } }),
  verifyTotp: (challengeToken: string, code: string) =>
    apiRequest<MfaSessionResponse>("/auth/mfa/verify-totp", {
      method: "POST",
      body: { challenge_token: challengeToken, code },
    }),
  verifyRecoveryCode: (challengeToken: string, code: string) =>
    apiRequest<MfaSessionResponse>("/auth/mfa/verify-recovery-code", {
      method: "POST",
      body: { challenge_token: challengeToken, code },
    }),
  webauthnAuthenticateOptions: (challengeToken: string) =>
    apiRequest<WebauthnCeremonyOptionsOut>("/auth/mfa/webauthn/authenticate-options", {
      method: "POST",
      body: { challenge_token: challengeToken },
    }),
  webauthnAuthenticate: (ceremonyToken: string, credential: unknown) =>
    apiRequest<MfaSessionResponse>("/auth/mfa/webauthn/authenticate", {
      method: "POST",
      body: { ceremony_token: ceremonyToken, credential },
    }),
  changePassword: (currentPassword: string, newPassword: string) =>
    apiRequest<void>("/auth/change-password", {
      method: "POST",
      body: { current_password: currentPassword, new_password: newPassword },
    }),
  whoami: () => apiRequest<WhoAmIResponse>("/auth/whoami"),
  completeMfaEnrollment: (enrollmentToken: string) =>
    apiRequest<MfaSessionResponse>("/auth/mfa/enrollment/complete", {
      method: "POST",
      body: { enrollment_token: enrollmentToken },
    }),
};

export const auditApi = {
  list: (options?: { limit?: number; action?: string; targetType?: string; actorUserId?: string }) => {
    const params = new URLSearchParams();
    if (options?.limit) params.set("limit", String(options.limit));
    if (options?.action) params.set("action", options.action);
    if (options?.targetType) params.set("target_type", options.targetType);
    if (options?.actorUserId) params.set("actor_user_id", options.actorUserId);
    const qs = params.toString();
    return apiRequest<AuditEventOut[]>(`/audit-events${qs ? `?${qs}` : ""}`);
  },
};

export const usersApi = {
  list: () => apiRequest<UserOut[]>("/users"),
  get: (id: string) => apiRequest<UserOut>(`/users/${id}`),
  create: (body: UserCreateRequest) => apiRequest<UserOut>("/users", { method: "POST", body }),
  disable: (id: string) => apiRequest<UserOut>(`/users/${id}/disable`, { method: "POST" }),
  enable: (id: string) => apiRequest<UserOut>(`/users/${id}/enable`, { method: "POST" }),
  setPassword: (id: string, password: string) =>
    apiRequest<UserOut>(`/users/${id}/set-password`, { method: "POST", body: { password } }),
  roleAssignments: (id: string) => apiRequest<UserRoleAssignmentOut[]>(`/users/${id}/role-assignments`),
  assignRole: (id: string, body: AssignRoleRequest) =>
    apiRequest<UserRoleAssignmentOut>(`/users/${id}/assign-role`, { method: "POST", body }),
  unassignRole: (id: string, assignmentId: string) =>
    apiRequest<void>(`/users/${id}/unassign-role`, { method: "POST", body: { assignment_id: assignmentId } }),
  setMfaRequired: (id: string, required: boolean) =>
    apiRequest<UserOut>(`/users/${id}/mfa-requirement`, { method: "POST", body: { required } }),
  resetMfa: (id: string) => apiRequest<UserOut>(`/users/${id}/reset-mfa`, { method: "POST" }),
  mfaPolicy: () => apiRequest<{ require_mfa_all_local_users: boolean }>("/mfa-policy"),
  setMfaPolicy: (required: boolean) =>
    apiRequest<{ require_mfa_all_local_users: boolean }>("/mfa-policy", {
      method: "PUT",
      body: { require_mfa_all_local_users: required },
    }),
};

export const mfaApi = {
  status: () => apiRequest<MfaStatusOut>("/mfa/status"),
  totpEnroll: () => apiRequest<TotpEnrollResponse>("/mfa/totp/enroll", { method: "POST" }),
  totpConfirm: (code: string) => apiRequest<void>("/mfa/totp/confirm", { method: "POST", body: { code } }),
  totpRemove: () => apiRequest<void>("/mfa/totp", { method: "DELETE" }),
  webauthnRegisterOptions: (nickname: string) =>
    apiRequest<WebauthnCeremonyOptionsOut>("/mfa/webauthn/register-options", {
      method: "POST",
      body: { nickname },
    }),
  webauthnRegister: (ceremonyToken: string, credential: unknown) =>
    apiRequest<{ id: string; nickname: string; created_at: string }>("/mfa/webauthn/register", {
      method: "POST",
      body: { ceremony_token: ceremonyToken, credential },
    }),
  webauthnRemove: (credentialId: string) => apiRequest<void>(`/mfa/webauthn/${credentialId}`, { method: "DELETE" }),
  recoveryCodesGenerate: () => apiRequest<RecoveryCodesOut>("/mfa/recovery-codes/generate", { method: "POST" }),
};

/** The MFA enrollment calls with an explicit enrollment token -- used by the
 * login page when an administrator requires MFA and the account has none. */
export function mfaEnrollmentApi(token: string) {
  return {
    totpEnroll: () => apiRequest<TotpEnrollResponse>("/mfa/totp/enroll", { method: "POST", token }),
    totpConfirm: (code: string) => apiRequest<void>("/mfa/totp/confirm", { method: "POST", body: { code }, token }),
    webauthnRegisterOptions: (nickname: string) =>
      apiRequest<WebauthnCeremonyOptionsOut>("/mfa/webauthn/register-options", {
        method: "POST",
        body: { nickname },
        token,
      }),
    webauthnRegister: (ceremonyToken: string, credential: unknown) =>
      apiRequest<{ id: string; nickname: string; created_at: string }>("/mfa/webauthn/register", {
        method: "POST",
        body: { ceremony_token: ceremonyToken, credential },
        token,
      }),
    recoveryCodesGenerate: () =>
      apiRequest<RecoveryCodesOut>("/mfa/recovery-codes/generate", { method: "POST", token }),
  };
}

export const pbsInstancesApi = {
  list: () => apiRequest<PbsInstanceOut[]>("/pbs-instances"),
  get: (id: string) => apiRequest<PbsInstanceOut>(`/pbs-instances/${id}`),
  create: (body: PbsInstanceCreateRequest) =>
    apiRequest<PbsInstanceOut>("/pbs-instances", { method: "POST", body }),
  update: (id: string, body: PbsInstanceUpdateRequest) =>
    apiRequest<PbsInstanceOut>(`/pbs-instances/${id}`, { method: "PATCH", body }),
  probeTls: (host: string, port: number) =>
    apiRequest<TlsProbeResponse>("/pbs-instances/probe-tls", { method: "POST", body: { host, port } }),
  testConnection: (id: string) =>
    apiRequest<TestConnectionResponse>(`/pbs-instances/${id}/test-connection`, { method: "POST" }),
  requiredPrivileges: () => apiRequest<RequiredPrivilegesOut>("/pbs-instances/required-privileges"),
  disable: (id: string) => apiRequest<PbsInstanceOut>(`/pbs-instances/${id}/disable`, { method: "POST" }),
  enable: (id: string) => apiRequest<PbsInstanceOut>(`/pbs-instances/${id}/enable`, { method: "POST" }),
  remove: (id: string) => apiRequest<void>(`/pbs-instances/${id}`, { method: "DELETE" }),
  availableDatastores: (id: string) =>
    apiRequest<RemoteDatastoreOut[]>(`/pbs-instances/${id}/available-datastores`),
  importDatastore: (id: string, name: string) =>
    apiRequest<DatastoreOut>(`/pbs-instances/${id}/datastores`, { method: "POST", body: { name } }),
  sync: (id: string) => apiRequest<PbsInstanceSyncOut>(`/pbs-instances/${id}/sync`, { method: "POST" }),
  tasks: (id: string, options?: { taskTypes?: string[]; limit?: number }) => {
    const params = new URLSearchParams();
    for (const type of options?.taskTypes ?? []) params.append("task_types", type);
    if (options?.limit) params.set("limit", String(options.limit));
    const qs = params.toString();
    return apiRequest<TaskRunOut[]>(`/pbs-instances/${id}/tasks${qs ? `?${qs}` : ""}`);
  },
  syncJobs: (id: string) => apiRequest<SyncJobOut[]>(`/pbs-instances/${id}/sync-jobs`),
  taskLog: (instanceId: string, upid: string) =>
    apiRequest<TaskLogOut>(`/pbs-instances/${instanceId}/tasks/${encodeURIComponent(upid)}/log`),
  getMaintenanceMode: (instanceId: string, datastoreName: string) =>
    apiRequest<DatastoreMaintenanceModeOut>(
      `/pbs-instances/${instanceId}/datastores/${encodeURIComponent(datastoreName)}/maintenance-mode`,
    ),
  setMaintenanceMode: (instanceId: string, datastoreName: string, body: SetDatastoreMaintenanceModeRequest) =>
    apiRequest<DatastoreMaintenanceModeOut>(
      `/pbs-instances/${instanceId}/datastores/${encodeURIComponent(datastoreName)}/maintenance-mode`,
      { method: "PUT", body },
    ),
  getGcSchedule: (instanceId: string, datastoreName: string) =>
    apiRequest<DatastoreGcScheduleOut>(
      `/pbs-instances/${instanceId}/datastores/${encodeURIComponent(datastoreName)}/gc-schedule`,
    ),
  setGcSchedule: (instanceId: string, datastoreName: string, body: SetDatastoreGcScheduleRequest) =>
    apiRequest<DatastoreGcScheduleOut>(
      `/pbs-instances/${instanceId}/datastores/${encodeURIComponent(datastoreName)}/gc-schedule`,
      { method: "PUT", body },
    ),
};

/** Live PBS job administration (docs/adr/0023) -- prune/verify/sync jobs
 * plus remotes, all pass-through CRUD against PBS's own config, no new
 * DB tables. Kept separate from `pbsInstancesApi` above: that object's
 * existing `syncJobs(id)` is a different, older thing entirely -- the
 * Collector's DB-cached inventory snapshot (`GET /pbs-instances/{id}/
 * sync-jobs`, manifest §8), not this live pass-through CRUD (which nests
 * under `/pbs-instances/{id}/datastores/{name}/sync-jobs` for list/create
 * and `/pbs-instances/{id}/sync-jobs/{id}` for get/update/delete). */
export const pbsJobsApi = {
  pruneJobCapabilities: (instanceId: string) =>
    apiRequest<JobCapabilitiesOut>(`/pbs-instances/${instanceId}/prune-jobs/capabilities`),
  /** Whether the token may set/remove a datastore's GC schedule
   * (`Datastore.Modify`); GC itself is read/written via
   * `pbsInstancesApi.getGcSchedule` / `setGcSchedule`. */
  gcScheduleCapabilities: (instanceId: string) =>
    apiRequest<JobCapabilitiesOut>(`/pbs-instances/${instanceId}/gc-schedule/capabilities`),
  pruneJobs: (instanceId: string, datastoreName: string) =>
    apiRequest<PbsPruneJobOut[]>(`/pbs-instances/${instanceId}/datastores/${encodeURIComponent(datastoreName)}/prune-jobs`),
  createPruneJob: (instanceId: string, datastoreName: string, body: PbsPruneJobCreateRequest) =>
    apiRequest<PbsPruneJobOut>(
      `/pbs-instances/${instanceId}/datastores/${encodeURIComponent(datastoreName)}/prune-jobs`,
      { method: "POST", body },
    ),
  updatePruneJob: (instanceId: string, jobId: string, body: PbsPruneJobUpdateRequest) =>
    apiRequest<PbsPruneJobOut>(`/pbs-instances/${instanceId}/prune-jobs/${encodeURIComponent(jobId)}`, {
      method: "PUT",
      body,
    }),
  deletePruneJob: (instanceId: string, jobId: string) =>
    apiRequest<void>(`/pbs-instances/${instanceId}/prune-jobs/${encodeURIComponent(jobId)}`, { method: "DELETE" }),

  verifyJobCapabilities: (instanceId: string) =>
    apiRequest<JobCapabilitiesOut>(`/pbs-instances/${instanceId}/verify-jobs/capabilities`),
  verifyJobs: (instanceId: string, datastoreName: string) =>
    apiRequest<PbsVerifyJobOut[]>(
      `/pbs-instances/${instanceId}/datastores/${encodeURIComponent(datastoreName)}/verify-jobs`,
    ),
  createVerifyJob: (instanceId: string, datastoreName: string, body: PbsVerifyJobCreateRequest) =>
    apiRequest<PbsVerifyJobOut>(
      `/pbs-instances/${instanceId}/datastores/${encodeURIComponent(datastoreName)}/verify-jobs`,
      { method: "POST", body },
    ),
  updateVerifyJob: (instanceId: string, jobId: string, body: PbsVerifyJobUpdateRequest) =>
    apiRequest<PbsVerifyJobOut>(`/pbs-instances/${instanceId}/verify-jobs/${encodeURIComponent(jobId)}`, {
      method: "PUT",
      body,
    }),
  deleteVerifyJob: (instanceId: string, jobId: string) =>
    apiRequest<void>(`/pbs-instances/${instanceId}/verify-jobs/${encodeURIComponent(jobId)}`, { method: "DELETE" }),

  remoteCapabilities: (instanceId: string) =>
    apiRequest<JobCapabilitiesOut>(`/pbs-instances/${instanceId}/remotes/capabilities`),
  remotes: (instanceId: string) => apiRequest<PbsRemoteOut[]>(`/pbs-instances/${instanceId}/remotes`),
  createRemote: (instanceId: string, body: PbsRemoteCreateRequest) =>
    apiRequest<PbsRemoteOut>(`/pbs-instances/${instanceId}/remotes`, { method: "POST", body }),
  updateRemote: (instanceId: string, name: string, body: PbsRemoteUpdateRequest) =>
    apiRequest<PbsRemoteOut>(`/pbs-instances/${instanceId}/remotes/${encodeURIComponent(name)}`, {
      method: "PUT",
      body,
    }),
  deleteRemote: (instanceId: string, name: string) =>
    apiRequest<void>(`/pbs-instances/${instanceId}/remotes/${encodeURIComponent(name)}`, { method: "DELETE" }),

  syncJobCapabilities: (instanceId: string) =>
    apiRequest<JobCapabilitiesOut>(`/pbs-instances/${instanceId}/sync-jobs/capabilities`),
  syncJobs: (instanceId: string, datastoreName: string) =>
    apiRequest<PbsSyncJobOut[]>(
      `/pbs-instances/${instanceId}/datastores/${encodeURIComponent(datastoreName)}/sync-jobs`,
    ),
  createSyncJob: (instanceId: string, datastoreName: string, body: PbsSyncJobCreateRequest) =>
    apiRequest<PbsSyncJobOut>(
      `/pbs-instances/${instanceId}/datastores/${encodeURIComponent(datastoreName)}/sync-jobs`,
      { method: "POST", body },
    ),
  updateSyncJob: (instanceId: string, jobId: string, body: PbsSyncJobUpdateRequest) =>
    apiRequest<PbsSyncJobOut>(`/pbs-instances/${instanceId}/sync-jobs/${encodeURIComponent(jobId)}`, {
      method: "PUT",
      body,
    }),
  deleteSyncJob: (instanceId: string, jobId: string) =>
    apiRequest<void>(`/pbs-instances/${instanceId}/sync-jobs/${encodeURIComponent(jobId)}`, { method: "DELETE" }),
};

export const tasksApi = {
  // Cross-instance task history (Jobs pages) -- the per-instance
  // pbsInstancesApi.tasks(...) above stays untouched, real tested surface.
  list: (options: {
    instanceIds: string[];
    taskTypes?: string[];
    status?: string;
    limit?: number;
    startedAfter?: string;
    startedBefore?: string;
  }) => {
    const params = new URLSearchParams();
    for (const id of options.instanceIds) params.append("pbs_instance_ids", id);
    for (const type of options.taskTypes ?? []) params.append("task_types", type);
    if (options.status) params.set("status", options.status);
    if (options.limit) params.set("limit", String(options.limit));
    if (options.startedAfter) params.set("started_after", options.startedAfter);
    if (options.startedBefore) params.set("started_before", options.startedBefore);
    return apiRequest<TaskRunOut[]>(`/tasks?${params.toString()}`);
  },
};

export const pveClustersApi = {
  list: () => apiRequest<PveClusterOut[]>("/pve-clusters"),
  get: (id: string) => apiRequest<PveClusterOut>(`/pve-clusters/${id}`),
  create: (body: PveClusterCreateRequest) =>
    apiRequest<PveClusterOut>("/pve-clusters", { method: "POST", body }),
  update: (id: string, body: PveClusterUpdateRequest) =>
    apiRequest<PveClusterOut>(`/pve-clusters/${id}`, { method: "PATCH", body }),
  probeTls: (host: string, port: number) =>
    apiRequest<TlsProbeResponse>("/pve-clusters/probe-tls", { method: "POST", body: { host, port } }),
  testConnection: (id: string) =>
    apiRequest<TestConnectionResponse>(`/pve-clusters/${id}/test-connection`, { method: "POST" }),
  requiredPrivileges: () => apiRequest<RequiredPrivilegesOut>("/pve-clusters/required-privileges"),
  disable: (id: string) => apiRequest<PveClusterOut>(`/pve-clusters/${id}/disable`, { method: "POST" }),
  enable: (id: string) => apiRequest<PveClusterOut>(`/pve-clusters/${id}/enable`, { method: "POST" }),
  remove: (id: string) => apiRequest<void>(`/pve-clusters/${id}`, { method: "DELETE" }),
  nodes: (id: string) => apiRequest<PveNodeOut[]>(`/pve-clusters/${id}/nodes`),
  storages: (id: string, node: string) =>
    apiRequest<PveStorageOut[]>(`/pve-clusters/${id}/nodes/${encodeURIComponent(node)}/storages`),
  guests: (id: string) => apiRequest<PveGuestOut[]>(`/pve-clusters/${id}/guests`),
  pools: (id: string) => apiRequest<PvePoolOut[]>(`/pve-clusters/${id}/pools`),
  checkVmid: (id: string, vmid: number) =>
    apiRequest<PveVmidCheckOut>(`/pve-clusters/${id}/vmid-check?vmid=${vmid}`),
  backupJobs: (id: string) => apiRequest<VzdumpJobOut[]>(`/pve-clusters/${id}/backup-jobs`),
  runBackupJob: (id: string, jobId: string) =>
    apiRequest<BackupRunOut>(`/pve-clusters/${id}/backup-jobs/${encodeURIComponent(jobId)}/run`, { method: "POST" }),
  taskProgress: (id: string, node: string, upid: string, start: number) =>
    apiRequest<PveTaskProgressOut>(
      `/pve-clusters/${id}/tasks/${encodeURIComponent(node)}/${encodeURIComponent(upid)}?start=${start}`,
    ),
  backupJobCapabilities: (id: string) => apiRequest<JobCapabilitiesOut>(`/pve-clusters/${id}/backup-jobs/capabilities`),
  createBackupJob: (id: string, body: VzdumpJobCreateRequest) =>
    apiRequest<VzdumpJobOut>(`/pve-clusters/${id}/backup-jobs`, { method: "POST", body }),
  updateBackupJob: (id: string, jobId: string, body: VzdumpJobUpdateRequest) =>
    apiRequest<VzdumpJobOut>(`/pve-clusters/${id}/backup-jobs/${encodeURIComponent(jobId)}`, {
      method: "PUT",
      body,
    }),
  deleteBackupJob: (id: string, jobId: string) =>
    apiRequest<void>(`/pve-clusters/${id}/backup-jobs/${encodeURIComponent(jobId)}`, { method: "DELETE" }),
  enableBackupJob: (id: string, jobId: string) =>
    apiRequest<VzdumpJobOut>(`/pve-clusters/${id}/backup-jobs/${encodeURIComponent(jobId)}/enable`, { method: "POST" }),
  /** `reenableAfterHours` (1-168): RESURGAM enables the job again
   * automatically after that long; omit/null to disable indefinitely. */
  disableBackupJob: (id: string, jobId: string, reenableAfterHours?: number | null) =>
    apiRequest<VzdumpJobOut>(`/pve-clusters/${id}/backup-jobs/${encodeURIComponent(jobId)}/disable`, {
      method: "POST",
      body: reenableAfterHours ? { reenable_after_hours: reenableAfterHours } : undefined,
    }),
  /** Set or change the re-enable timer of an already disabled job,
   * counted from now (1-168 hours). Does not change the job in PVE now. */
  setBackupJobReenable: (id: string, jobId: string, reenableAfterHours: number) =>
    apiRequest<VzdumpJobOut>(`/pve-clusters/${id}/backup-jobs/${encodeURIComponent(jobId)}/reenable-schedule`, {
      method: "PUT",
      body: { reenable_after_hours: reenableAfterHours },
    }),
  cancelBackupJobReenable: (id: string, jobId: string) =>
    apiRequest<void>(`/pve-clusters/${id}/backup-jobs/${encodeURIComponent(jobId)}/reenable-schedule`, {
      method: "DELETE",
    }),
  // An explicit customer-scope override only -- a job's default scope is
  // inherited from its target datastore/namespace, not set here.
  assignBackupJobCustomer: (id: string, jobId: string, customerId: string) =>
    apiRequest<VzdumpJobOut>(`/pve-clusters/${id}/backup-jobs/${encodeURIComponent(jobId)}/assign-customer`, {
      method: "POST",
      body: { customer_id: customerId },
    }),
  unassignBackupJobCustomer: (id: string, jobId: string) =>
    apiRequest<VzdumpJobOut>(`/pve-clusters/${id}/backup-jobs/${encodeURIComponent(jobId)}/unassign-customer`, {
      method: "POST",
    }),
  // ADR 0019: generates (or rotates) this cluster's resurgam-lab-agent
  // bearer token -- shown exactly once by the backend, never re-readable.
  rotateLabAgentToken: (id: string) =>
    apiRequest<LabAgentTokenOut>(`/pve-clusters/${id}/lab-agent-token`, { method: "POST" }),
  // Recovery Lab compute setup wizard: live readiness checklist, pickers,
  // two safe auto-fixes (each audited server-side) and the agent install info.
  labReadiness: (id: string, opts: { nodes?: string[]; overlayStorageId?: string } = {}) => {
    const params = new URLSearchParams();
    (opts.nodes ?? []).forEach((node) => params.append("nodes", node));
    if (opts.overlayStorageId) params.set("overlay_storage_id", opts.overlayStorageId);
    const query = params.toString();
    return apiRequest<LabReadinessOut>(`/pve-clusters/${id}/lab-readiness${query ? `?${query}` : ""}`);
  },
  sdnZones: (id: string) => apiRequest<PveSdnZoneOut[]>(`/pve-clusters/${id}/sdn-zones`),
  bridges: (id: string, node: string) =>
    apiRequest<PveBridgeOut[]>(`/pve-clusters/${id}/nodes/${encodeURIComponent(node)}/bridges`),
  storageConfig: (id: string, opts: { snippetsCapable?: boolean } = {}) =>
    apiRequest<PveStorageConfigOut[]>(
      `/pve-clusters/${id}/storage-config${opts.snippetsCapable ? "?snippets_capable=true" : ""}`,
    ),
  templates: (id: string) => apiRequest<PveTemplateOut[]>(`/pve-clusters/${id}/templates`),
  createLabOverlayStorage: (id: string, body: { storage_id: string; path?: string; nodes: string[] }) =>
    apiRequest<LabSetupFixOut>(`/pve-clusters/${id}/lab-setup/overlay-storage`, { method: "POST", body }),
  addLabStorageContent: (id: string, body: { storage_id: string; content_type: "snippets" | "images" }, nodes: string[] = []) => {
    const params = new URLSearchParams();
    nodes.forEach((node) => params.append("nodes", node));
    const query = params.toString();
    return apiRequest<LabSetupFixOut>(`/pve-clusters/${id}/lab-setup/storage-content${query ? `?${query}` : ""}`, {
      method: "POST",
      body,
    });
  },
  labAgentInstall: (id: string, overlayStorageId: string, overlayDir: string) =>
    apiRequest<LabAgentInstallOut>(
      `/pve-clusters/${id}/lab-setup/agent-install?overlay_storage_id=${encodeURIComponent(overlayStorageId)}&overlay_dir=${encodeURIComponent(overlayDir)}`,
    ),
  // Lab packages shipped with this RESURGAM (manual install / template build).
  labDownloads: () => apiRequest<LabArtifactsOut>("/pve-clusters/lab-setup/downloads"),
  downloadLabArtifact: (downloadPath: string, filename: string) => downloadFile(downloadPath, filename),
  // Guided one-time SSH session: default address, host key (no credentials
  // sent), then a job that installs the agent or builds the template.
  labSshTarget: (id: string, node: string) =>
    apiRequest<SshTargetOut>(`/pve-clusters/${id}/lab-setup/nodes/${encodeURIComponent(node)}/ssh-target`),
  labSshHostKey: (id: string, body: { node: string; host: string; port: number }) =>
    apiRequest<SshHostKeyOut>(`/pve-clusters/${id}/lab-setup/ssh/host-key`, { method: "POST", body }),
  startLabAgentInstall: (id: string, body: LabAgentInstallJobRequest) =>
    apiRequest<LabSetupJobOut>(`/pve-clusters/${id}/lab-setup/agent-install-jobs`, { method: "POST", body }),
  startLabTemplateBuild: (id: string, body: LabTemplateBuildJobRequest) =>
    apiRequest<LabSetupJobOut>(`/pve-clusters/${id}/lab-setup/template-build-jobs`, { method: "POST", body }),
  createLabPairing: (id: string, body: { node: string; overlay_storage_id?: string; overlay_dir?: string }) =>
    apiRequest<LabNodePairingOut>(`/pve-clusters/${id}/lab-setup/pairings`, { method: "POST", body }),
  labPairing: (id: string, pairingId: string) =>
    apiRequest<LabNodePairingOut>(`/pve-clusters/${id}/lab-setup/pairings/${pairingId}`),
  startLabAgentJob: (id: string, body: LabAgentSetupJobCreate) =>
    apiRequest<LabSetupJobOut>(`/pve-clusters/${id}/lab-setup/agent-jobs`, { method: "POST", body }),
  labSetupJob: (id: string, jobId: string) =>
    apiRequest<LabSetupJobOut>(`/pve-clusters/${id}/lab-setup/jobs/${jobId}`),
  labTemplateBuildScript: (
    id: string,
    opts: { node: string; storage: string; vmid?: number | null; imageSource: string; nodeImage?: string | null },
  ) => {
    const params = new URLSearchParams({ node: opts.node, storage: opts.storage, image_source: opts.imageSource });
    if (opts.vmid) params.set("vmid", String(opts.vmid));
    if (opts.nodeImage) params.set("node_image", opts.nodeImage);
    return apiRequest<LabTemplateBuildScriptOut>(`/pve-clusters/${id}/lab-setup/template-build-script?${params}`);
  },
  labNextVmid: (id: string) => apiRequest<{ vmid: number }>(`/pve-clusters/${id}/lab-setup/next-vmid`),
};

export const applicationGroupsApi = {
  list: () => apiRequest<ApplicationGroupOut[]>("/application-groups"),
  get: (id: string) => apiRequest<ApplicationGroupOut>(`/application-groups/${id}`),
  create: (body: ApplicationGroupCreateRequest) =>
    apiRequest<ApplicationGroupOut>("/application-groups", { method: "POST", body }),
  update: (id: string, body: ApplicationGroupUpdateRequest) =>
    apiRequest<ApplicationGroupOut>(`/application-groups/${id}`, { method: "PATCH", body }),
  remove: (id: string) => apiRequest<void>(`/application-groups/${id}`, { method: "DELETE" }),
};

export const restoreTestPlansApi = {
  create: (body: RestoreTestPlanCreateRequest) =>
    apiRequest<RestoreTestPlanOut>("/lab-runs", { method: "POST", body }),
  list: () => apiRequest<RestoreTestPlanOut[]>("/lab-runs"),
  get: (id: string) => apiRequest<RestoreTestPlanOut>(`/lab-runs/${id}`),
  // Returns the plaintext appliance run token exactly once (ADR 0019) --
  // never persisted, never returned again by `get`/`list`.
  execute: (id: string) => apiRequest<RestoreTestPlanExecuteOut>(`/lab-runs/${id}/execute`, { method: "POST" }),
  poll: (id: string) => apiRequest<RestoreTestPlanOut>(`/lab-runs/${id}/poll`, { method: "POST" }),
  cleanup: (id: string) => apiRequest<RestoreTestPlanOut>(`/lab-runs/${id}/cleanup`, { method: "POST" }),
};

export const restorePlansApi = {
  create: (body: RestorePlanCreateRequest) => apiRequest<RestorePlanOut>("/restore-plans", { method: "POST", body }),
  list: () => apiRequest<RestorePlanOut[]>("/restore-plans"),
  get: (id: string) => apiRequest<RestorePlanOut>(`/restore-plans/${id}`),
  recheck: (id: string) => apiRequest<RestorePlanOut>(`/restore-plans/${id}/recheck`, { method: "POST" }),
  // EPIC 9 (manifest §11.2): four-eyes approval and PVE-API execution.
  approve: (id: string) => apiRequest<RestorePlanOut>(`/restore-plans/${id}/approve`, { method: "POST" }),
  reject: (id: string, reason: string) =>
    apiRequest<RestorePlanOut>(`/restore-plans/${id}/reject`, { method: "POST", body: { reason } }),
  execute: (id: string) => apiRequest<RestorePlanOut>(`/restore-plans/${id}/execute`, { method: "POST" }),
  pollExecution: (id: string) =>
    apiRequest<RestorePlanOut>(`/restore-plans/${id}/execution/poll`, { method: "POST" }),
  // Admin capability (raised directly by the user): stops a live PVE
  // restore task mid-run. Real risk (a disk write can be interrupted
  // partway through) -- the frontend gates this behind an explicit typed
  // confirmation, not just a click.
  abortExecution: (id: string) =>
    apiRequest<RestorePlanOut>(`/restore-plans/${id}/execution/abort`, { method: "POST" }),
  // Instant Recovery (ADR 0030): move the running VM's disks to storage, or discard it.
  moveInstantRecovery: (id: string, bwlimitKib: number | null) =>
    apiRequest<RestorePlanOut>(`/restore-plans/${id}/instant-recovery/move`, {
      method: "POST",
      body: { bwlimit_kib: bwlimitKib },
    }),
  discardInstantRecovery: (id: string) =>
    apiRequest<RestorePlanOut>(`/restore-plans/${id}/instant-recovery/discard`, {
      method: "POST",
      body: { confirm: true },
    }),
};

export const customersApi = {
  list: () => apiRequest<CustomerOut[]>("/customers"),
  get: (id: string) => apiRequest<CustomerOut>(`/customers/${id}`),
  create: (body: CustomerCreateRequest) => apiRequest<CustomerOut>("/customers", { method: "POST", body }),
  update: (id: string, body: CustomerUpdateRequest) =>
    apiRequest<CustomerOut>(`/customers/${id}`, { method: "PATCH", body }),
  disable: (id: string) => apiRequest<CustomerOut>(`/customers/${id}/disable`, { method: "POST" }),
  enable: (id: string) => apiRequest<CustomerOut>(`/customers/${id}/enable`, { method: "POST" }),
  exportCsv: () => downloadFile("/customers/export", "customers.csv"),
  importCsv: (file: File) => uploadFile<CustomerImportResult>("/customers/import", file),
};

export const datastoresApi = {
  list: () => apiRequest<DatastoreOut[]>("/datastores"),
  get: (id: string) => apiRequest<DatastoreOut>(`/datastores/${id}`),
  assignCustomer: (id: string, customerId: string, role: DatastoreRole = "primary") =>
    apiRequest<DatastoreOut>(`/datastores/${id}/assign-customer`, {
      method: "POST",
      body: { customer_id: customerId, role },
    }),
  unassignCustomer: (id: string) =>
    apiRequest<DatastoreOut>(`/datastores/${id}/unassign-customer`, { method: "POST" }),
  remove: (id: string) => apiRequest<void>(`/datastores/${id}`, { method: "DELETE" }),
  backupGroups: (id: string, options?: { includeSuperseded?: boolean }) =>
    apiRequest<BackupGroupOut[]>(
      `/datastores/${id}/backup-groups${options?.includeSuperseded ? "?include_superseded=true" : ""}`,
    ),
  usage: (id: string) => apiRequest<DatastoreUsageOut>(`/datastores/${id}/usage`),
  /** Store (or replace) the PBS encryption keyfile content; write-only, never read back. */
};

export const inventoryApi = {
  snapshots: (groupId: string) => apiRequest<BackupSnapshotOut[]>(`/backup-groups/${groupId}/snapshots`),
  assignProtectionPolicy: (groupId: string, protectionPolicyId: string) =>
    apiRequest<BackupGroupOut>(`/backup-groups/${groupId}/assign-policy`, {
      method: "POST",
      body: { protection_policy_id: protectionPolicyId },
    }),
  unassignProtectionPolicy: (groupId: string) =>
    apiRequest<BackupGroupOut>(`/backup-groups/${groupId}/unassign-policy`, { method: "POST" }),
  assignCustomer: (groupId: string, customerId: string) =>
    apiRequest<BackupGroupOut>(`/backup-groups/${groupId}/assign-customer`, {
      method: "POST",
      body: { customer_id: customerId },
    }),
  unassignCustomer: (groupId: string) =>
    apiRequest<BackupGroupOut>(`/backup-groups/${groupId}/unassign-customer`, { method: "POST" }),
  // Manifest §13 (ADR 0016): per-VM File Indexing opt-in, in place of a
  // customer-wide-only switch.
  enableFileIndex: (groupId: string) =>
    apiRequest<BackupGroupOut>(`/backup-groups/${groupId}/enable-file-index`, { method: "POST" }),
  disableFileIndex: (groupId: string) =>
    apiRequest<BackupGroupOut>(`/backup-groups/${groupId}/disable-file-index`, { method: "POST" }),
  setFileIndexExcludePatterns: (groupId: string, patterns: string[]) =>
    apiRequest<BackupGroupOut>(`/backup-groups/${groupId}/file-index-exclude-patterns`, {
      method: "POST",
      body: { patterns },
    }),
  defaultFileIndexExcludePatterns: () =>
    apiRequest<FileIndexDefaultExcludePatternsOut>("/file-index-default-exclude-patterns"),
  // Guest-exec File Indexing (ADR 0021): the 3-way method choice,
  // alongside the exclude-patterns/enable-disable endpoints above.
  setFileIndexMethod: (groupId: string, method: FileIndexMethod, guestExecLivePveClusterId?: string | null) =>
    apiRequest<BackupGroupOut>(`/backup-groups/${groupId}/file-index-method`, {
      method: "POST",
      body: { method, guest_exec_live_pve_cluster_id: guestExecLivePveClusterId ?? null },
    }),
};

export const fileRestoreApi = {
  // ADR 0013: listing and downloading a snapshot's files now run as an
  // async job (out-of-process worker) instead of a synchronous request --
  // createJob returns immediately with status="pending"; the caller polls
  // getJob until it reaches a terminal status.
  createJob: (groupId: string, snapshotId: string, jobType: "list" | "download", path: string) =>
    apiRequest<FileRestoreJobOut>(`/backup-groups/${groupId}/snapshots/${snapshotId}/files/jobs`, {
      method: "POST",
      body: { job_type: jobType, path },
    }),
  // Folder / multi-selection download: the selected files and directories as ONE ZIP.
  createArchiveJob: (groupId: string, snapshotId: string, paths: string[]) =>
    apiRequest<FileRestoreJobOut>(`/backup-groups/${groupId}/snapshots/${snapshotId}/files/jobs`, {
      method: "POST",
      body: { job_type: "archive", paths },
    }),
  getJob: (jobId: string) => apiRequest<FileRestoreJobOut>(`/file-restore/jobs/${jobId}`),
  downloadTicket: (jobId: string) =>
    apiRequest<DownloadTicketOut>(`/file-restore/jobs/${jobId}/download-ticket`, { method: "POST" }),
  // Saves a completed download/archive job's content via a short-lived ticket link the browser
  // navigates to, so a multi-GB ZIP streams straight to disk instead of being held in the page
  // as a Blob (what `downloadFile` does).
  saveJobContent: async (jobId: string) => {
    const ticket = await fileRestoreApi.downloadTicket(jobId);
    startBrowserDownload(ticket.download_url);
  },
  capability: () => apiRequest<CapabilityReportOut>("/file-restore/capability"),
  // Admin visibility (raised directly by the user, alongside File Index's
  // own job list): every job the caller's scope can see.
  listJobs: (options?: { status?: string; jobType?: string; limit?: number }) => {
    const params = new URLSearchParams();
    if (options?.status) params.set("status", options.status);
    if (options?.jobType) params.set("job_type", options.jobType);
    if (options?.limit) params.set("limit", String(options.limit));
    const qs = params.toString();
    return apiRequest<FileRestoreJobOut[]>(`/file-restore/jobs${qs ? `?${qs}` : ""}`);
  },
  // Only a still-pending job can actually be cancelled -- see the
  // backend endpoint's own docstring for why a running one can't be.
  cancelJob: (jobId: string) => apiRequest<FileRestoreJobOut>(`/file-restore/jobs/${jobId}/cancel`, { method: "POST" }),
};

export const fileIndexApi = {
  // Manifest §13 (ADR 0016): a full-tree metadata index, opt-in per
  // customer -- separate job queue/worker from fileRestoreApi's
  // on-demand list/download jobs above (see worker/file_index_worker.py).
  createJob: (groupId: string, snapshotId: string) =>
    apiRequest<FileIndexJobOut>(`/backup-groups/${groupId}/snapshots/${snapshotId}/index-jobs`, { method: "POST" }),
  createBulkJob: (groupId: string) =>
    apiRequest<BulkFileIndexJobResult>(`/backup-groups/${groupId}/index-jobs/bulk`, { method: "POST" }),
  getJob: (jobId: string) => apiRequest<FileIndexJobOut>(`/file-index/jobs/${jobId}`),
  listJobs: (options?: {
    backupGroupId?: string;
    backupSnapshotId?: string;
    status?: FileIndexJobStatus;
    trigger?: FileIndexJobTrigger;
    limit?: number;
  }) => {
    const params = new URLSearchParams();
    if (options?.backupGroupId) params.set("backup_group_id", options.backupGroupId);
    if (options?.backupSnapshotId) params.set("backup_snapshot_id", options.backupSnapshotId);
    if (options?.status) params.set("status", options.status);
    if (options?.trigger) params.set("trigger", options.trigger);
    if (options?.limit) params.set("limit", String(options.limit));
    const qs = params.toString();
    return apiRequest<FileIndexJobOut[]>(`/file-index/jobs${qs ? `?${qs}` : ""}`);
  },
  cancelJob: (jobId: string) => apiRequest<FileIndexJobOut>(`/file-index/jobs/${jobId}/cancel`, { method: "POST" }),
  browse: (options: { backupGroupId: string; backupSnapshotId: string; path: string }) => {
    const params = new URLSearchParams({
      backup_group_id: options.backupGroupId,
      backup_snapshot_id: options.backupSnapshotId,
      path: options.path,
    });
    return apiRequest<FileIndexEntryOut[]>(`/file-index/browse?${params.toString()}`);
  },
  search: (options: {
    customerId: string;
    backupGroupId?: string;
    backupSnapshotId?: string;
    path?: string;
    filename?: string;
    type?: string;
    limit?: number;
    offset?: number;
  }) => {
    const params = new URLSearchParams();
    params.set("customer_id", options.customerId);
    if (options.backupGroupId) params.set("backup_group_id", options.backupGroupId);
    if (options.backupSnapshotId) params.set("backup_snapshot_id", options.backupSnapshotId);
    if (options.path) params.set("path", options.path);
    if (options.filename) params.set("filename", options.filename);
    if (options.type) params.set("type", options.type);
    if (options.limit) params.set("limit", String(options.limit));
    if (options.offset) params.set("offset", String(options.offset));
    return apiRequest<FileIndexSearchResponse>(`/file-index/search?${params.toString()}`);
  },
  snapshotStatus: (backupGroupId: string) =>
    apiRequest<SnapshotIndexStatusOut>(`/file-index/snapshot-status?backup_group_id=${backupGroupId}`),
};

export const fileIndexSettingsApi = {
  get: (customerId: string) => apiRequest<CustomerFileIndexSettingsOut>(`/customers/${customerId}/file-index-settings`),
  create: (customerId: string, body: CustomerFileIndexSettingsCreateRequest) =>
    apiRequest<CustomerFileIndexSettingsOut>(`/customers/${customerId}/file-index-settings`, {
      method: "POST",
      body,
    }),
  update: (customerId: string, body: CustomerFileIndexSettingsUpdateRequest) =>
    apiRequest<CustomerFileIndexSettingsOut>(`/customers/${customerId}/file-index-settings`, {
      method: "PATCH",
      body,
    }),
  remove: (customerId: string) =>
    apiRequest<void>(`/customers/${customerId}/file-index-settings`, { method: "DELETE" }),
};

export const protectionPoliciesApi = {
  list: () => apiRequest<ProtectionPolicyOut[]>("/protection-policies"),
  get: (id: string) => apiRequest<ProtectionPolicyOut>(`/protection-policies/${id}`),
  create: (body: ProtectionPolicyCreateRequest) =>
    apiRequest<ProtectionPolicyOut>("/protection-policies", { method: "POST", body }),
  remove: (id: string) => apiRequest<void>(`/protection-policies/${id}`, { method: "DELETE" }),
};

export const pricingPlansApi = {
  list: () => apiRequest<PricingPlanOut[]>("/pricing-plans"),
  get: (id: string) => apiRequest<PricingPlanOut>(`/pricing-plans/${id}`),
  create: (body: PricingPlanCreateRequest) =>
    apiRequest<PricingPlanOut>("/pricing-plans", { method: "POST", body }),
  update: (id: string, body: PricingPlanUpdateRequest) =>
    apiRequest<PricingPlanOut>(`/pricing-plans/${id}`, { method: "PATCH", body }),
  remove: (id: string) => apiRequest<void>(`/pricing-plans/${id}`, { method: "DELETE" }),
};

export const alarmsApi = {
  list: () => apiRequest<AlarmOut[]>("/alarms"),
};

export const billingApi = {
  list: () => apiRequest<CustomerBillingOut[]>("/billing/customers"),
  get: (customerId: string) => apiRequest<CustomerBillingOut>(`/billing/customers/${customerId}`),
  exportCsv: () => downloadFile("/billing/customers/export", "billing-usage.csv"),
};

function _recoveryEvidenceQuery(options: {
  customerId: string;
  dateFrom?: string;
  dateTo?: string;
  framework?: boolean;
}): string {
  const params = new URLSearchParams();
  params.set("customer_id", options.customerId);
  if (options.dateFrom) params.set("date_from", options.dateFrom);
  if (options.dateTo) params.set("date_to", options.dateTo);
  if (options.framework) params.set("framework", "true");
  return params.toString();
}

export const reportingApi = {
  recoveryEvidence: (options: { customerId: string; dateFrom?: string; dateTo?: string; framework?: boolean }) =>
    apiRequest<RecoveryEvidenceReportOut>(`/reporting/recovery-evidence?${_recoveryEvidenceQuery(options)}`),
  recoveryEvidencePdfPath: (options: { customerId: string; dateFrom?: string; dateTo?: string; framework?: boolean }) =>
    `/reporting/recovery-evidence/pdf?${_recoveryEvidenceQuery(options)}`,
  recoveryEvidenceHistory: (customerId: string) =>
    apiRequest<ReportHistoryOut[]>(`/reporting/recovery-evidence/history?customer_id=${customerId}`),
  recoveryEvidenceHistoryPdfPath: (historyId: string) => `/reporting/recovery-evidence/history/${historyId}/pdf`,
};

export const instanceSettingsApi = {
  get: () => apiRequest<InstanceSettingsOut>("/instance-settings"),
  update: (minReportRetentionDays: number, collectorPollIntervalMinutes: number) =>
    apiRequest<InstanceSettingsOut>("/instance-settings", {
      method: "PUT",
      body: {
        min_report_retention_days: minReportRetentionDays,
        collector_poll_interval_minutes: collectorPollIntervalMinutes,
      },
    }),
};

export const mailSettingsApi = {
  get: () => apiRequest<MailSettingsOut>("/mail-settings"),
  update: (body: UpdateMailSettingsRequest) => apiRequest<MailSettingsOut>("/mail-settings", { method: "PUT", body }),
  sendTest: (to: string) => apiRequest<SendTestEmailResponse>("/mail-settings/test", { method: "POST", body: { to } }),
};

export const legApplicabilityApi = {
  list: (customerId: string) => apiRequest<LegApplicabilityOut[]>(`/customers/${customerId}/leg-applicability`),
  update: (customerId: string, leg: string, applicable: boolean) =>
    apiRequest<LegApplicabilityOut>(`/customers/${customerId}/leg-applicability/${leg}`, {
      method: "PUT",
      body: { applicable },
    }),
};

export const alertMailerApi = {
  get: (customerId: string) => apiRequest<AlertMailerSettingsOut>(`/customers/${customerId}/alert-mailer`),
  update: (customerId: string, body: UpdateAlertMailerSettingsRequest) =>
    apiRequest<AlertMailerSettingsOut>(`/customers/${customerId}/alert-mailer`, { method: "PUT", body }),
  sendTest: (customerId: string, recipients?: string[]) =>
    apiRequest<SendTestAlertResponse>(`/customers/${customerId}/alert-mailer/test`, {
      method: "POST",
      body: recipients ? { recipients } : {},
    }),
};

export const notificationSettingsApi = {
  get: (customerId: string) => apiRequest<NotificationSettingsOut>(`/customers/${customerId}/notification-settings`),
  update: (customerId: string, body: UpdateNotificationSettingsRequest) =>
    apiRequest<NotificationSettingsOut>(`/customers/${customerId}/notification-settings`, { method: "PUT", body }),
};

export const complianceAttestationsApi = {
  listForDatastore: (datastoreId: string) =>
    apiRequest<ComplianceAttestationOut[]>(`/datastores/${datastoreId}/compliance-attestations`),
  createForDatastore: (datastoreId: string, body: { dimension: string; value: ComplianceAttestationValue; notes?: string }) =>
    apiRequest<ComplianceAttestationOut>(`/datastores/${datastoreId}/compliance-attestations`, { method: "POST", body }),
  listForCustomer: (customerId: string) =>
    apiRequest<ComplianceAttestationOut[]>(`/customers/${customerId}/compliance-attestations`),
  createForCustomer: (customerId: string, body: { dimension: string; value: ComplianceAttestationValue; notes?: string }) =>
    apiRequest<ComplianceAttestationOut>(`/customers/${customerId}/compliance-attestations`, { method: "POST", body }),
};

export const credentialsApi = {
  list: () => apiRequest<CredentialOut[]>("/credentials"),
  rotate: (secretReferenceId: string, newValue: string) =>
    apiRequest<CredentialOut>(`/credentials/${secretReferenceId}/rotate`, {
      method: "POST",
      body: { new_value: newValue },
    }),
};

export const brandingApi = {
  // Deliberately no auth-dependent behavior here -- the backend's
  // GET /branding and GET /branding/logo are unauthenticated on purpose
  // (the login page needs them before anyone is signed in), so calling
  // these before login is expected, not an edge case.
  get: () => apiRequest<BrandingSettingsOut>("/branding"),
  logoUrl: () => `${API_BASE_URL}/branding/logo`,
  update: (params: {
    logo?: File;
    primaryColor?: string;
    accentColor?: string;
    /** "" = back to "RESURGAM". */
    appName?: string;
  }) => {
    const formData = new FormData();
    if (params.logo) formData.append("logo", params.logo);
    if (params.appName !== undefined) {
      if (params.appName.trim() === "") formData.append("reset_app_name", "true");
      else formData.append("app_name", params.appName.trim());
    }
    if (params.primaryColor !== undefined) formData.append("primary_color", params.primaryColor);
    if (params.accentColor !== undefined) formData.append("accent_color", params.accentColor);
    return putFormData<BrandingSettingsOut>("/branding", formData);
  },
  /** Back to RESURGAM's own look: no custom logo, default colors. */
  reset: () => apiRequest<BrandingSettingsOut>("/branding", { method: "DELETE" }),
  removeLogo: () => apiRequest<BrandingSettingsOut>("/branding/logo", { method: "DELETE" }),
};

export const integrationsApi = {
  getOidc: () => apiRequest<OidcSettingsOut>("/integrations/oidc"),
  updateOidc: (body: UpdateOidcSettingsRequest) =>
    apiRequest<OidcSettingsOut>("/integrations/oidc", { method: "PUT", body }),
  testOidc: (issuer: string) =>
    apiRequest<OidcDiscoveryTestResponse>("/integrations/oidc/test", { method: "POST", body: { issuer } }),
};

export const systemApi = {
  // Unauthenticated on the backend (version string only).
  version: () => apiRequest<VersionResponse>("/version"),
};

export const updatesApi = {
  overview: () => apiRequest<UpdatesOverviewOut>("/updates"),
  bundles: () => apiRequest<UpdateBundleOut[]>("/updates/bundles"),
  uploadBundle: (file: File, onProgress?: (loaded: number, total: number) => void) =>
    uploadRawWithProgress<UpdateBundleOut>(`/updates/bundles?filename=${encodeURIComponent(file.name)}`, file, onProgress),
  deleteBundle: (name: string) =>
    apiRequest<void>(`/updates/bundles/${encodeURIComponent(name)}`, { method: "DELETE" }),
  requests: () => apiRequest<UpdateRequestStatusOut[]>("/updates/requests"),
  createRequest: (action: UpdateAction, bundle: string) =>
    apiRequest<UpdateRequestStatusOut>("/updates/requests", { method: "POST", body: { action, bundle } }),
  requestStatus: (requestId: string) =>
    apiRequest<UpdateRequestStatusOut>(`/updates/requests/${encodeURIComponent(requestId)}`),
  history: () => apiRequest<UpdateHistoryEntryOut[]>("/updates/history"),
  backups: () => apiRequest<UpdateBackupOut[]>("/updates/backups"),
};

export const updateSourceApi = {
  get: () => apiRequest<UpdateSourceOut>("/updates/source"),
  save: (body: UpdateSourceSettingsRequest) => apiRequest<UpdateSourceOut>("/updates/source", { method: "PUT", body }),
  remove: () => apiRequest<void>("/updates/source", { method: "DELETE" }),
  test: (body: UpdateSourceTestRequest) =>
    apiRequest<UpdateSourceTestOut>("/updates/source/test", { method: "POST", body }),
  check: () => apiRequest<UpdateSourceOut>("/updates/source/check", { method: "POST" }),
  startDownload: (version: string) =>
    apiRequest<UpdateSourceDownloadOut>("/updates/source/downloads", { method: "POST", body: { version } }),
  download: (downloadId: string) =>
    apiRequest<UpdateSourceDownloadOut>(`/updates/source/downloads/${encodeURIComponent(downloadId)}`),
};

export const configBackupApi = {
  /** The configuration document (no secrets), ADR 0024. */
  export: () => apiRequest<ConfigBackupDocument>("/config-backup"),
  /** Validate (dryRun) or apply a previously exported document. */
  import: (document: unknown, dryRun: boolean) =>
    apiRequest<ConfigImportResult>(`/config-backup/import?dry_run=${dryRun ? "true" : "false"}`, {
      method: "POST",
      body: document,
    }),
};

/** Disk Mounts (ADR 0025): a backup's disks shared read-only by a helper VM. */
export const diskMountsApi = {
  list: (activeOnly = false) => apiRequest<DiskMountOut[]>(`/disk-mounts${activeOnly ? "?active_only=true" : ""}`),
  get: (id: string) => apiRequest<DiskMountOut>(`/disk-mounts/${id}`),
  create: (body: DiskMountCreateRequest) =>
    apiRequest<DiskMountCreatedOut>("/disk-mounts", { method: "POST", body }),
  approve: (id: string) => apiRequest<DiskMountOut>(`/disk-mounts/${id}/approve`, { method: "POST" }),
  reject: (id: string, reason: string) =>
    apiRequest<DiskMountOut>(`/disk-mounts/${id}/reject`, { method: "POST", body: { reason } }),
  extend: (id: string, hours: number) =>
    apiRequest<DiskMountOut>(`/disk-mounts/${id}/extend`, { method: "POST", body: { hours } }),
  stop: (id: string) => apiRequest<DiskMountOut>(`/disk-mounts/${id}/stop`, { method: "POST" }),
  forceFinish: (id: string) => apiRequest<DiskMountOut>(`/disk-mounts/${id}/force-finish`, { method: "POST" }),
  snapshotDisks: (snapshotId: string) =>
    apiRequest<SnapshotDiskOut[]>(`/disk-mounts/snapshot-disks?backup_snapshot_id=${encodeURIComponent(snapshotId)}`),
  placement: () => apiRequest<DiskMountPlacementOut[]>("/disk-mounts/placement"),
  nodes: (clusterId: string) => apiRequest<string[]>(`/disk-mounts/placement/${clusterId}/nodes`),
  bridges: (clusterId: string, node: string) =>
    apiRequest<PveBridgeOut[]>(
      `/disk-mounts/placement/${clusterId}/nodes/${encodeURIComponent(node)}/bridges`,
    ),
  setHelperTemplate: (clusterId: string, vmid: number | null) =>
    apiRequest<DiskMountPlacementOut>(`/disk-mounts/placement/${clusterId}/helper-template`, {
      method: "PUT",
      body: { helper_template_vmid: vmid },
    }),
  startHelperTemplateBuild: (clusterId: string, body: LabTemplateBuildJobRequest) =>
    apiRequest<LabSetupJobOut>(`/pve-clusters/${clusterId}/lab-setup/share-helper-template-build-jobs`, {
      method: "POST",
      body,
    }),
};

/** Personal API tokens (docs/adr/0029). All of these need a login
 * session -- an API token can't manage tokens. */
export const apiTokensApi = {
  mine: () => apiRequest<ApiTokenOut[]>("/api-tokens/mine"),
  grantable: () => apiRequest<ApiTokenGrantableOut>("/api-tokens/grantable"),
  create: (body: ApiTokenCreateRequest) => apiRequest<ApiTokenCreatedOut>("/api-tokens", { method: "POST", body }),
  /** Every user's tokens -- platform admin only. */
  all: () => apiRequest<ApiTokenOut[]>("/api-tokens"),
  revoke: (id: string) => apiRequest<void>(`/api-tokens/${encodeURIComponent(id)}`, { method: "DELETE" }),
};

/** Encryption key store (ADR 0031, stage 2). */
export const encryptionKeysApi = {
  list: () => apiRequest<EncryptionKeyOut[]>("/encryption-keys"),
  missing: () => apiRequest<string[]>("/encryption-keys/missing"),
  upload: (body: { keyfile_content: string; label?: string; customer_id?: string | null }) =>
    apiRequest<EncryptionKeyOut>("/encryption-keys", { method: "POST", body }),
  update: (id: string, body: { label?: string; customer_id?: string | null; clear_customer?: boolean }) =>
    apiRequest<EncryptionKeyOut>(`/encryption-keys/${id}`, { method: "PATCH", body }),
  remove: (id: string, force = false) =>
    apiRequest<void>(`/encryption-keys/${id}${force ? "?force=true" : ""}`, { method: "DELETE" }),
  download: (id: string, confirmation: StepUpConfirmation) =>
    postDownload(`/encryption-keys/${id}/download`, { confirmation }, "pbs-key.json"),
  keySheet: (id: string, confirmation: StepUpConfirmation) =>
    postDownload(`/encryption-keys/${id}/key-sheet`, { confirmation }, "pbs-key-sheet.pdf"),
  confirmCopy: (id: string) => apiRequest<EncryptionKeyOut>(`/encryption-keys/${id}/confirm-copy`, { method: "POST" }),
  exportAll: (passphrase: string, confirmation: StepUpConfirmation) =>
    postDownload("/encryption-keys/export", { passphrase, confirmation }, "resurgam-encryption-keys.json"),
  importAll: (exportContent: string, passphrase: string) =>
    apiRequest<EncryptionKeyImportOut>("/encryption-keys/import", {
      method: "POST",
      body: { export_content: exportContent, passphrase },
    }),
  tenantPermission: (customerId: string) =>
    apiRequest<TenantKeyDownloadPermissionOut>(`/encryption-keys/tenants/${customerId}/download-permission`),
  setTenantPermission: (customerId: string, allowed: boolean) =>
    apiRequest<TenantKeyDownloadPermissionOut>(`/encryption-keys/tenants/${customerId}/download-permission`, {
      method: "PUT",
      body: { allowed },
    }),
};
