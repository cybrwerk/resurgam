import type { ComponentType, SVGProps } from "react";

import {
  ArchiveIcon,
  ChartIcon,
  CogIcon,
  HistoryIcon,
  HomeIcon,
  LifebuoyIcon,
  ServerIcon,
  ShieldIcon,
} from "../components/icons";

export interface NavLink {
  labelKey: string;
  path: string;
}

export interface NavSection {
  labelKey: string;
  /** Always shown next to the label; the only visible cue in the
   * collapsed icon rail. */
  icon: ComponentType<SVGProps<SVGSVGElement>>;
  path?: string;
  children?: NavLink[];
  /** A real UX-audit finding (2026-09-23): every nav entry showed for
   * every signed-in user regardless of role, including sections whose
   * every real action 403s for anyone but a platform_admin (confirmed
   * against the backend's own `require_role(RoleKey.PLATFORM_ADMIN)`
   * gates: Infrastruktur's write actions, and every endpoint behind
   * Administration -- Kunden/Schutzrichtlinien/Preispläne/Benutzer &
   * Rollen/Credentials/Integrationen/System-Jobs/Systemeinstellungen/
   * Audit Log). Backups/Jobs/Recovery/Reporting are deliberately left
   * ungated here: those use per-customer scope checks
   * (`require_restore_access`/`visible_customer_ids`), not a blanket
   * platform_admin requirement, so any authenticated role can
   * legitimately see something there. */
  requiresRole?: "platform_admin";
}

/** Navigation tree per docs/design/RESURGAM-Styleguide.md §8.1, which
 * supersedes the manifest's §26.2 tree for frontend specifics. Entries
 * without a real page yet still appear (routed to a shared "not part of
 * this milestone yet" placeholder) — the IA is visible as designed, not
 * silently missing, but nothing here is faked as working. */
export const NAV_TREE: NavSection[] = [
  { labelKey: "nav.overview", icon: HomeIcon, path: "/" },
  {
    labelKey: "nav.infrastructure",
    icon: ServerIcon,
    requiresRole: "platform_admin",
    children: [
      { labelKey: "nav.infrastructurePbsInstances", path: "/infrastructure/pbs-instances" },
      { labelKey: "nav.infrastructureDatastores", path: "/infrastructure/datastores" },
      { labelKey: "nav.infrastructurePveTargets", path: "/infrastructure/pve-targets" },
    ],
  },
  {
    labelKey: "nav.backups",
    icon: ArchiveIcon,
    children: [
      { labelKey: "nav.backupsVms", path: "/backups/vms" },
      { labelKey: "nav.backupsContainers", path: "/backups/containers" },
      { labelKey: "nav.backupsHosts", path: "/backups/hosts" },
      { labelKey: "nav.backupsFileBackup", path: "/backups/file-backup" },
    ],
  },
  {
    // Job *administration* (docs/adr/0023 built the backend/live-CRUD;
    // this is the Veeam-like cross-entity panel that authors job
    // definitions -- distinct from "History" below, which is the
    // pre-existing task-run log this section used to be named "Jobs"
    // and pointed to. Same four categories, reused labels/paths --
    // only what they point to changed.
    labelKey: "nav.jobs",
    icon: CogIcon,
    children: [
      { labelKey: "nav.jobsBackup", path: "/jobs/backup" },
      { labelKey: "nav.jobsVerification", path: "/jobs/verification" },
      { labelKey: "nav.jobsPruneGc", path: "/jobs/prune-gc" },
      { labelKey: "nav.jobsReplicationSync", path: "/jobs/replication-sync" },
    ],
  },
  {
    // The task-run history/protocol view -- what "Jobs" used to mean
    // before job *administration* (above) took that name. Same
    // `TasksPage` component and task-type filtering as before, just
    // renamed and moved to /history/* so the two "Jobs" meanings don't
    // collide.
    labelKey: "nav.history",
    icon: HistoryIcon,
    children: [
      { labelKey: "nav.historyBackup", path: "/history/backup" },
      { labelKey: "nav.historyVerification", path: "/history/verification" },
      { labelKey: "nav.historyPruneGc", path: "/history/prune-gc" },
      { labelKey: "nav.historyReplicationSync", path: "/history/replication-sync" },
    ],
  },
  {
    labelKey: "nav.recovery",
    icon: LifebuoyIcon,
    children: [
      { labelKey: "nav.recoveryRestoreCenter", path: "/recovery/restore-center" },
      { labelKey: "nav.recoveryFiles", path: "/recovery/files" },
      { labelKey: "nav.recoveryDiskMounts", path: "/recovery/disk-mounts" },
      { labelKey: "nav.recoveryLab", path: "/recovery/recovery-lab" },
    ],
  },
  {
    labelKey: "nav.reporting",
    icon: ChartIcon,
    children: [
      { labelKey: "nav.reportingCapacityTrends", path: "/reporting/capacity-trends" },
      { labelKey: "nav.reportingTenantUsage", path: "/reporting/tenant-usage" },
      { labelKey: "nav.reportingRecoveryEvidence", path: "/reporting/recovery-evidence" },
    ],
  },
  {
    labelKey: "nav.administration",
    icon: ShieldIcon,
    requiresRole: "platform_admin",
    children: [
      { labelKey: "nav.administrationCustomers", path: "/administration/customers" },
      { labelKey: "nav.administrationProtectionPolicies", path: "/administration/protection-policies" },
      { labelKey: "nav.administrationPricingPlans", path: "/administration/pricing-plans" },
      { labelKey: "nav.administrationUsersRoles", path: "/administration/users-roles" },
      { labelKey: "nav.administrationCredentials", path: "/administration/credentials" },
      { labelKey: "nav.administrationEncryptionKeys", path: "/administration/encryption-keys" },
      { labelKey: "nav.administrationIntegrations", path: "/administration/integrations" },
      { labelKey: "nav.administrationZabbix", path: "/administration/zabbix" },
      { labelKey: "nav.administrationSystemJobs", path: "/administration/system-jobs" },
      { labelKey: "nav.administrationSystemSettings", path: "/administration/system-settings" },
      { labelKey: "nav.administrationAuditLog", path: "/administration/audit-log" },
    ],
  },
];

export interface NavLocation {
  section: NavSection;
  link?: NavLink;
}

/** Where `pathname` sits in the tree -- the source of the top bar's
 * breadcrumb, so pages don't each hand-maintain "Section / Page". Exact
 * child match first, then the longest child path that prefixes it (a
 * detail route under a list page). Pages outside the tree return null. */
export function findNavLocation(pathname: string): NavLocation | null {
  let best: NavLocation | null = null;
  let bestLength = -1;
  for (const section of NAV_TREE) {
    if (section.path !== undefined && section.path === pathname) {
      return { section };
    }
    for (const link of section.children ?? []) {
      if (link.path === pathname) {
        return { section, link };
      }
      if (pathname.startsWith(`${link.path}/`) && link.path.length > bestLength) {
        best = { section, link };
        bestLength = link.path.length;
      }
    }
  }
  return best;
}

/** Paths with a real, working page in this slice — everything else in
 * NAV_TREE routes to the NotAvailable placeholder. The four History
 * entries are real: they share one TasksPage component, each filtering
 * the same underlying task history by task_type rather than being fake.
 * The four Jobs entries (job *administration*, docs/adr/0023) are real
 * too, each a cross-entity panel for one job category. */
export const IMPLEMENTED_PATHS = new Set<string>([
  "/",
  "/infrastructure/pbs-instances",
  "/infrastructure/datastores",
  "/infrastructure/pve-targets",
  "/backups/vms",
  "/backups/containers",
  "/backups/hosts",
  "/jobs/backup",
  "/jobs/verification",
  "/jobs/prune-gc",
  "/jobs/replication-sync",
  "/history/backup",
  "/history/verification",
  "/history/prune-gc",
  "/history/replication-sync",
  "/administration/customers",
  "/administration/protection-policies",
  "/administration/pricing-plans",
  "/administration/users-roles",
  "/administration/system-jobs",
  "/administration/zabbix",
  "/administration/credentials",
  "/administration/encryption-keys",
  "/administration/integrations",
  "/administration/system-settings",
  "/administration/audit-log",
  "/reporting/capacity-trends",
  "/reporting/tenant-usage",
  "/reporting/recovery-evidence",
  "/recovery/restore-center",
  "/recovery/files",
  "/recovery/disk-mounts",
  "/recovery/recovery-lab",
]);
