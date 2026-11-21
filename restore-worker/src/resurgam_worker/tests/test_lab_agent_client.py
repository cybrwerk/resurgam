import httpx
import pytest

from resurgam_worker.lab_agent.client import LabAgentApiError, LabAgentClient, _GuardedHttp


def _client_with_mock_transport(handler) -> LabAgentClient:
    client = LabAgentClient("https://resurgam.example.com/api/v1", "agent-token")
    client._client = _GuardedHttp(
        httpx.Client(
            base_url="https://resurgam.example.com/api/v1",
            headers={"Authorization": "Bearer agent-token"},
            transport=httpx.MockTransport(handler),
        )
    )
    return client


def test_list_jobs_parses_real_response_shape():
    payload = [
        {
            "member_id": "11111111-1111-1111-1111-111111111111",
            "pbs_host": "pbs1.example.internal",
            "pbs_port": 8007,
            "pbs_datastore": "backup-store",
            "pbs_namespace": "tenant-a",
            "backup_type": "vm",
            "backup_id": "114",
            "snapshot_time": "2023-10-20T00:25:29Z",
            "target_vmid": 114,
            "target_node": "pve1",
            "target_storage": "local-lvm",
            "vnet_name": "labe94e9",
            "pool": "resurgam-lab",
        }
    ]

    def handler(request):
        assert request.headers["Authorization"] == "Bearer agent-token"
        assert request.url.path == "/api/v1/lab-agent/jobs"
        return httpx.Response(200, json=payload)

    jobs = _client_with_mock_transport(handler).list_jobs()

    assert len(jobs) == 1
    assert jobs[0].member_id == "11111111-1111-1111-1111-111111111111"
    assert jobs[0].vnet_name == "labe94e9"
    assert jobs[0].pool == "resurgam-lab"


def test_list_jobs_raises_on_non_200():
    def handler(request):
        return httpx.Response(401, text="unauthorized")

    with pytest.raises(LabAgentApiError):
        _client_with_mock_transport(handler).list_jobs()


def test_claim_job_posts_to_expected_path():
    captured = {}

    def handler(request):
        captured["path"] = request.url.path
        return httpx.Response(204)

    _client_with_mock_transport(handler).claim_job("member-1")

    assert captured["path"] == "/api/v1/lab-agent/jobs/member-1/claim"


def test_claim_job_raises_on_conflict():
    def handler(request):
        return httpx.Response(409, json={"code": "not_queued"})

    with pytest.raises(LabAgentApiError):
        _client_with_mock_transport(handler).claim_job("member-1")


def test_report_status_sends_error_field_only_when_given():
    captured = {}

    def handler(request):
        captured["body"] = request.content
        return httpx.Response(204)

    client = _client_with_mock_transport(handler)
    client.report_status("member-1", "booted")

    assert b"error" not in captured["body"]


def test_report_status_includes_error_when_failed():
    captured = {}

    def handler(request):
        captured["body"] = request.content
        return httpx.Response(204)

    client = _client_with_mock_transport(handler)
    client.report_status("member-1", "failed", error="map failed")

    assert b"map failed" in captured["body"]


# --- Guest-exec File Indexing (ADR 0021) ------------------------------------


def test_list_file_index_jobs_parses_both_method_shapes():
    payload = [
        {"id": "job-1", "file_index_method": "guest_exec_live", "target_vmid": 114, "target_node": "pve1"},
        {
            "id": "job-2",
            "file_index_method": "guest_exec_ephemeral",
            "target_vmid": 999,
            "target_node": "pve1",
            "target_storage": "local-lvm",
            "pbs_host": "pbs.example",
            "pbs_port": 8007,
            "pbs_datastore": "store1",
            "pbs_namespace": "",
            "backup_type": "vm",
            "backup_id": "114",
            "snapshot_time": "2026-09-24T00:00:00Z",
            "pool": "resurgam-lab",
        },
    ]

    def handler(request):
        assert request.url.path == "/api/v1/lab-agent/file-index-jobs"
        return httpx.Response(200, json=payload)

    jobs = _client_with_mock_transport(handler).list_file_index_jobs()

    assert len(jobs) == 2
    assert jobs[0].file_index_method == "guest_exec_live"
    assert jobs[0].pbs_host is None
    assert jobs[1].file_index_method == "guest_exec_ephemeral"
    assert jobs[1].pbs_host == "pbs.example"


def test_claim_file_index_job_parses_claim_response():
    def handler(request):
        assert request.url.path == "/api/v1/lab-agent/file-index-jobs/job-1/claim"
        return httpx.Response(200, json={"since": "2026-09-20T00:00:00Z", "is_full_reindex": False})

    claim = _client_with_mock_transport(handler).claim_file_index_job("job-1")

    assert claim.since is not None
    assert claim.is_full_reindex is False


def test_submit_file_index_entries_batches_the_given_entries():
    from resurgam_worker.lab_agent.client import FileIndexEntry

    captured = {}

    def handler(request):
        captured["path"] = request.url.path
        captured["body"] = request.content
        return httpx.Response(204)

    entries = [FileIndexEntry(path="/etc/hostname", filename="hostname", type="file", size_bytes=4)]
    _client_with_mock_transport(handler).submit_file_index_entries("job-1", entries)

    assert captured["path"] == "/api/v1/lab-agent/file-index-jobs/job-1/entries"
    assert b"hostname" in captured["body"]


def test_submit_file_index_entries_no_ops_on_empty_list():
    def handler(request):
        raise AssertionError("should not make a request for an empty entry list")

    _client_with_mock_transport(handler).submit_file_index_entries("job-1", [])


def test_report_backup_completed_sends_expected_body():
    captured = {}

    def handler(request):
        captured["path"] = request.url.path
        captured["body"] = request.content
        return httpx.Response(204)

    _client_with_mock_transport(handler).report_backup_completed(114, "pve1", "2026-09-24T10:00:00Z")

    assert captured["path"] == "/api/v1/lab-agent/backup-completed"
    assert b'"vmid":114' in captured["body"]
    assert b'"node":"pve1"' in captured["body"]


@pytest.mark.parametrize(
    "exc",
    [httpx.ConnectError("connection refused"), httpx.ReadTimeout("timed out"), httpx.RemoteProtocolError("eof")],
)
def test_transport_errors_become_lab_agent_api_error(exc):
    def handler(request):
        raise exc

    client = _client_with_mock_transport(handler)
    with pytest.raises(LabAgentApiError) as info:
        client.list_setup_jobs()
    assert info.value.status_code == 0
    assert "unreachable" in str(info.value)
    with pytest.raises(LabAgentApiError):
        client.report_disk_mount_teardown("m1", "done")


def test_streaming_download_transport_error_becomes_lab_agent_api_error(tmp_path):
    def handler(request):
        raise httpx.ReadTimeout("timed out")

    client = _client_with_mock_transport(handler)
    with pytest.raises(LabAgentApiError) as info:
        client.download_setup_artifact("prober", str(tmp_path / "x.deb"))
    assert info.value.status_code == 0
