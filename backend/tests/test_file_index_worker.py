"""Tests for the out-of-process File Indexing job consumer (manifest §13,
ADR 0016): claim/heartbeat-staleness semantics, CT/host vs VM dispatch,
window pause/resume, cancellation, the VM per-run directory budget, and
scheduled-enqueue logic. Mirrors `test_file_restore_worker.py`'s
conventions (monkeypatch at the `browse.catalog`/`browse.file_restore`
call sites).
"""

from datetime import UTC, datetime, time, timedelta

from resurgam_api.connectivity.models import DatastoreListResult, RemoteDatastore


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


def _create_customer(db_session, name="Acme", slug="acme"):
    from resurgam_api.models.tenancy import Customer

    customer = Customer(
        name=name, slug=slug, debitor_number=f"debitor-{slug}", match_code=slug[:3].upper().ljust(3, "X")
    )
    db_session.add(customer)
    db_session.flush()
    db_session.commit()
    return customer.id


def _create_group(
    db_session, datastore_id, backup_type="ct", backup_id="100", customer_id=None, file_index_enabled=True
):
    from resurgam_api.models.inventory import BackupGroup

    group = BackupGroup(
        datastore_id=datastore_id,
        backup_type=backup_type,
        backup_id=backup_id,
        customer_id=customer_id,
        file_index_enabled=file_index_enabled,
    )
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


def _setup_group(
    client,
    platform_admin_headers,
    db_session,
    monkeypatch,
    *,
    backup_type="ct",
    backup_id="100",
    file_index_enabled=True,
):
    customer_id = _create_customer(db_session)
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch)
    group_id = _create_group(
        db_session,
        datastore_id,
        backup_type=backup_type,
        backup_id=backup_id,
        customer_id=customer_id,
        file_index_enabled=file_index_enabled,
    )
    snapshot_id = _add_snapshot(db_session, group_id)
    return customer_id, group_id, snapshot_id


def _make_index_job(db_session, group_id, snapshot_id, customer_id, *, status="pending", trigger="manual", **extra):
    from resurgam_api.models.file_index_job import FileIndexJob

    job = FileIndexJob(
        status=status,
        trigger=trigger,
        backup_group_id=group_id,
        backup_snapshot_id=snapshot_id,
        customer_id=customer_id,
        **extra,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    return job


def _make_settings(db_session, customer_id, **kwargs):
    from resurgam_api.models.customer_file_index_settings import CustomerFileIndexSettings

    defaults = {"enabled": True, "retention_days": 30}
    defaults.update(kwargs)
    settings_row = CustomerFileIndexSettings(customer_id=customer_id, **defaults)
    db_session.add(settings_row)
    db_session.commit()
    db_session.refresh(settings_row)
    return settings_row


# --- _claim_next_index_job -------------------------------------------------


def test_claim_next_index_job_returns_none_on_empty_queue(db_session):
    from resurgam_api.worker.file_index_worker import _claim_next_index_job

    assert _claim_next_index_job(db_session) is None


def test_claim_next_index_job_is_fifo_and_marks_running(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.models.file_index_job import FileIndexJob
    from resurgam_api.worker.file_index_worker import _claim_next_index_job

    customer_id, group_id, snapshot_id = _setup_group(client, platform_admin_headers, db_session, monkeypatch)
    older = _make_index_job(db_session, group_id, snapshot_id, customer_id)
    older.created_at = datetime.now(UTC) - timedelta(minutes=5)
    db_session.commit()
    _newer = _make_index_job(db_session, group_id, snapshot_id, customer_id)

    claimed_id = _claim_next_index_job(db_session)
    assert claimed_id == older.id

    claimed = db_session.get(FileIndexJob, claimed_id)
    assert claimed.status == "running"
    assert claimed.started_at is not None
    assert claimed.last_heartbeat_at is not None


def test_claim_next_index_job_also_claims_paused_jobs(client, platform_admin_headers, db_session, monkeypatch):
    """A `paused` job (waiting for its time window) is claimable exactly
    like `pending` -- the only difference is whether it has progress to
    resume from.
    """
    from resurgam_api.worker.file_index_worker import _claim_next_index_job

    customer_id, group_id, snapshot_id = _setup_group(client, platform_admin_headers, db_session, monkeypatch)
    paused = _make_index_job(db_session, group_id, snapshot_id, customer_id, status="paused", pending_paths=["/boot"])

    claimed_id = _claim_next_index_job(db_session)
    assert claimed_id == paused.id


# --- _sweep_stale_index_jobs -------------------------------------------------


def test_sweep_stale_index_jobs_covers_running_and_paused_not_pending(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    from resurgam_api.worker.file_index_worker import _sweep_stale_index_jobs

    customer_id, group_id, snapshot_id = _setup_group(client, platform_admin_headers, db_session, monkeypatch)
    stale_cutoff = datetime.now(UTC) - timedelta(seconds=test_settings.file_index_worker_heartbeat_timeout_seconds + 60)
    stale_running = _make_index_job(
        db_session, group_id, snapshot_id, customer_id, status="running", last_heartbeat_at=stale_cutoff
    )
    stale_paused = _make_index_job(
        db_session, group_id, snapshot_id, customer_id, status="paused", last_heartbeat_at=stale_cutoff
    )
    fresh_running = _make_index_job(
        db_session, group_id, snapshot_id, customer_id, status="running", last_heartbeat_at=datetime.now(UTC)
    )
    untouched_pending = _make_index_job(db_session, group_id, snapshot_id, customer_id, status="pending")

    _sweep_stale_index_jobs(db_session, test_settings)

    db_session.refresh(stale_running)
    db_session.refresh(stale_paused)
    db_session.refresh(fresh_running)
    db_session.refresh(untouched_pending)
    assert stale_running.status == "failed"
    assert stale_running.error_code == "worker_heartbeat_timeout"
    assert stale_paused.status == "failed"
    assert fresh_running.status == "running"
    assert untouched_pending.status == "pending"


# --- CT/host dispatch --------------------------------------------------------


def test_run_index_job_ct_host_indexes_full_catalog(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    from resurgam_worker.pbs_client.models import CatalogEntry, CatalogEntryType

    from resurgam_api.filerestore import browse
    from resurgam_api.models.file_index_entry import FileIndexEntry
    from resurgam_api.worker.file_index_worker import _run_index_job

    customer_id, group_id, snapshot_id = _setup_group(
        client, platform_admin_headers, db_session, monkeypatch, backup_type="ct"
    )
    job = _make_index_job(db_session, group_id, snapshot_id, customer_id, status="running")

    monkeypatch.setattr(
        browse.catalog,
        "dump_catalog",
        lambda *a, **kw: [
            CatalogEntry(path="/", type=CatalogEntryType.DIRECTORY),
            CatalogEntry(path="/etc", type=CatalogEntryType.DIRECTORY),
            CatalogEntry(path="/etc/hosts", type=CatalogEntryType.FILE, size=123, mtime="2023-02-20T06:23:01Z"),
        ],
    )

    _run_index_job(db_session, test_settings, job)

    db_session.refresh(job)
    assert job.status == "completed"
    assert job.entries_indexed == 2

    entries = db_session.query(FileIndexEntry).filter_by(file_index_job_id=job.id).all()
    paths = {e.path for e in entries}
    assert paths == {"/etc", "/etc/hosts"}
    hosts_entry = next(e for e in entries if e.path == "/etc/hosts")
    assert hosts_entry.filename == "hosts"
    assert hosts_entry.archive == "root.pxar"
    assert hosts_entry.size_bytes == 123
    assert hosts_entry.type == "file"
    assert hosts_entry.customer_id == customer_id


def test_run_index_job_ct_host_applies_exclude_patterns(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    """Raised directly by the user after watching a real VM's index job
    spend most of its time walking an irrelevant kernel-headers tree --
    exclusion patterns must filter the CT/host bulk dump too, not just
    the VM per-directory walk.
    """
    from resurgam_worker.pbs_client.models import CatalogEntry, CatalogEntryType

    from resurgam_api.filerestore import browse
    from resurgam_api.models.file_index_entry import FileIndexEntry
    from resurgam_api.models.inventory import BackupGroup
    from resurgam_api.worker.file_index_worker import _run_index_job

    customer_id, group_id, snapshot_id = _setup_group(
        client, platform_admin_headers, db_session, monkeypatch, backup_type="ct"
    )
    group = db_session.get(BackupGroup, group_id)
    group.file_index_exclude_patterns = [r"^/usr/src"]
    db_session.commit()
    job = _make_index_job(db_session, group_id, snapshot_id, customer_id, status="running")

    monkeypatch.setattr(
        browse.catalog,
        "dump_catalog",
        lambda *a, **kw: [
            CatalogEntry(path="/etc/hosts", type=CatalogEntryType.FILE, size=1),
            CatalogEntry(path="/usr/src", type=CatalogEntryType.DIRECTORY),
            CatalogEntry(path="/usr/src/linux-headers/vmlinux.h", type=CatalogEntryType.FILE, size=999),
        ],
    )

    _run_index_job(db_session, test_settings, job)

    db_session.refresh(job)
    assert job.status == "completed"
    paths = {e.path for e in db_session.query(FileIndexEntry).filter_by(file_index_job_id=job.id).all()}
    assert paths == {"/etc/hosts"}


def test_run_index_job_ct_host_resume_does_not_duplicate_entries(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    """CT/host resume re-issues the whole (cheap) `dump_catalog()` call --
    `ON CONFLICT DO NOTHING` on (file_index_job_id, path) must make
    re-running against a job that already has some entries a no-op for
    those rows, not a duplicate or a crash.
    """
    from resurgam_worker.pbs_client.models import CatalogEntry, CatalogEntryType

    from resurgam_api.filerestore import browse
    from resurgam_api.filerestore.index_entries import catalog_entries_to_rows
    from resurgam_api.models.file_index_entry import FileIndexEntry
    from resurgam_api.worker.file_index_worker import _insert_entries, _run_index_job

    customer_id, group_id, snapshot_id = _setup_group(
        client, platform_admin_headers, db_session, monkeypatch, backup_type="ct"
    )
    job = _make_index_job(db_session, group_id, snapshot_id, customer_id, status="paused")

    pre_existing = catalog_entries_to_rows(
        [CatalogEntry(path="/etc", type=CatalogEntryType.DIRECTORY)],
        file_index_job_id=job.id,
        backup_snapshot_id=snapshot_id,
        backup_group_id=group_id,
        customer_id=customer_id,
    )
    _insert_entries(db_session, pre_existing)
    db_session.commit()

    monkeypatch.setattr(
        browse.catalog,
        "dump_catalog",
        lambda *a, **kw: [
            CatalogEntry(path="/", type=CatalogEntryType.DIRECTORY),
            CatalogEntry(path="/etc", type=CatalogEntryType.DIRECTORY),
            CatalogEntry(path="/etc/hosts", type=CatalogEntryType.FILE, size=123, mtime="2023-02-20T06:23:01Z"),
        ],
    )

    job.status = "running"
    db_session.commit()
    _run_index_job(db_session, test_settings, job)

    db_session.refresh(job)
    assert job.status == "completed"
    entries = db_session.query(FileIndexEntry).filter_by(file_index_job_id=job.id).all()
    assert len(entries) == 2  # /etc (not duplicated) + /etc/hosts


# --- VM dispatch --------------------------------------------------------------


def test_run_index_job_vm_walks_directories_via_bfs(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    from resurgam_worker.pbs_client.models import FileRestoreEntryType, FileRestoreListEntry

    from resurgam_api.filerestore import browse
    from resurgam_api.models.file_index_entry import FileIndexEntry
    from resurgam_api.worker.file_index_worker import _run_index_job

    customer_id, group_id, snapshot_id = _setup_group(
        client, platform_admin_headers, db_session, monkeypatch, backup_type="vm", backup_id="205"
    )
    job = _make_index_job(db_session, group_id, snapshot_id, customer_id, status="running")

    responses = {
        "/": [
            FileRestoreListEntry(
                path="/drive-scsi0.img.fidx",
                name="drive-scsi0.img.fidx",
                type=FileRestoreEntryType.DIRECTORY,
                leaf=False,
            )
        ],
        "/drive-scsi0.img.fidx": [
            FileRestoreListEntry(
                path="/drive-scsi0.img.fidx/boot", name="boot", type=FileRestoreEntryType.DIRECTORY, leaf=False
            )
        ],
        "/drive-scsi0.img.fidx/boot": [
            FileRestoreListEntry(
                path="/drive-scsi0.img.fidx/boot/grub.cfg",
                name="grub.cfg",
                type=FileRestoreEntryType.FILE,
                leaf=True,
                size=42,
                mtime=1758437733,
            )
        ],
    }

    def fake_list_path(repository, snapshot, path, **kw):
        return responses[path]

    monkeypatch.setattr(browse.file_restore, "list_path", fake_list_path)
    fast_settings = test_settings.model_copy(update={"file_index_worker_vm_call_delay_seconds": 0.0})

    _run_index_job(db_session, fast_settings, job)

    db_session.refresh(job)
    assert job.status == "completed"
    assert job.pending_paths is None
    assert job.current_path is None

    entries = db_session.query(FileIndexEntry).filter_by(file_index_job_id=job.id).all()
    paths = {e.path for e in entries}
    assert paths == {"/drive-scsi0.img.fidx", "/drive-scsi0.img.fidx/boot", "/drive-scsi0.img.fidx/boot/grub.cfg"}
    grub_entry = next(e for e in entries if e.path == "/drive-scsi0.img.fidx/boot/grub.cfg")
    assert grub_entry.archive == "drive-scsi0.img.fidx"
    assert grub_entry.filename == "grub.cfg"
    assert grub_entry.size_bytes == 42


def test_run_index_job_vm_chunks_inserts_for_a_large_directory(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    """A single directory listing larger than the insert batch size must
    still be split into multiple INSERTs -- confirmed live against a real
    VM whose flat package-doc directory alone held 5900+ files, which blew
    past Postgres's 65535-bound-parameters-per-statement limit when
    inserted in one unchunked call.
    """
    from resurgam_worker.pbs_client.models import FileRestoreEntryType, FileRestoreListEntry

    from resurgam_api.filerestore import browse
    from resurgam_api.models.file_index_entry import FileIndexEntry
    from resurgam_api.worker.file_index_worker import _run_index_job

    customer_id, group_id, snapshot_id = _setup_group(
        client, platform_admin_headers, db_session, monkeypatch, backup_type="vm", backup_id="205"
    )
    job = _make_index_job(db_session, group_id, snapshot_id, customer_id, status="running")

    big_dir = [
        FileRestoreListEntry(
            path=f"/big/file{i}.txt",
            name=f"file{i}.txt",
            type=FileRestoreEntryType.FILE,
            leaf=True,
            size=1,
            mtime=1758437733,
        )
        for i in range(2500)
    ]

    def fake_list_path(repository, snapshot, path, **kw):
        if path == "/":
            return [FileRestoreListEntry(path="/big", name="big", type=FileRestoreEntryType.DIRECTORY, leaf=False)]
        if path == "/big":
            return big_dir
        return []

    monkeypatch.setattr(browse.file_restore, "list_path", fake_list_path)
    fast_settings = test_settings.model_copy(
        update={"file_index_worker_vm_call_delay_seconds": 0.0, "file_index_entries_insert_batch_size": 1000}
    )

    _run_index_job(db_session, fast_settings, job)

    db_session.refresh(job)
    assert job.status == "completed"
    assert job.error_code is None

    count = db_session.query(FileIndexEntry).filter_by(file_index_job_id=job.id).count()
    assert count == 2501  # /big itself + 2500 files


def test_run_index_job_vm_applies_exclude_patterns_and_skips_descent(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    """A matching directory is excluded entirely -- never indexed, and
    the walker never calls `list_path()` on it (confirmed by the fake
    raising if it's ever asked to list the excluded path), which is what
    actually saves the expensive per-directory subprocess cost.
    """
    from resurgam_worker.pbs_client.models import FileRestoreEntryType, FileRestoreListEntry

    from resurgam_api.filerestore import browse
    from resurgam_api.models.file_index_entry import FileIndexEntry
    from resurgam_api.models.inventory import BackupGroup
    from resurgam_api.worker.file_index_worker import _run_index_job

    customer_id, group_id, snapshot_id = _setup_group(
        client, platform_admin_headers, db_session, monkeypatch, backup_type="vm", backup_id="205"
    )
    group = db_session.get(BackupGroup, group_id)
    group.file_index_exclude_patterns = [r"/usr/src"]
    db_session.commit()
    job = _make_index_job(db_session, group_id, snapshot_id, customer_id, status="running")

    def fake_list_path(repository, snapshot, path, **kw):
        if path == "/":
            return [
                FileRestoreListEntry(path="/usr", name="usr", type=FileRestoreEntryType.DIRECTORY, leaf=False),
            ]
        if path == "/usr":
            return [
                FileRestoreListEntry(path="/usr/src", name="src", type=FileRestoreEntryType.DIRECTORY, leaf=False),
                FileRestoreListEntry(path="/usr/bin", name="bin", type=FileRestoreEntryType.DIRECTORY, leaf=False),
            ]
        if path == "/usr/src":
            raise AssertionError("must not descend into an excluded directory")
        if path == "/usr/bin":
            return []
        return []

    monkeypatch.setattr(browse.file_restore, "list_path", fake_list_path)
    fast_settings = test_settings.model_copy(update={"file_index_worker_vm_call_delay_seconds": 0.0})

    _run_index_job(db_session, fast_settings, job)

    db_session.refresh(job)
    assert job.status == "completed"
    paths = {e.path for e in db_session.query(FileIndexEntry).filter_by(file_index_job_id=job.id).all()}
    assert paths == {"/usr", "/usr/bin"}  # /usr/src itself and everything under it is gone


def test_run_index_job_vm_does_not_descend_into_symlinks_reported_as_directories(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    """Confirmed live against a real Ubuntu VM: `/usr/bin/X11` is a
    classic self-referential symlink (points back into its own parent),
    and `proxmox-file-restore list` reports it as `type: 'd'` (a plain
    directory) -- it resolves the symlink to its *target's* type before
    this walker ever sees it, never `'l'`. A first fix that filtered by
    `FileRestoreListEntry.type == SYMLINK` therefore never actually
    triggered for this case live: the walker looped
    `/X11/X11/X11/...` until the per-run directory budget cut it off,
    burning an entire run on one loop instead of the real tree. The real
    fix catches any path whose last two segments repeat, which stops it
    after exactly one repeat regardless of what `type` PBS reports.
    """
    from resurgam_worker.pbs_client.models import FileRestoreEntryType, FileRestoreListEntry

    from resurgam_api.filerestore import browse
    from resurgam_api.models.file_index_entry import FileIndexEntry
    from resurgam_api.worker.file_index_worker import _run_index_job

    customer_id, group_id, snapshot_id = _setup_group(
        client, platform_admin_headers, db_session, monkeypatch, backup_type="vm", backup_id="205"
    )
    job = _make_index_job(db_session, group_id, snapshot_id, customer_id, status="running")

    def fake_list_path(repository, snapshot, path, **kw):
        if path == "/":
            return [FileRestoreListEntry(path="/bin", name="bin", type=FileRestoreEntryType.DIRECTORY, leaf=False)]
        if path == "/bin":
            # A self-referential symlink back to its own parent -- PBS
            # reports it leaf=False AND type='d', identical to a real,
            # ordinary directory in every field this walker can see. Its
            # own listing (were it followed) would show the same real
            # sibling files *and* itself again -- e.g. real `/usr/bin`
            # contents alongside another `X11` entry pointing at itself.
            return [
                FileRestoreListEntry(path="/bin/X11", name="X11", type=FileRestoreEntryType.DIRECTORY, leaf=False),
                FileRestoreListEntry(
                    path="/bin/dbus-launch", name="dbus-launch", type=FileRestoreEntryType.FILE, leaf=True, size=1
                ),
            ]
        if path == "/bin/X11":
            # The walker is expected to list this once -- that's not yet
            # a repeat, and is exactly how the real files nested "inside"
            # the symlink loop are legitimately discovered once. Its own
            # listing looks identical to /bin's (the symlink target).
            return [
                FileRestoreListEntry(path="/bin/X11/X11", name="X11", type=FileRestoreEntryType.DIRECTORY, leaf=False),
                FileRestoreListEntry(
                    path="/bin/X11/dbus-launch", name="dbus-launch", type=FileRestoreEntryType.FILE, leaf=True, size=1
                ),
            ]
        raise AssertionError(f"must never list {path!r} -- /bin/X11/X11 repeats its own parent segment")

    monkeypatch.setattr(browse.file_restore, "list_path", fake_list_path)
    fast_settings = test_settings.model_copy(update={"file_index_worker_vm_call_delay_seconds": 0.0})

    _run_index_job(db_session, fast_settings, job)

    db_session.refresh(job)
    assert job.status == "completed"
    paths = {e.path for e in db_session.query(FileIndexEntry).filter_by(file_index_job_id=job.id).all()}
    # /bin/X11 is listed once (its own real content discovered), but the
    # loop stops there -- /bin/X11/X11 is indexed as an entry (it's a
    # real row in /bin/X11's own listing) but never itself walked into.
    assert paths == {"/bin", "/bin/dbus-launch", "/bin/X11", "/bin/X11/X11", "/bin/X11/dbus-launch"}


def test_run_index_job_vm_does_not_descend_into_symlinks_reported_as_symlinks(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    """Defense in depth for whatever case PBS *does* report `type: 'l'`
    for a browsable symlink, even though the live-confirmed case above
    shows it doesn't for a directory-resolving one.
    """
    from resurgam_worker.pbs_client.models import FileRestoreEntryType, FileRestoreListEntry

    from resurgam_api.filerestore import browse
    from resurgam_api.models.file_index_entry import FileIndexEntry
    from resurgam_api.worker.file_index_worker import _run_index_job

    customer_id, group_id, snapshot_id = _setup_group(
        client, platform_admin_headers, db_session, monkeypatch, backup_type="vm", backup_id="205"
    )
    job = _make_index_job(db_session, group_id, snapshot_id, customer_id, status="running")

    def fake_list_path(repository, snapshot, path, **kw):
        if path == "/":
            return [FileRestoreListEntry(path="/bin", name="bin", type=FileRestoreEntryType.DIRECTORY, leaf=False)]
        if path == "/bin":
            return [
                FileRestoreListEntry(path="/bin/link", name="link", type=FileRestoreEntryType.SYMLINK, leaf=False),
            ]
        raise AssertionError(f"must never list {path!r} -- that would mean the symlink was followed")

    monkeypatch.setattr(browse.file_restore, "list_path", fake_list_path)
    fast_settings = test_settings.model_copy(update={"file_index_worker_vm_call_delay_seconds": 0.0})

    _run_index_job(db_session, fast_settings, job)

    db_session.refresh(job)
    assert job.status == "completed"
    paths = {e.path for e in db_session.query(FileIndexEntry).filter_by(file_index_job_id=job.id).all()}
    assert paths == {"/bin", "/bin/link"}


def test_run_index_job_vm_skips_unmountable_partition_and_continues(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    """A partition `proxmox-file-restore` can't mount (e.g. an EFI/
    recovery partition with no supported filesystem) must be skipped, not
    fail the whole job -- confirmed live against a real Windows VM backup
    that hit exactly this on its EFI partition.
    """
    from resurgam_worker.pbs_client.errors import PathNotMountableError
    from resurgam_worker.pbs_client.models import FileRestoreEntryType, FileRestoreListEntry

    from resurgam_api.filerestore import browse
    from resurgam_api.models.file_index_entry import FileIndexEntry
    from resurgam_api.worker.file_index_worker import _run_index_job

    customer_id, group_id, snapshot_id = _setup_group(
        client, platform_admin_headers, db_session, monkeypatch, backup_type="vm", backup_id="205"
    )
    job = _make_index_job(db_session, group_id, snapshot_id, customer_id, status="running")

    def fake_list_path(repository, snapshot, path, **kw):
        if path == "/":
            return [
                FileRestoreListEntry(path="/efi", name="efi", type=FileRestoreEntryType.DIRECTORY, leaf=False),
                FileRestoreListEntry(path="/ntfs", name="ntfs", type=FileRestoreEntryType.DIRECTORY, leaf=False),
            ]
        if path == "/efi":
            raise PathNotMountableError(path, "all mounts failed or no supported file system")
        if path == "/ntfs":
            return [
                FileRestoreListEntry(
                    path="/ntfs/file.txt",
                    name="file.txt",
                    type=FileRestoreEntryType.FILE,
                    leaf=True,
                    size=1,
                    mtime=1758437733,
                )
            ]
        return []

    monkeypatch.setattr(browse.file_restore, "list_path", fake_list_path)
    fast_settings = test_settings.model_copy(update={"file_index_worker_vm_call_delay_seconds": 0.0})

    _run_index_job(db_session, fast_settings, job)

    db_session.refresh(job)
    assert job.status == "completed"
    assert job.error_code is None

    entries = db_session.query(FileIndexEntry).filter_by(file_index_job_id=job.id).all()
    paths = {e.path for e in entries}
    assert paths == {"/efi", "/ntfs", "/ntfs/file.txt"}


def test_run_index_job_vm_skips_unparseable_path_and_continues(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    """Regression test: `proxmox-file-restore list` returning a JSON shape
    `list_path` can't parse for one path (confirmed live 2026-09-20
    against a real Windows VM -- a bare JSON string instead of an entry
    array or the documented not-mountable error shape) must skip that
    one path, not fail the entire job and discard every other already-
    queued path's worth of remaining progress.
    """
    from resurgam_worker.pbs_client.errors import PbsOutputParseError
    from resurgam_worker.pbs_client.models import FileRestoreEntryType, FileRestoreListEntry

    from resurgam_api.filerestore import browse
    from resurgam_api.models.file_index_entry import FileIndexEntry
    from resurgam_api.worker.file_index_worker import _run_index_job

    customer_id, group_id, snapshot_id = _setup_group(
        client, platform_admin_headers, db_session, monkeypatch, backup_type="vm", backup_id="206"
    )
    job = _make_index_job(db_session, group_id, snapshot_id, customer_id, status="running")

    def fake_list_path(repository, snapshot, path, **kw):
        if path == "/":
            return [
                FileRestoreListEntry(path="/weird", name="weird", type=FileRestoreEntryType.DIRECTORY, leaf=False),
                FileRestoreListEntry(path="/ntfs", name="ntfs", type=FileRestoreEntryType.DIRECTORY, leaf=False),
            ]
        if path == "/weird":
            raise PbsOutputParseError(
                ["proxmox-file-restore"], '"unexpected string"', TypeError("string indices must be integers")
            )
        if path == "/ntfs":
            return [
                FileRestoreListEntry(
                    path="/ntfs/file.txt",
                    name="file.txt",
                    type=FileRestoreEntryType.FILE,
                    leaf=True,
                    size=1,
                    mtime=1758437733,
                )
            ]
        return []

    monkeypatch.setattr(browse.file_restore, "list_path", fake_list_path)
    fast_settings = test_settings.model_copy(update={"file_index_worker_vm_call_delay_seconds": 0.0})

    _run_index_job(db_session, fast_settings, job)

    db_session.refresh(job)
    assert job.status == "completed"
    assert job.error_code is None

    entries = db_session.query(FileIndexEntry).filter_by(file_index_job_id=job.id).all()
    paths = {e.path for e in entries}
    assert paths == {"/weird", "/ntfs", "/ntfs/file.txt"}


def test_run_index_job_vm_directory_budget_checkpoints_and_resumes(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    from resurgam_worker.pbs_client.models import FileRestoreEntryType, FileRestoreListEntry

    from resurgam_api.filerestore import browse
    from resurgam_api.worker.file_index_worker import _run_index_job

    customer_id, group_id, snapshot_id = _setup_group(
        client, platform_admin_headers, db_session, monkeypatch, backup_type="vm", backup_id="205"
    )
    job = _make_index_job(db_session, group_id, snapshot_id, customer_id, status="running")

    # Three top-level, unrelated directories -- a budget of 1 must stop
    # after the first and leave the other two queued.
    monkeypatch.setattr(
        browse.file_restore,
        "list_path",
        lambda repository, snapshot, path, **kw: (
            [
                FileRestoreListEntry(path="/a", name="a", type=FileRestoreEntryType.DIRECTORY, leaf=False),
                FileRestoreListEntry(path="/b", name="b", type=FileRestoreEntryType.DIRECTORY, leaf=False),
            ]
            if path == "/"
            else []
        ),
    )

    limited_settings = test_settings.model_copy(
        update={"file_index_worker_vm_max_directories_per_job_run": 1, "file_index_worker_vm_call_delay_seconds": 0.0}
    )
    _run_index_job(db_session, limited_settings, job)

    db_session.refresh(job)
    assert job.status == "pending"  # self-imposed checkpoint, immediately re-claimable
    assert job.pending_paths == ["/a", "/b"]
    assert job.entries_indexed == 2


def test_run_index_job_pauses_outside_configured_window(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    from resurgam_api.worker.file_index_worker import _run_index_job

    customer_id, group_id, snapshot_id = _setup_group(
        client, platform_admin_headers, db_session, monkeypatch, backup_type="ct"
    )
    # A window that can never contain "now", regardless of when the test
    # runs, by making start == end + 1 minute (a maximally narrow, always-
    # closed overnight window).
    now = datetime.now(UTC).time()
    closed_start = time((now.hour + 1) % 24, now.minute)
    closed_end = time((now.hour + 1) % 24, (now.minute + 1) % 60) if now.minute < 59 else time((now.hour + 2) % 24, 0)
    _make_settings(db_session, customer_id, window_start_time=closed_start, window_end_time=closed_end)
    job = _make_index_job(db_session, group_id, snapshot_id, customer_id, status="running")

    _run_index_job(db_session, test_settings, job)

    db_session.refresh(job)
    assert job.status == "paused"
    assert job.paused_at is not None


def test_run_index_job_cancellation_mid_run_keeps_partial_entries(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    from resurgam_worker.pbs_client.models import FileRestoreEntryType, FileRestoreListEntry

    from resurgam_api.filerestore import browse
    from resurgam_api.models.file_index_entry import FileIndexEntry
    from resurgam_api.worker.file_index_worker import _run_index_job

    customer_id, group_id, snapshot_id = _setup_group(
        client, platform_admin_headers, db_session, monkeypatch, backup_type="vm", backup_id="205"
    )
    job = _make_index_job(db_session, group_id, snapshot_id, customer_id, status="running")

    monkeypatch.setattr(
        browse.file_restore,
        "list_path",
        lambda repository, snapshot, path, **kw: (
            [
                FileRestoreListEntry(path="/a", name="a", type=FileRestoreEntryType.DIRECTORY, leaf=False),
                FileRestoreListEntry(path="/b", name="b", type=FileRestoreEntryType.DIRECTORY, leaf=False),
            ]
            if path == "/"
            else []
        ),
    )

    import resurgam_api.worker.file_index_worker as worker_module

    real_should_stop = worker_module._should_stop_for_cancel
    call_count = {"n": 0}

    def fake_should_stop(db, job_arg):
        call_count["n"] += 1
        if call_count["n"] == 2:
            job_arg.cancel_requested = True
            db.commit()
        return real_should_stop(db, job_arg)

    monkeypatch.setattr(worker_module, "_should_stop_for_cancel", fake_should_stop)
    fast_settings = test_settings.model_copy(update={"file_index_worker_vm_call_delay_seconds": 0.0})

    _run_index_job(db_session, fast_settings, job)

    db_session.refresh(job)
    assert job.status == "cancelled"
    # The root "/" listing (yielding "/a" and "/b" as directory entries)
    # completed before the second loop iteration's cancel check fired --
    # those entries must survive cancellation, not be discarded.
    entries = db_session.query(FileIndexEntry).filter_by(file_index_job_id=job.id).all()
    assert {e.path for e in entries} == {"/a", "/b"}


# --- reuse and link -------------------------------------------------------------


def test_run_index_job_links_unchanged_entries_to_prior_snapshot(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    """ "Reuse and link" (manifest §13, ADR 0016), raised directly by the
    user after watching a real VM re-index 250k+ largely unchanged
    entries on every snapshot: an entry whose (type, size_bytes, mtime)
    exactly matches the prior snapshot's entry at the same path links to
    it instead of duplicating its own size_bytes/mtime. A third
    generation, also unchanged, must link directly to the original root
    -- never chained through the second generation's own link.
    """
    from resurgam_worker.pbs_client.models import CatalogEntry, CatalogEntryType

    from resurgam_api.filerestore import browse
    from resurgam_api.models.file_index_entry import FileIndexEntry
    from resurgam_api.worker.file_index_worker import _run_index_job

    customer_id, group_id, snapshot1_id = _setup_group(
        client, platform_admin_headers, db_session, monkeypatch, backup_type="ct"
    )
    snapshot2_id = _add_snapshot(db_session, group_id, snapshot_time=datetime(2023, 9, 21, 0, 0, 3, tzinfo=UTC))
    snapshot3_id = _add_snapshot(db_session, group_id, snapshot_time=datetime(2023, 9, 22, 0, 0, 3, tzinfo=UTC))

    unchanged_entry = CatalogEntry(
        path="/etc/hosts", type=CatalogEntryType.FILE, size=123, mtime="2023-02-20T06:23:01Z"
    )

    job1 = _make_index_job(db_session, group_id, snapshot1_id, customer_id, status="running")
    monkeypatch.setattr(
        browse.catalog,
        "dump_catalog",
        lambda *a, **kw: [
            unchanged_entry,
            CatalogEntry(path="/etc/motd", type=CatalogEntryType.FILE, size=10, mtime="2023-02-20T06:23:01Z"),
        ],
    )
    _run_index_job(db_session, test_settings, job1)
    db_session.refresh(job1)
    assert job1.status == "completed"

    root_hosts = db_session.query(FileIndexEntry).filter_by(file_index_job_id=job1.id, path="/etc/hosts").one()
    assert root_hosts.linked_from_entry_id is None
    assert root_hosts.size_bytes == 123

    job2 = _make_index_job(db_session, group_id, snapshot2_id, customer_id, status="running")
    monkeypatch.setattr(
        browse.catalog,
        "dump_catalog",
        lambda *a, **kw: [
            unchanged_entry,
            CatalogEntry(
                path="/etc/motd", type=CatalogEntryType.FILE, size=20, mtime="2023-09-21T00:00:00Z"
            ),  # changed
        ],
    )
    _run_index_job(db_session, test_settings, job2)
    db_session.refresh(job2)
    assert job2.status == "completed"

    hosts2 = db_session.query(FileIndexEntry).filter_by(file_index_job_id=job2.id, path="/etc/hosts").one()
    assert hosts2.linked_from_entry_id == root_hosts.id
    assert hosts2.size_bytes is None  # not duplicated -- resolved via the link
    assert hosts2.mtime is None

    motd2 = db_session.query(FileIndexEntry).filter_by(file_index_job_id=job2.id, path="/etc/motd").one()
    assert motd2.linked_from_entry_id is None  # changed -- full row, not linked
    assert motd2.size_bytes == 20

    job3 = _make_index_job(db_session, group_id, snapshot3_id, customer_id, status="running")
    monkeypatch.setattr(browse.catalog, "dump_catalog", lambda *a, **kw: [unchanged_entry])
    _run_index_job(db_session, test_settings, job3)
    db_session.refresh(job3)
    assert job3.status == "completed"

    hosts3 = db_session.query(FileIndexEntry).filter_by(file_index_job_id=job3.id, path="/etc/hosts").one()
    assert hosts3.linked_from_entry_id == root_hosts.id  # one hop, not chained through hosts2


def test_sweep_expired_retention_keeps_rows_still_linked_by_a_newer_snapshot(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    """A root row past its own retention cutoff survives as long as a
    newer, still-in-retention row links to it for its data -- confirmed
    via AskUserQuestion with the user as the accepted tradeoff for "reuse
    and link" (retention_days becomes a floor, not an exact cutoff, for a
    row something newer still depends on). Once nothing links to it any
    more, it's purged on the next sweep like any other expired row.
    """
    from resurgam_api.models.file_index_entry import FileIndexEntry
    from resurgam_api.worker.file_index_worker import _sweep_expired_retention

    customer_id, group_id, old_snapshot_id = _setup_group(
        client, platform_admin_headers, db_session, monkeypatch, backup_type="ct"
    )
    new_snapshot_id = _add_snapshot(db_session, group_id, snapshot_time=datetime(2023, 9, 21, 0, 0, 3, tzinfo=UTC))
    _make_settings(db_session, customer_id, retention_days=1)
    old_job = _make_index_job(db_session, group_id, old_snapshot_id, customer_id, status="completed")
    new_job = _make_index_job(db_session, group_id, new_snapshot_id, customer_id, status="completed")

    root = FileIndexEntry(
        file_index_job_id=old_job.id,
        backup_snapshot_id=old_snapshot_id,
        backup_group_id=group_id,
        customer_id=customer_id,
        archive="root.pxar",
        path="/etc/hosts",
        filename="hosts",
        type="file",
        size_bytes=123,
    )
    db_session.add(root)
    db_session.flush()
    root_id = root.id  # captured before any delete -- the object itself gets detached after commit
    linker = FileIndexEntry(
        file_index_job_id=new_job.id,
        backup_snapshot_id=new_snapshot_id,
        backup_group_id=group_id,
        customer_id=customer_id,
        archive="root.pxar",
        path="/etc/hosts",
        filename="hosts",
        type="file",
        linked_from_entry_id=root_id,
    )
    db_session.add(linker)
    db_session.commit()
    linker_id = linker.id
    # Only the root is past the 1-day cutoff -- the linker (a fresh row
    # from "today's" run) is well within retention.
    db_session.query(FileIndexEntry).filter(FileIndexEntry.id == root_id).update(
        {"created_at": datetime.now(UTC) - timedelta(days=5)}, synchronize_session=False
    )
    db_session.commit()

    purged = _sweep_expired_retention(db_session)
    assert purged == 0
    assert db_session.get(FileIndexEntry, root_id) is not None  # still referenced -- must survive

    # Once the linker is gone (e.g. its own retention catches up later),
    # nothing references the root any more and it becomes purgeable.
    db_session.query(FileIndexEntry).filter(FileIndexEntry.id == linker_id).delete(synchronize_session=False)
    db_session.commit()
    purged_again = _sweep_expired_retention(db_session)
    assert purged_again == 1
    assert db_session.get(FileIndexEntry, root_id) is None


# --- scheduled enqueue --------------------------------------------------------


def test_enqueue_due_scheduled_work_targets_only_the_newest_snapshot(
    client, platform_admin_headers, db_session, monkeypatch
):
    from resurgam_api.models.file_index_entry import FileIndexEntry
    from resurgam_api.models.file_index_job import FileIndexJob
    from resurgam_api.worker.file_index_worker import _enqueue_due_scheduled_work

    customer_id, group_id, snapshot_id = _setup_group(client, platform_admin_headers, db_session, monkeypatch)
    already_indexed_snapshot_id = _add_snapshot(db_session, group_id, snapshot_time=datetime(2023, 9, 21, tzinfo=UTC))
    in_flight_snapshot_id = _add_snapshot(db_session, group_id, snapshot_time=datetime(2023, 9, 22, tzinfo=UTC))
    due_snapshot_id = _add_snapshot(db_session, group_id, snapshot_time=datetime(2023, 9, 23, tzinfo=UTC))

    fake_job_for_entry = _make_index_job(
        db_session, group_id, already_indexed_snapshot_id, customer_id, status="completed"
    )
    db_session.add(
        FileIndexEntry(
            file_index_job_id=fake_job_for_entry.id,
            backup_snapshot_id=already_indexed_snapshot_id,
            backup_group_id=group_id,
            customer_id=customer_id,
            archive="root.pxar",
            path="/etc",
            filename="etc",
            type="directory",
        )
    )
    _make_index_job(db_session, group_id, in_flight_snapshot_id, customer_id, status="running")
    db_session.commit()

    _make_settings(
        db_session,
        customer_id,
        schedule_enabled=True,
        schedule_interval_hours=6,
        next_scheduled_run_at=datetime.now(UTC) - timedelta(minutes=1),
    )

    _enqueue_due_scheduled_work(db_session)

    scheduled_jobs = db_session.query(FileIndexJob).filter_by(trigger="scheduled").all()
    scheduled_snapshot_ids = {j.backup_snapshot_id for j in scheduled_jobs}
    # Only the newest snapshot -- `_setup_group`'s own older, never-indexed
    # snapshot (`snapshot_id`) is NOT backfilled by the schedule (a VM with
    # hundreds of snapshots would otherwise queue hundreds of jobs).
    assert scheduled_snapshot_ids == {due_snapshot_id}
    assert snapshot_id not in scheduled_snapshot_ids


def test_enqueue_due_scheduled_work_skips_group_whose_newest_snapshot_is_done(
    client, platform_admin_headers, db_session, monkeypatch
):
    """Newest snapshot already indexed -> nothing to do, even though older
    snapshots were never indexed; and a completed job that stored no
    entries (e.g. everything excluded) also counts as done, so the same
    snapshot isn't re-queued on every run."""
    from resurgam_api.models.customer_file_index_settings import CustomerFileIndexSettings
    from resurgam_api.models.file_index_job import FileIndexJob
    from resurgam_api.worker.file_index_worker import _enqueue_due_scheduled_work

    customer_id, group_id, _old_snapshot_id = _setup_group(client, platform_admin_headers, db_session, monkeypatch)
    newest_snapshot_id = _add_snapshot(db_session, group_id, snapshot_time=datetime(2023, 9, 25, tzinfo=UTC))
    _make_index_job(db_session, group_id, newest_snapshot_id, customer_id, status="completed")  # stored nothing
    db_session.commit()
    _make_settings(
        db_session,
        customer_id,
        schedule_enabled=True,
        schedule_interval_hours=6,
        next_scheduled_run_at=datetime.now(UTC) - timedelta(minutes=1),
    )

    _enqueue_due_scheduled_work(db_session)

    assert db_session.query(FileIndexJob).filter_by(trigger="scheduled").count() == 0
    settings_row = db_session.query(CustomerFileIndexSettings).filter_by(customer_id=customer_id).one()
    assert settings_row.next_scheduled_run_at > datetime.now(UTC) + timedelta(hours=5)


def test_enqueue_due_scheduled_work_retries_newest_snapshot_after_a_failed_job(
    client, platform_admin_headers, db_session, monkeypatch
):
    from resurgam_api.models.file_index_job import FileIndexJob
    from resurgam_api.worker.file_index_worker import _enqueue_due_scheduled_work

    customer_id, group_id, snapshot_id = _setup_group(client, platform_admin_headers, db_session, monkeypatch)
    _make_index_job(db_session, group_id, snapshot_id, customer_id, status="failed")
    db_session.commit()
    _make_settings(
        db_session,
        customer_id,
        schedule_enabled=True,
        schedule_interval_hours=6,
        next_scheduled_run_at=datetime.now(UTC) - timedelta(minutes=1),
    )

    _enqueue_due_scheduled_work(db_session)

    scheduled = db_session.query(FileIndexJob).filter_by(trigger="scheduled").all()
    assert [j.backup_snapshot_id for j in scheduled] == [snapshot_id]


def test_enqueue_due_scheduled_work_skips_superseded_groups(client, platform_admin_headers, db_session, monkeypatch):
    """A reused VMID's closed-out prior generation (BackupGroup.superseded_at
    set) keeps whatever file_index_enabled it had before it closed -- it
    must never get endlessly re-enqueued for indexing data that's no
    longer this workload's current state.
    """
    from resurgam_api.models.file_index_job import FileIndexJob
    from resurgam_api.models.inventory import BackupGroup
    from resurgam_api.worker.file_index_worker import _enqueue_due_scheduled_work

    customer_id, group_id, _snapshot_id = _setup_group(client, platform_admin_headers, db_session, monkeypatch)
    group = db_session.get(BackupGroup, group_id)
    group.superseded_at = datetime.now(UTC)
    db_session.commit()

    _make_settings(
        db_session,
        customer_id,
        schedule_enabled=True,
        schedule_interval_hours=6,
        next_scheduled_run_at=datetime.now(UTC) - timedelta(minutes=1),
    )

    _enqueue_due_scheduled_work(db_session)

    assert db_session.query(FileIndexJob).filter_by(trigger="scheduled").count() == 0


def test_enqueue_due_scheduled_work_ignores_not_yet_due_customers(
    client, platform_admin_headers, db_session, monkeypatch
):
    from resurgam_api.models.file_index_job import FileIndexJob
    from resurgam_api.worker.file_index_worker import _enqueue_due_scheduled_work

    customer_id, _group_id, _snapshot_id = _setup_group(client, platform_admin_headers, db_session, monkeypatch)
    _make_settings(
        db_session,
        customer_id,
        schedule_enabled=True,
        schedule_interval_hours=6,
        next_scheduled_run_at=datetime.now(UTC) + timedelta(hours=1),
    )

    _enqueue_due_scheduled_work(db_session)

    assert db_session.query(FileIndexJob).filter_by(trigger="scheduled").count() == 0


def test_enqueue_due_scheduled_work_skips_groups_not_individually_opted_in(
    client, platform_admin_headers, db_session, monkeypatch
):
    """Per-VM opt-in (ADR 0016): a due, enabled customer alone isn't
    enough for the scheduler either -- each group must be individually
    enabled, same two-level AND gate the API enforces for manual/bulk.
    """
    from resurgam_api.models.file_index_job import FileIndexJob
    from resurgam_api.worker.file_index_worker import _enqueue_due_scheduled_work

    customer_id, _group_id, _snapshot_id = _setup_group(
        client, platform_admin_headers, db_session, monkeypatch, file_index_enabled=False
    )
    _make_settings(
        db_session,
        customer_id,
        schedule_enabled=True,
        schedule_interval_hours=6,
        next_scheduled_run_at=datetime.now(UTC) - timedelta(minutes=1),
    )

    _enqueue_due_scheduled_work(db_session)

    assert db_session.query(FileIndexJob).filter_by(trigger="scheduled").count() == 0


# --- _sweep_expired_retention ---------------------------------------------------


def test_sweep_expired_retention_purges_only_past_cutoff(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.models.file_index_entry import FileIndexEntry
    from resurgam_api.worker.file_index_worker import _sweep_expired_retention

    customer_id, group_id, snapshot_id = _setup_group(client, platform_admin_headers, db_session, monkeypatch)
    _make_settings(db_session, customer_id, retention_days=30)
    job = _make_index_job(db_session, group_id, snapshot_id, customer_id, status="completed")

    old_entry = FileIndexEntry(
        file_index_job_id=job.id,
        backup_snapshot_id=snapshot_id,
        backup_group_id=group_id,
        customer_id=customer_id,
        archive="root.pxar",
        path="/old",
        filename="old",
        type="file",
    )
    db_session.add(old_entry)
    db_session.flush()
    old_entry.created_at = datetime.now(UTC) - timedelta(days=31)
    fresh_entry = FileIndexEntry(
        file_index_job_id=job.id,
        backup_snapshot_id=snapshot_id,
        backup_group_id=group_id,
        customer_id=customer_id,
        archive="root.pxar",
        path="/fresh",
        filename="fresh",
        type="file",
    )
    db_session.add(fresh_entry)
    db_session.commit()

    purged = _sweep_expired_retention(db_session)

    assert purged == 1
    remaining_paths = {e.path for e in db_session.query(FileIndexEntry).filter_by(file_index_job_id=job.id).all()}
    assert remaining_paths == {"/fresh"}


def test_sweep_expired_retention_no_settings_purges_nothing(db_session):
    from resurgam_api.worker.file_index_worker import _sweep_expired_retention

    assert _sweep_expired_retention(db_session) == 0


# --- _within_window ------------------------------------------------------------


def test_within_window_handles_overnight_wraparound():
    from resurgam_api.worker.file_index_worker import _within_window

    start, end = time(22, 0), time(4, 0)
    assert _within_window(time(23, 0), start, end) is True
    assert _within_window(time(2, 0), start, end) is True
    assert _within_window(time(12, 0), start, end) is False


def test_within_window_none_means_unrestricted():
    from resurgam_api.worker.file_index_worker import _within_window

    assert _within_window(time(12, 0), None, None) is True


# --- symlink-loop guard ----------------------------------------------------------


def test_repeats_its_own_parent_segment():
    from resurgam_api.filerestore.index_entries import _repeats_its_own_parent_segment

    assert _repeats_its_own_parent_segment("/usr/bin/X11/X11") is True
    assert _repeats_its_own_parent_segment("/usr/bin/X11") is False
    assert _repeats_its_own_parent_segment("/") is False
    assert _repeats_its_own_parent_segment("/a") is False
    # Same name at unrelated, non-adjacent depths is a real, legitimate
    # pattern (e.g. a customer's own directory literally named "backup"
    # nested somewhere under an unrelated ancestor also named "backup")
    # -- only an *adjacent* repeat is treated as a loop signature.
    assert _repeats_its_own_parent_segment("/backup/etc/backup") is False


# --- error message truncation ---------------------------------------------------


def test_format_error_message_passes_short_messages_through():
    from resurgam_api.worker.file_index_worker import _format_error_message

    assert _format_error_message(ValueError("short")) == "short"


def test_format_error_message_truncates_oversized_messages():
    """Confirmed live: a failed bulk INSERT's `str(exc)` embedded the
    whole statement and every bound parameter, producing a 2.95MB
    `error_message` that broke plain `SELECT *` tooling against the row.
    """
    from resurgam_api.worker.file_index_worker import _format_error_message

    huge = "x" * 50_000
    formatted = _format_error_message(ValueError(huge))
    assert len(formatted) < 5_000
    assert formatted.startswith("x" * 100)
    assert "truncated, 50000 chars total" in formatted


# --- scheduled snapshot choice (short-lived intra-day snapshots) --------------


def _add_snapshots(db_session, group_id, times, *, last_synced_at=None):
    from resurgam_api.models.inventory import BackupSnapshot

    ids = {}
    for snapshot_time in times:
        snapshot = BackupSnapshot(backup_group_id=group_id, snapshot_time=snapshot_time)
        if last_synced_at is not None:
            snapshot.last_synced_at = last_synced_at
        db_session.add(snapshot)
        db_session.flush()
        ids[snapshot_time] = snapshot.id
    db_session.commit()
    return ids


def _set_server_timezone(db_session, timezone):
    from resurgam_api.models.pbs import PbsInstance

    for instance in db_session.query(PbsInstance).all():
        instance.server_timezone = timezone
    db_session.commit()


def _every_two_hours(first_day, days):
    return [first_day + timedelta(days=d, hours=h) for d in range(days) for h in range(1, 24, 2)]


def test_scheduled_snapshot_for_two_hourly_group_takes_last_snapshot_of_yesterday(
    client, platform_admin_headers, db_session, monkeypatch
):
    """Live 2026-11-14: the 03:00 snapshot of a 2-hourly VM was indexed at
    05:00 and pruned by PBS before noon. The last snapshot of the previous
    day is the one PBS keep-daily keeps."""
    from resurgam_api.models.inventory import BackupGroup
    from resurgam_api.worker.file_index_worker import _scheduled_snapshot_for

    _customer_id, group_id, _ = _setup_group(client, platform_admin_headers, db_session, monkeypatch)
    now = datetime(2026, 11, 14, 5, 0, tzinfo=UTC)
    ids = _add_snapshots(
        db_session, group_id, [*_every_two_hours(datetime(2026, 11, 10, tzinfo=UTC), 4), now - timedelta(hours=2)]
    )
    group = db_session.get(BackupGroup, group_id)

    chosen = _scheduled_snapshot_for(db_session, group, now)

    assert chosen.id == ids[datetime(2026, 11, 13, 23, 0, tzinfo=UTC)]


def test_scheduled_snapshot_for_counts_days_in_the_pbs_servers_timezone(
    client, platform_admin_headers, db_session, monkeypatch
):
    """PBS prune buckets by its *local* day: in Europe/Zurich (UTC+1 in
    November) a 23:30 UTC snapshot already belongs to the next day, so
    yesterday's last is the 22:30 UTC one."""
    from resurgam_api.models.inventory import BackupGroup
    from resurgam_api.worker.file_index_worker import _scheduled_snapshot_for

    _customer_id, group_id, _ = _setup_group(client, platform_admin_headers, db_session, monkeypatch)
    _set_server_timezone(db_session, "Europe/Zurich")
    now = datetime(2026, 11, 14, 5, 0, tzinfo=UTC)  # 06:00 in Zurich
    times = [datetime(2026, 11, d, h, 30, tzinfo=UTC) for d in (11, 12, 13) for h in (10, 22, 23)]  # three per day
    ids = _add_snapshots(db_session, group_id, times)
    group = db_session.get(BackupGroup, group_id)

    chosen = _scheduled_snapshot_for(db_session, group, now)

    # 2026-11-13 23:30 UTC is 2026-11-14 00:30 in Zurich -- today, not yesterday.
    assert chosen.id == ids[datetime(2026, 11, 13, 22, 30, tzinfo=UTC)]


def test_scheduled_snapshot_for_nightly_group_with_one_extra_backup_stays_on_newest(
    client, platform_admin_headers, db_session, monkeypatch
):
    from resurgam_api.models.inventory import BackupGroup
    from resurgam_api.worker.file_index_worker import _scheduled_snapshot_for

    _customer_id, group_id, _ = _setup_group(client, platform_admin_headers, db_session, monkeypatch)
    now = datetime(2026, 11, 14, 5, 0, tzinfo=UTC)
    nightly = [datetime(2026, 11, d, 3, 0, tzinfo=UTC) for d in range(7, 15)]
    ids = _add_snapshots(db_session, group_id, [*nightly, datetime(2026, 11, 12, 14, 0, tzinfo=UTC)])  # one manual
    group = db_session.get(BackupGroup, group_id)

    chosen = _scheduled_snapshot_for(db_session, group, now)

    assert chosen.id == ids[datetime(2026, 11, 14, 3, 0, tzinfo=UTC)]


def test_scheduled_snapshot_for_skips_rows_pbs_has_already_pruned(
    client, platform_admin_headers, db_session, monkeypatch
):
    """RESURGAM keeps `backup_snapshots` rows after PBS prunes them; a row
    the newest Collector pass no longer saw must not be picked."""
    from resurgam_api.models.inventory import BackupGroup
    from resurgam_api.worker.file_index_worker import _scheduled_snapshot_for

    _customer_id, group_id, _ = _setup_group(client, platform_admin_headers, db_session, monkeypatch)
    now = datetime(2026, 11, 14, 5, 0, tzinfo=UTC)
    synced = datetime.now(UTC)
    stale = synced - timedelta(hours=6)
    live = _add_snapshots(
        db_session,
        group_id,
        [datetime(2026, 11, 12, 23, 0, tzinfo=UTC), datetime(2026, 11, 13, 1, 0, tzinfo=UTC), now - timedelta(hours=2)],
        last_synced_at=synced,
    )
    _add_snapshots(db_session, group_id, _every_two_hours(datetime(2026, 11, 10, tzinfo=UTC), 2), last_synced_at=stale)
    _add_snapshots(
        db_session,
        group_id,
        [datetime(2026, 11, 13, h, 0, tzinfo=UTC) for h in (3, 13, 23)],  # pruned since
        last_synced_at=stale,
    )
    group = db_session.get(BackupGroup, group_id)

    chosen = _scheduled_snapshot_for(db_session, group, now)

    assert chosen.id == live[datetime(2026, 11, 13, 1, 0, tzinfo=UTC)]


def test_scheduled_snapshot_for_falls_back_to_newest_for_an_unknown_timezone(
    client, platform_admin_headers, db_session, monkeypatch
):
    from resurgam_api.models.inventory import BackupGroup
    from resurgam_api.worker.file_index_worker import _scheduled_snapshot_for

    _customer_id, group_id, _ = _setup_group(client, platform_admin_headers, db_session, monkeypatch)
    _set_server_timezone(db_session, "Not/AZone")
    now = datetime(2026, 11, 14, 5, 0, tzinfo=UTC)
    ids = _add_snapshots(db_session, group_id, _every_two_hours(datetime(2026, 11, 11, tzinfo=UTC), 3))
    group = db_session.get(BackupGroup, group_id)

    chosen = _scheduled_snapshot_for(db_session, group, now)

    # Unknown zone -> UTC days; still yesterday's last, not an exception.
    assert chosen.id == ids[datetime(2026, 11, 13, 23, 0, tzinfo=UTC)]


def test_enqueue_due_scheduled_work_does_not_requeue_yesterdays_already_indexed_snapshot(
    client, platform_admin_headers, db_session, monkeypatch
):
    """A 2-hourly group whose previous-day snapshot is already indexed gets
    no new job -- in particular not today's short-lived newest one."""
    from resurgam_api.models.file_index_job import FileIndexJob
    from resurgam_api.worker.file_index_worker import _enqueue_due_scheduled_work

    customer_id, group_id, _ = _setup_group(client, platform_admin_headers, db_session, monkeypatch)
    now = datetime.now(UTC)
    yesterday = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    times = [t for t in _every_two_hours(yesterday - timedelta(days=3), 5) if t < now]
    ids = _add_snapshots(db_session, group_id, times)
    yesterdays_last = max(t for t in times if t.date() < now.date())
    _make_index_job(db_session, group_id, ids[yesterdays_last], customer_id, status="completed")
    _make_settings(
        db_session,
        customer_id,
        schedule_enabled=True,
        schedule_interval_hours=24,
        next_scheduled_run_at=now - timedelta(minutes=1),
    )

    _enqueue_due_scheduled_work(db_session)

    assert db_session.query(FileIndexJob).filter_by(trigger="scheduled").count() == 0
