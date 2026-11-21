"""Tests for the out-of-process file-restore job consumer (ADR 0013):
claim/expire/run semantics, and dispatch to the CT (catalog) vs VM
(file_restore) `resurgam_worker.pbs_client` modules — the same dispatch
behavior `test_filerestore_api.py` used to test against the now-removed
synchronous GET endpoints, moved here since `browse.py` is only called
from the worker now.
"""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from resurgam_api.connectivity.models import DatastoreListResult, RemoteDatastore


@pytest.fixture
def test_settings(test_settings, tmp_path):
    """Download content is written to files now -- into this test's own directory."""
    return test_settings.model_copy(update={"file_restore_output_dir": str(tmp_path / "file-restore")})


def _fake_ct_extract(content: bytes):
    def fake(repository, snapshot, archive_name, path, out_path, **kw):
        Path(out_path).write_bytes(content)

    return fake


def _create_pbs_instance(client, headers) -> str:
    response = client.post(
        "/api/v1/pbs-instances",
        json={
            "display_name": "PBS",
            "host": "pbs1.internal.example",
            "port": 8007,
            "tls_trust_mode": "ca",
            "api_token": "resurgam@pbs!dev:secretvalue",
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


def _create_group(db_session, datastore_id, backup_type="ct", backup_id="100"):
    from resurgam_api.models.inventory import BackupGroup

    group = BackupGroup(datastore_id=datastore_id, backup_type=backup_type, backup_id=backup_id)
    db_session.add(group)
    db_session.flush()
    db_session.commit()
    return group.id


def _add_snapshot(db_session, group_id, snapshot_time=None):
    from resurgam_api.models.inventory import BackupSnapshot

    snapshot = BackupSnapshot(
        backup_group_id=group_id, snapshot_time=snapshot_time or datetime(2023, 9, 20, 0, 0, 3, tzinfo=UTC)
    )
    db_session.add(snapshot)
    db_session.flush()
    db_session.commit()
    return snapshot.id


def _setup_group(client, platform_admin_headers, db_session, monkeypatch, backup_type="ct", backup_id="100"):
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch)
    group_id = _create_group(db_session, datastore_id, backup_type=backup_type, backup_id=backup_id)
    snapshot_id = _add_snapshot(db_session, group_id)
    return group_id, snapshot_id


def _make_job(db_session, group_id, snapshot_id, *, job_type="list", status="pending", path="/", expires_in=600):
    from resurgam_api.models.filerestore_job import FileRestoreJob

    job = FileRestoreJob(
        job_type=job_type,
        status=status,
        backup_group_id=group_id,
        backup_snapshot_id=snapshot_id,
        path=path,
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    return job


# --- _claim_next_job -----------------------------------------------------


def test_claim_next_job_returns_none_on_empty_queue(db_session):
    from resurgam_api.worker.file_restore_worker import _claim_next_job

    assert _claim_next_job(db_session) is None


def test_claim_next_job_is_fifo_and_marks_running(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.models.filerestore_job import FileRestoreJob
    from resurgam_api.worker.file_restore_worker import _claim_next_job

    group_id, snapshot_id = _setup_group(client, platform_admin_headers, db_session, monkeypatch)
    older = _make_job(db_session, group_id, snapshot_id, path="/older")
    older.created_at = datetime.now(UTC) - timedelta(minutes=5)
    db_session.commit()
    _newer = _make_job(db_session, group_id, snapshot_id, path="/newer")

    claimed_id = _claim_next_job(db_session)
    assert claimed_id == older.id

    claimed = db_session.get(FileRestoreJob, claimed_id)
    assert claimed.status == "running"
    assert claimed.started_at is not None


# --- _expire_stale_jobs ---------------------------------------------------


def test_expire_stale_jobs_covers_pending_and_running(
    client, platform_admin_headers, db_session, monkeypatch, test_settings
):
    """A crashed worker leaves a job `running` forever with no heartbeat --
    the expiry sweep covering `running`, not just `pending`, is what makes
    that self-healing (ADR 0013). Its leftover files (partial content, staging) go with it.
    """
    from resurgam_api.filerestore import storage
    from resurgam_api.worker.file_restore_worker import _expire_stale_jobs

    group_id, snapshot_id = _setup_group(client, platform_admin_headers, db_session, monkeypatch)
    stale_pending = _make_job(db_session, group_id, snapshot_id, status="pending", expires_in=-1)
    stale_running = _make_job(db_session, group_id, snapshot_id, status="running", expires_in=-1)
    out = storage.ensure_output_dir(test_settings)
    (out / f"{stale_running.id}.part").write_bytes(b"leftover")
    (out / f"{stale_running.id}.staging").mkdir()
    fresh_pending = _make_job(db_session, group_id, snapshot_id, status="pending", expires_in=600)

    _expire_stale_jobs(db_session, test_settings)

    db_session.refresh(stale_pending)
    db_session.refresh(stale_running)
    db_session.refresh(fresh_pending)
    assert stale_pending.status == "expired"
    assert stale_running.status == "expired"
    assert fresh_pending.status == "pending"
    assert list(out.iterdir()) == []


def test_expire_stale_jobs_deletes_content_of_completed_jobs_past_their_window(
    client, platform_admin_headers, db_session, monkeypatch, test_settings
):
    from resurgam_api.filerestore import storage
    from resurgam_api.models.audit import AuditEvent
    from resurgam_api.worker.file_restore_worker import _expire_stale_jobs

    group_id, snapshot_id = _setup_group(client, platform_admin_headers, db_session, monkeypatch)
    out = storage.ensure_output_dir(test_settings)
    expired = _make_job(db_session, group_id, snapshot_id, job_type="archive", status="completed", expires_in=-1)
    expired.result_storage_name = f"{expired.id}.zip"
    expired.result_size_bytes = 3
    live = _make_job(db_session, group_id, snapshot_id, job_type="download", status="completed", expires_in=600)
    live.result_storage_name = f"{live.id}.bin"
    db_session.commit()
    (out / f"{expired.id}.zip").write_bytes(b"zip")
    (out / f"{live.id}.bin").write_bytes(b"bin")
    orphan = out / f"{uuid.uuid4()}.zip"
    orphan.write_bytes(b"left by a crash")
    unrelated = out / "README-not-a-job-file"
    unrelated.write_text("never touched")

    _expire_stale_jobs(db_session, test_settings)

    db_session.refresh(expired)
    db_session.refresh(live)
    assert expired.status == "completed"  # the job itself stays in the history
    assert expired.result_storage_name is None
    assert not (out / f"{expired.id}.zip").exists()
    assert live.result_storage_name == f"{live.id}.bin"
    assert (out / f"{live.id}.bin").exists()
    assert not orphan.exists()
    assert unrelated.exists()
    event = (
        db_session.query(AuditEvent).filter_by(action="file_restore_content_deleted", target_id=str(expired.id)).one()
    )
    assert event.detail["size_bytes"] == 3


# --- _run_job: dispatch, success and failure ------------------------------


def test_run_job_list_dispatches_ct_to_catalog_module(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    from resurgam_worker.pbs_client.models import CatalogEntry, CatalogEntryType

    from resurgam_api.filerestore import browse
    from resurgam_api.worker.file_restore_worker import _run_job

    group_id, snapshot_id = _setup_group(client, platform_admin_headers, db_session, monkeypatch, backup_type="ct")
    job = _make_job(db_session, group_id, snapshot_id, job_type="list", status="running", path="/")

    monkeypatch.setattr(
        browse.catalog,
        "dump_catalog",
        lambda *a, **kw: [
            CatalogEntry(path="/", type=CatalogEntryType.DIRECTORY),
            CatalogEntry(path="/etc", type=CatalogEntryType.DIRECTORY),
        ],
    )

    _run_job(db_session, test_settings, job)

    db_session.refresh(job)
    assert job.status == "completed"
    assert job.result == [{"path": "/etc", "name": "etc", "type": "directory", "size": None, "mtime": None}]
    assert job.completed_at is not None


def test_run_job_list_dispatches_vm_to_file_restore_module(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    from resurgam_worker.pbs_client.models import FileRestoreEntryType, FileRestoreListEntry

    from resurgam_api.filerestore import browse
    from resurgam_api.worker.file_restore_worker import _run_job

    group_id, snapshot_id = _setup_group(
        client, platform_admin_headers, db_session, monkeypatch, backup_type="vm", backup_id="205"
    )
    job = _make_job(db_session, group_id, snapshot_id, job_type="list", status="running", path="/")

    monkeypatch.setattr(
        browse.file_restore,
        "list_path",
        lambda *a, **kw: [
            FileRestoreListEntry(
                path="/boot", name="boot", type=FileRestoreEntryType.DIRECTORY, leaf=False, mtime=1758437733
            )
        ],
    )

    _run_job(db_session, test_settings, job)

    db_session.refresh(job)
    assert job.status == "completed"
    assert job.result == [
        {"path": "/boot", "name": "boot", "type": "directory", "size": None, "mtime": "2025-09-21T06:55:33Z"}
    ]


def test_run_job_download_writes_content_file(client, platform_admin_headers, db_session, test_settings, monkeypatch):
    from resurgam_api.filerestore import browse, storage
    from resurgam_api.models.audit import AuditEvent
    from resurgam_api.worker.file_restore_worker import _run_job

    group_id, snapshot_id = _setup_group(client, platform_admin_headers, db_session, monkeypatch, backup_type="ct")
    job = _make_job(db_session, group_id, snapshot_id, job_type="download", status="running", path="/etc/hosts")
    created_expiry = job.expires_at

    monkeypatch.setattr(browse.catalog, "extract_file_to_path", _fake_ct_extract(b"127.0.0.1 localhost\n"))

    _run_job(db_session, test_settings, job)

    db_session.refresh(job)
    assert job.status == "completed"
    assert job.result_filename == "hosts"
    assert job.result_size_bytes == len(b"127.0.0.1 localhost\n")
    assert job.result_storage_name == f"{job.id}.bin"
    assert (storage.output_dir(test_settings) / f"{job.id}.bin").read_bytes() == b"127.0.0.1 localhost\n"
    # the download window starts at completion
    assert job.expires_at >= created_expiry
    event = db_session.query(AuditEvent).filter_by(action="file_restore_job_completed", target_id=str(job.id)).one()
    assert event.detail == {
        "job_type": "download",
        "paths": ["/etc/hosts"],
        "filename": "hosts",
        "size_bytes": len(b"127.0.0.1 localhost\n"),
        "file_count": 1,
        "total_bytes": len(b"127.0.0.1 localhost\n"),
    }


def test_run_job_rejects_content_over_the_configured_size_limit(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    """Manifest §10.3 content quota -- now a disk-sized limit; nothing over it is kept."""
    from resurgam_api.filerestore import browse, storage
    from resurgam_api.worker.file_restore_worker import _run_job

    group_id, snapshot_id = _setup_group(client, platform_admin_headers, db_session, monkeypatch, backup_type="ct")
    job = _make_job(db_session, group_id, snapshot_id, job_type="download", status="running", path="/etc/hosts")

    monkeypatch.setattr(browse.catalog, "extract_file_to_path", _fake_ct_extract(b"x" * 1000))
    small_limit_settings = test_settings.model_copy(update={"file_restore_max_content_bytes": 10})

    _run_job(db_session, small_limit_settings, job)

    db_session.refresh(job)
    assert job.status == "failed"
    assert job.error_code == "content_too_large"
    assert job.result_storage_name is None
    assert list(storage.output_dir(test_settings).iterdir()) == []


def test_run_job_vm_job_triggers_best_effort_session_cleanup(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    """After a VM job finishes, the worker should try to stop whatever PBS
    session that snapshot might still have open (manifest §10.3) -- a
    no-op for CT/host jobs, which never open a session at all.
    """
    from resurgam_worker.pbs_client.models import FileRestoreEntryType, FileRestoreListEntry

    from resurgam_api.filerestore import browse
    from resurgam_api.worker.file_restore_worker import _run_job

    group_id, snapshot_id = _setup_group(
        client, platform_admin_headers, db_session, monkeypatch, backup_type="vm", backup_id="205"
    )
    job = _make_job(db_session, group_id, snapshot_id, job_type="list", status="running", path="/")

    monkeypatch.setattr(
        browse.file_restore,
        "list_path",
        lambda *a, **kw: [
            FileRestoreListEntry(path="/boot", name="boot", type=FileRestoreEntryType.DIRECTORY, leaf=False)
        ],
    )
    stopped = []
    monkeypatch.setattr(
        browse.file_restore, "status", lambda **kw: [_vm_session(snapshot_id="vm/205/2023-09-20T00:00:03Z")]
    )
    monkeypatch.setattr(browse.file_restore, "stop", lambda name, **kw: stopped.append(name))

    _run_job(db_session, test_settings, job)

    db_session.refresh(job)
    assert job.status == "completed"
    assert len(stopped) == 1


def test_run_job_ct_job_never_calls_session_status(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    """CT/host jobs never open a PBS restore-VM session -- cleanup must
    not even attempt to check for one.
    """
    from resurgam_api.filerestore import browse
    from resurgam_api.worker.file_restore_worker import _run_job

    group_id, snapshot_id = _setup_group(client, platform_admin_headers, db_session, monkeypatch, backup_type="ct")
    job = _make_job(db_session, group_id, snapshot_id, job_type="download", status="running", path="/etc/hosts")

    monkeypatch.setattr(browse.catalog, "extract_file_to_path", _fake_ct_extract(b"content"))

    def fail_if_called(**kw):
        raise AssertionError("status() must never be called for a CT/host job")

    monkeypatch.setattr(browse.file_restore, "status", fail_if_called)

    _run_job(db_session, test_settings, job)

    db_session.refresh(job)
    assert job.status == "completed"


def _vm_session(snapshot_id: str):
    from resurgam_worker.pbs_client.models import RestoreSessionStatus

    return RestoreSessionStatus(
        name=f"qemu_repo:ns/{snapshot_id}/2026-09-14T00:00:00Z", cid=1, pid=1, timeout=1200, uptime=1
    )


def test_run_job_maps_pbs_command_failure_to_failed_status(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    from resurgam_worker.pbs_client.errors import PbsCommandFailedError

    from resurgam_api.filerestore import browse
    from resurgam_api.worker.file_restore_worker import _run_job

    group_id, snapshot_id = _setup_group(client, platform_admin_headers, db_session, monkeypatch, backup_type="ct")
    job = _make_job(db_session, group_id, snapshot_id, job_type="list", status="running", path="/")

    def fake_dump_catalog(*a, **kw):
        raise PbsCommandFailedError(["proxmox-backup-client"], 1, "snapshot not found")

    monkeypatch.setattr(browse.catalog, "dump_catalog", fake_dump_catalog)

    _run_job(db_session, test_settings, job)

    db_session.refresh(job)
    assert job.status == "failed"
    assert job.error_code == "file_restore_failed"


def test_run_job_never_leaves_job_running_on_unexpected_exception(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    from resurgam_api.filerestore import browse
    from resurgam_api.worker.file_restore_worker import _run_job

    group_id, snapshot_id = _setup_group(client, platform_admin_headers, db_session, monkeypatch, backup_type="ct")
    job = _make_job(db_session, group_id, snapshot_id, job_type="list", status="running", path="/")

    def fake_dump_catalog(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(browse.catalog, "dump_catalog", fake_dump_catalog)

    _run_job(db_session, test_settings, job)

    db_session.refresh(job)
    assert job.status == "failed"
    assert job.error_code == "internal_error"


# --- SKIP LOCKED concurrency ------------------------------------------------


def test_claim_skips_row_locked_by_another_connection(engine):
    """The whole design rests on `SKIP LOCKED` genuinely skipping a row
    another connection is holding via `FOR UPDATE` but hasn't committed --
    `db_session`'s single-connection SAVEPOINT fixture can't exercise real
    cross-connection contention (nothing it writes is visible outside its
    own uncommitted SAVEPOINT), so this test sets up its own, separately
    committed data on real connections and cleans up explicitly, bypassing
    the fixture's rollback safety net.
    """
    from sqlalchemy.orm import sessionmaker

    from resurgam_api.models.datastore import Datastore
    from resurgam_api.models.filerestore_job import FileRestoreJob
    from resurgam_api.models.inventory import BackupGroup, BackupSnapshot
    from resurgam_api.models.pbs import PbsInstance
    from resurgam_api.models.secrets import SecretPurpose, SecretReference
    from resurgam_api.worker.file_restore_worker import _claim_next_job

    session_factory = sessionmaker(bind=engine)
    setup_session = session_factory()
    conn_a = session_factory()
    conn_b = session_factory()

    unique = uuid.uuid4().hex[:8]
    try:
        secret_ref = SecretReference(
            reference_key=f"skiplocked-test-{unique}", description="test", purpose=SecretPurpose.INFRA_CREDENTIAL
        )
        setup_session.add(secret_ref)
        setup_session.flush()
        instance = PbsInstance(
            display_name="skip-locked-test",
            host="pbs-skiplocked.example",
            port=8007,
            tls_trust_mode="ca",
            api_token_secret_ref_id=secret_ref.id,
        )
        setup_session.add(instance)
        setup_session.flush()
        datastore = Datastore(pbs_instance_id=instance.id, name=f"store-{unique}")
        setup_session.add(datastore)
        setup_session.flush()
        group = BackupGroup(datastore_id=datastore.id, backup_type="ct", backup_id="100")
        setup_session.add(group)
        setup_session.flush()
        snapshot = BackupSnapshot(backup_group_id=group.id, snapshot_time=datetime(2023, 9, 20, 0, 0, 3, tzinfo=UTC))
        setup_session.add(snapshot)
        setup_session.flush()
        job = FileRestoreJob(
            job_type="list",
            status="pending",
            backup_group_id=group.id,
            backup_snapshot_id=snapshot.id,
            path="/",
            expires_at=datetime.now(UTC) + timedelta(seconds=600),
        )
        setup_session.add(job)
        setup_session.commit()

        # Connection A claims (locks) the row but does not commit yet.
        claimed_a = _claim_next_job(conn_a)
        assert claimed_a == job.id

        # Connection B must skip it, not block or double-claim.
        claimed_b = _claim_next_job(conn_b)
        assert claimed_b is None

        conn_a.rollback()
    finally:
        setup_session.rollback()
        if "job" in locals():
            setup_session.query(FileRestoreJob).filter_by(id=job.id).delete()
        if "snapshot" in locals():
            setup_session.query(BackupSnapshot).filter_by(id=snapshot.id).delete()
        if "group" in locals():
            setup_session.query(BackupGroup).filter_by(id=group.id).delete()
        if "datastore" in locals():
            setup_session.query(Datastore).filter_by(id=datastore.id).delete()
        if "instance" in locals():
            setup_session.query(PbsInstance).filter_by(id=instance.id).delete()
        if "secret_ref" in locals():
            setup_session.query(SecretReference).filter_by(id=secret_ref.id).delete()
        setup_session.commit()
        setup_session.close()
        conn_a.close()
        conn_b.close()


# --- archive (folder / multi-selection ZIP) jobs -----------------------------------------------


def _make_archive_job(db_session, group_id, snapshot_id, paths, base):
    job = _make_job(db_session, group_id, snapshot_id, job_type="archive", status="running", path=base)
    job.paths = paths
    db_session.commit()
    return job


def _zip_contents(path):
    import zipfile

    with zipfile.ZipFile(path) as zf:
        return {info.filename: (zf.read(info) if not info.is_dir() else None) for info in zf.infolist()}


def _ct_catalog():
    from resurgam_worker.pbs_client.models import CatalogEntry, CatalogEntryType

    d, f = CatalogEntryType.DIRECTORY, CatalogEntryType.FILE
    return [
        CatalogEntry(path="/", type=d),
        CatalogEntry(path="/etc", type=d),
        CatalogEntry(path="/etc/nginx", type=d),
        CatalogEntry(path="/etc/nginx/nginx.conf", type=f, size=5),
        CatalogEntry(path="/etc/Grüsse.txt", type=f, size=3),
        CatalogEntry(path="/etc/broken", type=f, size=1),
        CatalogEntry(path="/etc/empty", type=d),
    ]


def test_run_job_archive_ct_builds_one_zip_from_a_multi_pattern_restore(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    from resurgam_api.filerestore import browse, storage
    from resurgam_api.models.audit import AuditEvent
    from resurgam_api.worker.file_restore_worker import _run_job

    group_id, snapshot_id = _setup_group(client, platform_admin_headers, db_session, monkeypatch, backup_type="ct")
    job = _make_archive_job(
        db_session, group_id, snapshot_id, ["/etc/Grüsse.txt", "/etc/broken", "/etc/missing", "/etc/nginx"], "/etc"
    )
    monkeypatch.setattr(browse.catalog, "dump_catalog", lambda *a, **kw: _ct_catalog())
    restore_calls = []

    def fake_restore_selection(repository, snapshot, archive_name, target_dir, paths, **kw):
        restore_calls.append((archive_name, paths, kw["guard"] is not None))
        root = Path(target_dir)
        (root / "etc" / "nginx").mkdir(parents=True)
        (root / "etc" / "nginx" / "nginx.conf").write_text("conf\n")
        (root / "etc" / "Grüsse.txt").write_text("hoi")
        return {"/etc/broken": "error at entry: Input/output error"}

    monkeypatch.setattr(browse.catalog, "restore_selection", fake_restore_selection)

    _run_job(db_session, test_settings, job)

    db_session.refresh(job)
    assert job.status == "completed", job.error_message
    # one restore call for everything that exists in the catalog
    assert restore_calls == [("root.pxar", ["/etc/Grüsse.txt", "/etc/broken", "/etc/nginx"], True)]
    assert job.result_filename == "ct-100-2023-09-20T000003Z.zip"
    assert job.result_storage_name == f"{job.id}.zip"
    zip_path = storage.output_dir(test_settings) / job.result_storage_name
    assert job.result_size_bytes == zip_path.stat().st_size
    contents = _zip_contents(zip_path)
    assert contents["Grüsse.txt"] == b"hoi"
    assert contents["nginx/nginx.conf"] == b"conf\n"
    assert "nginx/" in contents
    report = job.archive_report
    assert report["file_count"] == 2
    assert {s["path"] for s in report["skipped"]} == {"/etc/broken", "/etc/missing"}
    assert job.progress is None
    # staging is gone, only the ZIP remains
    assert [p.name for p in storage.output_dir(test_settings).iterdir()] == [f"{job.id}.zip"]
    event = db_session.query(AuditEvent).filter_by(action="file_restore_job_completed", target_id=str(job.id)).one()
    assert event.detail["paths"] == ["/etc/Grüsse.txt", "/etc/broken", "/etc/missing", "/etc/nginx"]
    assert event.detail["file_count"] == 2
    assert event.detail["skipped_count"] == 2
    assert event.detail["size_bytes"] == job.result_size_bytes


def test_run_job_archive_ct_rejects_a_selection_over_the_limit_before_restoring(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    from resurgam_api.filerestore import browse, storage
    from resurgam_api.worker.file_restore_worker import _run_job

    group_id, snapshot_id = _setup_group(client, platform_admin_headers, db_session, monkeypatch, backup_type="ct")
    job = _make_archive_job(db_session, group_id, snapshot_id, ["/etc"], "/")
    monkeypatch.setattr(browse.catalog, "dump_catalog", lambda *a, **kw: _ct_catalog())

    def must_not_restore(*a, **kw):
        raise AssertionError("the slow restore must not start for a selection over the limit")

    monkeypatch.setattr(browse.catalog, "restore_selection", must_not_restore)

    _run_job(db_session, test_settings.model_copy(update={"file_restore_max_content_bytes": 5}), job)

    db_session.refresh(job)
    assert job.status == "failed"
    assert job.error_code == "content_too_large"
    assert "FILE_RESTORE_MAX_CONTENT_BYTES" in job.error_message
    assert list(storage.output_dir(test_settings).iterdir()) == []


def test_run_job_archive_checks_free_disk_space_for_the_estimate(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    from resurgam_api.filerestore import browse, storage
    from resurgam_api.worker.file_restore_worker import _run_job

    group_id, snapshot_id = _setup_group(client, platform_admin_headers, db_session, monkeypatch, backup_type="ct")
    job = _make_archive_job(db_session, group_id, snapshot_id, ["/etc"], "/")
    monkeypatch.setattr(browse.catalog, "dump_catalog", lambda *a, **kw: _ct_catalog())
    # 100 bytes free, reserve 90: enough to start, not for 2 x 9 bytes of estimated content
    monkeypatch.setattr(storage, "disk_space", lambda s: storage.DiskSpace(free_bytes=100, total_bytes=1000))
    settings = test_settings.model_copy(update={"file_restore_min_free_bytes": 90})

    _run_job(db_session, settings, job)

    db_session.refresh(job)
    assert job.status == "failed"
    assert job.error_code == "insufficient_disk_space"


def test_disk_guard_aborts_when_free_space_drops_below_the_reserve(test_settings, monkeypatch):
    import pytest as _pytest

    from resurgam_api.filerestore import browse, storage

    free = {"value": 10_000}
    monkeypatch.setattr(storage, "disk_space", lambda s: storage.DiskSpace(free_bytes=free["value"], total_bytes=10**6))
    guard = browse._DiskGuard(test_settings.model_copy(update={"file_restore_min_free_bytes": 1000}))
    guard()  # fine
    free["value"] = 500
    with _pytest.raises(browse.FileRestoreError) as exc_info:
        guard()
    assert exc_info.value.code == "insufficient_disk_space"


def test_disk_guard_aborts_when_written_data_exceeds_the_limit(test_settings, monkeypatch):
    import pytest as _pytest

    from resurgam_api.filerestore import browse, storage

    free = {"value": 10_000}
    monkeypatch.setattr(storage, "disk_space", lambda s: storage.DiskSpace(free_bytes=free["value"], total_bytes=10**6))
    guard = browse._DiskGuard(
        test_settings.model_copy(update={"file_restore_max_content_bytes": 1000, "file_restore_min_free_bytes": 0})
    )
    free["value"] = 10_000 - 2_500  # 2.5 KB written, more than 2 x the 1 KB limit
    with _pytest.raises(browse.FileRestoreError) as exc_info:
        guard()
    assert exc_info.value.code == "content_too_large"


def test_run_job_archive_vm_merges_directory_zips_and_streams_files(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    import zipfile

    from resurgam_worker.pbs_client.errors import PbsCommandFailedError
    from resurgam_worker.pbs_client.models import FileRestoreEntryType, FileRestoreListEntry

    from resurgam_api.filerestore import browse, storage
    from resurgam_api.worker.file_restore_worker import _run_job

    base = "/drive-scsi0.img.fidx/part/2/Users"
    group_id, snapshot_id = _setup_group(
        client, platform_admin_headers, db_session, monkeypatch, backup_type="vm", backup_id="205"
    )
    job = _make_archive_job(
        db_session, group_id, snapshot_id, [f"{base}/Präsentation", f"{base}/notes:v2.txt", f"{base}/locked"], base
    )
    monkeypatch.setattr(browse.file_restore, "status", lambda **kw: [])
    listed = []

    def fake_list(repository, snapshot, path, **kw):
        listed.append(path)
        return [
            FileRestoreListEntry(
                path=f"{base}/Präsentation", name="Präsentation", type=FileRestoreEntryType.DIRECTORY, leaf=False
            ),
            FileRestoreListEntry(
                path=f"{base}/notes:v2.txt", name="notes:v2.txt", type=FileRestoreEntryType.FILE, leaf=True, size=4
            ),
            FileRestoreListEntry(
                path=f"{base}/locked", name="locked", type=FileRestoreEntryType.FILE, leaf=True, size=1
            ),
        ]

    extract_calls = []

    def fake_extract(repository, snapshot, path, out_path, *, as_zip=False, **kw):
        extract_calls.append((path, as_zip))
        if path.endswith("locked"):
            raise PbsCommandFailedError(["proxmox-file-restore"], 255, "Error: Permission denied")
        if as_zip:
            with zipfile.ZipFile(out_path, "w") as zf:  # what the restore daemon's zip_directory produces
                zf.writestr("Präsentation/", b"")
                zf.writestr("Präsentation/Folie 1.txt", b"slide")
        else:
            Path(out_path).write_bytes(b"memo")

    monkeypatch.setattr(browse.file_restore, "list_path", fake_list)
    monkeypatch.setattr(browse.file_restore, "extract_to_file", fake_extract)

    _run_job(db_session, test_settings, job)

    db_session.refresh(job)
    assert job.status == "completed", job.error_message
    assert listed == [base]  # one listing for the shared parent
    assert extract_calls == [
        (f"{base}/Präsentation", True),
        (f"{base}/notes:v2.txt", False),
        (f"{base}/locked", False),
    ]
    contents = _zip_contents(storage.output_dir(test_settings) / job.result_storage_name)
    assert contents["Präsentation/Folie 1.txt"] == b"slide"
    assert contents["notes_v2.txt"] == b"memo"  # ':' is not allowed on Windows
    report = job.archive_report
    assert report["skipped"][0]["path"] == f"{base}/locked"
    assert "Permission denied" in report["skipped"][0]["reason"]
    assert report["renamed"] == [{"path": f"{base}/notes:v2.txt", "archive_name": "notes_v2.txt"}]


def test_run_job_archive_fails_when_nothing_could_be_extracted(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    from resurgam_api.filerestore import browse, storage
    from resurgam_api.worker.file_restore_worker import _run_job

    group_id, snapshot_id = _setup_group(client, platform_admin_headers, db_session, monkeypatch, backup_type="ct")
    job = _make_archive_job(db_session, group_id, snapshot_id, ["/nope", "/also-nope"], "/")
    monkeypatch.setattr(browse.catalog, "dump_catalog", lambda *a, **kw: _ct_catalog())

    _run_job(db_session, test_settings, job)

    db_session.refresh(job)
    assert job.status == "failed"
    assert job.error_code == "nothing_extracted"
    assert "/nope: not found in the snapshot" in job.error_message
    assert list(storage.output_dir(test_settings).iterdir()) == []


def test_run_job_archive_records_progress_while_running(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    from resurgam_api.filerestore import browse
    from resurgam_api.worker import file_restore_worker
    from resurgam_api.worker.file_restore_worker import _run_job

    group_id, snapshot_id = _setup_group(client, platform_admin_headers, db_session, monkeypatch, backup_type="ct")
    job = _make_archive_job(db_session, group_id, snapshot_id, ["/etc/nginx"], "/etc")
    monkeypatch.setattr(browse.catalog, "dump_catalog", lambda *a, **kw: _ct_catalog())

    def fake_restore_selection(repository, snapshot, archive_name, target_dir, paths, **kw):
        (Path(target_dir) / "etc" / "nginx").mkdir(parents=True)
        (Path(target_dir) / "etc" / "nginx" / "nginx.conf").write_text("conf\n")
        return {}

    monkeypatch.setattr(browse.catalog, "restore_selection", fake_restore_selection)
    seen = []
    real_writer = file_restore_worker._progress_writer

    def recording_writer(db, j):
        write = real_writer(db, j)

        def wrapped(progress):
            seen.append(dict(progress))
            write(progress)

        return wrapped

    monkeypatch.setattr(file_restore_worker, "_progress_writer", recording_writer)

    _run_job(db_session, test_settings, job)

    assert seen[0] == {"phase": "extracting", "entries_done": 0, "entries_total": 1, "bytes_done": 0}
    assert seen[-1]["phase"] == "packing"
    assert seen[-1]["entries_done"] == 1


def test_run_job_archive_ct_keeps_a_selected_empty_directory(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    """`restore --pattern` doesn't create a selected directory with nothing below it (seen with
    proxmox-backup-client 4.2.8) -- it must still appear in the ZIP, not as "skipped"."""
    from resurgam_api.filerestore import browse, storage
    from resurgam_api.worker.file_restore_worker import _run_job

    group_id, snapshot_id = _setup_group(client, platform_admin_headers, db_session, monkeypatch, backup_type="ct")
    job = _make_archive_job(db_session, group_id, snapshot_id, ["/etc/empty", "/etc/Grüsse.txt"], "/etc")
    monkeypatch.setattr(browse.catalog, "dump_catalog", lambda *a, **kw: _ct_catalog())

    def fake_restore_selection(repository, snapshot, archive_name, target_dir, paths, **kw):
        (Path(target_dir) / "etc").mkdir(parents=True)
        (Path(target_dir) / "etc" / "Grüsse.txt").write_text("hoi")
        return {}

    monkeypatch.setattr(browse.catalog, "restore_selection", fake_restore_selection)

    _run_job(db_session, test_settings, job)

    db_session.refresh(job)
    assert job.status == "completed", job.error_message
    contents = _zip_contents(storage.output_dir(test_settings) / job.result_storage_name)
    assert "empty/" in contents
    assert job.archive_report["skipped"] == []


# --- lanes, deadlines, cancel, robustness ----------------------------------------------


def _set_status(db_session, job_id, status):
    from sqlalchemy import update

    from resurgam_api.models.filerestore_job import FileRestoreJob

    db_session.execute(update(FileRestoreJob).where(FileRestoreJob.id == job_id).values(status=status))
    db_session.commit()


def test_claim_is_per_lane_and_sets_the_run_deadline(
    client, platform_admin_headers, db_session, monkeypatch, test_settings
):
    from resurgam_api.models.filerestore_job import FileRestoreJob
    from resurgam_api.worker.file_restore_worker import (
        BROWSE_JOB_TYPES,
        RUN_DEADLINE_GRACE_SECONDS,
        TRANSFER_JOB_TYPES,
        _claim_next_job,
    )

    group_id, snapshot_id = _setup_group(client, platform_admin_headers, db_session, monkeypatch)
    archive = _make_job(db_session, group_id, snapshot_id, job_type="archive", path="/a")
    archive.created_at = datetime.now(UTC) - timedelta(minutes=5)
    db_session.commit()
    listing = _make_job(db_session, group_id, snapshot_id, job_type="list", path="/b")

    # The browse lane takes the listing even though the (older) archive waits.
    assert _claim_next_job(db_session, BROWSE_JOB_TYPES, test_settings) == listing.id
    assert _claim_next_job(db_session, BROWSE_JOB_TYPES, test_settings) is None
    before = datetime.now(UTC)
    assert _claim_next_job(db_session, TRANSFER_JOB_TYPES, test_settings) == archive.id
    claimed = db_session.get(FileRestoreJob, archive.id)
    budget = test_settings.file_restore_extract_timeout_seconds + RUN_DEADLINE_GRACE_SECONDS
    assert claimed.expires_at >= before + timedelta(seconds=budget - 5)


def test_pending_jobs_waiting_behind_a_long_job_are_not_expired(
    client, platform_admin_headers, db_session, monkeypatch, test_settings
):
    """The claim deadline only runs while the lane is idle: after every job the lane pushes it
    forward for whatever still waits, and its own sweep only covers its own job types."""
    from resurgam_api.worker.file_restore_worker import (
        BROWSE_JOB_TYPES,
        TRANSFER_JOB_TYPES,
        _expire_stale_pending,
        _extend_pending,
    )

    group_id, snapshot_id = _setup_group(client, platform_admin_headers, db_session, monkeypatch)
    waiting = _make_job(db_session, group_id, snapshot_id, job_type="download", expires_in=-60)
    stale_listing = _make_job(db_session, group_id, snapshot_id, job_type="list", expires_in=-60)

    _expire_stale_pending(db_session, test_settings, BROWSE_JOB_TYPES)
    db_session.refresh(waiting)
    db_session.refresh(stale_listing)
    assert stale_listing.status == "expired"
    assert waiting.status == "pending"  # not the browse lane's job

    _extend_pending(db_session, test_settings, TRANSFER_JOB_TYPES)  # the transfer lane finished a job
    _expire_stale_pending(db_session, test_settings, TRANSFER_JOB_TYPES)
    db_session.refresh(waiting)
    assert waiting.status == "pending"
    assert waiting.expires_at > datetime.now(UTC)


def test_main_sweep_leaves_pending_jobs_and_active_scratch_files_alone(
    client, platform_admin_headers, db_session, monkeypatch, test_settings
):
    from resurgam_api.filerestore import storage
    from resurgam_api.worker.file_restore_worker import _expire_stale_jobs

    group_id, snapshot_id = _setup_group(client, platform_admin_headers, db_session, monkeypatch)
    pending = _make_job(db_session, group_id, snapshot_id, job_type="download", expires_in=-60)
    cancelled_active = _make_job(db_session, group_id, snapshot_id, job_type="archive", status="cancelled")
    out = storage.ensure_output_dir(test_settings)
    (out / f"{cancelled_active.id}.part").write_bytes(b"still being written")

    _expire_stale_jobs(db_session, test_settings, pending_types=(), active_job_ids=frozenset({cancelled_active.id}))

    db_session.refresh(pending)
    assert pending.status == "pending"
    assert (out / f"{cancelled_active.id}.part").exists()


def test_fail_orphaned_running_jobs_at_start(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.worker.file_restore_worker import _fail_orphaned_running_jobs

    group_id, snapshot_id = _setup_group(client, platform_admin_headers, db_session, monkeypatch)
    left_over = _make_job(db_session, group_id, snapshot_id, job_type="archive", status="running")
    waiting = _make_job(db_session, group_id, snapshot_id, job_type="list")

    assert _fail_orphaned_running_jobs(db_session) == 1
    db_session.refresh(left_over)
    db_session.refresh(waiting)
    assert left_over.status == "failed" and left_over.error_code == "worker_restarted"
    assert waiting.status == "pending"


def test_cancelling_a_running_archive_stops_it_through_the_guard(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    from resurgam_api.filerestore import browse, storage
    from resurgam_api.worker.file_restore_worker import _run_job

    group_id, snapshot_id = _setup_group(client, platform_admin_headers, db_session, monkeypatch, backup_type="ct")
    job = _make_archive_job(db_session, group_id, snapshot_id, ["/etc/nginx"], "/etc")
    monkeypatch.setattr(browse.catalog, "dump_catalog", lambda *a, **kw: _ct_catalog())

    def fake_restore_selection(repository, snapshot, archive_name, target_dir, paths, **kw):
        (Path(target_dir) / "etc" / "nginx").mkdir(parents=True)
        _set_status(db_session, job.id, "cancelled")  # the user clicks Cancel mid-run
        kw["guard"]()  # what run_guarded does every couple of seconds
        raise AssertionError("the guard must have stopped the job")

    monkeypatch.setattr(browse.catalog, "restore_selection", fake_restore_selection)

    _run_job(db_session, test_settings, job)

    db_session.refresh(job)
    assert job.status == "cancelled"
    assert job.result_storage_name is None
    assert list(storage.output_dir(test_settings).iterdir()) == []


def test_a_cancel_racing_the_end_of_a_listing_is_not_overwritten(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    from resurgam_api.filerestore import browse
    from resurgam_api.worker.file_restore_worker import _run_job

    group_id, snapshot_id = _setup_group(client, platform_admin_headers, db_session, monkeypatch, backup_type="ct")
    job = _make_job(db_session, group_id, snapshot_id, job_type="list", status="running", path="/")

    def fake_list(*a, **kw):
        _set_status(db_session, job.id, "cancelled")
        return []

    monkeypatch.setattr(browse, "list_directory", fake_list)

    _run_job(db_session, test_settings, job)

    db_session.refresh(job)
    assert job.status == "cancelled"
    assert job.result is None


def test_disk_guard_enforces_the_overall_deadline(test_settings):
    import time as _time

    import pytest as _pytest

    from resurgam_api.filerestore import browse

    guard = browse._DiskGuard(test_settings, deadline=_time.monotonic() - 1)
    with _pytest.raises(browse.FileRestoreError) as exc:
        guard()
    assert exc.value.code == "deadline_exceeded"


def test_disk_guard_refuses_the_member_that_would_cross_the_limit(test_settings):
    import pytest as _pytest
    from resurgam_worker.zip_builder import ZipReport

    from resurgam_api.filerestore import browse

    settings = test_settings.model_copy(update={"file_restore_max_content_bytes": 100})
    guard = browse._DiskGuard(settings)
    guard(ZipReport(total_bytes=60, pending_bytes=40))  # exactly at the limit: fine
    with _pytest.raises(browse.FileRestoreError) as exc:
        guard(ZipReport(total_bytes=60, pending_bytes=41))
    assert exc.value.code == "content_too_large"


def test_archive_write_failure_fails_cleanly_and_deletes_the_partial_zip(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    from resurgam_worker.zip_builder import ZipBuilder, ZipWriteError

    from resurgam_api.filerestore import browse, storage
    from resurgam_api.worker.file_restore_worker import _run_job

    group_id, snapshot_id = _setup_group(client, platform_admin_headers, db_session, monkeypatch, backup_type="ct")
    job = _make_archive_job(db_session, group_id, snapshot_id, ["/etc/nginx"], "/etc")
    monkeypatch.setattr(browse.catalog, "dump_catalog", lambda *a, **kw: _ct_catalog())
    monkeypatch.setattr(browse.catalog, "restore_selection", lambda *a, **kw: {})

    def disk_full(self, *a, **kw):
        raise ZipWriteError(OSError(28, "No space left on device"))

    monkeypatch.setattr(ZipBuilder, "add_tree", disk_full)

    _run_job(db_session, test_settings, job)

    db_session.refresh(job)
    assert job.status == "failed"
    assert job.error_code == "archive_write_failed"
    assert "No space left on device" in job.error_message
    assert list(storage.output_dir(test_settings).iterdir()) == []


def test_archive_filename_never_exceeds_the_column(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.filerestore import browse
    from resurgam_api.models.inventory import BackupGroup, BackupSnapshot

    group_id, snapshot_id = _setup_group(client, platform_admin_headers, db_session, monkeypatch)
    group = db_session.get(BackupGroup, group_id)
    snapshot = db_session.get(BackupSnapshot, snapshot_id)

    long_doc = "r" * 250 + ".docx"
    name = browse.archive_filename(group, snapshot, [f"/data/{long_doc}"])
    assert len(name) == 255 and name.endswith(".docx.zip")
    name = browse.archive_filename(group, snapshot, ["/data/" + "d" * 255])
    assert len(name) == 255 and name.endswith(".zip")
    assert browse.archive_filename(group, snapshot, ["/data/short.txt"]) == "short.txt.zip"


def test_a_result_that_cannot_be_recorded_fails_the_job_instead_of_the_worker(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    from resurgam_api.filerestore import browse
    from resurgam_api.worker import file_restore_worker
    from resurgam_api.worker.file_restore_worker import _run_job

    group_id, snapshot_id = _setup_group(client, platform_admin_headers, db_session, monkeypatch, backup_type="ct")
    job = _make_job(db_session, group_id, snapshot_id, job_type="download", status="running", path="/etc/hosts")
    monkeypatch.setattr(browse.catalog, "extract_file_to_path", _fake_ct_extract(b"content"))

    def broken_audit(job):
        raise ValueError("value too long for type character varying(255)")

    monkeypatch.setattr(file_restore_worker, "download_audit_detail", broken_audit)

    _run_job(db_session, test_settings, job)  # must not raise

    db_session.refresh(job)
    assert job.status == "failed"
    assert job.error_code == "internal_error"
    assert job.result_storage_name is None


def test_lane_step_survives_an_unexpected_error(client, platform_admin_headers, db_session, test_settings, monkeypatch):
    from contextlib import nullcontext

    from resurgam_api.worker import file_restore_worker
    from resurgam_api.worker.file_restore_worker import BROWSE_JOB_TYPES, _Lane, _lane_step

    group_id, snapshot_id = _setup_group(client, platform_admin_headers, db_session, monkeypatch)
    job = _make_job(db_session, group_id, snapshot_id, job_type="list")

    def explode(db, settings, job):
        raise RuntimeError("boom")

    monkeypatch.setattr(file_restore_worker, "_run_job", explode)
    lane = _Lane("browse", BROWSE_JOB_TYPES)

    assert _lane_step(lambda: nullcontext(db_session), test_settings, lane) is True
    db_session.refresh(job)
    assert job.status == "failed" and job.error_code == "internal_error"
    assert lane.current()[0] is None
    assert _lane_step(lambda: nullcontext(db_session), test_settings, lane) is False


def test_hung_lane_is_detected_past_budget_and_grace(test_settings):
    import time as _time

    from resurgam_api.worker.file_restore_worker import (
        RUN_DEADLINE_GRACE_SECONDS,
        TRANSFER_JOB_TYPES,
        _hung_lane,
        _Lane,
    )

    lane = _Lane("transfer", TRANSFER_JOB_TYPES)
    lane.set(uuid.uuid4(), "archive")
    assert _hung_lane(test_settings, [lane]) is None
    lane.started = (
        _time.monotonic() - test_settings.file_restore_extract_timeout_seconds - RUN_DEADLINE_GRACE_SECONDS - 1
    )
    assert _hung_lane(test_settings, [lane]) is lane
