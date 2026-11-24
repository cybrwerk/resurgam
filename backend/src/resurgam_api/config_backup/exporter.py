"""Build the configuration export document (ADR 0024).

Never puts a secret into the document: no API token secrets (only the
token ID, derived exactly like the PBS/PVE list endpoints derive it), no
password hashes, no TOTP/WebAuthn/recovery-code material, no OIDC client
secret, no SMTP password, no datastore encryption key, no lab-agent token
hash, no update-source access token. Where such a secret exists the document only says *that* it existed
(`*_set` flags), so the import can list it under "to re-enter".
"""

import base64
import socket
import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from resurgam_api.api.v1._connection_helpers import stored_token_id
from resurgam_api.api.v1.datastores import datastore_encryption
from resurgam_api.config import Settings
from resurgam_api.inventory.file_index_defaults import default_file_index_exclude_patterns
from resurgam_api.models.application_group import ApplicationGroup, ApplicationGroupMember
from resurgam_api.models.billing import PricingPlan
from resurgam_api.models.branding_settings import BrandingSettings
from resurgam_api.models.customer_file_index_settings import CustomerFileIndexSettings
from resurgam_api.models.customer_notification_settings import CustomerNotificationSettings
from resurgam_api.models.datastore import Datastore, DatastoreCustomerAssignment
from resurgam_api.models.identity import Role, ScopeType, User, UserRoleAssignment
from resurgam_api.models.instance_settings import InstanceSettings
from resurgam_api.models.inventory import BackupGroup, ProtectionPolicy
from resurgam_api.models.leg_applicability import ThreeTwoOneOneZeroLegSetting
from resurgam_api.models.mail_settings import MailSettings
from resurgam_api.models.oidc_settings import OidcSettings
from resurgam_api.models.pbs import PbsInstance
from resurgam_api.models.pve import (
    PveBackupJobCustomerAssignment,
    PveCluster,
    PveClusterDatastore,
    PveClusterPbsInstance,
)
from resurgam_api.models.tenancy import Customer
from resurgam_api.models.update_source import UpdateSourceSettings
from resurgam_api.schemas.config_backup import (
    CONFIG_BACKUP_FORMAT,
    ApplicationGroupEntry,
    ApplicationGroupMemberEntry,
    BackupGroupSettingsEntry,
    BackupJobCustomerAssignmentEntry,
    BrandingSection,
    ConfigBackupDocument,
    ConfigBackupSections,
    ConnectionRef,
    CustomerEntry,
    DatastoreAssignmentEntry,
    DatastoreEntry,
    DatastoreRef,
    FileIndexSettingsEntry,
    InstanceSettingsSection,
    LegApplicabilityEntry,
    MailSettingsSection,
    NotificationSettingsEntry,
    OidcSettingsSection,
    PbsInstanceEntry,
    PolicyRef,
    PricingPlanEntry,
    ProtectionPolicyEntry,
    PveClusterEntry,
    RoleAssignmentEntry,
    RoleScopeRef,
    UpdateSourceSection,
    UserEntry,
)
from resurgam_api.version import running_version

# Logos up to this size are embedded (base64 grows them by a third); larger
# ones are left out with a note so the document stays a manageable size.
MAX_EMBEDDED_LOGO_BYTES = 512 * 1024


class _Lookup:
    """id -> natural-key maps, built once per export."""

    def __init__(self, db: Session) -> None:
        self.customers = {c.id: c for c in db.query(Customer).all()}
        self.pbs = {p.id: p for p in db.query(PbsInstance).all()}
        self.pve = {p.id: p for p in db.query(PveCluster).all()}
        self.datastores = {d.id: d for d in db.query(Datastore).all()}
        self.policies = {p.id: p for p in db.query(ProtectionPolicy).all()}

    def slug(self, customer_id: uuid.UUID | None) -> str | None:
        customer = self.customers.get(customer_id) if customer_id else None
        return customer.slug if customer else None

    def pbs_ref(self, pbs_id: uuid.UUID | None) -> ConnectionRef | None:
        instance = self.pbs.get(pbs_id) if pbs_id else None
        return ConnectionRef(host=instance.host, port=instance.port) if instance else None

    def pve_ref(self, pve_id: uuid.UUID | None) -> ConnectionRef | None:
        cluster = self.pve.get(pve_id) if pve_id else None
        return ConnectionRef(host=cluster.host, port=cluster.port) if cluster else None

    def datastore_ref(self, datastore_id: uuid.UUID | None) -> DatastoreRef | None:
        datastore = self.datastores.get(datastore_id) if datastore_id else None
        if datastore is None:
            return None
        instance = self.pbs[datastore.pbs_instance_id]
        return DatastoreRef(pbs_host=instance.host, pbs_port=instance.port, datastore=datastore.name)

    def group_ref_fields(self, group: BackupGroup) -> dict | None:
        ref = self.datastore_ref(group.datastore_id)
        if ref is None:
            return None
        return {
            **ref.model_dump(),
            "namespace": group.namespace,
            "backup_type": group.backup_type,
            "backup_id": group.backup_id,
        }


def _instance_settings(db: Session) -> InstanceSettingsSection | None:
    row = db.query(InstanceSettings).first()
    if row is None:
        return None
    return InstanceSettingsSection(
        min_report_retention_days=row.min_report_retention_days,
        collector_poll_interval_minutes=row.collector_poll_interval_minutes,
        require_mfa_all_local_users=row.require_mfa_all_local_users,
    )


def _branding(db: Session) -> BrandingSection | None:
    row = db.query(BrandingSettings).first()
    if row is None:
        return None
    section = BrandingSection(primary_color=row.primary_color, accent_color=row.accent_color, app_name=row.app_name)
    if row.logo is not None:
        section.logo_content_type = row.logo_content_type
        if len(row.logo) <= MAX_EMBEDDED_LOGO_BYTES:
            section.logo_base64 = base64.b64encode(row.logo).decode("ascii")
        else:
            section.logo_omitted_reason = (
                f"The logo is {len(row.logo)} bytes, larger than the {MAX_EMBEDDED_LOGO_BYTES}-byte limit for "
                "embedding -- upload it again after the import (System Settings -> Branding)."
            )
    return section


def _update_source(db: Session) -> UpdateSourceSection | None:
    row = db.query(UpdateSourceSettings).first()
    if row is None:
        return None
    return UpdateSourceSection(
        provider=row.provider,
        repository_url=row.repository_url,
        tls_verify=row.tls_verify,
        include_prereleases=row.include_prereleases,
        auto_check=row.auto_check,
        check_interval_hours=row.check_interval_hours,
        token_set=row.token_ref_id is not None,
    )


def _mail(db: Session) -> MailSettingsSection | None:
    row = db.query(MailSettings).first()
    if row is None:
        return None
    return MailSettingsSection(
        smtp_host=row.smtp_host,
        smtp_port=row.smtp_port,
        smtp_username=row.smtp_username,
        use_tls=row.use_tls,
        from_address=row.from_address,
        from_name=row.from_name,
        enabled=row.enabled,
        smtp_password_set=row.smtp_password_ref_id is not None,
    )


def _oidc(db: Session) -> OidcSettingsSection | None:
    row = db.query(OidcSettings).first()
    if row is None:
        return None
    return OidcSettingsSection(
        issuer=row.issuer,
        client_id=row.client_id,
        redirect_uri=row.redirect_uri,
        enabled=row.enabled,
        client_secret_set=row.client_secret_ref_id is not None,
    )


def _customers(db: Session, lookup: _Lookup) -> list[CustomerEntry]:
    notifications = {n.customer_id: n for n in db.query(CustomerNotificationSettings).all()}
    file_index = {f.customer_id: f for f in db.query(CustomerFileIndexSettings).all()}
    legs: dict[uuid.UUID, list[LegApplicabilityEntry]] = {}
    for leg in db.query(ThreeTwoOneOneZeroLegSetting).order_by(ThreeTwoOneOneZeroLegSetting.leg).all():
        legs.setdefault(leg.customer_id, []).append(LegApplicabilityEntry(leg=leg.leg, applicable=leg.applicable))

    out = []
    for customer in sorted(lookup.customers.values(), key=lambda c: c.slug):
        n = notifications.get(customer.id)
        f = file_index.get(customer.id)
        out.append(
            CustomerEntry(
                name=customer.name,
                slug=customer.slug,
                debitor_number=customer.debitor_number,
                match_code=customer.match_code,
                disabled=customer.disabled,
                report_retention_days=customer.report_retention_days,
                notification_settings=NotificationSettingsEntry(
                    report_notification_email=n.report_notification_email,
                    report_notifications_enabled=n.report_notifications_enabled,
                    alarm_notification_email=n.alarm_notification_email,
                    alarm_notifications_enabled=n.alarm_notifications_enabled,
                    billing_notification_email=n.billing_notification_email,
                    billing_notifications_enabled=n.billing_notifications_enabled,
                    alarm_min_severity=n.alarm_min_severity,
                    alarm_notify_resolved=n.alarm_notify_resolved,
                    alarm_reminder_hours=n.alarm_reminder_hours,
                )
                if n
                else None,
                file_index_settings=FileIndexSettingsEntry(
                    enabled=f.enabled,
                    retention_days=f.retention_days,
                    schedule_enabled=f.schedule_enabled,
                    schedule_interval_hours=f.schedule_interval_hours,
                    window_start_time=f.window_start_time,
                    window_end_time=f.window_end_time,
                    guest_exec_pve_cluster=lookup.pve_ref(f.guest_exec_pve_cluster_id),
                    guest_exec_target_node=f.guest_exec_target_node,
                    guest_exec_target_storage=f.guest_exec_target_storage,
                    full_reindex_every_n_runs=f.full_reindex_every_n_runs,
                )
                if f
                else None,
                leg_applicability=legs.get(customer.id, []),
            )
        )
    return out


def _pbs_instances(db: Session, settings: Settings, lookup: _Lookup) -> list[PbsInstanceEntry]:
    datastores_by_instance: dict[uuid.UUID, list[Datastore]] = {}
    for datastore in lookup.datastores.values():
        if datastore.removed_from_pbs_at is None:
            datastores_by_instance.setdefault(datastore.pbs_instance_id, []).append(datastore)
    out = []
    for instance in sorted(lookup.pbs.values(), key=lambda i: (i.host, i.port)):
        out.append(
            PbsInstanceEntry(
                display_name=instance.display_name,
                location=instance.location,
                host=instance.host,
                port=instance.port,
                tls_trust_mode=instance.tls_trust_mode,
                tls_fingerprint_sha256=instance.tls_fingerprint_sha256,
                token_id=stored_token_id(db, settings, instance.api_token_secret_ref_id),
                disabled=instance.disabled,
                datastores=[
                    DatastoreEntry(
                        name=d.name,
                        path=d.path,
                        comment=d.comment,
                        # ADR 0031: keys live in the key store now; "set" means this
                        # datastore holds encrypted backups whose key RESURGAM had.
                        encryption_key_set=d.encryption_key_secret_ref_id is not None
                        or bool(datastore_encryption(db, d.id)["key_fingerprints_in_use"]),
                    )
                    for d in sorted(datastores_by_instance.get(instance.id, []), key=lambda d: d.name)
                ],
            )
        )
    return out


def _pve_clusters(db: Session, settings: Settings, lookup: _Lookup) -> list[PveClusterEntry]:
    pbs_links: dict[uuid.UUID, list[ConnectionRef]] = {}
    for link in db.query(PveClusterPbsInstance).all():
        ref = lookup.pbs_ref(link.pbs_instance_id)
        if ref:
            pbs_links.setdefault(link.pve_cluster_id, []).append(ref)
    ds_links: dict[uuid.UUID, list[DatastoreRef]] = {}
    for link in db.query(PveClusterDatastore).all():
        ref = lookup.datastore_ref(link.datastore_id)
        if ref:
            ds_links.setdefault(link.pve_cluster_id, []).append(ref)
    job_assignments: dict[uuid.UUID, list[BackupJobCustomerAssignmentEntry]] = {}
    for assignment in db.query(PveBackupJobCustomerAssignment).order_by(PveBackupJobCustomerAssignment.job_id).all():
        slug = lookup.slug(assignment.customer_id)
        if slug:
            job_assignments.setdefault(assignment.pve_cluster_id, []).append(
                BackupJobCustomerAssignmentEntry(job_id=assignment.job_id, customer_slug=slug)
            )

    out = []
    for cluster in sorted(lookup.pve.values(), key=lambda c: (c.host, c.port)):
        out.append(
            PveClusterEntry(
                display_name=cluster.display_name,
                host=cluster.host,
                port=cluster.port,
                tls_trust_mode=cluster.tls_trust_mode,
                tls_fingerprint_sha256=cluster.tls_fingerprint_sha256,
                token_id=stored_token_id(db, settings, cluster.api_token_secret_ref_id),
                disabled=cluster.disabled,
                allowed_target_storages=cluster.allowed_target_storages,
                restore_network_rules=cluster.restore_network_rules,
                require_restore_approval=cluster.require_restore_approval,
                is_lab_compute=cluster.is_lab_compute,
                lab_sdn_zone_name=cluster.lab_sdn_zone_name,
                lab_appliance_template_vmid=cluster.lab_appliance_template_vmid,
                lab_snippet_storage_id=cluster.lab_snippet_storage_id,
                lab_appliance_uplink_bridge=cluster.lab_appliance_uplink_bridge,
                lab_appliance_uplink_vlan_tag=cluster.lab_appliance_uplink_vlan_tag,
                lab_appliance_uplink_ip_mode=cluster.lab_appliance_uplink_ip_mode or "dhcp",
                lab_appliance_uplink_ip_range_start=cluster.lab_appliance_uplink_ip_range_start,
                lab_appliance_uplink_ip_range_end=cluster.lab_appliance_uplink_ip_range_end,
                lab_appliance_uplink_ip_prefix_length=cluster.lab_appliance_uplink_ip_prefix_length,
                lab_appliance_uplink_gateway=cluster.lab_appliance_uplink_gateway,
                lab_appliance_uplink_dns_servers=cluster.lab_appliance_uplink_dns_servers,
                lab_agent_token_set=cluster.lab_agent_token_hash is not None,
                linked_pbs_instances=sorted(pbs_links.get(cluster.id, []), key=lambda r: (r.host, r.port)),
                linked_datastores=sorted(
                    ds_links.get(cluster.id, []), key=lambda r: (r.pbs_host, r.pbs_port, r.datastore)
                ),
                backup_job_customer_assignments=job_assignments.get(cluster.id, []),
            )
        )
    return out


def _datastore_assignments(db: Session, lookup: _Lookup) -> list[DatastoreAssignmentEntry]:
    out = []
    for assignment in db.query(DatastoreCustomerAssignment).all():
        datastore = lookup.datastores.get(assignment.datastore_id)
        ref = lookup.datastore_ref(assignment.datastore_id)
        slug = lookup.slug(assignment.customer_id)
        if datastore is None or datastore.removed_from_pbs_at is not None or ref is None or slug is None:
            continue
        out.append(DatastoreAssignmentEntry(**ref.model_dump(), customer_slug=slug, role=assignment.role))
    return sorted(out, key=lambda a: (a.pbs_host, a.pbs_port, a.datastore))


def _protection_policies(lookup: _Lookup) -> list[ProtectionPolicyEntry]:
    out = []
    for policy in lookup.policies.values():
        slug = lookup.slug(policy.customer_id)
        if slug is None:
            continue
        out.append(
            ProtectionPolicyEntry(
                customer_slug=slug,
                name=policy.name,
                expected_interval_hours=policy.expected_interval_hours,
                max_time_since_last_backup_hours=policy.max_time_since_last_backup_hours,
                expected_verify_deadline_hours=policy.expected_verify_deadline_hours,
                min_snapshot_count=policy.min_snapshot_count,
                sync_dr_required=policy.sync_dr_required,
            )
        )
    return sorted(out, key=lambda p: (p.customer_slug, p.name))


def _pricing_plans(db: Session, lookup: _Lookup) -> list[PricingPlanEntry]:
    out = [
        PricingPlanEntry(
            customer_slug=lookup.slug(plan.customer_id),
            name=plan.name,
            price_per_vm_month=plan.price_per_vm_month,
            price_per_ct_month=plan.price_per_ct_month,
            price_per_host_month=plan.price_per_host_month,
            price_per_gb_month=plan.price_per_gb_month,
            currency=plan.currency,
        )
        for plan in db.query(PricingPlan).all()
        if plan.customer_id is None or lookup.slug(plan.customer_id) is not None
    ]
    return sorted(out, key=lambda p: (p.customer_slug or "", p.name))


def _scope_ref(lookup: _Lookup, scope_type: str, scope_id: uuid.UUID | None) -> RoleScopeRef | None | bool:
    """The natural-key scope, None for global, or False when the scoped
    object no longer exists (the assignment is then left out)."""
    if scope_type == ScopeType.GLOBAL.value:
        return None
    if scope_type == ScopeType.CUSTOMER.value:
        slug = lookup.slug(scope_id)
        return RoleScopeRef(customer_slug=slug) if slug else False
    if scope_type == ScopeType.PBS_INSTANCE.value:
        ref = lookup.pbs_ref(scope_id)
        return RoleScopeRef(pbs_instance=ref) if ref else False
    if scope_type == ScopeType.DATASTORE.value:
        ref = lookup.datastore_ref(scope_id)
        return RoleScopeRef(datastore=ref) if ref else False
    if scope_type == ScopeType.PVE_CLUSTER.value:
        ref = lookup.pve_ref(scope_id)
        return RoleScopeRef(pve_cluster=ref) if ref else False
    return False


def _users(db: Session, lookup: _Lookup) -> list[UserEntry]:
    roles = {r.id: r.key for r in db.query(Role).all()}
    assignments: dict[uuid.UUID, list[RoleAssignmentEntry]] = {}
    for assignment in db.query(UserRoleAssignment).all():
        scope = _scope_ref(lookup, assignment.scope_type, assignment.scope_id)
        if scope is False:
            continue
        assignments.setdefault(assignment.user_id, []).append(
            RoleAssignmentEntry(
                role=roles[assignment.role_id],
                scope_type=assignment.scope_type,
                scope=scope,
                source=assignment.source,
            )
        )
    out = []
    for user in db.query(User).order_by(User.email).all():
        out.append(
            UserEntry(
                email=user.email,
                display_name=user.display_name,
                external_subject=user.external_subject,
                is_breakglass=user.is_breakglass,
                disabled=user.disabled,
                has_local_password=user.password_hash is not None,
                mfa_required=user.mfa_required,
                role_assignments=sorted(
                    assignments.get(user.id, []), key=lambda a: (a.role.value, a.scope_type.value, str(a.scope))
                ),
            )
        )
    return out


def _backup_group_settings(db: Session, lookup: _Lookup) -> list[BackupGroupSettingsEntry]:
    defaults = default_file_index_exclude_patterns()
    out = []
    groups = db.query(BackupGroup).filter(BackupGroup.superseded_at.is_(None)).all()
    for group in groups:
        non_default = (
            group.protection_policy_id is not None
            or group.customer_id is not None
            or group.file_index_enabled
            or group.file_index_method != "external_mount"
            or group.guest_exec_live_pve_cluster_id is not None
            or list(group.file_index_exclude_patterns or []) != defaults
        )
        ref = lookup.group_ref_fields(group)
        if not non_default or ref is None:
            continue
        policy = lookup.policies.get(group.protection_policy_id) if group.protection_policy_id else None
        policy_slug = lookup.slug(policy.customer_id) if policy else None
        out.append(
            BackupGroupSettingsEntry(
                **ref,
                protection_policy=PolicyRef(customer_slug=policy_slug, name=policy.name) if policy_slug else None,
                customer_slug=lookup.slug(group.customer_id),
                file_index_enabled=group.file_index_enabled,
                file_index_exclude_patterns=list(group.file_index_exclude_patterns or []),
                file_index_method=group.file_index_method,
                guest_exec_live_pve_cluster=lookup.pve_ref(group.guest_exec_live_pve_cluster_id),
            )
        )
    return sorted(out, key=lambda g: (g.pbs_host, g.pbs_port, g.datastore, g.namespace, g.backup_type, g.backup_id))


def _application_groups(db: Session, lookup: _Lookup) -> list[ApplicationGroupEntry]:
    groups_by_id = {g.id: g for g in db.query(BackupGroup).all()}
    members: dict[uuid.UUID, list[ApplicationGroupMemberEntry]] = {}
    for member in db.query(ApplicationGroupMember).order_by(ApplicationGroupMember.startup_order).all():
        group = groups_by_id.get(member.backup_group_id)
        ref = lookup.group_ref_fields(group) if group else None
        if ref is None:
            continue
        members.setdefault(member.application_group_id, []).append(
            ApplicationGroupMemberEntry(
                **ref,
                startup_order=member.startup_order,
                check_types=list(member.check_types or []),
                expected_ip=member.expected_ip,
            )
        )
    out = []
    for app_group in db.query(ApplicationGroup).all():
        slug = lookup.slug(app_group.customer_id)
        if slug is None:
            continue
        out.append(
            ApplicationGroupEntry(
                customer_slug=slug,
                name=app_group.name,
                lab_network_mode=app_group.lab_network_mode,
                lab_subnet_cidr=app_group.lab_subnet_cidr,
                lab_gateway_ip=app_group.lab_gateway_ip,
                lab_dns_ip=app_group.lab_dns_ip,
                lab_dhcp_range_start=app_group.lab_dhcp_range_start,
                lab_dhcp_range_end=app_group.lab_dhcp_range_end,
                lab_schedule_enabled=app_group.lab_schedule_enabled,
                lab_schedule_interval_hours=app_group.lab_schedule_interval_hours,
                lab_schedule_pve_cluster=lookup.pve_ref(app_group.lab_schedule_pve_cluster_id),
                lab_schedule_target_node=app_group.lab_schedule_target_node,
                lab_schedule_target_storage=app_group.lab_schedule_target_storage,
                members=members.get(app_group.id, []),
            )
        )
    return sorted(out, key=lambda g: (g.customer_slug, g.name))


def build_config_backup(db: Session, settings: Settings) -> ConfigBackupDocument:
    lookup = _Lookup(db)
    return ConfigBackupDocument(
        format=CONFIG_BACKUP_FORMAT,
        exported_at=datetime.now(UTC),
        resurgam_version=running_version(),
        instance_name=socket.gethostname() or None,
        sections=ConfigBackupSections(
            instance_settings=_instance_settings(db),
            branding=_branding(db),
            update_source=_update_source(db),
            mail_settings=_mail(db),
            oidc_settings=_oidc(db),
            customers=_customers(db, lookup),
            pbs_instances=_pbs_instances(db, settings, lookup),
            pve_clusters=_pve_clusters(db, settings, lookup),
            datastore_assignments=_datastore_assignments(db, lookup),
            protection_policies=_protection_policies(lookup),
            pricing_plans=_pricing_plans(db, lookup),
            users=_users(db, lookup),
            backup_group_settings=_backup_group_settings(db, lookup),
            application_groups=_application_groups(db, lookup),
        ),
    )
