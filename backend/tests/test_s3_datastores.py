"""ADR 0033: S3-backed PBS datastores -- backend detection, the audited on-site
mark per S3 endpoint, offsite rules, and capacity/billing figures that follow
the bucket instead of the local cache filesystem."""

import uuid
from datetime import UTC, datetime, timedelta

from resurgam_api.collector import sync as collector_sync_module
from resurgam_api.collector.sync import discover_datastores, sync_instance_s3_endpoints
from resurgam_api.connectivity import pbs_api
from resurgam_api.connectivity.models import (
    DatastoreListResult,
    RemoteDatastore,
    RemoteS3Endpoint,
    S3EndpointListResult,
    TrustConfig,
)
from resurgam_api.inventory.usage import compute_s3_traffic
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.datastore import Datastore, PbsS3Endpoint
from resurgam_api.models.pbs import TlsTrustMode
from resurgam_api.models.usage import UsageMeasurement
from tests.test_backup_gaps import (
    _assign,
    _create_customer,
    _create_pbs_instance,
    _group,
    _import_datastore,
    _offsite_by_id,
    _sync_job,
    _viewer_headers,
)
from tests.test_collector_sync import _FakeSettings, _make_pbs_instance

NOW = datetime.now(UTC).replace(microsecond=0)
_TRUST = TrustConfig(trust_mode=TlsTrustMode.CA)


def _make_s3(db_session, datastore_id, endpoint_name="rustfs", bucket="pbs-test"):
    datastore = db_session.get(Datastore, uuid.UUID(str(datastore_id)))
    datastore.backend_type = "s3"
    datastore.s3_endpoint_name = endpoint_name
    datastore.s3_bucket = bucket
    db_session.commit()
    return datastore


def _endpoint(db_session, instance_id, name="rustfs", endpoint="s3.example", on_site=False):
    row = PbsS3Endpoint(
        pbs_instance_id=uuid.UUID(str(instance_id)), name=name, endpoint=endpoint, port=9000, on_site=on_site
    )
    db_session.add(row)
    db_session.commit()
    return row


def _measure(db_session, datastore_id, at, **fields):
    defaults = {"used_bytes": 80, "available_bytes": 20, "total_bytes": 100, "logical_bytes": 0}
    defaults.update(fields)
    row = UsageMeasurement(datastore_id=uuid.UUID(str(datastore_id)), captured_at=at, **defaults)
    db_session.add(row)
    db_session.commit()
    return row


# --- PBS API parsing --------------------------------------------------------------


def test_parse_backend_property():
    assert pbs_api.parse_backend({}) == {"backend_type": "filesystem"}
    assert pbs_api.parse_backend({"backend": "type=s3,client=rustfs-test,bucket=pbs-test"}) == {
        "backend_type": "s3",
        "s3_endpoint": "rustfs-test",
        "s3_bucket": "pbs-test",
    }
    # The property string's default key is `type`.
    assert pbs_api.parse_backend({"backend": "s3,client=c,bucket=b"})["backend_type"] == "s3"
    assert pbs_api.parse_backend({"backend": "type=filesystem"}) == {"backend_type": "filesystem"}


def test_list_datastores_endpoints_usage_and_statistics(monkeypatch):
    responses = {
        pbs_api.DATASTORE_CONFIG_PATH: [
            {"name": "s3-test", "path": "/var/cache", "backend": "type=s3,client=rustfs,bucket=pbs-test"},
            {"name": "local", "path": "/mnt/local"},
        ],
        pbs_api.S3_CONFIG_PATH: [
            {"id": "rustfs", "endpoint": "127.0.0.1", "port": 9000, "path-style": True, "region": "us-east-1"}
        ],
        pbs_api.DATASTORE_USAGE_PATH: [
            {
                "store": "s3-test",
                "used": 80,
                "avail": 20,
                "total": 100,
                "backend-type": "s3",
                "gc-status": {"disk-bytes": 0, "upid": None},
            },
            {
                "store": "local",
                "used": 1,
                "avail": 2,
                "total": 3,
                "backend-type": "filesystem",
                "gc-status": {"disk-bytes": 7, "upid": "UPID:x"},
            },
        ],
        "/api2/json/admin/datastore/s3-test/status": {
            "backend-type": "s3",
            "s3-statistics": {
                "get": 1,
                "put": 81,
                "post": 0,
                "head": 2,
                "delete": 0,
                "uploaded": 84430550,
                "downloaded": 5,
            },
        },
        "/api2/json/admin/datastore/local/status": {"backend-type": "filesystem"},
    }
    monkeypatch.setattr(pbs_api, "_get_data", lambda h, p, t, k, path, timeout, **kw: (responses[path], None))

    stores = {d.name: d for d in pbs_api.list_datastores("pbs", 8007, _TRUST, "tok").datastores}
    assert (stores["s3-test"].backend_type, stores["s3-test"].s3_endpoint, stores["s3-test"].s3_bucket) == (
        "s3",
        "rustfs",
        "pbs-test",
    )
    assert stores["local"].backend_type == "filesystem"

    endpoint = pbs_api.list_s3_endpoints("pbs", 8007, _TRUST, "tok").endpoints[0]
    assert (endpoint.id, endpoint.endpoint, endpoint.port, endpoint.path_style) == ("rustfs", "127.0.0.1", 9000, True)

    usage = {u.name: u for u in pbs_api.list_datastore_usage("pbs", 8007, _TRUST, "tok").usage}
    assert usage["s3-test"].backend_type == "s3"
    # No GC yet: PBS reports zeros with `upid: null` -- that is "unknown", not 0.
    assert usage["s3-test"].gc_disk_bytes is None
    assert usage["local"].gc_disk_bytes == 7

    stats = pbs_api.get_s3_statistics("pbs", 8007, _TRUST, "tok", 5, store="s3-test")
    assert (stats.put, stats.uploaded_bytes, stats.downloaded_bytes) == (81, 84430550, 5)
    assert pbs_api.get_s3_statistics("pbs", 8007, _TRUST, "tok", 5, store="local") is None


# --- Collector -----------------------------------------------------------------------


def test_discovery_stores_backend_and_endpoint_sync_keeps_on_site_mark(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    monkeypatch.setattr(
        collector_sync_module,
        "list_datastores",
        lambda *a, **kw: DatastoreListResult(
            reachable=True,
            datastores=[
                RemoteDatastore(
                    name="s3-test", path="/cache", backend_type="s3", s3_endpoint="rustfs", s3_bucket="pbs-test"
                )
            ],
        ),
    )
    discover_datastores(db_session, _FakeSettings(), instance)
    store = db_session.query(Datastore).filter_by(pbs_instance_id=instance.id, name="s3-test").one()
    assert (store.backend_type, store.s3_endpoint_name, store.s3_bucket) == ("s3", "rustfs", "pbs-test")

    endpoints = [RemoteS3Endpoint(id="rustfs", endpoint="10.0.0.5", port=9000)]
    monkeypatch.setattr(
        collector_sync_module,
        "list_s3_endpoints",
        lambda *a, **kw: S3EndpointListResult(reachable=True, endpoints=endpoints),
    )
    assert sync_instance_s3_endpoints(db_session, _FakeSettings(), instance) is True
    row = db_session.query(PbsS3Endpoint).filter_by(pbs_instance_id=instance.id).one()
    assert (row.name, row.endpoint, row.on_site) == ("rustfs", "10.0.0.5", False)

    row.on_site = True
    endpoints[0] = RemoteS3Endpoint(id="rustfs", endpoint="10.0.0.6", port=9000)
    sync_instance_s3_endpoints(db_session, _FakeSettings(), instance)
    assert (row.endpoint, row.on_site) == ("10.0.0.6", True)

    endpoints.clear()
    sync_instance_s3_endpoints(db_session, _FakeSettings(), instance)
    assert row.removed_from_pbs_at is not None


def test_endpoint_sync_skips_instances_without_s3(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    calls = []
    monkeypatch.setattr(collector_sync_module, "list_s3_endpoints", lambda *a, **kw: calls.append(1))
    assert sync_instance_s3_endpoints(db_session, _FakeSettings(), instance) is False
    assert calls == []


# --- 3-2-1 offsite rules -----------------------------------------------------------------


def test_local_copy_to_s3_counts_as_offsite_until_marked_on_site(
    client, platform_admin_headers, db_session, monkeypatch
):
    a = _create_pbs_instance(client, platform_admin_headers, "Site A", "pbs-a.example")
    local = _import_datastore(client, platform_admin_headers, a, monkeypatch, "local")
    s3 = _import_datastore(client, platform_admin_headers, a, monkeypatch, "s3-test")
    _make_s3(db_session, s3)
    endpoint = _endpoint(db_session, a)
    # A local pull job (no remote) from `local` into the S3 datastore.
    _sync_job(db_session, a, "s3-copy", "s3-test", "local")
    _group(db_session, local, "100", (5, 30))
    _group(db_session, s3, "100", (5, 30))

    offsite = _offsite_by_id(client.get("/api/v1/backup-gaps", headers=platform_admin_headers).json())
    assert list(offsite) == ["100"]  # the copy in S3 is not a workload of its own
    assert offsite["100"]["status"] == "ok"
    assert offsite["100"]["copy_location"] == "Site A / s3-test (S3 bucket pbs-test @ s3.example)"

    # The operator says the object store stands in the same rack.
    response = client.put(
        f"/api/v1/s3-endpoints/{endpoint.id}/location",
        json={"on_site": True, "reason": "MinIO in the same rack as the PBS."},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["on_site"] is True
    assert response.json()["datastores"] == ["s3-test"]
    offsite = _offsite_by_id(client.get("/api/v1/backup-gaps", headers=platform_admin_headers).json())
    assert offsite["100"]["status"] == "local_copy_only"

    audit = db_session.query(AuditEvent).filter_by(action="s3_endpoint_location_changed").one()
    assert audit.detail["on_site_before"] is False
    assert audit.detail["reason"] == "MinIO in the same rack as the PBS."

    datastore = client.get(f"/api/v1/datastores/{s3}", headers=platform_admin_headers).json()
    assert (datastore["backend_type"], datastore["s3_bucket"], datastore["s3_offsite"]) == ("s3", "pbs-test", False)


def test_backup_stored_on_s3_is_stored_offsite(client, platform_admin_headers, db_session, monkeypatch):
    a = _create_pbs_instance(client, platform_admin_headers, "Site A", "pbs-a.example")
    s3 = _import_datastore(client, platform_admin_headers, a, monkeypatch, "s3-direct")
    _make_s3(db_session, s3, endpoint_name="not-synced-yet")
    _group(db_session, s3, "200", (5,))

    body = client.get("/api/v1/backup-gaps", headers=platform_admin_headers).json()
    entry = _offsite_by_id(body)["200"]
    assert entry["status"] == "stored_offsite"
    assert entry["severity"] is None
    # An endpoint RESURGAM hasn't read yet still counts: "offsite unless marked".
    assert entry["copy_location"] == "S3 bucket pbs-test @ not-synced-yet"
    assert body["summary"]["offsite_gaps"] == 0

    # Marked on site, the backup has no offsite copy at all.
    endpoint = _endpoint(db_session, a, name="not-synced-yet", on_site=True)
    assert endpoint.on_site
    entry = _offsite_by_id(client.get("/api/v1/backup-gaps", headers=platform_admin_headers).json())["200"]
    assert entry["status"] == "no_copy_job"


def test_location_change_validation_and_permissions(client, platform_admin_headers, db_session, test_settings):
    a = _create_pbs_instance(client, platform_admin_headers, "Site A", "pbs-a.example")
    endpoint = _endpoint(db_session, a)
    url = f"/api/v1/s3-endpoints/{endpoint.id}/location"

    assert (
        client.put(url, json={"on_site": True, "reason": "   short   "}, headers=platform_admin_headers).status_code
        == 422
    )
    assert client.put(url, json={"on_site": True}, headers=platform_admin_headers).status_code == 422
    missing = f"/api/v1/s3-endpoints/{uuid.uuid4()}/location"
    assert (
        client.put(
            missing, json={"on_site": True, "reason": "a valid reason"}, headers=platform_admin_headers
        ).status_code
        == 404
    )

    viewer = _viewer_headers(db_session, test_settings, _create_customer(db_session, "acme"))
    assert client.get("/api/v1/s3-endpoints", headers=viewer).status_code == 403
    assert client.put(url, json={"on_site": True, "reason": "a valid reason"}, headers=viewer).status_code == 403
    listed = client.get("/api/v1/s3-endpoints", headers=platform_admin_headers).json()
    assert [(e["name"], e["on_site"], e["pbs_instance_name"]) for e in listed] == [("rustfs", False, "Site A")]


# --- capacity, alarms, billing --------------------------------------------------------------


def test_s3_traffic_sums_deltas_and_survives_counter_reset():
    def row(hours_ago, put, uploaded):
        return UsageMeasurement(
            captured_at=NOW - timedelta(hours=hours_ago),
            s3_get_requests=0,
            s3_put_requests=put,
            s3_post_requests=0,
            s3_head_requests=0,
            s3_delete_requests=0,
            s3_uploaded_bytes=uploaded,
            s3_downloaded_bytes=0,
        )

    history = [row(30, 100, 1000), row(20, 150, 1500), row(10, 20, 200), row(1, 50, 500)]
    # Baseline is the row just before the window (30 h ago); 150-100, reset -> 20, 50-20.
    traffic = compute_s3_traffic(history, NOW - timedelta(hours=24))
    assert traffic["s3_put_requests"] == 50 + 20 + 30
    assert traffic["s3_uploaded_bytes"] == 500 + 200 + 300
    assert compute_s3_traffic(history[:1], NOW - timedelta(hours=24)) is None
    assert compute_s3_traffic([], NOW) is None


def test_usage_endpoint_for_s3_datastore(client, platform_admin_headers, db_session, monkeypatch):
    a = _create_pbs_instance(client, platform_admin_headers, "Site A", "pbs-a.example")
    s3 = _import_datastore(client, platform_admin_headers, a, monkeypatch, "s3-test")
    _make_s3(db_session, s3)
    counters = {
        "s3_get_requests": 0,
        "s3_post_requests": 0,
        "s3_head_requests": 0,
        "s3_delete_requests": 0,
        "s3_downloaded_bytes": 0,
    }
    _measure(
        db_session,
        s3,
        NOW - timedelta(hours=30),
        s3_data_bytes=1000,
        s3_put_requests=10,
        s3_uploaded_bytes=100,
        **counters,
    )
    _measure(
        db_session,
        s3,
        NOW - timedelta(hours=2),
        s3_data_bytes=1500,
        s3_put_requests=40,
        s3_uploaded_bytes=400,
        **counters,
    )
    # The newest row has no GC figure: the change uses the newest one that has.
    _measure(db_session, s3, NOW - timedelta(minutes=5), s3_put_requests=50, s3_uploaded_bytes=500, **counters)

    body = client.get(f"/api/v1/datastores/{s3}/usage", headers=platform_admin_headers).json()
    assert body["backend_type"] == "s3"
    assert body["forecast"]["status"] == "not_applicable"
    assert body["daily_change_bytes"] == 500
    assert body["s3_traffic_24h"]["s3_put_requests"] == 40
    assert body["s3_traffic_24h"]["s3_uploaded_bytes"] == 400
    assert body["latest"]["s3_put_requests"] == 50


def test_alarm_names_the_cache_and_skips_the_forecast(client, platform_admin_headers, db_session, monkeypatch):
    a = _create_pbs_instance(client, platform_admin_headers, "Site A", "pbs-a.example")
    s3 = _import_datastore(client, platform_admin_headers, a, monkeypatch, "s3-test")
    _make_s3(db_session, s3)
    # Fast growth on the cache disk for 10 days -- would be "full soon" on a filesystem datastore.
    for day in range(10, 0, -1):
        _measure(db_session, s3, NOW - timedelta(days=day), used_bytes=86 - day, available_bytes=14 + day)
    _measure(db_session, s3, NOW, used_bytes=86, available_bytes=14)

    alarms = [
        alarm
        for alarm in client.get("/api/v1/alarms", headers=platform_admin_headers).json()
        if alarm["entity_id"] == s3
    ]
    assert [alarm["type"] for alarm in alarms] == ["datastore_usage_warning"]
    assert alarms[0]["message"] == "s3-test: local S3 cache filesystem 86.0% used"


def test_billing_uses_bucket_size_for_s3(client, platform_admin_headers, db_session, monkeypatch):
    a = _create_pbs_instance(client, platform_admin_headers, "Site A", "pbs-a.example")
    s3 = _import_datastore(client, platform_admin_headers, a, monkeypatch, "s3-test")
    _make_s3(db_session, s3)
    customer = _create_customer(db_session, "acme")
    _assign(db_session, s3, customer)
    from resurgam_api.models.inventory import BackupGroup, BackupSnapshot

    group = BackupGroup(datastore_id=uuid.UUID(s3), backup_type="vm", backup_id="1", namespace="")
    db_session.add(group)
    db_session.flush()
    db_session.add(BackupSnapshot(backup_group_id=group.id, snapshot_time=NOW, size_bytes=1000))
    db_session.commit()

    # No GC yet: the bucket size is unknown, the estimate says so.
    _measure(db_session, s3, NOW - timedelta(hours=1), used_bytes=900_000, logical_bytes=1000)
    body = client.get(f"/api/v1/billing/customers/{customer}", headers=platform_admin_headers).json()
    assert body["physical_bytes_estimate_complete"] is False
    assert body["estimated_physical_bytes"] == 0

    _measure(db_session, s3, NOW, used_bytes=900_000, logical_bytes=1000, s3_data_bytes=400)
    body = client.get(f"/api/v1/billing/customers/{customer}", headers=platform_admin_headers).json()
    assert body["physical_bytes_estimate_complete"] is True
    assert body["estimated_physical_bytes"] == 400
