"""resurgam_api.updates.updater -- the host updater's step logic (ADR 0024), host commands faked."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from resurgam_api.updates import updater
from resurgam_api.updates.updater import Host, Layout, Run, UpdateError
from tests.bundle_factory import default_members, host_package_members, make_bundle, make_key_pair

REV_OLD, REV_NEW = "aaa111", "bbb222"


class FakeHost(Host):
    """Simulates docker/systemd/postgres. ``fail`` names a method that raises; flags shape the new release."""

    def __init__(self, layout: Layout) -> None:
        super().__init__(layout)
        self.calls: list[tuple] = []
        self.fail: set[str] = set()
        self.revision = REV_OLD
        self.running_version = "0.2.0"
        self.images: set[str] = {"resurgam-backend:0.2.0", "resurgam-web:0.2.0"}
        self.active = set(updater.WORKERS)
        self.migration_breaks_startup = False  # new release: migration half-runs, API never ready
        self.new_release_has_migration = True
        self.report_wrong_version = False
        self.crash_loop_restarts = 0
        self.clock = 0.0

    def _maybe_fail(self, name):
        self.calls.append((name,))
        if name in self.fail:
            raise UpdateError(f"simulated {name} failure")

    def alembic_version(self):
        self.calls.append(("alembic_version",))
        return self.revision

    def pg_dump(self, dest: Path):
        self._maybe_fail("pg_dump")
        dest.write_bytes(b"PGDMP" + self.revision.encode())

    def restore_db(self, dump: Path):
        self._maybe_fail("restore_db")
        self.calls[-1] = ("restore_db", dump)
        self.revision = dump.read_bytes()[5:].decode()

    def docker_load(self, image_tar: Path):
        self._maybe_fail("docker_load")
        version = image_tar.read_text().split()[-1]
        self.images.add(f"resurgam-{image_tar.stem}:{version}")

    def image_exists(self, ref):
        return ref in self.images

    def remove_image(self, ref):
        self.calls.append(("remove_image", ref))
        self.images.discard(ref)

    def compose_up(self):
        self._maybe_fail("compose_up")
        version = updater.read_env(self.layout.env_file)["RESURGAM_VERSION"]
        self.running_version = version
        if version != "0.2.0" and self.new_release_has_migration:
            self.revision = REV_NEW

    def service_running(self, service):
        return True

    def restart_count(self, service):
        self.calls.append(("restart_count", service))
        broken = self.migration_breaks_startup and self.running_version != "0.2.0"
        return self.crash_loop_restarts if broken else 0

    def backend_get_json(self, path):
        if path == "/api/v1/readiness":
            broken = self.migration_breaks_startup and self.running_version != "0.2.0"
            return None if broken else {"status": "ready", "database": "ok"}
        version = "9.9.9" if self.report_wrong_version and self.running_version != "0.2.0" else self.running_version
        return {"version": version}

    def systemctl(self, *args):
        self._maybe_fail(f"systemctl_{args[0]}")
        self.calls[-1] = ("systemctl", *args)
        if args[0] == "stop":
            self.active -= set(args[1:])
        elif args[0] == "start":
            self.active |= set(args[1:])

    def unit_active(self, unit):
        return unit in self.active

    def try_restart(self, unit):
        self.calls.append(("try_restart", unit))

    def build_venv(self, venv: Path, wheels: Path):
        self._maybe_fail("build_venv")
        (venv / "bin").mkdir(parents=True)

    def sleep(self, seconds):
        self.clock += seconds

    def monotonic(self):
        return self.clock

    def names(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def env(tmp_path):
    layout = Layout(
        opt_root=tmp_path / "opt" / "resurgam",
        venvs=tmp_path / "opt" / "venvs",
        data=tmp_path / "var" / "lib" / "resurgam",
        systemd_dir=tmp_path / "etc" / "systemd" / "system",
    )
    rel = layout.releases / "0.2.0"
    (rel / "appliance").mkdir(parents=True)
    (rel / "appliance" / "docker-compose.yml").write_text("services: {}\n")
    (rel / "backend" / "packaging" / "systemd").mkdir(parents=True)
    (rel / "backend" / "packaging" / "systemd" / "resurgam-collector-scheduler.service").write_text("# 0.2.0\n")
    os.symlink("releases/0.2.0", layout.current)
    (layout.venv_for("0.2.0") / "bin").mkdir(parents=True)
    os.symlink("resurgam-backend-0.2.0", layout.venv_link)
    layout.stable.mkdir(parents=True)
    os.symlink(str(layout.current / "appliance" / "docker-compose.yml"), layout.compose_file)
    layout.env_file.write_text("POSTGRES_PASSWORD=secret\n# keep me\nRESURGAM_VERSION=0.2.0\n")
    layout.env_file.chmod(0o600)
    layout.systemd_dir.mkdir(parents=True)
    for d in (layout.incoming, layout.requests, layout.status, layout.backups):
        d.mkdir(parents=True)
    key, pub = make_key_pair(tmp_path)
    (layout.stable / "release-signing.pub").write_bytes(pub.read_bytes())
    host = FakeHost(layout)

    class Env:
        pass

    e = Env()
    e.layout, e.host, e.key, e.tmp = layout, host, key, tmp_path
    return e


def _run(env, bundle_path, action="apply", request_id="req-1"):
    run = Run(
        env.layout, env.host, request_id=request_id, action=action, bundle_path=bundle_path,
        requested_by="tester", ready_timeout=30, worker_settle=1,
    )  # fmt: skip
    return run.execute(), json.loads((env.layout.status / f"{request_id}.json").read_text())


def _bundle(env, version="0.2.1", **kwargs):
    return make_bundle(env.layout.incoming / f"up-{version}.bundle", env.key, version=version, **kwargs)


def _current(env):
    return os.readlink(env.layout.current), os.readlink(env.layout.venv_link)


# --- verify ---------------------------------------------------------------------------------------


def test_verify_only_changes_nothing(env):
    state, status = _run(env, _bundle(env), action="verify")
    assert state == "succeeded"
    assert status["step"] == "done" and status["to_version"] == "0.2.1" and status["from_version"] == "0.2.0"
    assert status["manifest"] == {"version": "0.2.1", "changelog": "- things", "min_upgrade_from": "0.2.0"}
    assert env.host.calls == []
    assert _current(env) == ("releases/0.2.0", "resurgam-backend-0.2.0")
    assert updater.read_history(env.layout) == []
    assert not (env.layout.work / "req-1").exists()


def test_foreign_signature_fails_verification_before_anything_happens(env):
    foreign, _ = make_key_pair(env.tmp, "foreign")
    path = make_bundle(env.layout.incoming / "f.bundle", foreign)
    state, status = _run(env, path)
    assert state == "failed" and status["step"] == "verify"
    assert "signature does not match" in status["message"]
    assert env.host.calls == []


def test_tampered_component_fails_verification(env):
    path = _bundle(env, tamper={"images/backend.tar": b"backend image 0.2.X"})
    state, status = _run(env, path)
    assert state == "failed" and "images/backend.tar does not match" in status["message"]
    assert env.host.calls == []


@pytest.mark.parametrize(
    ("version", "min_from", "match"), [("0.2.0", "0.2.0", "not newer"), ("0.4.0", "0.3.0", "requires at least")]
)
def test_version_rules_fail_verification(env, version, min_from, match):
    state, status = _run(env, _bundle(env, version=version, min_upgrade_from=min_from))
    assert state == "failed" and match in status["message"]
    assert env.host.calls == [] and list(env.layout.backups.iterdir()) == []


# --- apply ----------------------------------------------------------------------------------------


def test_successful_apply(env):
    path = _bundle(env)
    state, status = _run(env, path)
    assert state == "succeeded", status["message"]
    assert status["step"] == "done" and status["message"] == "Updated 0.2.0 -> 0.2.1."
    assert _current(env) == ("releases/0.2.1", "resurgam-backend-0.2.1")
    assert updater.read_env(env.layout.env_file)["RESURGAM_VERSION"] == "0.2.1"
    env_text = env.layout.env_file.read_text()
    assert "POSTGRES_PASSWORD=secret" in env_text and "# keep me" in env_text
    assert env.layout.env_file.stat().st_mode & 0o777 == 0o600
    assert (env.layout.releases / "0.2.1" / "VERSION").read_text() == "0.2.1\n"
    assert "# 0.2.1" in (env.layout.systemd_dir / "resurgam-collector-scheduler.service").read_text()
    backup = Path(status["backup_file"])
    assert backup.exists() and backup.name.startswith("pre-0.2.1-")
    assert env.host.active == set(updater.WORKERS)
    assert "restore_db" not in env.host.names()
    assert not path.exists(), "an applied bundle is removed from incoming/"
    [entry] = updater.read_history(env.layout)
    assert entry["state"] == "succeeded" and entry["from_version"] == "0.2.0" and entry["to_version"] == "0.2.1"
    assert entry["requested_by"] == "tester" and entry["backup_file"] == str(backup)
    # order: backup before stop before load before compose
    names = env.host.names()
    assert names.index("pg_dump") < names.index("systemctl") < names.index("docker_load") < names.index("compose_up")


def test_backup_failure_changes_nothing(env):
    env.host.fail.add("pg_dump")
    state, status = _run(env, _bundle(env))
    assert state == "failed" and status["step"] == "backup"
    assert "nothing was changed" in status["message"]
    assert "systemctl" not in env.host.names() and env.host.active == set(updater.WORKERS)
    assert _current(env) == ("releases/0.2.0", "resurgam-backend-0.2.0")
    assert updater.read_history(env.layout)[0]["state"] == "failed"


def test_failure_before_migration_rolls_back_without_db_restore(env):
    env.host.fail.add("build_venv")
    state, status = _run(env, _bundle(env))
    assert state == "rolled_back", status["message"]
    assert status["step"] == "rollback"
    assert "simulated build_venv failure" in status["message"]
    assert "database restored" not in status["message"]
    assert "restore_db" not in env.host.names() and "compose_up" not in env.host.names()
    assert _current(env) == ("releases/0.2.0", "resurgam-backend-0.2.0")
    assert env.host.active == set(updater.WORKERS), "workers restarted"
    assert not (env.layout.releases / "0.2.1").exists() and not env.layout.venv_for("0.2.1").exists()
    assert "resurgam-backend:0.2.1" not in env.host.images
    assert updater.read_history(env.layout)[0]["state"] == "rolled_back"


def test_image_load_failure_rolls_back_without_db_restore(env):
    env.host.fail.add("docker_load")
    state, _ = _run(env, _bundle(env))
    assert state == "rolled_back"
    assert "restore_db" not in env.host.names()
    assert env.host.active == set(updater.WORKERS)


def test_failed_migration_restores_the_database(env):
    env.host.migration_breaks_startup = True
    state, status = _run(env, _bundle(env))
    assert state == "rolled_back", status["message"]
    assert "database restored from the pre-update backup" in status["message"]
    assert "did not become ready" in status["message"]
    [restore] = [c for c in env.host.calls if c[0] == "restore_db"]
    assert restore[1] == Path(status["backup_file"])
    assert env.host.revision == REV_OLD
    assert env.host.running_version == "0.2.0"
    assert _current(env) == ("releases/0.2.0", "resurgam-backend-0.2.0")
    assert updater.read_env(env.layout.env_file)["RESURGAM_VERSION"] == "0.2.0"
    assert "# 0.2.0" in (env.layout.systemd_dir / "resurgam-collector-scheduler.service").read_text()
    assert env.host.names().count("compose_up") == 2
    assert env.host.active == set(updater.WORKERS)
    # restore happens before the previous version is brought back up
    names = env.host.names()
    assert names.index("restore_db") < len(names) - 1 - names[::-1].index("compose_up")


def test_crash_looping_backend_fails_fast_and_rolls_back(env):
    env.host.migration_breaks_startup = True
    env.host.crash_loop_restarts = 3
    state, status = _run(env, _bundle(env))
    assert state == "rolled_back"
    assert "keeps crashing on start (3 restarts)" in status["message"]
    assert env.host.clock < 30, "did not wait out the readiness timeout"
    assert "restore_db" in env.host.names()


def test_failure_after_start_without_schema_change_leaves_the_database(env):
    env.host.new_release_has_migration = False
    env.host.report_wrong_version = True
    state, status = _run(env, _bundle(env))
    assert state == "rolled_back"
    assert "reports version '9.9.9'" in status["message"]
    assert "restore_db" not in env.host.names()
    assert env.host.names().count("compose_up") == 2
    assert env.host.running_version == "0.2.0"


def test_worker_that_does_not_stay_up_triggers_rollback(env):
    original = env.host.systemctl

    def systemctl(*args):
        original(*args)
        if args[0] == "start" and updater.read_env(env.layout.env_file)["RESURGAM_VERSION"] == "0.2.1":
            env.host.active.discard("resurgam-file-index-worker")

    env.host.systemctl = systemctl
    state, status = _run(env, _bundle(env))
    assert state == "rolled_back"
    assert "resurgam-file-index-worker" in status["message"]
    assert "restore_db" in env.host.names()  # the new backend had migrated


def test_failed_rollback_is_reported_as_failed_with_the_backup(env):
    env.host.migration_breaks_startup = True
    env.host.fail.add("restore_db")
    state, status = _run(env, _bundle(env))
    assert state == "failed"
    assert "AND the automatic rollback failed" in status["message"]
    assert status["backup_file"] in status["message"]


def test_only_previously_running_workers_are_restarted(env):
    env.host.active = {"resurgam-collector-scheduler"}
    state, _ = _run(env, _bundle(env))
    assert state == "succeeded"
    assert env.host.active == {"resurgam-collector-scheduler"}


def test_backups_are_pruned_to_the_last_five(env):
    for i in range(7):
        p = env.layout.backups / f"pre-0.1.{i}-2026.dump"
        p.write_bytes(b"x")
        os.utime(p, (1000 + i, 1000 + i))
    (env.layout.backups / "keep-me.txt").write_text("unrelated")
    updater.prune_backups(env.layout)
    left = sorted(p.name for p in env.layout.backups.glob("pre-*.dump"))
    assert left == [f"pre-0.1.{i}-2026.dump" for i in range(2, 7)]
    assert (env.layout.backups / "keep-me.txt").exists()


def test_old_releases_are_pruned_but_never_the_current_one(env):
    for v in ("0.1.0", "0.1.5", "0.1.9"):
        (env.layout.releases / v).mkdir()
        (env.layout.venv_for(v)).mkdir()
    updater.prune_releases(env.layout, env.host)
    assert sorted(p.name for p in env.layout.releases.iterdir()) == ["0.1.5", "0.1.9", "0.2.0"]
    assert not env.layout.venv_for("0.1.0").exists() and env.layout.venv_for("0.2.0").exists()
    assert ("remove_image", "resurgam-backend:0.1.0") in env.host.calls


def test_legacy_layout_is_refused(env):
    env.layout.current.unlink()
    env.layout.current.mkdir()
    state, status = _run(env, _bundle(env))
    assert state == "failed" and "predates the release layout" in status["message"]


# --- request queue --------------------------------------------------------------------------------


def _request(env, request_id, **doc):
    (env.layout.requests / f"{request_id}.json").write_text(json.dumps(doc))


def _status(env, request_id):
    return json.loads((env.layout.status / f"{request_id}.json").read_text())


def test_process_requests_runs_queue_and_rejects_bad_requests(env):
    path = _bundle(env)
    _request(env, "a-verify", action="verify", bundle=path.name, requested_by="alice")
    _request(env, "b-bad-action", action="reboot", bundle=path.name)
    _request(env, "c-traversal", action="apply", bundle="../../etc/passwd")
    (env.layout.requests / "d-junk.json").write_text("{not json")
    count = updater.process_requests(env.layout, env.host, ready_timeout=30, worker_settle=1)
    assert count == 4
    assert _status(env, "a-verify")["state"] == "succeeded"
    assert "Unknown action" in _status(env, "b-bad-action")["message"]
    assert "plain file name" in _status(env, "c-traversal")["message"]
    assert "not valid JSON" in _status(env, "d-junk")["message"]
    assert list(env.layout.requests.iterdir()) == []


def test_process_requests_applies_from_incoming(env):
    path = _bundle(env)
    _request(env, "r1", action="apply", bundle=path.name, requested_by="alice")
    updater.process_requests(env.layout, env.host, ready_timeout=30, worker_settle=1)
    assert _status(env, "r1")["state"] == "succeeded"
    assert updater.read_history(env.layout)[0]["requested_by"] == "alice"


def test_interrupted_run_is_marked_failed_on_next_start(env):
    (env.layout.requests / "r9.claimed").write_text("{}")
    (env.layout.status / "r9.json").write_text(json.dumps({"request_id": "r9", "state": "running", "step": "install"}))
    updater.process_requests(env.layout, env.host)
    status = _status(env, "r9")
    assert status["state"] == "failed" and "interrupted during step 'install'" in status["message"]
    assert not (env.layout.requests / "r9.claimed").exists()


def test_lock_prevents_concurrent_runs(env):
    with (
        updater.updater_lock(env.layout, wait=False),
        pytest.raises(UpdateError, match="in progress"),
        updater.updater_lock(env.layout, wait=False),
    ):
        pass


# --- real Host command construction (subprocess mocked) -------------------------------------------


def test_host_pg_dump_and_restore_commands(env, monkeypatch):
    seen = []

    def fake_run(cmd, stdin=None, stdout=None, **kwargs):
        seen.append(cmd)
        if hasattr(stdout, "write"):
            stdout.write(b"PGDMP-data")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(updater.subprocess, "run", fake_run)
    host = Host(env.layout)
    dest = env.layout.backups / "pre-0.2.1-x.dump"
    host.pg_dump(dest)
    assert dest.read_bytes() == b"PGDMP-data" and dest.stat().st_mode & 0o777 == 0o600
    base = ["docker", "compose", "--project-name", "appliance", "--project-directory", str(env.layout.stable)]
    assert seen[0][: len(base)] == base
    assert seen[0][-6:] == ["postgres", "pg_dump", "-U", "resurgam", "-Fc", "resurgam"]
    assert "--list" in seen[1]
    seen.clear()
    host.restore_db(dest)
    assert ["stop", "backend"] == seen[0][-2:]
    assert any('DROP DATABASE IF EXISTS "resurgam" WITH (FORCE)' in c for c in seen[2])
    assert seen[-1][-6:] == ["pg_restore", "--exit-on-error", "-U", "resurgam", "-d", "resurgam"]


def test_host_reports_command_failures_readably(env, monkeypatch):
    monkeypatch.setattr(
        updater.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, b"", b"boom\nreason here")
    )
    with pytest.raises(UpdateError, match=r"exit 1.*reason here"):
        Host(env.layout).docker_load(Path("/x.tar"))
    assert Host(env.layout).alembic_version() is None


def test_host_rejects_odd_database_names(env):
    updater.set_env_value(env.layout.env_file, "POSTGRES_DB", 'x"; DROP TABLE')
    with pytest.raises(UpdateError, match="plain identifiers"):
        Host(env.layout).pg_dump(env.layout.backups / "x.dump")


def test_status_command_works_without_root(env, capsys):
    assert updater.main(["status", "--json"], layout=env.layout, host=env.host) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["version"] == "0.2.0" and doc["releases"] == ["0.2.0"]


# --- host_setup: the new release's own hook (ADR 0024) --------------------------------------------

# A real shell hook (the updater runs it with sh): records its arguments and environment, then
# exits with $HOOK_EXIT (default 0). Stands in for appliance/release/host-setup.sh.
HOOK = """#!/bin/sh
echo "args: $*" >> "$HOOK_RECORD"
echo "env: $RESURGAM_FROM_VERSION -> $RESURGAM_TO_VERSION in $RESURGAM_RELEASE_DIR" >> "$HOOK_RECORD"
echo "host-setup: installing things"
echo "host-setup: done"
exit "${HOOK_EXIT:-0}"
"""


@pytest.fixture
def hook_record(tmp_path, monkeypatch):
    record = tmp_path / "hook-record.txt"
    monkeypatch.setenv("HOOK_RECORD", str(record))
    return record


def _hook_bundle(env, version="0.2.1", *, hook=HOOK, host_packages=True, tamper=None):
    source_files = {"appliance/release/host-setup.sh": hook} if hook is not None else None
    members = default_members(version, source_files=source_files)
    if host_packages:
        members.update(host_package_members())
    return make_bundle(
        env.layout.incoming / f"up-{version}.bundle", env.key, version=version, members=members, tamper=tamper
    )


def test_host_setup_hook_runs_with_the_bundle_repository_before_the_switch(env, hook_record):
    state, status = _run(env, _hook_bundle(env))
    assert state == "succeeded", status["message"]
    args, hook_env = hook_record.read_text().splitlines()
    work = env.layout.work / "req-1" / "bundle"
    assert args == f"args: --repo {work / 'host-packages'} --packages {work / 'host-packages.list'}"
    assert hook_env == f"env: 0.2.0 -> 0.2.1 in {env.layout.releases / '0.2.1'}"
    log = "\n".join(status["log"])
    assert "[host_setup]" in log
    assert "Host packages to ensure: pve-qemu-kvm, proxmox-backup-client (2 .debs in the bundle)" in log
    assert "  host-setup: installing things" in log and "Host setup complete" in log
    steps = [line.split("[")[1].split("]")[0] for line in status["log"] if "] " in line and "[" in line]
    assert steps.index("install") < steps.index("host_setup") < steps.index("migrate")
    # The host probe is refreshed with the new release's code once its units are installed.
    names = env.host.names()
    assert ("try_restart", "resurgam-host-probe.service") in env.host.calls
    assert names.index("try_restart") < names.index("compose_up")


def test_failing_host_setup_hook_rolls_back_before_anything_is_switched(env, hook_record, monkeypatch):
    monkeypatch.setenv("HOOK_EXIT", "3")
    state, status = _run(env, _hook_bundle(env))
    assert state == "rolled_back", status["message"]
    assert "host-setup hook of 0.2.1 failed (exit 3)" in status["message"]
    assert "stay installed" in status["message"]
    assert "  host-setup: done" in "\n".join(status["log"]), "the hook's output is in the log"
    assert "compose_up" not in env.host.names() and "restore_db" not in env.host.names()
    assert _current(env) == ("releases/0.2.0", "resurgam-backend-0.2.0")
    assert not (env.layout.releases / "0.2.1").exists() and not env.layout.venv_for("0.2.1").exists()
    assert env.host.active == set(updater.WORKERS), "workers restarted"
    assert updater.read_history(env.layout)[0]["state"] == "rolled_back"


def test_host_setup_is_rerun_on_retry_and_on_every_later_update(env, hook_record, monkeypatch):
    """The hook must be idempotent because it runs again on a retry and on every later release."""
    monkeypatch.setenv("HOOK_EXIT", "1")
    assert _run(env, _hook_bundle(env), request_id="try-1")[0] == "rolled_back"
    monkeypatch.setenv("HOOK_EXIT", "0")
    assert _run(env, _hook_bundle(env), request_id="try-2")[0] == "succeeded"
    assert _run(env, _hook_bundle(env, version="0.2.2"), request_id="next")[0] == "succeeded"
    assert hook_record.read_text().count("args: --repo") == 3
    assert "env: 0.2.1 -> 0.2.2" in hook_record.read_text()


def test_a_release_without_host_packages_still_runs_its_hook_without_a_repository(env, hook_record):
    state, status = _run(env, _hook_bundle(env, host_packages=False))
    assert state == "succeeded", status["message"]
    assert hook_record.read_text().splitlines()[0] == "args: "


def test_a_release_without_a_hook_skips_host_setup(env, hook_record):
    state, status = _run(env, _hook_bundle(env, hook=None))
    assert state == "succeeded", status["message"]
    assert not hook_record.exists()
    assert "ships no appliance/release/host-setup.sh" in "\n".join(status["log"])


def test_an_inconsistent_host_repository_rolls_back_without_running_the_hook(env, hook_record):
    # Signed manifest and Packages index disagree (a builder bug): caught before apt ever sees it.
    members = default_members("0.2.1", source_files={"appliance/release/host-setup.sh": HOOK})
    members.update(host_package_members())
    members["host-packages/Packages"] = members["host-packages/Packages"].replace(b"Size: ", b"Size: 9")
    path = make_bundle(env.layout.incoming / "bad.bundle", env.key, version="0.2.1", members=members)
    state, status = _run(env, path)
    assert state == "rolled_back"
    assert "does not match its Packages entry" in status["message"]
    assert not hook_record.exists()
