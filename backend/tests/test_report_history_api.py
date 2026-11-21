"""Endpoint + worker-sweep tests for the report history library:
checksum round-trips the actual downloaded bytes, redownload returns
the exact archived PDF (not a fresh regeneration), history listing is
scoped the same way the report itself is, the retention floor rejects
an under-floor customer value but a real `null` clears the override
back to the floor, and the sweep purges only genuinely aged rows.
"""

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError


def _create_customer(db_session, name="Acme", slug="acme"):
    from resurgam_api.models.tenancy import Customer

    customer = Customer(
        name=name, slug=slug, debitor_number=f"debitor-{slug}", match_code=slug[:3].upper().ljust(3, "X")
    )
    db_session.add(customer)
    db_session.flush()
    db_session.commit()
    return customer.id


def test_generating_a_pdf_records_a_history_entry_with_a_matching_checksum(client, platform_admin_headers, db_session):
    customer_id = _create_customer(db_session)

    response = client.get(
        f"/api/v1/reporting/recovery-evidence/pdf?customer_id={customer_id}", headers=platform_admin_headers
    )
    assert response.status_code == 200
    real_checksum = hashlib.sha256(response.content).hexdigest()

    history = client.get(
        f"/api/v1/reporting/recovery-evidence/history?customer_id={customer_id}", headers=platform_admin_headers
    )
    assert history.status_code == 200
    body = history.json()
    assert len(body) == 1
    assert body[0]["sha256_checksum"] == real_checksum
    assert body[0]["report_type"] == "recovery_evidence"
    assert body[0]["customer_id"] == str(customer_id)


def test_redownload_returns_the_exact_archived_bytes_not_a_fresh_regeneration(
    client, platform_admin_headers, db_session, monkeypatch
):
    from resurgam_api.api.v1 import pbs_instances as pbs_instances_module
    from resurgam_api.connectivity.models import DatastoreListResult, RemoteDatastore
    from resurgam_api.models.inventory import BackupGroup, BackupSnapshot

    customer_id = _create_customer(db_session)
    instance = client.post(
        "/api/v1/pbs-instances",
        json={
            "display_name": "PBS",
            "host": "pbs1.internal.example",
            "port": 8007,
            "tls_trust_mode": "ca",
            "api_token": "test@pbs!token:secretvalue",
        },
        headers=platform_admin_headers,
    ).json()
    monkeypatch.setattr(
        pbs_instances_module,
        "list_datastores",
        lambda *a, **kw: DatastoreListResult(reachable=True, datastores=[RemoteDatastore(name="store1")]),
    )
    datastore = client.post(
        f"/api/v1/pbs-instances/{instance['id']}/datastores", json={"name": "store1"}, headers=platform_admin_headers
    ).json()
    group = BackupGroup(datastore_id=datastore["id"], backup_type="vm", backup_id="100", customer_id=customer_id)
    db_session.add(group)
    db_session.flush()
    db_session.add(BackupSnapshot(backup_group_id=group.id, snapshot_time=datetime.now(UTC), size_bytes=1000))
    db_session.commit()

    first_response = client.get(
        f"/api/v1/reporting/recovery-evidence/pdf?customer_id={customer_id}", headers=platform_admin_headers
    )
    history_id = client.get(
        f"/api/v1/reporting/recovery-evidence/history?customer_id={customer_id}", headers=platform_admin_headers
    ).json()[0]["id"]

    # Add a second snapshot -- a fresh regeneration would now differ from the archived bytes.
    db_session.add(BackupSnapshot(backup_group_id=group.id, snapshot_time=datetime.now(UTC), size_bytes=2000))
    db_session.commit()

    redownload = client.get(
        f"/api/v1/reporting/recovery-evidence/history/{history_id}/pdf", headers=platform_admin_headers
    )
    assert redownload.status_code == 200
    assert redownload.content == first_response.content

    fresh = client.get(
        f"/api/v1/reporting/recovery-evidence/pdf?customer_id={customer_id}", headers=platform_admin_headers
    )
    assert fresh.content != first_response.content  # sanity: the extra snapshot really did change the fresh report


def test_a_second_pdf_generation_creates_a_second_history_entry(client, platform_admin_headers, db_session):
    customer_id = _create_customer(db_session)
    client.get(f"/api/v1/reporting/recovery-evidence/pdf?customer_id={customer_id}", headers=platform_admin_headers)
    client.get(f"/api/v1/reporting/recovery-evidence/pdf?customer_id={customer_id}", headers=platform_admin_headers)

    history = client.get(
        f"/api/v1/reporting/recovery-evidence/history?customer_id={customer_id}", headers=platform_admin_headers
    ).json()
    assert len(history) == 2


def test_history_denies_out_of_scope_customer(client, platform_admin_headers, db_session, test_settings):
    from resurgam_api.auth.sessions import create_session_token
    from resurgam_api.models.identity import Role, RoleKey, ScopeType, User, UserRoleAssignment

    customer_a = _create_customer(db_session, name="Acme", slug="acme")
    customer_b = _create_customer(db_session, name="Beta", slug="beta")
    client.get(f"/api/v1/reporting/recovery-evidence/pdf?customer_id={customer_a}", headers=platform_admin_headers)

    user = User(email="viewer@example.com", display_name="Scoped Viewer")
    db_session.add(user)
    db_session.flush()
    role = db_session.query(Role).filter_by(key=RoleKey.VIEWER.value).one()
    db_session.add(
        UserRoleAssignment(user_id=user.id, role_id=role.id, scope_type=ScopeType.CUSTOMER.value, scope_id=customer_b)
    )
    db_session.commit()
    token, _ = create_session_token(user.id, test_settings)
    viewer_b_headers = {"Authorization": f"Bearer {token}"}

    response = client.get(
        f"/api/v1/reporting/recovery-evidence/history?customer_id={customer_a}", headers=viewer_b_headers
    )
    assert response.status_code == 404


def test_redownload_denies_out_of_scope_customer(client, platform_admin_headers, db_session, test_settings):
    from resurgam_api.auth.sessions import create_session_token
    from resurgam_api.models.identity import Role, RoleKey, ScopeType, User, UserRoleAssignment

    customer_a = _create_customer(db_session, name="Acme", slug="acme")
    customer_b = _create_customer(db_session, name="Beta", slug="beta")
    client.get(f"/api/v1/reporting/recovery-evidence/pdf?customer_id={customer_a}", headers=platform_admin_headers)
    history_id = client.get(
        f"/api/v1/reporting/recovery-evidence/history?customer_id={customer_a}", headers=platform_admin_headers
    ).json()[0]["id"]

    user = User(email="viewer2@example.com", display_name="Scoped Viewer")
    db_session.add(user)
    db_session.flush()
    role = db_session.query(Role).filter_by(key=RoleKey.VIEWER.value).one()
    db_session.add(
        UserRoleAssignment(user_id=user.id, role_id=role.id, scope_type=ScopeType.CUSTOMER.value, scope_id=customer_b)
    )
    db_session.commit()
    token, _ = create_session_token(user.id, test_settings)
    viewer_b_headers = {"Authorization": f"Bearer {token}"}

    response = client.get(f"/api/v1/reporting/recovery-evidence/history/{history_id}/pdf", headers=viewer_b_headers)
    assert response.status_code == 404


def test_database_trigger_rejects_a_raw_update_but_allows_delete(client, platform_admin_headers, db_session):
    """Unlike audit_events/compliance_attestations, report_history rows
    ARE deletable (the retention sweep needs to purge them) -- only
    UPDATE is blocked.
    """
    customer_id = _create_customer(db_session)
    client.get(f"/api/v1/reporting/recovery-evidence/pdf?customer_id={customer_id}", headers=platform_admin_headers)

    with pytest.raises(DatabaseError):
        db_session.execute(text("UPDATE report_history SET sha256_checksum = 'tampered'"))
        db_session.flush()
    db_session.rollback()

    from resurgam_api.models.report_history import ReportHistory

    remaining = db_session.query(ReportHistory).filter_by(customer_id=customer_id).one()
    db_session.delete(remaining)
    db_session.flush()  # DELETE must succeed where UPDATE didn't
    db_session.rollback()


def test_customer_report_retention_below_the_floor_is_rejected(client, platform_admin_headers, db_session):
    customer_id = _create_customer(db_session)
    response = client.patch(
        f"/api/v1/customers/{customer_id}", json={"report_retention_days": 10}, headers=platform_admin_headers
    )
    assert response.status_code == 400


def test_customer_report_retention_at_or_above_the_floor_is_accepted(client, platform_admin_headers, db_session):
    customer_id = _create_customer(db_session)
    response = client.patch(
        f"/api/v1/customers/{customer_id}", json={"report_retention_days": 400}, headers=platform_admin_headers
    )
    assert response.status_code == 200
    assert response.json()["report_retention_days"] == 400


def test_customer_report_retention_can_be_cleared_back_to_the_floor_via_explicit_null(
    client, platform_admin_headers, db_session
):
    """Regression-shaped test, mirroring the PATCH /pve-clusters fix:
    an explicit `null` must clear the override, not be silently ignored
    the way an omitted field is.
    """
    customer_id = _create_customer(db_session)
    client.patch(
        f"/api/v1/customers/{customer_id}", json={"report_retention_days": 400}, headers=platform_admin_headers
    )

    clear_response = client.patch(
        f"/api/v1/customers/{customer_id}", json={"report_retention_days": None}, headers=platform_admin_headers
    )
    assert clear_response.status_code == 200
    assert clear_response.json()["report_retention_days"] is None

    # Omitting the field entirely (vs. sending null) must leave the current value alone.
    client.patch(
        f"/api/v1/customers/{customer_id}", json={"report_retention_days": 400}, headers=platform_admin_headers
    )
    unrelated_update = client.patch(
        f"/api/v1/customers/{customer_id}", json={"name": "New Name"}, headers=platform_admin_headers
    )
    assert unrelated_update.json()["report_retention_days"] == 400


def test_instance_settings_default_and_update(client, platform_admin_headers):
    default = client.get("/api/v1/instance-settings", headers=platform_admin_headers)
    assert default.status_code == 200
    assert default.json()["min_report_retention_days"] == 365
    assert default.json()["collector_poll_interval_minutes"] == 15

    updated = client.put(
        "/api/v1/instance-settings",
        json={"min_report_retention_days": 730, "collector_poll_interval_minutes": 30},
        headers=platform_admin_headers,
    )
    assert updated.status_code == 200
    assert updated.json()["min_report_retention_days"] == 730
    assert updated.json()["collector_poll_interval_minutes"] == 30

    again = client.get("/api/v1/instance-settings", headers=platform_admin_headers)
    assert again.json()["min_report_retention_days"] == 730
    assert again.json()["collector_poll_interval_minutes"] == 30


def test_customer_report_retention_floor_check_uses_the_current_instance_setting(
    client, platform_admin_headers, db_session
):
    client.put(
        "/api/v1/instance-settings",
        json={"min_report_retention_days": 500, "collector_poll_interval_minutes": 15},
        headers=platform_admin_headers,
    )
    customer_id = _create_customer(db_session)

    below_new_floor = client.patch(
        f"/api/v1/customers/{customer_id}", json={"report_retention_days": 400}, headers=platform_admin_headers
    )
    assert below_new_floor.status_code == 400

    at_new_floor = client.patch(
        f"/api/v1/customers/{customer_id}", json={"report_retention_days": 500}, headers=platform_admin_headers
    )
    assert at_new_floor.status_code == 200


def test_sweep_purges_only_genuinely_aged_report_history_rows(client, platform_admin_headers, db_session):
    from resurgam_api.models.report_history import ReportHistory, ReportHistoryContent
    from resurgam_api.worker.file_index_worker import _sweep_expired_report_history

    customer_id = _create_customer(db_session)
    client.patch(
        f"/api/v1/customers/{customer_id}", json={"report_retention_days": 400}, headers=platform_admin_headers
    )

    # Set `created_at` explicitly at insert time (past the customer's
    # 400-day retention) rather than updating it afterwards -- the
    # immutability trigger blocks UPDATE unconditionally, even one that
    # only touches created_at, so this has to be set on the INSERT itself.
    aged = ReportHistory(
        report_type="recovery_evidence",
        customer_id=customer_id,
        date_from=datetime.now(UTC).date(),
        date_to=datetime.now(UTC).date(),
        framework=False,
        generated_by_user_id=None,
        sha256_checksum="a" * 64,
        created_at=datetime.now(UTC) - timedelta(days=401),
    )
    db_session.add(aged)
    db_session.flush()
    aged_id = aged.id  # captured before the sweep expires this row's identity
    db_session.add(ReportHistoryContent(report_history_id=aged_id, pdf_content=b"old"))
    db_session.commit()

    fresh = ReportHistory(
        report_type="recovery_evidence",
        customer_id=customer_id,
        date_from=datetime.now(UTC).date(),
        date_to=datetime.now(UTC).date(),
        framework=False,
        generated_by_user_id=None,
        sha256_checksum="b" * 64,
    )
    db_session.add(fresh)
    db_session.flush()
    fresh_id = fresh.id
    db_session.add(ReportHistoryContent(report_history_id=fresh_id, pdf_content=b"new"))
    db_session.commit()

    purged = _sweep_expired_report_history(db_session)
    assert purged == 1

    remaining_ids = {row.id for row in db_session.query(ReportHistory).filter_by(customer_id=customer_id).all()}
    assert remaining_ids == {fresh_id}
    # The cascade-deleted content row is gone too.
    assert db_session.get(ReportHistoryContent, aged_id) is None
