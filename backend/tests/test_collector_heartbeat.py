"""Connection heartbeat (`collector/heartbeat.py`) and its wiring into the
collector scheduler tick. PBS/PVE HTTP is always mocked -- never called."""

from datetime import UTC, datetime, timedelta

from resurgam_api.collector import heartbeat as heartbeat_module
from resurgam_api.collector.heartbeat import heartbeat_pbs_instance, heartbeat_pve_cluster, run_due_heartbeats
from resurgam_api.connectivity import health as health_module
from resurgam_api.connectivity.health import HealthCheckResult
from resurgam_api.models.pbs import ConnectionHealthStatus
from resurgam_api.worker import collector_scheduler as scheduler_module
from tests.test_collector_sync import _FakeSettings, _make_pbs_instance, _make_pve_cluster


def _ok(*a, **kw):
    return HealthCheckResult(status=ConnectionHealthStatus.OK, version="9.9", permissions_checked=True)


def _down(*a, **kw):
    return HealthCheckResult(status=ConnectionHealthStatus.UNREACHABLE, message="connection refused")


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = ""

    def json(self):
        return self._payload


def _mock_network(monkeypatch, responses: dict[str, _FakeResponse]) -> list[dict]:
    """Mock DNS + HTTP underneath `connectivity.health` -- no socket is opened."""
    calls: list[dict] = []
    monkeypatch.setattr(
        health_module.socket, "getaddrinfo", lambda host, port, **kw: [(None, None, None, "", ("192.0.2.10", port))]
    )

    def fake_get(url, headers=None, timeout=None, verify=None):
        calls.append({"url": url, "headers": headers})
        for suffix, response in responses.items():
            if url.endswith(suffix):
                return response
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(health_module.httpx, "get", fake_get)
    return calls


def test_pbs_heartbeat_records_auth_failed_through_shared_classifier(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    calls = _mock_network(monkeypatch, {"/version": _FakeResponse(401)})

    result = heartbeat_pbs_instance(db_session, _FakeSettings(), instance)

    assert result.status == ConnectionHealthStatus.AUTH_FAILED
    assert instance.last_health_status == "auth_failed"
    assert instance.last_health_check_at is not None
    # The stored token is sent as-is with PBS's own auth scheme.
    assert calls[0]["headers"] == {"Authorization": "PBSAPIToken=test@pbs!token:secret"}


def test_pve_heartbeat_records_ok_and_version(db_session, monkeypatch):
    cluster = _make_pve_cluster(db_session)
    _mock_network(
        monkeypatch,
        {
            "/version": _FakeResponse(200, {"data": {"version": "9.2.1"}}),
            # Unreadable permissions -> still "ok" (privileges just not verified).
            "/access/permissions": _FakeResponse(500),
        },
    )

    result = heartbeat_pve_cluster(db_session, _FakeSettings(), cluster)

    assert result.status == ConnectionHealthStatus.OK
    assert cluster.last_health_status == "ok"
    assert cluster.version == "9.2.1"


def test_unreachable_heartbeat_keeps_last_known_version(db_session, monkeypatch):
    cluster = _make_pve_cluster(db_session)
    cluster.version = "9.2"
    monkeypatch.setattr(heartbeat_module, "check_pve_health", _down)

    heartbeat_pve_cluster(db_session, _FakeSettings(), cluster)

    assert cluster.last_health_status == "unreachable"
    assert cluster.version == "9.2"
    assert cluster.last_health_check_at is not None


def test_run_due_heartbeats_checks_pbs_and_pve(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    cluster = _make_pve_cluster(db_session)
    db_session.commit()
    monkeypatch.setattr(heartbeat_module, "check_pbs_health", _ok)
    monkeypatch.setattr(heartbeat_module, "check_pve_health", _down)

    summary = run_due_heartbeats(db_session, _FakeSettings())

    assert summary == {"checked": 2, "failed": 0}
    assert instance.last_health_status == "ok"
    assert cluster.last_health_status == "unreachable"


def test_run_due_heartbeats_skips_disabled_and_recently_checked(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    instance.last_health_check_at = datetime.now(UTC) - timedelta(minutes=1)
    instance.last_health_status = "ok"
    disabled_cluster = _make_pve_cluster(db_session, disabled=True)
    stale_cluster = _make_pve_cluster(db_session)
    stale_cluster.last_health_check_at = datetime.now(UTC) - timedelta(minutes=30)
    db_session.commit()
    calls: list[str] = []
    monkeypatch.setattr(heartbeat_module, "check_pbs_health", lambda *a, **kw: calls.append("pbs") or _ok())
    monkeypatch.setattr(heartbeat_module, "check_pve_health", lambda *a, **kw: calls.append("pve") or _ok())

    summary = run_due_heartbeats(db_session, _FakeSettings(), interval_minutes=5)

    assert summary == {"checked": 1, "failed": 0}
    assert calls == ["pve"]
    assert stale_cluster.last_health_status == "ok"
    assert disabled_cluster.last_health_check_at is None


def test_one_failing_target_does_not_stop_the_others(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    cluster = _make_pve_cluster(db_session)
    db_session.commit()

    def explode(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(heartbeat_module, "check_pbs_health", explode)
    monkeypatch.setattr(heartbeat_module, "check_pve_health", _ok)

    summary = run_due_heartbeats(db_session, _FakeSettings())

    assert summary == {"checked": 1, "failed": 1}
    db_session.refresh(cluster)
    db_session.refresh(instance)
    assert cluster.last_health_status == "ok"
    assert instance.last_health_check_at is None


def test_scheduler_tick_runs_heartbeat_and_sync_even_if_heartbeat_pass_raises(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    db_session.commit()
    synced: list = []

    def heartbeat_crash(*a, **kw):
        raise RuntimeError("heartbeat pass crashed")

    monkeypatch.setattr(scheduler_module, "run_due_heartbeats", heartbeat_crash)
    monkeypatch.setattr(scheduler_module, "sync_pbs_instance", lambda db, settings, inst: synced.append(inst.id))

    scheduler_module.run_tick(db_session, _FakeSettings())

    assert synced == [instance.id]


def test_scheduler_tick_heartbeats_before_syncing(db_session, monkeypatch):
    _make_pbs_instance(db_session)
    db_session.commit()
    order: list[str] = []
    monkeypatch.setattr(scheduler_module, "run_due_heartbeats", lambda *a, **kw: order.append("heartbeat"))
    monkeypatch.setattr(scheduler_module, "sync_pbs_instance", lambda *a, **kw: order.append("sync"))

    scheduler_module.run_tick(db_session, _FakeSettings())

    assert order == ["heartbeat", "sync"]


def test_scheduler_tick_one_sync_failure_does_not_stop_the_next(db_session, monkeypatch):
    first = _make_pbs_instance(db_session)
    second = _make_pbs_instance_second(db_session)
    db_session.commit()
    attempted: list = []

    def flaky_sync(db, settings, inst):
        attempted.append(inst.id)
        if inst.id == first.id:
            raise RuntimeError("PBS restarting")

    monkeypatch.setattr(scheduler_module, "run_due_heartbeats", lambda *a, **kw: None)
    monkeypatch.setattr(scheduler_module, "sync_pbs_instance", flaky_sync)

    scheduler_module.run_tick(db_session, _FakeSettings())

    assert set(attempted) == {first.id, second.id}


def _make_pbs_instance_second(db_session):
    from resurgam_api.models.pbs import PbsInstance
    from resurgam_api.models.secrets import SecretPurpose, SecretReference

    ref = SecretReference(reference_key="heartbeat-second", description="test", purpose=SecretPurpose.INFRA_CREDENTIAL)
    db_session.add(ref)
    db_session.flush()
    instance = PbsInstance(
        display_name="PBS 2",
        host="pbs2.internal.example",
        port=8007,
        tls_trust_mode="ca",
        api_token_secret_ref_id=ref.id,
    )
    db_session.add(instance)
    db_session.flush()
    return instance


def _make_placeholder_pbs_instance(db_session):
    """A PBS instance as a configuration import creates it: the stored token
    is only the token ID, the secret was never exported."""
    from resurgam_api.models.pbs import PbsInstance
    from resurgam_api.models.secrets import SecretPurpose, SecretReference
    from resurgam_api.secrets.local_provider import LocalEncryptedSecretsProvider

    ref = SecretReference(
        reference_key="heartbeat-imported", description="test", purpose=SecretPurpose.INFRA_CREDENTIAL
    )
    db_session.add(ref)
    db_session.flush()
    LocalEncryptedSecretsProvider(db_session, _FakeSettings.secrets_local_encryption_key).store(
        "heartbeat-imported", "root@pam!resurgam"
    )
    instance = PbsInstance(
        display_name="Imported",
        host="imported.internal.example",
        port=8007,
        tls_trust_mode="ca",
        api_token_secret_ref_id=ref.id,
    )
    db_session.add(instance)
    db_session.flush()
    return instance


def test_heartbeat_reports_secret_not_set_without_any_network_call(db_session, monkeypatch):
    instance = _make_placeholder_pbs_instance(db_session)
    calls = _mock_network(monkeypatch, {})

    result = heartbeat_pbs_instance(db_session, _FakeSettings(), instance)

    assert calls == []
    assert result.status == ConnectionHealthStatus.AUTH_FAILED
    assert "secret not set" in result.message
    assert instance.last_health_status == "auth_failed"
    assert instance.last_health_check_at is not None


def test_heartbeat_with_no_stored_value_is_secret_not_set_not_a_crash(db_session, monkeypatch):
    instance = _make_pbs_instance_second(db_session)  # SecretReference without any stored value
    db_session.commit()
    _mock_network(monkeypatch, {})

    summary = run_due_heartbeats(db_session, _FakeSettings())

    assert summary["failed"] == 0
    db_session.refresh(instance)
    assert instance.last_health_status == "auth_failed"


def test_scheduler_skips_inventory_sync_for_a_secret_less_instance(db_session, monkeypatch):
    imported = _make_placeholder_pbs_instance(db_session)
    normal = _make_pbs_instance(db_session)
    db_session.commit()
    synced: list = []
    monkeypatch.setattr(scheduler_module, "run_due_heartbeats", lambda *a, **kw: None)
    monkeypatch.setattr(scheduler_module, "sync_pbs_instance", lambda db, settings, inst: synced.append(inst.id))

    scheduler_module.run_tick(db_session, _FakeSettings())

    assert synced == [normal.id]
    assert imported.id not in synced
