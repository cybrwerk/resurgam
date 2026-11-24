"""Apply (or dry-run) a configuration export document (ADR 0024).

Objects are matched by natural keys -- customer slug, PBS/PVE host + port,
datastore name within its PBS instance, user email, policy name within its
customer, the customer (or "global") of a pricing plan, a backup group's
PBS identity -- never by internal UUIDs. Matching is additive: existing
objects are updated to the document's values, nothing that is missing
from the document is deleted, and secrets are never touched.

Dry run and apply run the *same* code inside one SAVEPOINT; a dry run
rolls it back at the end, so its summary is exactly what an apply would
do (including objects that only resolve because an earlier section of the
same document creates them).

Imported PBS/PVE connections get a secret-less placeholder token (the
bare token ID, `connectivity.token_format.token_secret_missing`): the
heartbeat reports them as `auth_failed: secret not set`, the Collector
scheduler skips them, and the edit form only needs the secret.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from dataclasses import dataclass, field

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from resurgam_api import update_source
from resurgam_api.api.v1._connection_helpers import retrieve_token
from resurgam_api.config import Settings
from resurgam_api.connectivity.token_format import TOKEN_ID_RE, token_id_from_stored, token_secret_missing
from resurgam_api.errors import ResurgamApiError
from resurgam_api.models.application_group import ApplicationGroup, ApplicationGroupMember
from resurgam_api.models.billing import PricingPlan
from resurgam_api.models.branding_settings import BrandingSettings
from resurgam_api.models.customer_file_index_settings import CustomerFileIndexSettings
from resurgam_api.models.customer_notification_settings import CustomerNotificationSettings
from resurgam_api.models.datastore import Datastore, DatastoreCustomerAssignment
from resurgam_api.models.encryption_key import EncryptionKey
from resurgam_api.models.identity import Role, ScopeType, User, UserRoleAssignment
from resurgam_api.models.instance_settings import InstanceSettings
from resurgam_api.models.inventory import BackupGroup, ProtectionPolicy
from resurgam_api.models.leg_applicability import ThreeTwoOneOneZeroLegSetting
from resurgam_api.models.mail_settings import MailSettings
from resurgam_api.models.oidc_settings import OidcSettings
from resurgam_api.models.pbs import ConnectionHealthStatus, PbsInstance
from resurgam_api.models.pve import (
    PveBackupJobCustomerAssignment,
    PveCluster,
    PveClusterDatastore,
    PveClusterPbsInstance,
)
from resurgam_api.models.secrets import SecretPurpose, SecretReference
from resurgam_api.models.tenancy import Customer
from resurgam_api.models.update_source import UpdateSourceSettings
from resurgam_api.schemas.config_backup import (
    BackupGroupRef,
    ConfigBackupDocument,
    ConfigImportItem,
    ConfigImportResult,
    ConfigImportSectionResult,
    ConnectionRef,
    DatastoreRef,
    ReEnterItem,
    RoleScopeRef,
)
from resurgam_api.secrets.local_provider import LocalEncryptedSecretsProvider
from resurgam_api.updates.release_source import ReleaseSourceError

# Result sections, in the order they are applied. "datastores" is nested in
# the document (under pbs_instances) but reported on its own.
SECTION_ORDER = (
    "instance_settings",
    "branding",
    "update_source",
    "mail_settings",
    "oidc_settings",
    "customers",
    "pbs_instances",
    "datastores",
    "pve_clusters",
    "datastore_assignments",
    "protection_policies",
    "pricing_plans",
    "users",
    "backup_group_settings",
    "application_groups",
)

_PAGES = {
    "pbs": "/infrastructure/pbs-instances",
    "pve": "/infrastructure/pve-targets",
    "datastores": "/infrastructure/datastores",
    "users": "/administration/users-roles",
    "integrations": "/administration/integrations",
    "system": "/administration/system-settings",
}

_RANK = {"skip": 0, "update": 1, "create": 2, "conflict": 3}


class _Ambiguous(Exception):
    """More than one existing object matches a natural key."""


@dataclass
class _Ctx:
    db: Session
    settings: Settings
    actor_user_id: uuid.UUID | None
    dry_run: bool
    items: dict[str, dict[str, ConfigImportItem]] = field(default_factory=lambda: {s: {} for s in SECTION_ORDER})
    reenter: list[ReEnterItem] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    unsynced_skips: int = 0

    def record(self, section: str, key: str, action: str, reason: str | None = None) -> None:
        current = self.items[section].get(key)
        if current is None:
            self.items[section][key] = ConfigImportItem(key=key, action=action, reason=reason)
            return
        if _RANK[action] > _RANK[current.action]:
            current.action = action
        if reason:
            current.reason = f"{current.reason}; {reason}" if current.reason else reason

    def provider(self) -> LocalEncryptedSecretsProvider:
        return LocalEncryptedSecretsProvider(self.db, self.settings.secrets_local_encryption_key)

    def target_id(self, obj) -> str | None:
        return None if self.dry_run else str(obj.id)


def _assign(obj, values: dict) -> list[str]:
    """Set every attribute that differs; returns the changed names."""
    changed = []
    for name, value in values.items():
        if getattr(obj, name) != value:
            setattr(obj, name, value)
            changed.append(name)
    return changed


def _changed_reason(changed: list[str]) -> str:
    return "changed: " + ", ".join(changed)


def _conn_key(host: str, port: int) -> str:
    return f"{host}:{port}"


def _ds_key(ref: DatastoreRef) -> str:
    return f"{_conn_key(ref.pbs_host, ref.pbs_port)}/{ref.datastore}"


def _group_key(ref: BackupGroupRef) -> str:
    ns = ref.namespace or "(root)"
    return f"{_ds_key(ref)}/{ns}/{ref.backup_type}/{ref.backup_id}"


# --- lookups -------------------------------------------------------------------


def _customer(ctx: _Ctx, slug: str | None) -> Customer | None:
    if not slug:
        return None
    return ctx.db.query(Customer).filter_by(slug=slug).one_or_none()


def _one(rows: list):
    if len(rows) > 1:
        raise _Ambiguous()
    return rows[0] if rows else None


def _pbs(ctx: _Ctx, ref: ConnectionRef | None) -> PbsInstance | None:
    if ref is None:
        return None
    return _one(ctx.db.query(PbsInstance).filter_by(host=ref.host, port=ref.port).all())


def _pve(ctx: _Ctx, ref: ConnectionRef | None) -> PveCluster | None:
    if ref is None:
        return None
    return _one(ctx.db.query(PveCluster).filter_by(host=ref.host, port=ref.port).all())


def _datastore(ctx: _Ctx, ref: DatastoreRef | None) -> Datastore | None:
    if ref is None:
        return None
    instance = _pbs(ctx, ConnectionRef(host=ref.pbs_host, port=ref.pbs_port))
    if instance is None:
        return None
    return ctx.db.query(Datastore).filter_by(pbs_instance_id=instance.id, name=ref.datastore).one_or_none()


def _backup_group(ctx: _Ctx, ref: BackupGroupRef) -> BackupGroup | None:
    datastore = _datastore(ctx, ref)
    if datastore is None:
        return None
    return (
        ctx.db.query(BackupGroup)
        .filter_by(
            datastore_id=datastore.id,
            namespace=ref.namespace,
            backup_type=ref.backup_type,
            backup_id=ref.backup_id,
            superseded_at=None,
        )
        .one_or_none()
    )


def _safe(fn, *args):
    """A lookup result, or None + a reason when the natural key is ambiguous."""
    try:
        return fn(*args), None
    except _Ambiguous:
        return None, "more than one existing object matches this host:port -- resolve the duplicate first"


# --- singletons -------------------------------------------------------------------


def _singleton(ctx: _Ctx, section: str, model, values: dict) -> object:
    row = ctx.db.query(model).first()
    if row is None:
        row = model(**values)
        ctx.db.add(row)
        ctx.db.flush()
        ctx.record(section, section, "create")
        return row
    changed = _assign(row, values)
    ctx.record(section, section, "update" if changed else "skip", _changed_reason(changed) if changed else None)
    return row


def _import_instance_settings(ctx: _Ctx, doc: ConfigBackupDocument) -> None:
    section = doc.sections.instance_settings
    if section is not None:
        values = section.model_dump()
        # Older export files don't carry the MFA switch: leave it as it is.
        if values["require_mfa_all_local_users"] is None:
            del values["require_mfa_all_local_users"]
        _singleton(ctx, "instance_settings", InstanceSettings, values)


def _import_branding(ctx: _Ctx, doc: ConfigBackupDocument) -> None:
    section = doc.sections.branding
    if section is None:
        return
    values: dict = {"primary_color": section.primary_color, "accent_color": section.accent_color}
    # Older export files have no app title: absent = keep the current one,
    # an explicit null = back to the default name.
    if "app_name" in section.model_fields_set:
        values["app_name"] = " ".join((section.app_name or "").split()) or None
    if section.logo_base64:
        try:
            values["logo"] = base64.b64decode(section.logo_base64, validate=True)
        except (binascii.Error, ValueError):
            ctx.record("branding", "branding", "conflict", "logo_base64 is not valid base64 -- logo not imported")
        else:
            values["logo_content_type"] = section.logo_content_type
    row = _singleton(ctx, "branding", BrandingSettings, values)
    if section.logo_omitted_reason and row.logo is None:
        ctx.reenter.append(
            ReEnterItem(
                kind="branding_logo",
                target="Branding",
                target_id=None,
                page=_PAGES["system"],
                detail="The logo was too large to embed in the export -- upload it again (System Settings, Branding).",
            )
        )


def _import_update_source(ctx: _Ctx, doc: ConfigBackupDocument) -> None:
    """The release repository and its flags; the access token never travels
    in an export. Like a save in System Settings -> Updates, a stored token is
    removed when the provider or API host changes -- it is never sent to a
    host it was not saved for -- and the cached check result is dropped when
    the repository changes."""
    section = doc.sections.update_source
    if section is None:
        return
    repository_url = section.repository_url.strip()
    try:
        update_source.build_client(section.provider, repository_url, None, section.tls_verify)
    except ReleaseSourceError as exc:
        ctx.record("update_source", "update_source", "conflict", f"{exc.message} -- update source not imported")
        return
    values = {
        "provider": section.provider,
        "repository_url": repository_url,
        "tls_verify": section.tls_verify,
        "include_prereleases": section.include_prereleases,
        "auto_check": section.auto_check,
        "check_interval_hours": section.check_interval_hours,
    }
    row = ctx.db.query(UpdateSourceSettings).first()
    if row is not None:
        if (row.provider, row.repository_url, row.include_prereleases) != (
            section.provider,
            repository_url,
            section.include_prereleases,
        ):
            row.available_releases = None
            row.latest_version = None
            row.last_check_error = None
            row.last_checked_at = None
        if row.token_ref_id is not None and not update_source.same_token_scope(row, section.provider, repository_url):
            ctx.provider().delete(update_source.TOKEN_REFERENCE_KEY)
            row.token_ref_id = None
            ctx.record("update_source", "update_source", "update", "stored access token removed (other host)")
    row = _singleton(ctx, "update_source", UpdateSourceSettings, values)
    if section.token_set and row.token_ref_id is None:
        ctx.reenter.append(
            ReEnterItem(
                kind="update_source_token",
                target=f"Update source {repository_url}",
                target_id=None,
                page=_PAGES["system"],
                detail="Enter the repository's access token again (System Settings, Updates, Update source) and "
                "run the connection test.",
            )
        )


def _import_mail(ctx: _Ctx, doc: ConfigBackupDocument) -> None:
    section = doc.sections.mail_settings
    if section is None:
        return
    values = section.model_dump(exclude={"smtp_password_set"})
    row = _singleton(ctx, "mail_settings", MailSettings, values)
    if section.smtp_password_set and row.smtp_password_ref_id is None:
        ctx.reenter.append(
            ReEnterItem(
                kind="smtp_password",
                target=f"SMTP {section.smtp_host}",
                target_id=None,
                page=_PAGES["system"],
                detail="Enter the SMTP password again (System Settings, Mail) and send a test mail.",
            )
        )


def _import_oidc(ctx: _Ctx, doc: ConfigBackupDocument) -> None:
    section = doc.sections.oidc_settings
    if section is None:
        return
    values = section.model_dump(exclude={"client_secret_set"})
    row = _singleton(ctx, "oidc_settings", OidcSettings, values)
    if section.client_secret_set and row.client_secret_ref_id is None:
        ctx.reenter.append(
            ReEnterItem(
                kind="oidc_client_secret",
                target=f"OIDC {section.client_id}",
                target_id=None,
                page=_PAGES["integrations"],
                detail="Enter the OIDC client secret again (Integrations) and run the discovery test before relying "
                "on SSO.",
            )
        )


# --- customers -------------------------------------------------------------------------


def _import_customers(ctx: _Ctx, doc: ConfigBackupDocument) -> None:
    seen: set[str] = set()
    for entry in doc.sections.customers:
        key = entry.slug
        if key in seen:
            ctx.record("customers", key, "conflict", "the slug appears more than once in the file")
            continue
        seen.add(key)
        customer = _customer(ctx, entry.slug)
        clashes = []
        for column in ("debitor_number", "match_code"):
            other = ctx.db.query(Customer).filter(getattr(Customer, column) == getattr(entry, column)).one_or_none()
            if other is not None and (customer is None or other.id != customer.id):
                clashes.append(f"{column} {getattr(entry, column)!r} is already used by customer '{other.slug}'")
        if clashes:
            ctx.record("customers", key, "conflict", "; ".join(clashes))
            continue
        values = {
            "name": entry.name,
            "debitor_number": entry.debitor_number,
            "match_code": entry.match_code,
            "disabled": entry.disabled,
            "report_retention_days": entry.report_retention_days,
        }
        if customer is None:
            customer = Customer(slug=entry.slug, **values)
            ctx.db.add(customer)
            ctx.db.flush()
            ctx.record("customers", key, "create")
        else:
            changed = _assign(customer, values)
            ctx.record("customers", key, "update" if changed else "skip", _changed_reason(changed) if changed else None)

        if entry.notification_settings is not None:
            values = entry.notification_settings.model_dump()
            row = ctx.db.query(CustomerNotificationSettings).filter_by(customer_id=customer.id).one_or_none()
            if row is None:
                ctx.db.add(CustomerNotificationSettings(customer_id=customer.id, **values))
                ctx.record("customers", key, "update", "notification settings added")
            elif _assign(row, values):
                ctx.record("customers", key, "update", "notification settings changed")

        for leg in entry.leg_applicability:
            row = (
                ctx.db.query(ThreeTwoOneOneZeroLegSetting).filter_by(customer_id=customer.id, leg=leg.leg).one_or_none()
            )
            if row is None:
                ctx.db.add(
                    ThreeTwoOneOneZeroLegSetting(
                        customer_id=customer.id,
                        leg=leg.leg,
                        applicable=leg.applicable,
                        updated_by_user_id=ctx.actor_user_id,
                    )
                )
                ctx.record("customers", key, "update", f"3-2-1-1-0 leg '{leg.leg}' scoping added")
            elif row.applicable != leg.applicable:
                row.applicable = leg.applicable
                row.updated_by_user_id = ctx.actor_user_id
                ctx.record("customers", key, "update", f"3-2-1-1-0 leg '{leg.leg}' scoping changed")
        ctx.db.flush()


def _import_customer_file_index(ctx: _Ctx, doc: ConfigBackupDocument) -> None:
    """Second customer pass: file-index settings reference a PVE cluster,
    which is imported after the customers."""
    for entry in doc.sections.customers:
        if entry.file_index_settings is None:
            continue
        customer = _customer(ctx, entry.slug)
        if customer is None or ctx.items["customers"].get(entry.slug, None) is None:
            continue
        if ctx.items["customers"][entry.slug].action == "conflict":
            continue
        fi = entry.file_index_settings
        cluster, error = _safe(_pve, ctx, fi.guest_exec_pve_cluster)
        note = None
        if fi.guest_exec_pve_cluster is not None and cluster is None:
            note = (
                f"file-index PVE cluster {_conn_key(fi.guest_exec_pve_cluster.host, fi.guest_exec_pve_cluster.port)} "
                f"not found ({error or 'not in this database or the file'}) -- left empty"
            )
        values = fi.model_dump(exclude={"guest_exec_pve_cluster"})
        values["guest_exec_pve_cluster_id"] = cluster.id if cluster else None
        row = ctx.db.query(CustomerFileIndexSettings).filter_by(customer_id=customer.id).one_or_none()
        if row is None:
            ctx.db.add(CustomerFileIndexSettings(customer_id=customer.id, **values))
            ctx.record("customers", entry.slug, "update", "file-index settings added")
        elif _assign(row, values):
            ctx.record("customers", entry.slug, "update", "file-index settings changed")
        if note:
            ctx.record("customers", entry.slug, "update", note)
        ctx.db.flush()


# --- connections ---------------------------------------------------------------


def _placeholder_token(token_id: str | None) -> str:
    """What an imported connection stores instead of a real token: the bare
    token ID (so the edit form keeps it and only the secret is needed), or
    "" when the export had no parseable ID."""
    if token_id and TOKEN_ID_RE.match(token_id.strip()):
        return token_id.strip()
    return ""


def _new_secret_ref(ctx: _Ctx, prefix: str, label: str, token_id: str | None) -> SecretReference:
    reference_key = f"{prefix}-{uuid.uuid4()}"
    ref = SecretReference(
        reference_key=reference_key,
        description=f"{label} (imported from a configuration backup -- secret not set)",
        purpose=SecretPurpose.INFRA_CREDENTIAL,
    )
    ctx.db.add(ref)
    ctx.db.flush()
    ctx.provider().store(reference_key, _placeholder_token(token_id))
    return ref


def _token_note(ctx: _Ctx, secret_ref_id: uuid.UUID, file_token_id: str | None) -> str | None:
    stored = retrieve_token(ctx.db, ctx.settings, secret_ref_id)
    stored_id = token_id_from_stored(stored)
    if file_token_id and stored_id and stored_id != file_token_id:
        return (
            f"token ID in the file ({file_token_id}) differs from the stored one ({stored_id}) -- kept the stored token"
        )
    return None


def _import_pbs_instances(ctx: _Ctx, doc: ConfigBackupDocument) -> None:
    seen: set[str] = set()
    for entry in doc.sections.pbs_instances:
        key = _conn_key(entry.host, entry.port)
        if key in seen:
            ctx.record("pbs_instances", key, "conflict", "this host:port appears more than once in the file")
            continue
        seen.add(key)
        instance, error = _safe(_pbs, ctx, ConnectionRef(host=entry.host, port=entry.port))
        if error:
            ctx.record("pbs_instances", key, "conflict", error)
            continue
        values = {
            "display_name": entry.display_name,
            "location": entry.location,
            "tls_trust_mode": entry.tls_trust_mode.value,
            "tls_fingerprint_sha256": entry.tls_fingerprint_sha256,
            "disabled": entry.disabled,
        }
        if instance is None:
            ref = _new_secret_ref(
                ctx, "pbs-instance-token", f"API token for PBS instance '{entry.display_name}'", entry.token_id
            )
            instance = PbsInstance(
                host=entry.host,
                port=entry.port,
                api_token_secret_ref_id=ref.id,
                last_health_status=ConnectionHealthStatus.UNKNOWN.value,
                **values,
            )
            ctx.db.add(instance)
            ctx.db.flush()
            ctx.record("pbs_instances", key, "create", "token secret not set -- enter it after the import")
        else:
            changed = _assign(instance, values)
            ctx.record(
                "pbs_instances", key, "update" if changed else "skip", _changed_reason(changed) if changed else None
            )
            note = _token_note(ctx, instance.api_token_secret_ref_id, entry.token_id)
            if note:
                ctx.record("pbs_instances", key, "skip", note)

        for ds in entry.datastores:
            ds_key = f"{key}/{ds.name}"
            datastore = ctx.db.query(Datastore).filter_by(pbs_instance_id=instance.id, name=ds.name).one_or_none()
            if datastore is None:
                datastore = Datastore(pbs_instance_id=instance.id, name=ds.name, path=ds.path, comment=ds.comment)
                ctx.db.add(datastore)
                ctx.db.flush()
                ctx.record("datastores", ds_key, "create")
            else:
                changed = _assign(datastore, {"path": ds.path, "comment": ds.comment})
                ctx.record(
                    "datastores", ds_key, "update" if changed else "skip", _changed_reason(changed) if changed else None
                )
            if (
                ds.encryption_key_set
                and datastore.encryption_key_secret_ref_id is None
                and ctx.db.query(EncryptionKey.id).first() is None
            ):
                ctx.reenter.append(
                    ReEnterItem(
                        kind="datastore_encryption_key",
                        target=ds_key,
                        target_id=ctx.target_id(datastore),
                        page=_PAGES["datastores"],
                        detail="This datastore holds encrypted backups. Import your encryption key export (or "
                        "upload the keys) under Administration -> Encryption keys -- without them, indexing and "
                        "file restores from encrypted backups fail.",
                    )
                )
        ctx.db.flush()


def _import_pve_clusters(ctx: _Ctx, doc: ConfigBackupDocument) -> None:
    seen: set[str] = set()
    for entry in doc.sections.pve_clusters:
        key = _conn_key(entry.host, entry.port)
        if key in seen:
            ctx.record("pve_clusters", key, "conflict", "this host:port appears more than once in the file")
            continue
        seen.add(key)
        cluster, error = _safe(_pve, ctx, ConnectionRef(host=entry.host, port=entry.port))
        if error:
            ctx.record("pve_clusters", key, "conflict", error)
            continue
        values = entry.model_dump(
            include={
                "display_name",
                "tls_fingerprint_sha256",
                "disabled",
                "allowed_target_storages",
                "restore_network_rules",
                "require_restore_approval",
                "is_lab_compute",
                "lab_sdn_zone_name",
                "lab_appliance_template_vmid",
                "lab_snippet_storage_id",
                "lab_appliance_uplink_bridge",
                "lab_appliance_uplink_vlan_tag",
                "lab_appliance_uplink_ip_mode",
                "lab_appliance_uplink_ip_range_start",
                "lab_appliance_uplink_ip_range_end",
                "lab_appliance_uplink_ip_prefix_length",
                "lab_appliance_uplink_gateway",
                "lab_appliance_uplink_dns_servers",
            }
        )
        values["tls_trust_mode"] = entry.tls_trust_mode.value
        if cluster is None:
            ref = _new_secret_ref(
                ctx, "pve-cluster-token", f"API token for PVE cluster '{entry.display_name}'", entry.token_id
            )
            cluster = PveCluster(
                host=entry.host,
                port=entry.port,
                api_token_secret_ref_id=ref.id,
                last_health_status=ConnectionHealthStatus.UNKNOWN.value,
                **values,
            )
            ctx.db.add(cluster)
            ctx.db.flush()
            ctx.record("pve_clusters", key, "create", "token secret not set -- enter it after the import")
        else:
            changed = _assign(cluster, values)
            ctx.record(
                "pve_clusters", key, "update" if changed else "skip", _changed_reason(changed) if changed else None
            )
            note = _token_note(ctx, cluster.api_token_secret_ref_id, entry.token_id)
            if note:
                ctx.record("pve_clusters", key, "skip", note)

        for link in entry.linked_pbs_instances:
            instance, _err = _safe(_pbs, ctx, link)
            if instance is None:
                ctx.record(
                    "pve_clusters",
                    key,
                    "skip",
                    f"linked PBS {_conn_key(link.host, link.port)} not found -- link left out",
                )
                continue
            if (
                ctx.db.query(PveClusterPbsInstance)
                .filter_by(pve_cluster_id=cluster.id, pbs_instance_id=instance.id)
                .first()
                is None
            ):
                ctx.db.add(PveClusterPbsInstance(pve_cluster_id=cluster.id, pbs_instance_id=instance.id))
                ctx.record("pve_clusters", key, "update", f"linked PBS {_conn_key(link.host, link.port)}")
        for link in entry.linked_datastores:
            datastore, _err = _safe(_datastore, ctx, link)
            if datastore is None:
                ctx.record("pve_clusters", key, "skip", f"linked datastore {_ds_key(link)} not found -- link left out")
                continue
            if (
                ctx.db.query(PveClusterDatastore)
                .filter_by(pve_cluster_id=cluster.id, datastore_id=datastore.id)
                .first()
                is None
            ):
                ctx.db.add(PveClusterDatastore(pve_cluster_id=cluster.id, datastore_id=datastore.id))
                ctx.record("pve_clusters", key, "update", f"linked datastore {_ds_key(link)}")
        for job in entry.backup_job_customer_assignments:
            customer = _customer(ctx, job.customer_slug)
            if customer is None:
                ctx.record(
                    "pve_clusters", key, "skip", f"backup job '{job.job_id}': customer '{job.customer_slug}' not found"
                )
                continue
            row = (
                ctx.db.query(PveBackupJobCustomerAssignment)
                .filter_by(pve_cluster_id=cluster.id, job_id=job.job_id)
                .one_or_none()
            )
            if row is None:
                ctx.db.add(
                    PveBackupJobCustomerAssignment(
                        pve_cluster_id=cluster.id, job_id=job.job_id, customer_id=customer.id
                    )
                )
                ctx.record("pve_clusters", key, "update", f"backup job '{job.job_id}' scoped to '{job.customer_slug}'")
            elif row.customer_id != customer.id:
                row.customer_id = customer.id
                ctx.record(
                    "pve_clusters", key, "update", f"backup job '{job.job_id}' re-scoped to '{job.customer_slug}'"
                )

        if entry.lab_agent_token_set and cluster.lab_agent_token_hash is None:
            ctx.reenter.append(
                ReEnterItem(
                    kind="lab_agent_token",
                    target=f"{entry.display_name} ({key})",
                    target_id=ctx.target_id(cluster),
                    page=_PAGES["pve"],
                    detail="Generate a new Recovery Lab agent token for this cluster and configure resurgam-lab-agent "
                    "with it (the old token's hash can't be exported).",
                )
            )
        ctx.db.flush()


# --- assignments, policies, plans ------------------------------------------------------


def _import_datastore_assignments(ctx: _Ctx, doc: ConfigBackupDocument) -> None:
    for entry in doc.sections.datastore_assignments:
        key = f"{_ds_key(entry)} -> {entry.customer_slug}"
        datastore, error = _safe(_datastore, ctx, entry)
        if datastore is None:
            ctx.record(
                "datastore_assignments",
                key,
                "conflict",
                error or "datastore not found (not in this database or the file)",
            )
            continue
        customer = _customer(ctx, entry.customer_slug)
        if customer is None:
            ctx.record("datastore_assignments", key, "conflict", f"customer '{entry.customer_slug}' not found")
            continue
        row = ctx.db.query(DatastoreCustomerAssignment).filter_by(datastore_id=datastore.id).one_or_none()
        if row is None:
            ctx.db.add(DatastoreCustomerAssignment(datastore_id=datastore.id, customer_id=customer.id, role=entry.role))
            ctx.record("datastore_assignments", key, "create")
        elif row.customer_id != customer.id:
            other = ctx.db.get(Customer, row.customer_id)
            ctx.record(
                "datastore_assignments",
                key,
                "conflict",
                f"datastore is already assigned to customer '{other.slug if other else row.customer_id}' -- "
                "change the assignment on the Datastores page if the file is right",
            )
        elif row.role != entry.role:
            row.role = entry.role
            ctx.record("datastore_assignments", key, "update", f"role -> {entry.role}")
        else:
            ctx.record("datastore_assignments", key, "skip")
    ctx.db.flush()


def _import_protection_policies(ctx: _Ctx, doc: ConfigBackupDocument) -> None:
    for entry in doc.sections.protection_policies:
        key = f"{entry.customer_slug}/{entry.name}"
        customer = _customer(ctx, entry.customer_slug)
        if customer is None:
            ctx.record("protection_policies", key, "conflict", f"customer '{entry.customer_slug}' not found")
            continue
        values = entry.model_dump(exclude={"customer_slug", "name"})
        row = ctx.db.query(ProtectionPolicy).filter_by(customer_id=customer.id, name=entry.name).one_or_none()
        if row is None:
            ctx.db.add(ProtectionPolicy(customer_id=customer.id, name=entry.name, **values))
            ctx.record("protection_policies", key, "create")
        else:
            changed = _assign(row, values)
            ctx.record(
                "protection_policies",
                key,
                "update" if changed else "skip",
                _changed_reason(changed) if changed else None,
            )
        ctx.db.flush()


def _import_pricing_plans(ctx: _Ctx, doc: ConfigBackupDocument) -> None:
    seen: set[str] = set()
    for entry in doc.sections.pricing_plans:
        key = entry.customer_slug or "global"
        if key in seen:
            ctx.record("pricing_plans", key, "conflict", "more than one plan for the same customer/global in the file")
            continue
        seen.add(key)
        customer_id = None
        if entry.customer_slug:
            customer = _customer(ctx, entry.customer_slug)
            if customer is None:
                ctx.record("pricing_plans", key, "conflict", f"customer '{entry.customer_slug}' not found")
                continue
            customer_id = customer.id
        values = entry.model_dump(exclude={"customer_slug"})
        query = ctx.db.query(PricingPlan)
        query = (
            query.filter(PricingPlan.customer_id.is_(None))
            if customer_id is None
            else query.filter_by(customer_id=customer_id)
        )
        row = query.one_or_none()
        if row is None:
            ctx.db.add(PricingPlan(customer_id=customer_id, **values))
            ctx.record("pricing_plans", key, "create")
        else:
            changed = _assign(row, values)
            ctx.record(
                "pricing_plans", key, "update" if changed else "skip", _changed_reason(changed) if changed else None
            )
        ctx.db.flush()


# --- users -------------------------------------------------------------------------


def _resolve_scope(ctx: _Ctx, scope_type: ScopeType, scope: RoleScopeRef | None) -> tuple[uuid.UUID | None, str | None]:
    """(scope_id, error). scope_id None + no error means global."""
    if scope_type == ScopeType.GLOBAL:
        return None, None
    scope = scope or RoleScopeRef()
    if scope_type == ScopeType.CUSTOMER:
        obj = _customer(ctx, scope.customer_slug)
        label = f"customer '{scope.customer_slug}'"
    elif scope_type == ScopeType.PBS_INSTANCE:
        obj, _ = _safe(_pbs, ctx, scope.pbs_instance)
        label = f"PBS {_conn_key(scope.pbs_instance.host, scope.pbs_instance.port)}" if scope.pbs_instance else "PBS ?"
    elif scope_type == ScopeType.DATASTORE:
        obj, _ = _safe(_datastore, ctx, scope.datastore)
        label = f"datastore {_ds_key(scope.datastore)}" if scope.datastore else "datastore ?"
    else:
        obj, _ = _safe(_pve, ctx, scope.pve_cluster)
        label = f"PVE {_conn_key(scope.pve_cluster.host, scope.pve_cluster.port)}" if scope.pve_cluster else "PVE ?"
    if obj is None:
        return None, f"{label} not found"
    return obj.id, None


def _import_users(ctx: _Ctx, doc: ConfigBackupDocument) -> None:
    roles = {r.key: r for r in ctx.db.query(Role).all()}
    seen: set[str] = set()
    for entry in doc.sections.users:
        key = entry.email
        if key.lower() in seen:
            ctx.record("users", key, "conflict", "this email appears more than once in the file")
            continue
        seen.add(key.lower())
        user = ctx.db.query(User).filter_by(email=entry.email).one_or_none()
        if entry.external_subject:
            other = ctx.db.query(User).filter_by(external_subject=entry.external_subject).one_or_none()
            if other is not None and (user is None or other.id != user.id):
                ctx.record("users", key, "conflict", f"the OIDC subject is already linked to user '{other.email}'")
                continue
        if user is not None and user.is_breakglass != entry.is_breakglass:
            ctx.record(
                "users", key, "conflict", "an existing user with this email differs in being the break-glass user"
            )
            continue
        values = {
            "display_name": entry.display_name,
            "external_subject": entry.external_subject,
            "disabled": entry.disabled,
        }
        if "mfa_required" in entry.model_fields_set:
            values["mfa_required"] = entry.mfa_required
        if user is not None and user.id == ctx.actor_user_id and entry.disabled:
            values["disabled"] = False
            ctx.record("users", key, "skip", "not disabled: this is the account running the import")
        if user is None:
            user = User(email=entry.email, is_breakglass=entry.is_breakglass, password_hash=None, **values)
            ctx.db.add(user)
            ctx.db.flush()
            ctx.record("users", key, "create")
        else:
            changed = _assign(user, values)
            ctx.record("users", key, "update" if changed else "skip", _changed_reason(changed) if changed else None)

        for assignment in entry.role_assignments:
            role = roles.get(assignment.role.value)
            label = f"{assignment.role.value}@{assignment.scope_type.value}"
            if role is None:
                ctx.record("users", key, "skip", f"role '{assignment.role.value}' unknown -- assignment left out")
                continue
            scope_id, error = _resolve_scope(ctx, assignment.scope_type, assignment.scope)
            if error:
                ctx.record("users", key, "skip", f"{label}: {error} -- assignment left out")
                continue
            query = ctx.db.query(UserRoleAssignment).filter_by(
                user_id=user.id, role_id=role.id, scope_type=assignment.scope_type.value
            )
            query = (
                query.filter(UserRoleAssignment.scope_id.is_(None))
                if scope_id is None
                else query.filter_by(scope_id=scope_id)
            )
            if query.first() is None:
                ctx.db.add(
                    UserRoleAssignment(
                        user_id=user.id,
                        role_id=role.id,
                        scope_type=assignment.scope_type.value,
                        scope_id=scope_id,
                        source=assignment.source,
                    )
                )
                ctx.record("users", key, "update", f"role {label} added")
        ctx.db.flush()

        if entry.has_local_password and not entry.is_breakglass and user.password_hash is None:
            ctx.reenter.append(
                ReEnterItem(
                    kind="user_password",
                    target=entry.email,
                    target_id=ctx.target_id(user),
                    page=_PAGES["users"],
                    detail="This user had a local password. Set a new one (Users & Roles) -- or let them sign in "
                    "via SSO if OIDC is configured. MFA must be enrolled again.",
                )
            )


# --- collector-owned objects -------------------------------------------------------------


def _import_backup_group_settings(ctx: _Ctx, doc: ConfigBackupDocument) -> None:
    for entry in doc.sections.backup_group_settings:
        key = _group_key(entry)
        try:
            group = _backup_group(ctx, entry)
        except _Ambiguous:
            ctx.record("backup_group_settings", key, "conflict", "more than one PBS instance matches this host:port")
            continue
        if group is None:
            ctx.unsynced_skips += 1
            ctx.record(
                "backup_group_settings",
                key,
                "skip",
                "backup group not synced yet -- import the file again after the first Collector sync",
            )
            continue
        values: dict = {
            "file_index_enabled": entry.file_index_enabled,
            "file_index_method": entry.file_index_method,
        }
        if entry.file_index_exclude_patterns is not None:
            values["file_index_exclude_patterns"] = entry.file_index_exclude_patterns
        notes = []
        policy = None
        if entry.protection_policy is not None:
            customer = _customer(ctx, entry.protection_policy.customer_slug)
            policy = (
                ctx.db.query(ProtectionPolicy)
                .filter_by(customer_id=customer.id, name=entry.protection_policy.name)
                .one_or_none()
                if customer
                else None
            )
            if policy is None:
                notes.append(f"policy {entry.protection_policy.customer_slug}/{entry.protection_policy.name} not found")
        if entry.protection_policy is None:
            values["protection_policy_id"] = None
        else:
            values["protection_policy_id"] = policy.id if policy else group.protection_policy_id
        customer = _customer(ctx, entry.customer_slug)
        if entry.customer_slug and customer is None:
            notes.append(f"customer '{entry.customer_slug}' not found")
        values["customer_id"] = customer.id if customer else (None if not entry.customer_slug else group.customer_id)
        cluster, _ = _safe(_pve, ctx, entry.guest_exec_live_pve_cluster)
        if entry.guest_exec_live_pve_cluster is not None and cluster is None:
            notes.append("guest_exec_live PVE cluster not found")
        values["guest_exec_live_pve_cluster_id"] = cluster.id if cluster else None
        changed = _assign(group, values)
        reason = "; ".join(([_changed_reason(changed)] if changed else []) + notes) or None
        ctx.record("backup_group_settings", key, "update" if changed else "skip", reason)
    ctx.db.flush()


def _import_application_groups(ctx: _Ctx, doc: ConfigBackupDocument) -> None:
    for entry in doc.sections.application_groups:
        key = f"{entry.customer_slug}/{entry.name}"
        customer = _customer(ctx, entry.customer_slug)
        if customer is None:
            ctx.record("application_groups", key, "conflict", f"customer '{entry.customer_slug}' not found")
            continue
        cluster, _ = _safe(_pve, ctx, entry.lab_schedule_pve_cluster)
        values = entry.model_dump(exclude={"customer_slug", "name", "members", "lab_schedule_pve_cluster"})
        values["lab_schedule_pve_cluster_id"] = cluster.id if cluster else None
        row = ctx.db.query(ApplicationGroup).filter_by(customer_id=customer.id, name=entry.name).first()
        if row is None:
            row = ApplicationGroup(
                customer_id=customer.id, name=entry.name, created_by_user_id=ctx.actor_user_id, **values
            )
            ctx.db.add(row)
            ctx.db.flush()
            ctx.record("application_groups", key, "create")
        else:
            changed = _assign(row, values)
            ctx.record(
                "application_groups",
                key,
                "update" if changed else "skip",
                _changed_reason(changed) if changed else None,
            )
        if entry.lab_schedule_pve_cluster is not None and cluster is None:
            ctx.record("application_groups", key, "skip", "Lab schedule PVE cluster not found -- left empty")

        for member in entry.members:
            try:
                group = _backup_group(ctx, member)
            except _Ambiguous:
                group = None
            if group is None:
                ctx.unsynced_skips += 1
                ctx.record(
                    "application_groups",
                    key,
                    "skip",
                    f"member {_group_key(member)} not synced yet -- import again after the first Collector sync",
                )
                continue
            member_values = {
                "startup_order": member.startup_order,
                "check_types": member.check_types,
                "expected_ip": member.expected_ip,
            }
            existing = (
                ctx.db.query(ApplicationGroupMember)
                .filter_by(application_group_id=row.id, backup_group_id=group.id)
                .first()
            )
            if existing is None:
                ctx.db.add(
                    ApplicationGroupMember(application_group_id=row.id, backup_group_id=group.id, **member_values)
                )
                ctx.record("application_groups", key, "update", f"member {member.backup_type}/{member.backup_id} added")
            elif _assign(existing, member_values):
                ctx.record(
                    "application_groups", key, "update", f"member {member.backup_type}/{member.backup_id} changed"
                )
        ctx.db.flush()


# --- re-enter scan ------------------------------------------------------------------


def _connection_reenter(ctx: _Ctx, doc: ConfigBackupDocument) -> None:
    """Every connection from the file that has no usable token secret --
    newly created ones and existing ones still waiting for theirs. Listed
    first: nothing else works before these are entered."""
    items: list[ReEnterItem] = []
    for kind, model, entries, page, product in (
        ("pbs_token_secret", PbsInstance, doc.sections.pbs_instances, _PAGES["pbs"], "PBS"),
        ("pve_token_secret", PveCluster, doc.sections.pve_clusters, _PAGES["pve"], "PVE"),
    ):
        for entry in entries:
            rows = ctx.db.query(model).filter_by(host=entry.host, port=entry.port).all()
            if len(rows) != 1:
                continue
            row = rows[0]
            stored = retrieve_token(ctx.db, ctx.settings, row.api_token_secret_ref_id)
            if not token_secret_missing(stored):
                continue
            token_id = token_id_from_stored(stored)
            items.append(
                ReEnterItem(
                    kind=kind,
                    target=f"{row.display_name} ({_conn_key(row.host, row.port)})",
                    target_id=ctx.target_id(row),
                    page=page,
                    detail=(
                        f"Edit the {product} entry and enter the API token secret"
                        + (f" for token {token_id}" if token_id else " and token ID")
                        + ". Until then it shows 'auth_failed: secret not set' and the Collector skips it."
                    ),
                )
            )
    ctx.reenter[:0] = items


# --- entry point ----------------------------------------------------------------------


def import_config_backup(
    db: Session, settings: Settings, doc: ConfigBackupDocument, *, dry_run: bool, actor_user_id: uuid.UUID | None
) -> ConfigImportResult:
    """Run the import in one SAVEPOINT; roll it back for a dry run, release
    it otherwise. The caller commits (and audits) an applied import."""
    ctx = _Ctx(db=db, settings=settings, actor_user_id=actor_user_id, dry_run=dry_run)
    savepoint = db.begin_nested()
    try:
        _import_instance_settings(ctx, doc)
        _import_branding(ctx, doc)
        _import_update_source(ctx, doc)
        _import_mail(ctx, doc)
        _import_oidc(ctx, doc)
        _import_customers(ctx, doc)
        _import_pbs_instances(ctx, doc)
        _import_pve_clusters(ctx, doc)
        _import_customer_file_index(ctx, doc)
        _import_datastore_assignments(ctx, doc)
        _import_protection_policies(ctx, doc)
        _import_pricing_plans(ctx, doc)
        _import_users(ctx, doc)
        _import_backup_group_settings(ctx, doc)
        _import_application_groups(ctx, doc)
        db.flush()
        _connection_reenter(ctx, doc)
    except IntegrityError as exc:
        savepoint.rollback()
        raise ResurgamApiError(
            code="config_backup_import_failed",
            message="The import was rolled back because the database rejected a change "
            f"({exc.orig.__class__.__name__ if exc.orig else 'integrity error'}). Nothing was written. Run a dry run "
            "and resolve the reported conflicts, then try again.",
            status_code=409,
        ) from None
    except Exception:
        savepoint.rollback()
        raise
    if dry_run:
        savepoint.rollback()
    else:
        savepoint.commit()

    ctx.notes.append(
        "Configuration backups never contain secrets: PBS/PVE token secrets, the SMTP password, the OIDC client "
        "secret, datastore encryption keys, the lab-agent token and the update source's access token must be "
        "entered again (see the list)."
    )
    if any(entry.has_local_password and not entry.is_breakglass for entry in doc.sections.users):
        ctx.notes.append(
            "Users are imported without passwords and without MFA enrolments: SSO users sign in via OIDC as "
            "before; local users need a new password set by an admin (Users & Roles), then enrol MFA again."
        )
    if ctx.unsynced_skips:
        ctx.notes.append(
            f"{ctx.unsynced_skips} backup-group setting(s) or application-group member(s) refer to backup groups "
            "that haven't been synced from PBS yet. Enter the PBS token secrets, let the Collector sync, then import "
            "the same file again -- the import is idempotent."
        )

    sections = []
    for name in SECTION_ORDER:
        items = list(ctx.items[name].values())
        counts = {action: sum(1 for i in items if i.action == action) for action in _RANK}
        sections.append(ConfigImportSectionResult(section=name, items=items, **counts))
    return ConfigImportResult(
        dry_run=dry_run,
        applied=not dry_run,
        source_resurgam_version=doc.resurgam_version,
        source_exported_at=doc.exported_at,
        source_instance_name=doc.instance_name,
        sections=sections,
        to_reenter=ctx.reenter,
        notes=ctx.notes,
    )
