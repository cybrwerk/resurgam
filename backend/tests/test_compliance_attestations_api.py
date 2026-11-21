"""Endpoint tests for the 3-2-1-1-0 compliance self-attestation API
(`api/v1/compliance_attestations.py`) -- append-only (a second POST
creates a new row, never updates the first), the database-level
immutability trigger actually rejects a raw UPDATE, and the
customer_admin-scoped-to-their-own-customer access rule.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from resurgam_api.connectivity.models import DatastoreListResult, RemoteDatastore


def _create_pbs_instance(client, headers) -> str:
    response = client.post(
        "/api/v1/pbs-instances",
        json={
            "display_name": "PBS",
            "host": "pbs1.internal.example",
            "port": 8007,
            "tls_trust_mode": "ca",
            "api_token": "test@pbs!token:secretvalue",
        },
        headers=headers,
    )
    assert response.status_code == 200
    return response.json()["id"]


def _import_datastore(client, headers, instance_id, monkeypatch, name="store1") -> str:
    from resurgam_api.api.v1 import pbs_instances as pbs_instances_module

    monkeypatch.setattr(
        pbs_instances_module,
        "list_datastores",
        lambda *a, **kw: DatastoreListResult(reachable=True, datastores=[RemoteDatastore(name=name)]),
    )
    response = client.post(f"/api/v1/pbs-instances/{instance_id}/datastores", json={"name": name}, headers=headers)
    assert response.status_code == 200
    return response.json()["id"]


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
    return {"Authorization": f"Bearer {token}"}, user.id


def _setup_datastore(client, platform_admin_headers, monkeypatch) -> str:
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    return _import_datastore(client, platform_admin_headers, instance_id, monkeypatch)


def test_platform_admin_can_record_and_list_datastore_attestation(client, platform_admin_headers, monkeypatch):
    datastore_id = _setup_datastore(client, platform_admin_headers, monkeypatch)

    response = client.post(
        f"/api/v1/datastores/{datastore_id}/compliance-attestations",
        json={"dimension": "immutable_copy", "value": True, "notes": "Confirmed via vendor console."},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["value"] is True
    assert body["dimension"] == "immutable_copy"
    assert body["asserted_by_display_name"]

    history = client.get(f"/api/v1/datastores/{datastore_id}/compliance-attestations", headers=platform_admin_headers)
    assert history.status_code == 200
    assert len(history.json()) == 1


def test_a_second_attestation_creates_a_new_row_not_an_update(client, platform_admin_headers, monkeypatch):
    datastore_id = _setup_datastore(client, platform_admin_headers, monkeypatch)

    first = client.post(
        f"/api/v1/datastores/{datastore_id}/compliance-attestations",
        json={"dimension": "immutable_copy", "value": False},
        headers=platform_admin_headers,
    )
    second = client.post(
        f"/api/v1/datastores/{datastore_id}/compliance-attestations",
        json={"dimension": "immutable_copy", "value": True},
        headers=platform_admin_headers,
    )
    assert first.json()["id"] != second.json()["id"]

    # Both requests land inside this test's single wrapping transaction
    # (conftest's SAVEPOINT-backed db_session), so Postgres `now()` --
    # frozen to transaction start, not per-statement -- gives both rows
    # the same `created_at`; a real deployment's separate per-request
    # transactions wouldn't tie like this. Assert both rows survive
    # rather than depending on tie-breaking order.
    history = client.get(
        f"/api/v1/datastores/{datastore_id}/compliance-attestations", headers=platform_admin_headers
    ).json()
    assert len(history) == 2  # both rows kept -- the first is not overwritten
    ids = {row["id"] for row in history}
    assert ids == {first.json()["id"], second.json()["id"]}
    values = {row["id"]: row["value"] for row in history}
    assert values[first.json()["id"]] is False
    assert values[second.json()["id"]] is True


def test_database_trigger_rejects_a_raw_update(client, platform_admin_headers, db_session, monkeypatch):
    datastore_id = _setup_datastore(client, platform_admin_headers, monkeypatch)
    client.post(
        f"/api/v1/datastores/{datastore_id}/compliance-attestations",
        json={"dimension": "immutable_copy", "value": True},
        headers=platform_admin_headers,
    )
    with pytest.raises(DatabaseError):
        db_session.execute(text("UPDATE compliance_attestations SET notes = 'tampered'"))
        db_session.flush()
    db_session.rollback()


def test_invalid_dimension_for_datastore_scope_is_rejected(client, platform_admin_headers, monkeypatch):
    datastore_id = _setup_datastore(client, platform_admin_headers, monkeypatch)
    response = client.post(
        f"/api/v1/datastores/{datastore_id}/compliance-attestations",
        json={"dimension": "production_copy_count", "value": 3},
        headers=platform_admin_headers,
    )
    assert response.status_code == 400


def test_wrong_value_type_for_the_dimension_is_rejected(client, platform_admin_headers, monkeypatch):
    """`value` is a valid member of the schema's `bool | int | list[str]`
    union (an int), so Pydantic itself lets it through -- the app-level
    check must still catch that an int is the wrong shape for
    `immutable_copy`, which is bool-typed.
    """
    datastore_id = _setup_datastore(client, platform_admin_headers, monkeypatch)
    response = client.post(
        f"/api/v1/datastores/{datastore_id}/compliance-attestations",
        json={"dimension": "immutable_copy", "value": 42},
        headers=platform_admin_headers,
    )
    assert response.status_code == 400


def test_customer_admin_can_attest_for_their_own_customer(client, db_session, test_settings):
    customer_id = _create_customer(db_session)
    headers, _user_id = _headers_with_role(client, db_session, test_settings, "customer_admin", "customer", customer_id)

    response = client.post(
        f"/api/v1/customers/{customer_id}/compliance-attestations",
        json={"dimension": "production_copy_count", "value": 3},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["value"] == 3


def test_customer_admin_cannot_attest_for_a_different_customer(client, db_session, test_settings):
    own_customer = _create_customer(db_session, name="Acme", slug="acme")
    other_customer = _create_customer(db_session, name="Beta", slug="beta")
    headers, _user_id = _headers_with_role(
        client, db_session, test_settings, "customer_admin", "customer", own_customer
    )

    response = client.post(
        f"/api/v1/customers/{other_customer}/compliance-attestations",
        json={"dimension": "production_copy_count", "value": 3},
        headers=headers,
    )
    assert response.status_code == 404


def test_media_type_diversity_requires_a_list_of_strings(client, db_session, test_settings):
    customer_id = _create_customer(db_session)
    headers, _user_id = _headers_with_role(client, db_session, test_settings, "customer_admin", "customer", customer_id)

    good = client.post(
        f"/api/v1/customers/{customer_id}/compliance-attestations",
        json={"dimension": "media_type_diversity", "value": ["disk", "tape"]},
        headers=headers,
    )
    assert good.status_code == 200

    # A list of ints isn't a valid member of the `bool | int | list[str]`
    # union at all -- Pydantic itself rejects it (422) before the
    # app-level dimension check ever runs.
    bad = client.post(
        f"/api/v1/customers/{customer_id}/compliance-attestations",
        json={"dimension": "media_type_diversity", "value": [1, 2]},
        headers=headers,
    )
    assert bad.status_code == 422

    # A value that *is* schema-valid (a list of strings) but wrong for
    # `production_copy_count` (int-typed) must still be caught by the
    # app-level dimension check.
    wrong_dimension = client.post(
        f"/api/v1/customers/{customer_id}/compliance-attestations",
        json={"dimension": "production_copy_count", "value": ["disk", "tape"]},
        headers=headers,
    )
    assert wrong_dimension.status_code == 400


def test_plain_viewer_cannot_attest_for_a_customer(client, db_session, test_settings):
    customer_id = _create_customer(db_session)
    headers, _user_id = _headers_with_role(client, db_session, test_settings, "viewer", "customer", customer_id)

    response = client.post(
        f"/api/v1/customers/{customer_id}/compliance-attestations",
        json={"dimension": "production_copy_count", "value": 3},
        headers=headers,
    )
    assert response.status_code == 404
