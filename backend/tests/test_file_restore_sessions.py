"""Tests for manifest §10.3 "Session-Lifecycle" enforcement
(`filerestore/sessions.py`) -- `proxmox-file-restore status`/`stop` were
real, tested-in-isolation primitives that nothing in runtime code ever
called before this. These tests exercise the two wiring points directly,
mocking `resurgam_worker.pbs_client.file_restore` (shared by `browse.py`
and `sessions.py`, same module object) rather than a real PBS instance.
"""

from resurgam_worker.pbs_client.errors import PbsCommandFailedError
from resurgam_worker.pbs_client.models import RestoreSessionStatus

from resurgam_api.filerestore.sessions import stop_session_best_effort, stop_stale_session_if_needed


def _session(name="qemu_repo:ns/vm/106/2026-09-14T00:00:00Z", uptime=100):
    return RestoreSessionStatus(name=name, cid=1, pid=1234, timeout=1200, uptime=uptime)


def test_stop_stale_session_if_needed_stops_when_over_the_limit(monkeypatch):
    from resurgam_worker.pbs_client import file_restore

    stopped = []
    monkeypatch.setattr(file_restore, "status", lambda **kw: [_session(uptime=1000)])
    monkeypatch.setattr(file_restore, "stop", lambda name, **kw: stopped.append(name))

    stop_stale_session_if_needed("vm/106/2026-09-14T00:00:00Z", token=None, max_age_seconds=900)

    assert stopped == ["qemu_repo:ns/vm/106/2026-09-14T00:00:00Z"]


def test_stop_stale_session_if_needed_leaves_a_fresh_session_alone(monkeypatch):
    from resurgam_worker.pbs_client import file_restore

    stopped = []
    monkeypatch.setattr(file_restore, "status", lambda **kw: [_session(uptime=100)])
    monkeypatch.setattr(file_restore, "stop", lambda name, **kw: stopped.append(name))

    stop_stale_session_if_needed("vm/106/2026-09-14T00:00:00Z", token=None, max_age_seconds=900)

    assert stopped == []


def test_stop_stale_session_if_needed_matches_by_snapshot_substring_only(monkeypatch):
    """`status()` returns every session PBS-wide -- must not touch a
    different snapshot's session, even one that's well over any duration.
    """
    from resurgam_worker.pbs_client import file_restore

    stopped = []
    monkeypatch.setattr(
        file_restore,
        "status",
        lambda **kw: [_session(name="qemu_repo:ns/vm/999/2020-01-01T00:00:00Z", uptime=99999)],
    )
    monkeypatch.setattr(file_restore, "stop", lambda name, **kw: stopped.append(name))

    stop_stale_session_if_needed("vm/106/2026-09-14T00:00:00Z", token=None, max_age_seconds=900)

    assert stopped == []


def test_stop_stale_session_if_needed_swallows_pbs_errors(monkeypatch):
    """Best-effort: a failed status/stop check must never block the real
    list/extract call about to happen.
    """
    from resurgam_worker.pbs_client import file_restore

    def raise_error(**kw):
        raise PbsCommandFailedError(["proxmox-file-restore", "status"], 1, "boom")

    monkeypatch.setattr(file_restore, "status", raise_error)

    stop_stale_session_if_needed("vm/106/2026-09-14T00:00:00Z", token=None, max_age_seconds=900)  # must not raise


def test_stop_session_best_effort_stops_a_matching_session(monkeypatch):
    from resurgam_worker.pbs_client import file_restore

    stopped = []
    monkeypatch.setattr(file_restore, "status", lambda **kw: [_session(uptime=1)])
    monkeypatch.setattr(file_restore, "stop", lambda name, **kw: stopped.append(name))

    stop_session_best_effort("vm/106/2026-09-14T00:00:00Z", token=None)

    assert stopped == ["qemu_repo:ns/vm/106/2026-09-14T00:00:00Z"]


def test_stop_session_best_effort_is_a_noop_when_nothing_matches(monkeypatch):
    from resurgam_worker.pbs_client import file_restore

    stopped = []
    monkeypatch.setattr(file_restore, "status", lambda **kw: [])
    monkeypatch.setattr(file_restore, "stop", lambda name, **kw: stopped.append(name))

    stop_session_best_effort("vm/106/2026-09-14T00:00:00Z", token=None)  # must not raise

    assert stopped == []


def test_stop_session_best_effort_swallows_pbs_errors(monkeypatch):
    from resurgam_worker.pbs_client import file_restore

    monkeypatch.setattr(file_restore, "status", lambda **kw: [_session(uptime=1)])

    def raise_error(name, **kw):
        raise PbsCommandFailedError(["proxmox-file-restore", "stop", name], 1, "boom")

    monkeypatch.setattr(file_restore, "stop", raise_error)

    stop_session_best_effort("vm/106/2026-09-14T00:00:00Z", token=None)  # must not raise


def test_a_held_session_is_not_stopped_for_another_job(monkeypatch):
    """Browse and transfer lanes run at once: one job's cleanup or age check must not stop the
    session the other one is still using."""
    from resurgam_api.filerestore import sessions

    stopped = []
    monkeypatch.setattr(
        sessions.file_restore,
        "status",
        lambda **kw: [type("S", (), {"name": "qemu_x/vm/1/2026", "uptime": 10_000})()],
    )
    monkeypatch.setattr(sessions.file_restore, "stop", lambda name, **kw: stopped.append(name))

    with sessions.hold_session("vm/1/2026"):  # the archive
        with sessions.hold_session("vm/1/2026"):  # a listing of the same snapshot
            sessions.stop_stale_session_if_needed("vm/1/2026", token=None, max_age_seconds=900)
        sessions.stop_session_best_effort("vm/1/2026", token=None)  # the listing's cleanup
        assert stopped == []
    sessions.stop_session_best_effort("vm/1/2026", token=None)
    assert stopped == ["qemu_x/vm/1/2026"]
