"""Endpoint tests for the per-customer 3-2-1-1-0 leg on/off toggle
(`api/v1/leg_applicability.py`) -- default-applicable when unset,
platform_admin-only writes (unlike compliance attestations, a
customer's own customer_admin cannot toggle their own checks off), and
that toggling a leg off actually overrides its status in the Recovery
Evidence report.
"""

from datetime import UTC


def _create_customer(db_session, name="Acme", slug="acme"):
    from resurgam_api.models.tenancy import Customer

    customer = Customer(
        name=name, slug=slug, debitor_number=f"debitor-{slug}", match_code=slug[:3].upper().ljust(3, "X")
    )
    db_session.add(customer)
    db_session.flush()
    db_session.commit()
    return customer.id


def _headers_with_role(
    client, db_session, test_settings, role_key, scope_type, scope_id=None, email="user@example.com"
):
    from resurgam_api.auth.sessions import create_session_token
    from resurgam_api.models.identity import Role, User, UserRoleAssignment

    user = User(email=email, display_name="Test User")
    db_session.add(user)
    db_session.flush()
    role = db_session.query(Role).filter_by(key=role_key).one()
    db_session.add(UserRoleAssignment(user_id=user.id, role_id=role.id, scope_type=scope_type, scope_id=scope_id))
    db_session.commit()
    token, _ = create_session_token(user.id, test_settings)
    return {"Authorization": f"Bearer {token}"}


def test_defaults_to_applicable_for_every_leg(client, platform_admin_headers, db_session):
    customer_id = _create_customer(db_session)
    response = client.get(f"/api/v1/customers/{customer_id}/leg-applicability", headers=platform_admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 5
    assert all(row["applicable"] is True for row in body)
    assert all(row["updated_by_display_name"] is None for row in body)


def test_platform_admin_can_toggle_a_leg_off(client, platform_admin_headers, db_session):
    customer_id = _create_customer(db_session)
    response = client.put(
        f"/api/v1/customers/{customer_id}/leg-applicability/offsite",
        json={"applicable": False},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["applicable"] is False
    assert body["updated_by_display_name"]

    listing = client.get(f"/api/v1/customers/{customer_id}/leg-applicability", headers=platform_admin_headers).json()
    offsite = next(row for row in listing if row["leg"] == "offsite")
    assert offsite["applicable"] is False
    other = next(row for row in listing if row["leg"] == "zero_errors")
    assert other["applicable"] is True  # unaffected


def test_toggling_the_same_leg_twice_updates_in_place_not_a_new_row(client, platform_admin_headers, db_session):
    customer_id = _create_customer(db_session)
    client.put(
        f"/api/v1/customers/{customer_id}/leg-applicability/offsite",
        json={"applicable": False},
        headers=platform_admin_headers,
    )
    client.put(
        f"/api/v1/customers/{customer_id}/leg-applicability/offsite",
        json={"applicable": True},
        headers=platform_admin_headers,
    )

    from resurgam_api.models.leg_applicability import ThreeTwoOneOneZeroLegSetting

    rows = db_session.query(ThreeTwoOneOneZeroLegSetting).filter_by(customer_id=customer_id, leg="offsite").all()
    assert len(rows) == 1
    assert rows[0].applicable is True


def test_invalid_leg_name_is_rejected(client, platform_admin_headers, db_session):
    customer_id = _create_customer(db_session)
    response = client.put(
        f"/api/v1/customers/{customer_id}/leg-applicability/not-a-real-leg",
        json={"applicable": False},
        headers=platform_admin_headers,
    )
    assert response.status_code == 400


def test_customer_admin_cannot_toggle_their_own_leg(client, db_session, test_settings):
    customer_id = _create_customer(db_session)
    headers = _headers_with_role(client, db_session, test_settings, "customer_admin", "customer", customer_id)

    response = client.put(
        f"/api/v1/customers/{customer_id}/leg-applicability/offsite",
        json={"applicable": False},
        headers=headers,
    )
    assert response.status_code == 403


def test_disabled_leg_overrides_the_recovery_evidence_report(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.api.v1 import pbs_instances as pbs_instances_module
    from resurgam_api.connectivity.models import DatastoreListResult, RemoteDatastore

    customer_id = _create_customer(db_session)
    instance_response = client.post(
        "/api/v1/pbs-instances",
        json={
            "display_name": "PBS",
            "host": "pbs1.internal.example",
            "port": 8007,
            "tls_trust_mode": "ca",
            "api_token": "test@pbs!token:secretvalue",
        },
        headers=platform_admin_headers,
    )
    instance_id = instance_response.json()["id"]
    monkeypatch.setattr(
        pbs_instances_module,
        "list_datastores",
        lambda *a, **kw: DatastoreListResult(reachable=True, datastores=[RemoteDatastore(name="store1")]),
    )
    datastore_response = client.post(
        f"/api/v1/pbs-instances/{instance_id}/datastores", json={"name": "store1"}, headers=platform_admin_headers
    )
    datastore_id = datastore_response.json()["id"]

    from datetime import datetime

    from resurgam_api.models.inventory import BackupGroup, BackupSnapshot

    group = BackupGroup(datastore_id=datastore_id, backup_type="vm", backup_id="100", customer_id=customer_id)
    db_session.add(group)
    db_session.flush()
    db_session.add(BackupSnapshot(backup_group_id=group.id, snapshot_time=datetime.now(UTC), size_bytes=1000))
    db_session.commit()

    # Before toggling: offsite leg is a real "not_met" (no offsite copy configured).
    before = client.get(
        f"/api/v1/reporting/recovery-evidence?customer_id={customer_id}", headers=platform_admin_headers
    ).json()
    offsite_before = next(leg for leg in before["three_two_one_one_zero"] if leg["leg"] == "offsite")
    assert offsite_before["status"] == "not_met"

    client.put(
        f"/api/v1/customers/{customer_id}/leg-applicability/offsite",
        json={"applicable": False},
        headers=platform_admin_headers,
    )

    after = client.get(
        f"/api/v1/reporting/recovery-evidence?customer_id={customer_id}", headers=platform_admin_headers
    ).json()
    offsite_after = next(leg for leg in after["three_two_one_one_zero"] if leg["leg"] == "offsite")
    assert offsite_after["status"] == "not_applicable"
    # Every other leg is untouched by the override.
    zero_errors_after = next(leg for leg in after["three_two_one_one_zero"] if leg["leg"] == "zero_errors")
    assert zero_errors_after["status"] != "not_applicable"
