import type { ReactNode } from "react";
import { Route, Routes } from "react-router-dom";

import { AppShell } from "./components/AppShell";
import { ProtectedRoute } from "./components/ProtectedRoute";
import { NAV_TREE, IMPLEMENTED_PATHS, type NavSection } from "./nav/navTree";
import { AuditLogPage } from "./pages/AuditLogPage";
import { AuthCallbackPage } from "./pages/AuthCallbackPage";
import { BillingPage } from "./pages/BillingPage";
import { BackupGapsPage } from "./pages/BackupGapsPage";
import { CapacityTrendsPage } from "./pages/CapacityTrendsPage";
import { CredentialsPage } from "./pages/CredentialsPage";
import { CustomerPanelPage } from "./pages/CustomerPanelPage";
import { EncryptionKeysPage } from "./pages/EncryptionKeysPage";
import { CustomersPage } from "./pages/CustomersPage";
import { DatastoresPage } from "./pages/DatastoresPage";
import { DiskMountsPage } from "./pages/DiskMountsPage";
import { FileWorkspacePage } from "./pages/FileWorkspacePage";
import { RestoreCenterPage } from "./pages/RestoreCenterPage";
import { IntegrationsPage } from "./pages/IntegrationsPage";
import { ZabbixMonitoringPage } from "./pages/ZabbixMonitoringPage";
import { JobsBackupPage } from "./pages/JobsBackupPage";
import { JobsPruneGcPage } from "./pages/JobsPruneGcPage";
import { JobsReplicationSyncPage } from "./pages/JobsReplicationSyncPage";
import { JobsVerificationPage } from "./pages/JobsVerificationPage";
import { LoginPage } from "./pages/LoginPage";
import { NotAvailablePage } from "./pages/NotAvailablePage";
import { OverviewPage } from "./pages/OverviewPage";
import { PbsInstancesPage } from "./pages/PbsInstancesPage";
import { PricingPlansPage } from "./pages/PricingPlansPage";
import { ProtectedWorkloadsPage } from "./pages/ProtectedWorkloadsPage";
import { ProtectionPoliciesPage } from "./pages/ProtectionPoliciesPage";
import { ProfilePage } from "./pages/ProfilePage";
import { PveClustersPage } from "./pages/PveClustersPage";
import { RecoveryEvidencePage } from "./pages/RecoveryEvidencePage";
import { RecoveryLabPage } from "./pages/RecoveryLabPage";
import { SystemJobsPage } from "./pages/SystemJobsPage";
import { SystemSettingsPage } from "./pages/SystemSettingsPage";
import { TasksPage } from "./pages/TasksPage";
import { UsersPage } from "./pages/UsersPage";

const PAGE_BY_PATH: Record<string, ReactNode> = {
  "/": <OverviewPage />,
  "/infrastructure/pbs-instances": <PbsInstancesPage />,
  "/infrastructure/datastores": <DatastoresPage />,
  "/infrastructure/pve-targets": <PveClustersPage />,
  "/backups/vms": <ProtectedWorkloadsPage titleKey="nav.backupsVms" backupType="vm" />,
  "/backups/containers": <ProtectedWorkloadsPage titleKey="nav.backupsContainers" backupType="ct" />,
  "/backups/hosts": <ProtectedWorkloadsPage titleKey="nav.backupsHosts" backupType="host" />,
  "/jobs/backup": <JobsBackupPage />,
  "/jobs/verification": <JobsVerificationPage />,
  "/jobs/prune-gc": <JobsPruneGcPage />,
  "/jobs/replication-sync": <JobsReplicationSyncPage />,
  "/history/backup": <TasksPage titleKey="history.backupTitle" taskTypes={["backup"]} />,
  "/history/verification": <TasksPage titleKey="history.verificationTitle" taskTypes={["verify"]} />,
  "/history/prune-gc": <TasksPage titleKey="history.pruneGcTitle" taskTypes={["prune", "garbage_collection"]} />,
  "/history/replication-sync": <TasksPage titleKey="history.replicationSyncTitle" taskTypes={["sync"]} showSyncJobs />,
  "/administration/customers": <CustomersPage />,
  "/administration/protection-policies": <ProtectionPoliciesPage />,
  "/administration/pricing-plans": <PricingPlansPage />,
  "/administration/users-roles": <UsersPage />,
  "/administration/system-jobs": <SystemJobsPage />,
  "/administration/credentials": <CredentialsPage />,
  "/administration/encryption-keys": <EncryptionKeysPage />,
  "/administration/integrations": <IntegrationsPage />,
  "/administration/zabbix": <ZabbixMonitoringPage />,
  "/administration/system-settings": <SystemSettingsPage />,
  "/administration/audit-log": <AuditLogPage />,
  "/reporting/capacity-trends": <CapacityTrendsPage />,
  "/reporting/backup-gaps": <BackupGapsPage />,
  "/reporting/tenant-usage": <BillingPage />,
  "/reporting/recovery-evidence": <RecoveryEvidencePage />,
  "/recovery/restore-center": <RestoreCenterPage />,
  "/recovery/files": <FileWorkspacePage />,
  "/recovery/disk-mounts": <DiskMountsPage />,
  "/recovery/recovery-lab": <RecoveryLabPage />,
};

function flattenNavPaths(sections: NavSection[]): string[] {
  return sections.flatMap((section) => [
    ...(section.path ? [section.path] : []),
    ...(section.children?.map((child) => child.path) ?? []),
  ]);
}

export function App() {
  const navPaths = flattenNavPaths(NAV_TREE);

  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/auth/callback" element={<AuthCallbackPage />} />

      <Route
        element={
          <ProtectedRoute>
            <AppShell />
          </ProtectedRoute>
        }
      >
        {navPaths.map((path) => (
          <Route
            key={path}
            path={path === "/" ? "/" : path}
            element={IMPLEMENTED_PATHS.has(path) ? PAGE_BY_PATH[path] : <NotAvailablePage />}
          />
        ))}
        <Route path="/profile" element={<ProfilePage />} />
        <Route path="/customers/:customerId/panel" element={<CustomerPanelPage />} />
      </Route>
    </Routes>
  );
}
