"""Regression tests locking in response shapes captured from a real PVE
9.2 cluster (token/host anonymized) — see
docs/milestone-1-pbs-pve-api-assumptions.md for the full verification
notes. `probe_pve` was verified 2026-09-14; `list_nodes`/`list_storages`/
`list_guests`/`check_vmid_free`/`next_free_vmid` were verified live
2026-09-16 by probing a real cluster directly (curl, using the dev
environment's own already-configured PveCluster token) before this code
was written, so these tests assert the real shapes, not guessed ones.
"""

import httpx

from resurgam_api.connectivity.models import TrustConfig, VzdumpJob
from resurgam_api.connectivity.pve_api import (
    PveWriteError,
    apply_sdn_changes,
    check_vmid_free,
    clone_vm,
    create_pool,
    create_sdn_vnet,
    create_sdn_zone,
    create_vzdump_job,
    delete_sdn_vnet,
    delete_vzdump_job,
    destroy_vm,
    get_own_permissions,
    get_pool_members,
    get_sdn_zone,
    get_vzdump_job,
    list_guests,
    list_nodes,
    list_pools,
    list_storages,
    list_vzdump_jobs,
    next_free_vmid,
    probe_pve,
    set_vzdump_job_enabled,
    start_vm_restore,
    stop_vm,
    update_vzdump_job,
)
from resurgam_api.models.pbs import TlsTrustMode

_TRUST = TrustConfig(trust_mode=TlsTrustMode.CA)


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


def test_probe_pve_parses_real_version_shape(monkeypatch):
    payload = {"data": {"release": "9.2", "repoid": "f6997e698c7933ea", "version": "9.2.11"}}
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse(payload))

    result = probe_pve("pve.example", 8006, _TRUST, "user@realm!token=secret")

    assert result.reachable is True
    assert result.version == "9.2.11"


def test_probe_pve_sends_pve_auth_header(monkeypatch):
    captured = {}

    def fake_get(url, headers=None, params=None, timeout=None, verify=None):
        captured["headers"] = headers
        return _FakeResponse({"data": {"version": "9.2.11"}})

    monkeypatch.setattr(httpx, "get", fake_get)

    probe_pve("pve.example", 8006, _TRUST, "user@realm!token=secret")

    # Confirmed live: PVE uses "=" between token name and secret, unlike
    # PBS's ":" — both were already correctly distinguished in the code.
    assert captured["headers"]["Authorization"] == "PVEAPIToken=user@realm!token=secret"


def test_list_nodes_parses_real_shape(monkeypatch):
    payload = {
        "data": [
            {
                "node": "pve1",
                "status": "online",
                "cpu": 0.0476595227976443,
                "maxmem": 49859919872,
                "uptime": 6002192,
            }
        ]
    }
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse(payload))

    result = list_nodes("pve.example", 8006, _TRUST, "user@realm!token=secret")

    assert result.reachable is True
    assert len(result.nodes) == 1
    assert result.nodes[0].node == "pve1"
    assert result.nodes[0].status == "online"


def test_list_storages_merges_cluster_config_and_node_status(monkeypatch):
    # Real shapes: GET /storage (cluster-wide config, has server/datastore/
    # namespace for type=pbs) and GET /nodes/{node}/storage (live status,
    # has avail/used/total but NOT server/datastore/namespace) must be
    # merged by `storage` id — confirmed live these are genuinely two
    # different endpoints with disjoint field sets.
    config_payload = {
        "data": [
            {
                "storage": "pbs-main",
                "type": "pbs",
                "content": "backup",
                "server": "pbs1.example.internal",
                "datastore": "backup-store",
                "namespace": "tenant-a",
                "shared": 1,
            },
            {"storage": "local-lvm", "type": "lvmthin", "content": "rootdir,images"},
            # NFS storages also carry a generic "server" field — real data
            # (ISO-NTL on the live cluster) confirmed this must NOT leak
            # into pbs_server, since it isn't a type: "pbs" storage.
            {"storage": "ISO-NTL", "type": "nfs", "content": "iso", "server": "172.20.0.21", "shared": 1},
        ]
    }
    status_payload = {
        "data": [
            {
                "storage": "pbs-main",
                "type": "pbs",
                "content": "backup",
                "active": 1,
                "avail": 2141124141056,
                "used": 2152268197888,
                "total": 4293392338944,
            },
            {
                "storage": "local-lvm",
                "type": "lvmthin",
                "content": "rootdir,images",
                "active": 1,
                "avail": 746626280011,
                "used": 177873818,
                "total": 924500099072,
            },
            {"storage": "ISO-NTL", "type": "nfs", "content": "iso", "active": 1, "avail": 1, "used": 1, "total": 2},
        ]
    }
    responses = iter([_FakeResponse(config_payload), _FakeResponse(status_payload)])
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: next(responses))

    result = list_storages("pve.example", 8006, _TRUST, "user@realm!token=secret", "pve1")

    assert result.reachable is True
    by_id = {s.storage: s for s in result.storages}
    pbs_storage = by_id["pbs-main"]
    assert pbs_storage.type == "pbs"
    assert pbs_storage.content == ["backup"]
    assert pbs_storage.pbs_server == "pbs1.example.internal"
    assert pbs_storage.pbs_datastore == "backup-store"
    assert pbs_storage.pbs_namespace == "tenant-a"
    assert pbs_storage.avail_bytes == 2141124141056
    assert pbs_storage.active is True

    local_lvm = by_id["local-lvm"]
    assert local_lvm.pbs_server is None
    assert local_lvm.content == ["rootdir", "images"]

    # regression: an NFS storage's own "server" field must not leak into
    # pbs_server just because both happen to have a "server" config key.
    iso_ntl = by_id["ISO-NTL"]
    assert iso_ntl.type == "nfs"
    assert iso_ntl.pbs_server is None
    assert iso_ntl.pbs_datastore is None


def test_list_guests_parses_real_cluster_resources_shape(monkeypatch):
    payload = {
        "data": [
            {"vmid": 100, "node": "pve1", "type": "qemu", "name": "WinSRV-Eval", "status": "stopped"},
            {"vmid": 101, "node": "pve1", "type": "lxc", "name": "docker", "status": "running"},
        ]
    }
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse(payload))

    result = list_guests("pve.example", 8006, _TRUST, "user@realm!token=secret")

    assert result.reachable is True
    assert len(result.guests) == 2
    assert result.guests[0].vmid == 100
    assert result.guests[0].guest_type == "qemu"
    assert result.guests[1].guest_type == "lxc"


def test_check_vmid_free_true_for_unclaimed_vmid(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse({"data": "999999"}))

    result = check_vmid_free("pve.example", 8006, _TRUST, "user@realm!token=secret", 999999)

    assert result.reachable is True
    assert result.free is True
    assert result.vmid == 999999


def test_check_vmid_free_false_for_taken_vmid_not_treated_as_error(monkeypatch):
    # Confirmed live: a taken vmid returns HTTP 400 with this exact error
    # shape — a real, expected outcome, not a connectivity failure.
    payload = {"data": None, "errors": {"vmid": "VM 100 already exists"}, "message": "Parameter verification failed.\n"}
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse(payload, status_code=400))

    result = check_vmid_free("pve.example", 8006, _TRUST, "user@realm!token=secret", 100)

    assert result.reachable is True
    assert result.free is False
    assert result.error is None


def test_check_vmid_free_reports_genuine_connectivity_error(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse({"message": "auth failed"}, status_code=401))

    result = check_vmid_free("pve.example", 8006, _TRUST, "user@realm!token=secret", 100)

    assert result.reachable is False
    assert result.free is None


def test_next_free_vmid_casts_string_response_to_int(monkeypatch):
    # Confirmed live: PVE returns the next free vmid as a *string*.
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse({"data": "109"}))

    result = next_free_vmid("pve.example", 8006, _TRUST, "user@realm!token=secret")

    assert result.reachable is True
    assert result.vmid == 109
    assert isinstance(result.vmid, int)


def test_list_storages_sends_vmid_as_query_param(monkeypatch):
    captured = {}

    def fake_get(url, headers=None, params=None, timeout=None, verify=None):
        captured["params"] = params
        return _FakeResponse({"data": "999999"})

    monkeypatch.setattr(httpx, "get", fake_get)

    check_vmid_free("pve.example", 8006, _TRUST, "user@realm!token=secret", 999999)

    assert captured["params"] == {"vmid": 999999}


def test_list_vzdump_jobs_parses_real_cluster_backup_shape(monkeypatch):
    # Confirmed live 2026-09-19 against a real PVE 9.2 cluster:
    # `enabled` is a real 0/1 int on the wire, not a boolean.
    payload = {
        "data": [
            {"enabled": 1, "schedule": "02:30", "storage": "pbs-main", "id": "backup-cda2bff1-c477", "type": "vzdump"},
            {"enabled": 0, "schedule": "05:00", "storage": "pbs-main", "id": "backup-9f1a2b3c-000", "all": 1},
        ]
    }
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse(payload))

    result = list_vzdump_jobs("pve.example", 8006, _TRUST, "user@realm!token=secret")

    assert result.reachable is True
    assert len(result.jobs) == 2
    assert result.jobs[0].enabled is True
    assert result.jobs[0].schedule == "02:30"
    assert result.jobs[1].enabled is False
    assert result.jobs[1].all_guests is True


def test_list_pools_parses_real_shape(monkeypatch):
    # Confirmed live 2026-09-27 against a real PVE 9.2 cluster.
    payload = {"data": [{"poolid": "resurgam-lab", "comment": "RESURGAM Recovery Lab"}]}
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse(payload))

    result = list_pools("pve.example", 8006, _TRUST, "user@realm!token=secret")

    assert result.reachable is True
    assert result.pools[0].poolid == "resurgam-lab"
    assert result.pools[0].comment == "RESURGAM Recovery Lab"


def test_set_vzdump_job_enabled_sends_partial_update(monkeypatch):
    captured = {}

    def fake_put(url, headers=None, data=None, timeout=None, verify=None):
        captured["url"] = url
        captured["data"] = data
        return _FakeResponse({"data": None})

    monkeypatch.setattr(httpx, "put", fake_put)

    set_vzdump_job_enabled(
        "pve.example", 8006, _TRUST, "user@realm!token=secret", 5.0, job_id="backup-abc123", enabled=False
    )

    assert captured["url"].endswith("/cluster/backup/backup-abc123")
    assert captured["data"] == {"enabled": 0}


def test_set_vzdump_job_enabled_raises_on_failure(monkeypatch):
    monkeypatch.setattr(httpx, "put", lambda *a, **kw: _FakeResponse({"message": "no such job"}, status_code=400))

    try:
        set_vzdump_job_enabled("pve.example", 8006, _TRUST, "user@realm!token=secret", 5.0, job_id="nope", enabled=True)
        assert False, "expected PveWriteError"
    except PveWriteError:
        pass


# --- vzdump job CRUD + capability gating (docs/adr/0023-backup-and-maintenance-job-administration) -----


def test_get_vzdump_job_parses_full_shape(monkeypatch):
    payload = {
        "data": {
            "id": "backup-abc123",
            "enabled": 1,
            "schedule": "02:30",
            "storage": "pbs-main",
            "vmid": "116,123",
            "mode": "snapshot",
            "compress": "zstd",
            "notes-template": "{{guestname}}",
            "mailto": "ops@example.com",
        }
    }
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse(payload))

    job = get_vzdump_job("pve.example", 8006, _TRUST, "user@realm!token=secret", 5.0, job_id="backup-abc123")

    assert job.id == "backup-abc123"
    assert job.mode == "snapshot"
    assert job.notes_template == "{{guestname}}"
    assert job.mailto == "ops@example.com"


def test_get_vzdump_job_returns_none_on_error(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse({"message": "not found"}, status_code=404))

    assert get_vzdump_job("pve.example", 8006, _TRUST, "user@realm!token=secret", 5.0, job_id="nope") is None


def test_create_vzdump_job_requires_explicit_id():
    """Confirmed live (2026-09-26): POST /cluster/backup's response is
    empty even on success, and real PVE-autogenerated IDs are random
    UUIDs with no creation-time ordering -- there's no reliable way to
    find "the job just created" afterward without knowing its ID up
    front. RESURGAM always generates and sends its own.
    """
    job = VzdumpJob(id="", enabled=True, schedule="03:00")
    try:
        create_vzdump_job("pve.example", 8006, _TRUST, "user@realm!token=secret", 5.0, job=job)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_create_vzdump_job_sends_id_and_omits_prune_backups_when_unset(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, data=None, timeout=None, verify=None):
        captured["data"] = data
        return _FakeResponse({"data": None})

    monkeypatch.setattr(httpx, "post", fake_post)

    job = VzdumpJob(id="resurgam-abc123def456", enabled=True, schedule="03:00", storage="pbs-main", all_guests=True)
    create_vzdump_job("pve.example", 8006, _TRUST, "user@realm!token=secret", 5.0, job=job)

    assert captured["data"]["id"] == "resurgam-abc123def456"
    assert captured["data"]["schedule"] == "03:00"
    assert captured["data"]["all"] == 1
    # No prune_keep_* set on the job -- omitted entirely, not sent as "".
    assert "prune-backups" not in captured["data"]
    assert "remove" not in captured["data"]


def test_create_vzdump_job_encodes_prune_keep_fields(monkeypatch):
    # Real vzdump-level retention is a real, live-found (2026-09-27)
    # feature some jobs on this environment already use -- see
    # VzdumpJob's own docstring for why this isn't the "retention
    # placement correction" being reversed, just no longer hidden.
    captured = {}
    monkeypatch.setattr(
        httpx, "post", lambda url, **kw: (captured.update(data=kw.get("data")), _FakeResponse({"data": None}))[1]
    )

    job = VzdumpJob(
        id="resurgam-retain1",
        enabled=True,
        schedule="03:00",
        prune_keep_daily=7,
        prune_keep_weekly=4,
        prune_keep_monthly=3,
    )
    create_vzdump_job("pve.example", 8006, _TRUST, "user@realm!token=secret", 5.0, job=job)

    assert captured["data"]["prune-backups"] == "keep-daily=7,keep-weekly=4,keep-monthly=3"


def test_get_vzdump_job_decodes_nested_prune_backups_object(monkeypatch):
    # Confirmed live 2026-09-27: GET returns prune-backups as a nested
    # JSON object with string-typed values, not the flat property string
    # the write side expects.
    payload = {
        "data": {
            "id": "backup-cda2bff1-c477",
            "enabled": 1,
            "schedule": "02:30",
            "prune-backups": {"keep-monthly": "3", "keep-weekly": "4", "keep-daily": "7"},
        }
    }
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse(payload))

    job = get_vzdump_job("pve.example", 8006, _TRUST, "user@realm!token=secret", 5.0, job_id="backup-cda2bff1-c477")

    assert job.prune_keep_daily == 7
    assert job.prune_keep_weekly == 4
    assert job.prune_keep_monthly == 3
    assert job.prune_keep_last is None


def test_create_vzdump_job_raises_on_failure(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _FakeResponse({"message": "already exists"}, status_code=400))

    job = VzdumpJob(id="resurgam-dup", enabled=True)
    try:
        create_vzdump_job("pve.example", 8006, _TRUST, "user@realm!token=secret", 5.0, job=job)
        assert False, "expected PveWriteError"
    except PveWriteError:
        pass


def test_update_vzdump_job_sends_only_given_changes(monkeypatch):
    captured = {}

    def fake_put(url, headers=None, data=None, timeout=None, verify=None):
        captured["url"] = url
        captured["data"] = data
        return _FakeResponse({"data": None})

    monkeypatch.setattr(httpx, "put", fake_put)

    update_vzdump_job(
        "pve.example",
        8006,
        _TRUST,
        "user@realm!token=secret",
        5.0,
        job_id="backup-abc123",
        changes={"schedule": "04:00"},
    )

    assert captured["url"].endswith("/cluster/backup/backup-abc123")
    assert captured["data"] == {"schedule": "04:00"}


def test_delete_vzdump_job_calls_delete(monkeypatch):
    captured = {}

    def fake_delete(url, headers=None, params=None, timeout=None, verify=None):
        captured["url"] = url
        return _FakeResponse({"data": None})

    monkeypatch.setattr(httpx, "delete", fake_delete)

    delete_vzdump_job("pve.example", 8006, _TRUST, "user@realm!token=secret", 5.0, job_id="backup-abc123")

    assert captured["url"].endswith("/cluster/backup/backup-abc123")


def test_delete_vzdump_job_raises_on_failure(monkeypatch):
    monkeypatch.setattr(httpx, "delete", lambda *a, **kw: _FakeResponse({"message": "no such job"}, status_code=400))

    try:
        delete_vzdump_job("pve.example", 8006, _TRUST, "user@realm!token=secret", 5.0, job_id="nope")
        assert False, "expected PveWriteError"
    except PveWriteError:
        pass


def test_get_own_permissions_parses_real_shape(monkeypatch):
    # Real shape confirmed live 2026-09-26 against the actual test
    # cluster's own configured token.
    payload = {"data": {"/": {"Sys.Audit": 1, "Sys.Modify": 1}, "/storage/pbs-main": {"Datastore.Allocate": 1}}}
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse(payload))

    result = get_own_permissions("pve.example", 8006, _TRUST, "user@realm!token=secret")

    assert result.reachable is True
    assert result.privileges["/"]["Sys.Modify"] == 1


def test_get_own_permissions_reports_unreachable_on_error(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse({"message": "unauthorized"}, status_code=401))

    result = get_own_permissions("pve.example", 8006, _TRUST, "user@realm!token=secret")

    assert result.reachable is False


def test_start_vm_restore_sends_live_restore_flag(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, data=None, timeout=None, verify=None):
        captured["data"] = data
        return _FakeResponse({"data": "UPID:pve:00001234:...:qmrestore:106:root@pam:"})

    monkeypatch.setattr(httpx, "post", fake_post)

    start_vm_restore(
        "pve.example",
        8006,
        _TRUST,
        "user@realm!token=secret",
        5.0,
        node="pve1",
        vmid=106,
        archive="pbs-main:backup/vm/106/...",
        storage="local",
        unique=True,
        force=False,
        live_restore=True,
    )

    assert captured["data"]["live-restore"] == 1


def test_start_vm_restore_sends_net0_when_given(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, data=None, timeout=None, verify=None):
        captured["data"] = data
        return _FakeResponse({"data": "UPID:pve:00001234:...:qmrestore:106:root@pam:"})

    monkeypatch.setattr(httpx, "post", fake_post)

    start_vm_restore(
        "pve.example",
        8006,
        _TRUST,
        "user@realm!token=secret",
        5.0,
        node="pve1",
        vmid=106,
        archive="pbs-main:backup/vm/106/...",
        storage="local",
        unique=True,
        force=False,
        net0="model=virtio,bridge=labe94e9",
    )

    assert captured["data"]["net0"] == "model=virtio,bridge=labe94e9"


def test_start_vm_restore_omits_live_restore_by_default(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, data=None, timeout=None, verify=None):
        captured["data"] = data
        return _FakeResponse({"data": "UPID:pve:00001234:...:qmrestore:106:root@pam:"})

    monkeypatch.setattr(httpx, "post", fake_post)

    start_vm_restore(
        "pve.example",
        8006,
        _TRUST,
        "user@realm!token=secret",
        5.0,
        node="pve1",
        vmid=106,
        archive="pbs-main:backup/vm/106/...",
        storage="local",
        unique=True,
        force=False,
    )

    assert "live-restore" not in captured["data"]


def test_get_sdn_zone_returns_none_when_absent(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse({"message": "no such zone"}, status_code=404))

    result = get_sdn_zone("pve.example", 8006, _TRUST, "user@realm!token=secret", 5.0, name="resurgam-lab")

    assert result is None


def test_get_sdn_zone_returns_none_for_real_500_shape(monkeypatch):
    # Confirmed live 2026-09-19: PVE reports a missing zone as HTTP 500,
    # not 404 -- the real shape this test locks in.
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *a, **kw: _FakeResponse({"message": "sdn zone 'resurgam' does not exist\n"}, status_code=500),
    )

    result = get_sdn_zone("pve.example", 8006, _TRUST, "user@realm!token=secret", 5.0, name="resurgam")

    assert result is None


def test_get_sdn_zone_returns_config_when_present(monkeypatch):
    monkeypatch.setattr(
        httpx, "get", lambda *a, **kw: _FakeResponse({"data": {"zone": "resurgam-lab", "type": "simple"}})
    )

    result = get_sdn_zone("pve.example", 8006, _TRUST, "user@realm!token=secret", 5.0, name="resurgam-lab")

    assert result == {"zone": "resurgam-lab", "type": "simple"}


def test_create_sdn_zone_sends_type(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, data=None, timeout=None, verify=None):
        captured["url"] = url
        captured["data"] = data
        return _FakeResponse({"data": None})

    monkeypatch.setattr(httpx, "post", fake_post)

    create_sdn_zone(
        "pve.example", 8006, _TRUST, "user@realm!token=secret", 5.0, name="resurgam-lab", zone_type="simple"
    )

    assert captured["url"].endswith("/cluster/sdn/zones")
    assert captured["data"] == {"zone": "resurgam-lab", "type": "simple"}


def test_create_sdn_vnet_sends_zone(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, data=None, timeout=None, verify=None):
        captured["data"] = data
        return _FakeResponse({"data": None})

    monkeypatch.setattr(httpx, "post", fake_post)

    create_sdn_vnet(
        "pve.example", 8006, _TRUST, "user@realm!token=secret", 5.0, vnet_name="lababcd1234", zone_name="resurgam-lab"
    )

    assert captured["data"] == {"vnet": "lababcd1234", "zone": "resurgam-lab"}


def test_delete_sdn_vnet_calls_delete(monkeypatch):
    captured = {}

    def fake_delete(url, headers=None, params=None, timeout=None, verify=None):
        captured["url"] = url
        return _FakeResponse({"data": None})

    monkeypatch.setattr(httpx, "delete", fake_delete)

    delete_sdn_vnet("pve.example", 8006, _TRUST, "user@realm!token=secret", 5.0, vnet_name="lababcd1234")

    assert captured["url"].endswith("/cluster/sdn/vnets/lababcd1234")


def test_delete_sdn_vnet_treats_already_gone_as_success(monkeypatch):
    # Confirmed live 2026-09-27: cleanup calling delete on a vnet a
    # previous attempt already removed gets this exact real 500 shape --
    # same "500, not 404" quirk as get_sdn_zone's own missing-object case.
    monkeypatch.setattr(
        httpx,
        "delete",
        lambda *a, **kw: _FakeResponse({"message": "sdn vnet 'lab183c8' does not exist\n"}, status_code=500),
    )

    delete_sdn_vnet("pve.example", 8006, _TRUST, "user@realm!token=secret", 5.0, vnet_name="lab183c8")  # must not raise


def test_delete_sdn_vnet_raises_on_a_real_failure(monkeypatch):
    monkeypatch.setattr(
        httpx, "delete", lambda *a, **kw: _FakeResponse({"message": "permission denied"}, status_code=403)
    )

    try:
        delete_sdn_vnet("pve.example", 8006, _TRUST, "user@realm!token=secret", 5.0, vnet_name="lab183c8")
        assert False, "expected PveWriteError"
    except PveWriteError:
        pass


def test_get_pool_members_parses_real_shape(monkeypatch):
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *a, **kw: _FakeResponse(
            {
                "data": {
                    "poolid": "resurgam-lab",
                    "comment": "RESURGAM Recovery Lab",
                    "members": [
                        {"vmid": 111, "node": "pve1", "type": "qemu", "status": "running"},
                        {"vmid": 114, "node": "pve1", "type": "qemu", "status": "stopped"},
                        {"storage": "local-lvm", "node": "pve1", "type": "storage"},  # no vmid -- must be skipped
                    ],
                }
            }
        ),
    )

    result = get_pool_members("pve.example", 8006, _TRUST, "user@realm!token=secret", 5.0, poolid="resurgam-lab")

    assert result.reachable is True
    assert result.member_vmids == frozenset({111, 114})


def test_get_pool_members_unreachable_reports_error(monkeypatch):
    monkeypatch.setattr(
        httpx, "get", lambda *a, **kw: _FakeResponse({"message": "pool does not exist"}, status_code=500)
    )

    result = get_pool_members("pve.example", 8006, _TRUST, "user@realm!token=secret", 5.0, poolid="resurgam-lab")

    assert result.reachable is False


def test_stop_vm_returns_upid(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _FakeResponse({"data": "UPID:pve:1:qmstop:111:root@pam:"}))

    upid = stop_vm("pve.example", 8006, _TRUST, "user@realm!token=secret", 5.0, node="pve1", vmid=111)

    assert upid == "UPID:pve:1:qmstop:111:root@pam:"


def test_stop_vm_returns_none_when_vm_does_not_exist(monkeypatch):
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **kw: _FakeResponse(
            {"message": "Configuration file 'nodes/pve1/qemu-server/111.conf' does not exist\n"}, status_code=500
        ),
    )

    upid = stop_vm("pve.example", 8006, _TRUST, "user@realm!token=secret", 5.0, node="pve1", vmid=111)

    assert upid is None


def test_destroy_vm_sends_purge_params(monkeypatch):
    captured = {}

    def fake_delete(url, headers=None, params=None, timeout=None, verify=None):
        captured["url"] = url
        captured["params"] = params
        return _FakeResponse({"data": "UPID:pve:1:qmdestroy:111:root@pam:"})

    monkeypatch.setattr(httpx, "delete", fake_delete)

    destroy_vm("pve.example", 8006, _TRUST, "user@realm!token=secret", 5.0, node="pve1", vmid=111)

    assert captured["url"].endswith("/nodes/pve1/qemu/111")
    assert captured["params"] == {"purge": 1, "destroy-unreferenced-disks": 1}


def test_destroy_vm_treats_already_gone_as_success(monkeypatch):
    monkeypatch.setattr(
        httpx,
        "delete",
        lambda *a, **kw: _FakeResponse(
            {"message": "Configuration file 'nodes/pve1/qemu-server/111.conf' does not exist\n"}, status_code=500
        ),
    )

    destroy_vm("pve.example", 8006, _TRUST, "user@realm!token=secret", 5.0, node="pve1", vmid=111)  # must not raise


def test_apply_sdn_changes_puts_reload(monkeypatch):
    captured = {}

    def fake_put(url, headers=None, data=None, timeout=None, verify=None):
        captured["url"] = url
        return _FakeResponse({"data": None})

    monkeypatch.setattr(httpx, "put", fake_put)

    apply_sdn_changes("pve.example", 8006, _TRUST, "user@realm!token=secret", 5.0)

    assert captured["url"].endswith("/cluster/sdn")


def test_create_pool_sends_poolid_and_comment(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, data=None, timeout=None, verify=None):
        captured["data"] = data
        return _FakeResponse({"data": None})

    monkeypatch.setattr(httpx, "post", fake_post)

    create_pool(
        "pve.example", 8006, _TRUST, "user@realm!token=secret", 5.0, poolid="resurgam-lab", comment="Recovery Lab"
    )

    assert captured["data"] == {"poolid": "resurgam-lab", "comment": "Recovery Lab"}


def test_clone_vm_sends_expected_params(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, data=None, timeout=None, verify=None):
        captured["url"] = url
        captured["data"] = data
        return _FakeResponse({"data": "UPID:pve:00001234:...:qmclone:900:root@pam:"})

    monkeypatch.setattr(httpx, "post", fake_post)

    upid = clone_vm(
        "pve.example",
        8006,
        _TRUST,
        "user@realm!token=secret",
        5.0,
        node="pve1",
        source_vmid=9000,
        target_vmid=901,
        name="lab-appliance-901",
        target_node="pve1",
        target_storage="local",
        pool="resurgam-lab",
    )

    assert upid == "UPID:pve:00001234:...:qmclone:900:root@pam:"
    assert captured["url"].endswith("/nodes/pve1/qemu/9000/clone")
    assert captured["data"] == {
        "newid": 901,
        "name": "lab-appliance-901",
        "full": 1,
        "target": "pve1",
        "storage": "local",
        "pool": "resurgam-lab",
    }


def test_clone_vm_raises_on_failure(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _FakeResponse({"message": "no such vm"}, status_code=400))

    try:
        clone_vm(
            "pve.example",
            8006,
            _TRUST,
            "user@realm!token=secret",
            5.0,
            node="pve1",
            source_vmid=9000,
            target_vmid=901,
            name="lab-appliance-901",
        )
        assert False, "expected PveWriteError"
    except PveWriteError:
        pass
