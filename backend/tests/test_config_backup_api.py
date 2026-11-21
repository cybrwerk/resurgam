"""Configuration export/import (ADR 0024): `GET /config-backup` and
`POST /config-backup/import`. No PBS/PVE is ever contacted -- the import
deliberately never verifies connections, and the heartbeat check for a
secret-less connection short-circuits before any network call."""

import base64
import copy
import json
from datetime import UTC, datetime, time
from decimal import Decimal

import pytest

from resurgam_api.auth.local import hash_password
from resurgam_api.auth.sessions import create_session_token
from resurgam_api.collector.heartbeat import heartbeat_pbs_instance, stored_token_is_placeholder
from resurgam_api.models.application_group import ApplicationGroup, ApplicationGroupMember
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.billing import PricingPlan
from resurgam_api.models.branding_settings import BrandingSettings
from resurgam_api.models.customer_file_index_settings import CustomerFileIndexSettings
from resurgam_api.models.customer_notification_settings import CustomerNotificationSettings
from resurgam_api.models.datastore import Datastore, DatastoreCustomerAssignment
from resurgam_api.models.identity import Role, RoleKey, ScopeType, User, UserRoleAssignment
from resurgam_api.models.instance_settings import InstanceSettings
from resurgam_api.models.inventory import BackupGroup, ProtectionPolicy
from resurgam_api.models.leg_applicability import ThreeTwoOneOneZeroLegSetting
from resurgam_api.models.mail_settings import MailSettings
from resurgam_api.models.mfa import RecoveryCode, TotpCredential, WebauthnCredential
from resurgam_api.models.oidc_settings import OidcSettings
from resurgam_api.models.pbs import PbsInstance
from resurgam_api.models.pve import (
    PveBackupJobCustomerAssignment,
    PveCluster,
    PveClusterDatastore,
    PveClusterPbsInstance,
)
from resurgam_api.models.secrets import SecretPurpose, SecretReference
from resurgam_api.models.tenancy import Customer
from resurgam_api.models.update_source import UpdateSourceSettings
from resurgam_api.secrets.local_provider import LocalEncryptedSecretsProvider

PBS_SECRET = "11111111-aaaa-4bbb-8ccc-pbs000secret"
PVE_SECRET = "22222222-aaaa-4bbb-8ccc-pve000secret"
SMTP_PASSWORD = "smtp-pass-Zq9!unique"
OIDC_SECRET = "oidc-client-secret-Xy7unique"
TOTP_SECRET = "JBSWY3DPEHPK3PXPUNIQUE"
ENCRYPTION_KEY = '{"kdf":null,"data":"ENCKEYunique000"}'
USER_PASSWORD = "user-password-unique-123"
LAB_TOKEN_HASH = "$2b$12$labagenttokenhashuniqueXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
WEBAUTHN_PUBLIC_KEY = "webauthn-public-key-unique"
RECOVERY_CODE_HASH = "$2b$12$recoverycodehashuniqueYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYY"

URL = "/api/v1/config-backup"


def _secret(db, provider, key: str, value: str, purpose: SecretPurpose) -> SecretReference:
    ref = SecretReference(reference_key=key, description="test", purpose=purpose)
    db.add(ref)
    db.flush()
    provider.store(key, value)
    return ref


def _seed(db, test_settings) -> dict:
    provider = LocalEncryptedSecretsProvider(db, test_settings.secrets_local_encryption_key)
    db.add(InstanceSettings(min_report_retention_days=400, collector_poll_interval_minutes=30))
    db.add(BrandingSettings(logo=b"\x89PNG-tiny-logo", logo_content_type="image/png", primary_color="#123456"))
    smtp_ref = _secret(db, provider, "cb-smtp", SMTP_PASSWORD, SecretPurpose.SMTP_PASSWORD)
    db.add(
        MailSettings(
            smtp_host="mail.example.test",
            smtp_port=587,
            smtp_username="mailer",
            smtp_password_ref_id=smtp_ref.id,
            use_tls=True,
            from_address="noreply@example.test",
            enabled=True,
        )
    )
    oidc_ref = _secret(db, provider, "cb-oidc", OIDC_SECRET, SecretPurpose.OIDC_CLIENT_SECRET)
    db.add(
        OidcSettings(
            issuer="https://sso.example.test/app/",
            client_id="resurgam",
            client_secret_ref_id=oidc_ref.id,
            redirect_uri="https://resurgam.example.test/auth/callback",
            enabled=True,
        )
    )
    acme = Customer(name="Acme AG", slug="acme", debitor_number="D-100", match_code="ACM", report_retention_days=500)
    beta = Customer(name="Beta GmbH", slug="beta", debitor_number="D-200", match_code="BET")
    db.add_all([acme, beta])
    db.flush()
    db.add(
        CustomerNotificationSettings(
            customer_id=acme.id, report_notification_email="reports@acme.test", report_notifications_enabled=True
        )
    )
    db.add(ThreeTwoOneOneZeroLegSetting(customer_id=acme.id, leg="offsite", applicable=False))

    pbs_ref = _secret(db, provider, "cb-pbs", f"root@pam!resurgam:{PBS_SECRET}", SecretPurpose.INFRA_CREDENTIAL)
    pbs = PbsInstance(
        display_name="PBS One",
        host="pbs1.example.test",
        port=8007,
        tls_trust_mode="fingerprint",
        tls_fingerprint_sha256="ab" * 32,
        api_token_secret_ref_id=pbs_ref.id,
        version="4.2.1",
    )
    db.add(pbs)
    db.flush()
    key_ref = _secret(db, provider, "cb-enckey", ENCRYPTION_KEY, SecretPurpose.PBS_ENCRYPTION_KEY)
    store1 = Datastore(
        pbs_instance_id=pbs.id, name="store1", path="/mnt/store1", encryption_key_secret_ref_id=key_ref.id
    )
    store2 = Datastore(pbs_instance_id=pbs.id, name="store2")
    gone = Datastore(pbs_instance_id=pbs.id, name="gone", removed_from_pbs_at=datetime.now(UTC))
    db.add_all([store1, store2, gone])
    db.flush()
    db.add(DatastoreCustomerAssignment(datastore_id=store1.id, customer_id=acme.id, role="primary"))
    db.add(DatastoreCustomerAssignment(datastore_id=store2.id, customer_id=beta.id, role="offsite"))

    pve_ref = _secret(db, provider, "cb-pve", f"resurgam@pve!resurgam={PVE_SECRET}", SecretPurpose.INFRA_CREDENTIAL)
    pve = PveCluster(
        display_name="PVE Cluster",
        host="pve1.example.test",
        port=8006,
        tls_trust_mode="ca",
        api_token_secret_ref_id=pve_ref.id,
        is_lab_compute=True,
        lab_appliance_template_vmid=9000,
        lab_agent_token_hash=LAB_TOKEN_HASH,
        allowed_target_storages=["local-lvm"],
    )
    db.add(pve)
    db.flush()
    db.add(PveClusterPbsInstance(pve_cluster_id=pve.id, pbs_instance_id=pbs.id))
    db.add(PveClusterDatastore(pve_cluster_id=pve.id, datastore_id=store1.id))
    db.add(PveBackupJobCustomerAssignment(pve_cluster_id=pve.id, job_id="backup-abc", customer_id=acme.id))
    db.add(
        CustomerFileIndexSettings(
            customer_id=acme.id,
            enabled=True,
            retention_days=30,
            window_start_time=time(22, 0),
            window_end_time=time(4, 0),
            guest_exec_pve_cluster_id=pve.id,
        )
    )
    policy = ProtectionPolicy(
        customer_id=acme.id, name="Daily", expected_interval_hours=24, max_time_since_last_backup_hours=30
    )
    db.add(policy)
    db.add(
        PricingPlan(
            customer_id=None,
            name="Standard",
            price_per_vm_month=Decimal("10.00"),
            price_per_gb_month=Decimal("0.0500"),
        )
    )
    db.add(
        PricingPlan(
            customer_id=acme.id, name="Acme", price_per_vm_month=Decimal("8.00"), price_per_gb_month=Decimal("0.04")
        )
    )
    db.flush()

    group = BackupGroup(
        datastore_id=store1.id,
        namespace="",
        backup_type="vm",
        backup_id="100",
        protection_policy_id=policy.id,
        file_index_enabled=True,
    )
    plain_group = BackupGroup(datastore_id=store1.id, namespace="", backup_type="vm", backup_id="101")
    db.add_all([group, plain_group])
    db.flush()
    app_group = ApplicationGroup(
        customer_id=acme.id, name="ERP", lab_schedule_enabled=True, lab_schedule_interval_hours=24
    )
    db.add(app_group)
    db.flush()
    db.add(
        ApplicationGroupMember(
            application_group_id=app_group.id, backup_group_id=group.id, startup_order=1, check_types=["icmp"]
        )
    )

    alice = User(email="alice@example.test", display_name="Alice", password_hash=hash_password(USER_PASSWORD))
    bob = User(email="bob@example.test", display_name="Bob", external_subject="oidc-sub-bob")
    db.add_all([alice, bob])
    db.flush()
    totp_ref = _secret(db, provider, "cb-totp", TOTP_SECRET, SecretPurpose.MFA_TOTP)
    db.add(TotpCredential(user_id=alice.id, secret_ref_id=totp_ref.id, confirmed_at=datetime.now(UTC)))
    db.add(
        WebauthnCredential(
            user_id=alice.id, nickname="key", credential_id="cred-unique", public_key=WEBAUTHN_PUBLIC_KEY
        )
    )
    db.add(RecoveryCode(user_id=alice.id, code_hash=RECOVERY_CODE_HASH))
    roles = {r.key: r for r in db.query(Role).all()}
    db.add(
        UserRoleAssignment(
            user_id=alice.id, role_id=roles["customer_admin"].id, scope_type="customer", scope_id=acme.id
        )
    )
    db.add(UserRoleAssignment(user_id=alice.id, role_id=roles["viewer"].id, scope_type="datastore", scope_id=store2.id))
    db.add(
        UserRoleAssignment(
            user_id=bob.id, role_id=roles["platform_admin"].id, scope_type="global", scope_id=None, source="oidc"
        )
    )
    db.add(
        UserRoleAssignment(
            user_id=bob.id, role_id=roles["backup_operator"].id, scope_type="pve_cluster", scope_id=pve.id
        )
    )
    db.commit()
    return {"alice_hash": alice.password_hash}


def _wipe(db, keep_email: str) -> None:
    """A "fresh appliance": every configuration table emptied, except the
    calling admin (break-glass) and their own role assignment."""
    keep = db.query(User).filter_by(email=keep_email).one()
    for model in (
        ApplicationGroupMember,
        ApplicationGroup,
        BackupGroup,
        PricingPlan,
        ProtectionPolicy,
        DatastoreCustomerAssignment,
        PveBackupJobCustomerAssignment,
        PveClusterDatastore,
        PveClusterPbsInstance,
        CustomerFileIndexSettings,
        CustomerNotificationSettings,
        ThreeTwoOneOneZeroLegSetting,
        TotpCredential,
        WebauthnCredential,
        RecoveryCode,
    ):
        db.query(model).delete()
    db.query(UserRoleAssignment).filter(UserRoleAssignment.user_id != keep.id).delete()
    db.query(AuditEvent).filter(AuditEvent.actor_user_id != keep.id).delete()
    db.query(User).filter(User.id != keep.id).delete()
    for model in (PveCluster, Datastore, PbsInstance, MailSettings, OidcSettings, BrandingSettings, InstanceSettings):
        db.query(model).delete()
    db.query(SecretReference).delete()
    db.query(Customer).delete()
    db.commit()


def _admin_email(client, headers) -> str:
    return client.get("/api/v1/auth/whoami", headers=headers).json()["email"]


def _import(client, headers, doc, dry_run: bool):
    return client.post(f"{URL}/import?dry_run={'true' if dry_run else 'false'}", json=doc, headers=headers)


def _section(result: dict, name: str) -> dict:
    return next(s for s in result["sections"] if s["section"] == name)


def _token_for(client_db, settings, model, host):
    row = client_db.query(model).filter_by(host=host).one()
    ref = client_db.get(SecretReference, row.api_token_secret_ref_id)
    return LocalEncryptedSecretsProvider(client_db, settings.secrets_local_encryption_key).retrieve(ref.reference_key)


# --- export --------------------------------------------------------------------------


def test_export_contains_no_secrets(client, db_session, test_settings, platform_admin_headers):
    seeded = _seed(db_session, test_settings)
    response = client.get(URL, headers=platform_admin_headers)
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert ".json" in response.headers["content-disposition"]
    raw = response.text
    for secret in (
        PBS_SECRET,
        PVE_SECRET,
        SMTP_PASSWORD,
        OIDC_SECRET,
        TOTP_SECRET,
        ENCRYPTION_KEY,
        "ENCKEYunique000",
        USER_PASSWORD,
        seeded["alice_hash"],
        LAB_TOKEN_HASH,
        WEBAUTHN_PUBLIC_KEY,
        "cred-unique",
        RECOVERY_CODE_HASH,
        test_settings.secrets_local_encryption_key,
    ):
        assert secret not in raw, f"secret leaked into the export: {secret[:12]}..."
    for forbidden_key in ("password_hash", "ciphertext", "secret_ref_id", "lab_agent_token_hash", "code_hash"):
        assert forbidden_key not in raw

    doc = response.json()
    assert doc["format"] == 1
    sections = doc["sections"]
    pbs = sections["pbs_instances"][0]
    assert pbs["token_id"] == "root@pam!resurgam"
    assert pbs["tls_fingerprint_sha256"] == "ab" * 32
    assert [d["name"] for d in pbs["datastores"]] == ["store1", "store2"]  # removed datastore left out
    assert pbs["datastores"][0]["encryption_key_set"] is True
    pve = sections["pve_clusters"][0]
    assert pve["token_id"] == "resurgam@pve!resurgam"
    assert pve["lab_agent_token_set"] is True
    assert pve["linked_pbs_instances"] == [{"host": "pbs1.example.test", "port": 8007}]
    assert sections["mail_settings"]["smtp_password_set"] is True
    assert sections["oidc_settings"]["client_secret_set"] is True
    assert base64.b64decode(sections["branding"]["logo_base64"]) == b"\x89PNG-tiny-logo"
    alice = next(u for u in sections["users"] if u["email"] == "alice@example.test")
    assert alice["has_local_password"] is True
    assert {"role": "customer_admin", "scope_type": "customer", "scope": {"customer_slug": "acme"}}.items() <= {
        **alice["role_assignments"][0],
        "scope": {k: v for k, v in alice["role_assignments"][0]["scope"].items() if v is not None},
    }.items()
    # natural keys only: no internal UUIDs of seeded objects
    acme_id = str(db_session.query(Customer).filter_by(slug="acme").one().id)
    assert acme_id not in raw
    # only groups with non-default settings
    assert [g["backup_id"] for g in sections["backup_group_settings"]] == ["100"]
    assert sections["application_groups"][0]["members"][0]["backup_id"] == "100"


def test_export_and_import_require_platform_admin(client, db_session, test_settings):
    user = User(email="viewer@example.test", display_name="Viewer")
    db_session.add(user)
    db_session.flush()
    role = db_session.query(Role).filter_by(key=RoleKey.VIEWER.value).one()
    db_session.add(UserRoleAssignment(user_id=user.id, role_id=role.id, scope_type=ScopeType.GLOBAL.value))
    db_session.commit()
    token, _ = create_session_token(user.id, test_settings)
    headers = {"Authorization": f"Bearer {token}"}

    assert client.get(URL, headers=headers).status_code == 403
    assert client.post(f"{URL}/import", json={"format": 1}, headers=headers).status_code == 403
    assert client.get(URL).status_code == 401


# --- import --------------------------------------------------------------------------


def test_round_trip_into_a_fresh_database(client, db_session, test_settings, platform_admin_headers):
    _seed(db_session, test_settings)
    original = client.get(URL, headers=platform_admin_headers).json()
    _wipe(db_session, _admin_email(client, platform_admin_headers))

    result = _import(client, platform_admin_headers, original, dry_run=False)
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["applied"] is True
    assert _section(body, "customers")["create"] == 2
    assert _section(body, "pbs_instances")["create"] == 1
    assert _section(body, "datastores")["create"] == 2
    assert all(s["conflict"] == 0 for s in body["sections"]), body["sections"]
    # backup groups are Collector-owned: not synced yet -> skipped with a note
    assert _section(body, "backup_group_settings")["skip"] == 1
    assert any("synced" in n for n in body["notes"])

    kinds = {item["kind"] for item in body["to_reenter"]}
    assert kinds == {
        "pbs_token_secret",
        "pve_token_secret",
        "datastore_encryption_key",
        "smtp_password",
        "oidc_client_secret",
        "lab_agent_token",
        "user_password",
    }
    assert body["to_reenter"][0]["kind"] == "pbs_token_secret"
    assert body["to_reenter"][0]["page"] == "/infrastructure/pbs-instances"
    assert all(
        item["target_id"]
        for item in body["to_reenter"]
        if item["kind"] != "smtp_password" and item["kind"] != "oidc_client_secret"
    )
    assert next(i for i in body["to_reenter"] if i["kind"] == "user_password")["target"] == "alice@example.test"

    # The Collector "syncs" the groups; importing the same file again applies the rest.
    store1 = db_session.query(Datastore).filter_by(name="store1").one()
    db_session.add(BackupGroup(datastore_id=store1.id, namespace="", backup_type="vm", backup_id="100"))
    db_session.add(BackupGroup(datastore_id=store1.id, namespace="", backup_type="vm", backup_id="101"))
    db_session.commit()
    second = _import(client, platform_admin_headers, original, dry_run=False).json()
    assert _section(second, "backup_group_settings")["update"] == 1
    assert _section(second, "customers")["create"] == 0
    assert _section(second, "customers")["update"] == 0

    again = client.get(URL, headers=platform_admin_headers).json()

    def normalized(doc):
        sections = copy.deepcopy(doc["sections"])
        # secrets are never exported, so their "was set" flags are expected to differ
        sections["mail_settings"]["smtp_password_set"] = None
        sections["oidc_settings"]["client_secret_set"] = None
        for pbs in sections["pbs_instances"]:
            for ds in pbs["datastores"]:
                ds["encryption_key_set"] = None
        for pve in sections["pve_clusters"]:
            pve["lab_agent_token_set"] = None
        for user in sections["users"]:
            user["has_local_password"] = None
        return json.loads(json.dumps(sections, sort_keys=True))

    assert normalized(again) == normalized(original)

    # Imported connections: token ID kept, secret missing, clean auth_failed, Collector skips them.
    stored = _token_for(db_session, test_settings, PbsInstance, "pbs1.example.test")
    assert stored == "root@pam!resurgam"
    pbs_list = client.get("/api/v1/pbs-instances", headers=platform_admin_headers).json()
    assert pbs_list[0]["token_id"] == "root@pam!resurgam"
    assert pbs_list[0]["token_secret_set"] is False
    pve_list = client.get("/api/v1/pve-clusters", headers=platform_admin_headers).json()
    assert pve_list[0]["token_secret_set"] is False
    instance = db_session.query(PbsInstance).one()
    assert stored_token_is_placeholder(db_session, test_settings, instance.api_token_secret_ref_id) is True
    result = heartbeat_pbs_instance(db_session, test_settings, instance)  # must not raise, no network
    assert result.status.value == "auth_failed"
    assert "secret not set" in result.message
    assert instance.last_health_status == "auth_failed"


def test_entering_only_the_secret_completes_an_imported_connection(
    client, db_session, test_settings, platform_admin_headers
):
    _seed(db_session, test_settings)
    original = client.get(URL, headers=platform_admin_headers).json()
    _wipe(db_session, _admin_email(client, platform_admin_headers))
    _import(client, platform_admin_headers, original, dry_run=False)
    instance_id = db_session.query(PbsInstance).one().id

    response = client.patch(
        f"/api/v1/pbs-instances/{instance_id}", json={"token_secret": PBS_SECRET}, headers=platform_admin_headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["token_secret_set"] is True
    assert _token_for(db_session, test_settings, PbsInstance, "pbs1.example.test") == f"root@pam!resurgam:{PBS_SECRET}"


def test_dry_run_changes_nothing(client, db_session, test_settings, platform_admin_headers):
    _seed(db_session, test_settings)
    original = client.get(URL, headers=platform_admin_headers).json()
    _wipe(db_session, _admin_email(client, platform_admin_headers))
    counts_before = {
        m.__name__: db_session.query(m).count() for m in (Customer, PbsInstance, User, SecretReference, Datastore)
    }
    audit_before = db_session.query(AuditEvent).count()

    response = _import(client, platform_admin_headers, original, dry_run=True)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dry_run"] is True and body["applied"] is False
    assert _section(body, "customers")["create"] == 2
    assert _section(body, "datastore_assignments")["create"] == 2  # resolved against datastores created in-run
    assert all(item["target_id"] is None for item in body["to_reenter"])

    db_session.expire_all()
    counts_after = {
        m.__name__: db_session.query(m).count() for m in (Customer, PbsInstance, User, SecretReference, Datastore)
    }
    assert counts_after == counts_before
    assert db_session.query(AuditEvent).count() == audit_before


def test_default_is_dry_run(client, db_session, test_settings, platform_admin_headers):
    _seed(db_session, test_settings)
    doc = client.get(URL, headers=platform_admin_headers).json()
    doc["sections"]["customers"].append(
        {"name": "New", "slug": "newco", "debitor_number": "D-900", "match_code": "NEW"}
    )
    body = client.post(f"{URL}/import", json=doc, headers=platform_admin_headers).json()
    assert body["dry_run"] is True
    assert db_session.query(Customer).filter_by(slug="newco").one_or_none() is None


def test_reimport_into_same_instance_is_all_skip(client, db_session, test_settings, platform_admin_headers):
    _seed(db_session, test_settings)
    doc = client.get(URL, headers=platform_admin_headers).json()
    body = _import(client, platform_admin_headers, doc, dry_run=True).json()
    for section in body["sections"]:
        assert section["create"] == 0 and section["update"] == 0 and section["conflict"] == 0, section
    # secrets that are still in place are not listed as "to re-enter"
    assert body["to_reenter"] == []


def test_conflicts_are_reported_and_left_untouched(client, db_session, test_settings, platform_admin_headers):
    _seed(db_session, test_settings)
    doc = client.get(URL, headers=platform_admin_headers).json()
    sections = doc["sections"]
    # a new customer whose debitor number is already used by "beta"
    sections["customers"].append({"name": "Clash", "slug": "clash", "debitor_number": "D-200", "match_code": "CLA"})
    # store2 is assigned to beta in the DB; the file says acme
    for assignment in sections["datastore_assignments"]:
        if assignment["datastore"] == "store2":
            assignment["customer_slug"] = "acme"
    # a policy for a customer that exists nowhere
    sections["protection_policies"].append(
        {"customer_slug": "ghost", "name": "X", "expected_interval_hours": 1, "max_time_since_last_backup_hours": 2}
    )
    # duplicate PBS host:port in the file
    sections["pbs_instances"].append(copy.deepcopy(sections["pbs_instances"][0]))

    body = _import(client, platform_admin_headers, doc, dry_run=False).json()

    clash = next(i for i in _section(body, "customers")["items"] if i["key"] == "clash")
    assert clash["action"] == "conflict" and "debitor_number" in clash["reason"] and "beta" in clash["reason"]
    store2 = next(i for i in _section(body, "datastore_assignments")["items"] if "store2" in i["key"])
    assert store2["action"] == "conflict" and "beta" in store2["reason"]
    ghost = next(i for i in _section(body, "protection_policies")["items"] if i["key"] == "ghost/X")
    assert ghost["action"] == "conflict"
    assert _section(body, "pbs_instances")["conflict"] == 1

    db_session.expire_all()
    assert db_session.query(Customer).filter_by(slug="clash").one_or_none() is None
    beta = db_session.query(Customer).filter_by(slug="beta").one()
    store2_row = db_session.query(Datastore).filter_by(name="store2").one()
    assert (
        db_session.query(DatastoreCustomerAssignment).filter_by(datastore_id=store2_row.id).one().customer_id == beta.id
    )


def test_update_changes_existing_objects_by_natural_key(client, db_session, test_settings, platform_admin_headers):
    _seed(db_session, test_settings)
    doc = client.get(URL, headers=platform_admin_headers).json()
    doc["sections"]["customers"][0]["name"] = "Acme Holding AG"
    doc["sections"]["pbs_instances"][0]["display_name"] = "PBS Renamed"
    body = _import(client, platform_admin_headers, doc, dry_run=False).json()
    acme = next(i for i in _section(body, "customers")["items"] if i["key"] == "acme")
    assert acme["action"] == "update" and "name" in acme["reason"]
    assert _section(body, "pbs_instances")["items"][0]["action"] == "update"
    db_session.expire_all()
    assert db_session.query(Customer).filter_by(slug="acme").one().name == "Acme Holding AG"
    # the real token secret is untouched by an update
    assert _token_for(db_session, test_settings, PbsInstance, "pbs1.example.test") == f"root@pam!resurgam:{PBS_SECRET}"


@pytest.mark.parametrize("bad_format", [2, 0, "1", None])
def test_rejects_unsupported_format(client, platform_admin_headers, bad_format):
    doc = {"format": bad_format, "exported_at": "2026-10-07T00:00:00Z", "resurgam_version": "9.9.9", "sections": {}}
    response = _import(client, platform_admin_headers, doc, dry_run=True)
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "config_backup_unsupported_format"
    assert "format 1" in body["message"]


def test_rejects_invalid_document_with_field_errors(client, platform_admin_headers):
    doc = {
        "format": 1,
        "exported_at": "2026-10-07T00:00:00Z",
        "resurgam_version": "0.2.0",
        "sections": {"customers": [{"name": "X", "slug": "Bad Slug!", "debitor_number": "1", "match_code": "XX"}]},
    }
    response = _import(client, platform_admin_headers, doc, dry_run=True)
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "config_backup_invalid"
    fields = {e["field"] for e in body["details"]["errors"]}
    assert "sections.customers.0.slug" in fields
    assert "sections.customers.0.match_code" in fields


def test_applied_import_is_audited(client, db_session, test_settings, platform_admin_headers):
    _seed(db_session, test_settings)
    doc = client.get(URL, headers=platform_admin_headers).json()
    _import(client, platform_admin_headers, doc, dry_run=False)
    assert db_session.query(AuditEvent).filter_by(action="config_backup_imported").count() == 1
    assert db_session.query(AuditEvent).filter_by(action="config_backup_exported").count() == 1


def test_large_logo_is_left_out_with_a_note(client, db_session, platform_admin_headers):
    db_session.add(BrandingSettings(logo=b"x" * (600 * 1024), logo_content_type="image/png"))
    db_session.commit()
    branding = client.get(URL, headers=platform_admin_headers).json()["sections"]["branding"]
    assert branding["logo_base64"] is None
    assert "larger than" in branding["logo_omitted_reason"]


def test_openapi_documents_both_routes(client):
    spec = client.get("/api/openapi.json").json()
    export_op = spec["paths"]["/api/v1/config-backup"]["get"]
    import_op = spec["paths"]["/api/v1/config-backup/import"]["post"]
    assert export_op["description"] and import_op["description"]
    assert import_op["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("ConfigBackupDocument")
    assert any(p["name"] == "dry_run" and p.get("description") for p in import_op["parameters"])
    schemas = spec["components"]["schemas"]
    for name in ("ConfigBackupDocument", "ConfigImportResult", "PbsInstanceEntry", "ReEnterItem", "UserEntry"):
        for field_name, field in schemas[name]["properties"].items():
            assert field.get("description"), f"{name}.{field_name} has no description"


# --- MFA switch, app title, update source ---------------------------------------------

UPDATE_TOKEN = "glpat-update-source-token-unique"


def _seed_new_settings(db, test_settings) -> None:
    provider = LocalEncryptedSecretsProvider(db, test_settings.secrets_local_encryption_key)
    db.add(
        InstanceSettings(
            min_report_retention_days=400, collector_poll_interval_minutes=30, require_mfa_all_local_users=True
        )
    )
    db.add(BrandingSettings(primary_color="#123456", app_name="Acme Backup"))
    token_ref = _secret(db, provider, "update-source-token", UPDATE_TOKEN, SecretPurpose.UPDATE_SOURCE_TOKEN)
    db.add(
        UpdateSourceSettings(
            provider="gitlab",
            repository_url="https://gitlab.example.test/group/resurgam",
            token_ref_id=token_ref.id,
            tls_verify=False,
            include_prereleases=True,
            auto_check=False,
            check_interval_hours=48,
        )
    )
    db.commit()


def _wipe_new_settings(db) -> None:
    db.query(UpdateSourceSettings).delete()
    for model in (BrandingSettings, InstanceSettings):
        db.query(model).delete()
    db.query(SecretReference).filter_by(reference_key="update-source-token").delete()
    db.commit()


def test_mfa_switch_app_title_and_update_source_round_trip(client, db_session, test_settings, platform_admin_headers):
    _seed_new_settings(db_session, test_settings)
    response = client.get(URL, headers=platform_admin_headers)
    assert UPDATE_TOKEN not in response.text
    sections = response.json()["sections"]
    assert sections["instance_settings"]["require_mfa_all_local_users"] is True
    assert sections["branding"]["app_name"] == "Acme Backup"
    assert sections["update_source"] == {
        "provider": "gitlab",
        "repository_url": "https://gitlab.example.test/group/resurgam",
        "tls_verify": False,
        "include_prereleases": True,
        "auto_check": False,
        "check_interval_hours": 48,
        "token_set": True,
    }

    original = response.json()
    _wipe_new_settings(db_session)
    body = _import(client, platform_admin_headers, original, dry_run=False).json()
    assert _section(body, "update_source")["create"] == 1
    reenter = [i for i in body["to_reenter"] if i["kind"] == "update_source_token"]
    assert len(reenter) == 1 and reenter[0]["page"] == "/administration/system-settings"

    db_session.expire_all()
    assert db_session.query(InstanceSettings).one().require_mfa_all_local_users is True
    assert db_session.query(BrandingSettings).one().app_name == "Acme Backup"
    source = db_session.query(UpdateSourceSettings).one()
    assert (source.provider, source.repository_url, source.check_interval_hours) == (
        "gitlab",
        "https://gitlab.example.test/group/resurgam",
        48,
    )
    assert (source.tls_verify, source.include_prereleases, source.auto_check) == (False, True, False)
    assert source.token_ref_id is None  # the token never travels


def test_older_export_without_new_fields_leaves_them_unchanged(
    client, db_session, test_settings, platform_admin_headers
):
    _seed_new_settings(db_session, test_settings)
    doc = client.get(URL, headers=platform_admin_headers).json()
    # What an export from before these fields existed looks like.
    del doc["sections"]["instance_settings"]["require_mfa_all_local_users"]
    del doc["sections"]["branding"]["app_name"]
    del doc["sections"]["update_source"]

    body = _import(client, platform_admin_headers, doc, dry_run=False).json()
    assert _section(body, "update_source")["items"] == []
    db_session.expire_all()
    assert db_session.query(InstanceSettings).one().require_mfa_all_local_users is True
    assert db_session.query(BrandingSettings).one().app_name == "Acme Backup"
    source = db_session.query(UpdateSourceSettings).one()
    assert source.token_ref_id is not None
    assert source.check_interval_hours == 48


def test_import_can_clear_app_title_and_mfa_switch(client, db_session, test_settings, platform_admin_headers):
    _seed_new_settings(db_session, test_settings)
    doc = client.get(URL, headers=platform_admin_headers).json()
    doc["sections"]["instance_settings"]["require_mfa_all_local_users"] = False
    doc["sections"]["branding"]["app_name"] = None

    body = _import(client, platform_admin_headers, doc, dry_run=False).json()
    assert _section(body, "branding")["update"] == 1
    db_session.expire_all()
    assert db_session.query(InstanceSettings).one().require_mfa_all_local_users is False
    assert db_session.query(BrandingSettings).one().app_name is None


def test_update_source_on_another_host_drops_the_stored_token(
    client, db_session, test_settings, platform_admin_headers
):
    _seed_new_settings(db_session, test_settings)
    doc = client.get(URL, headers=platform_admin_headers).json()
    doc["sections"]["update_source"]["provider"] = "github"
    doc["sections"]["update_source"]["repository_url"] = "https://github.com/example/resurgam"

    body = _import(client, platform_admin_headers, doc, dry_run=False).json()
    item = _section(body, "update_source")["items"][0]
    assert item["action"] == "update"
    assert "token removed" in item["reason"]
    assert any(i["kind"] == "update_source_token" for i in body["to_reenter"])
    db_session.expire_all()
    source = db_session.query(UpdateSourceSettings).one()
    assert source.token_ref_id is None
    assert source.repository_url == "https://github.com/example/resurgam"


def test_update_source_with_unusable_url_is_a_conflict(client, db_session, test_settings, platform_admin_headers):
    _seed_new_settings(db_session, test_settings)
    doc = client.get(URL, headers=platform_admin_headers).json()
    doc["sections"]["update_source"]["repository_url"] = "https://github.com/only-an-owner"

    body = _import(client, platform_admin_headers, doc, dry_run=False).json()
    assert _section(body, "update_source")["conflict"] == 1
    db_session.expire_all()
    assert db_session.query(UpdateSourceSettings).one().repository_url == "https://gitlab.example.test/group/resurgam"
