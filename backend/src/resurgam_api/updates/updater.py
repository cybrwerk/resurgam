"""Host-side appliance updater: ``resurgam-update`` (ADR 0024).

Runs as root on the appliance VM -- never inside the API container. Installed as
``/usr/local/sbin/resurgam-update``, a wrapper that runs this module with the interpreter of the
venv ``/opt/venvs/resurgam-backend`` points at *when it starts* (symlink resolved). An apply
swaps that link to the new release's venv; this process keeps running from the old, resolved
one (which is never removed while it is in use), so it never mixes code from two releases.

Commands::

    resurgam-update status [--json]       installed version, releases, backups, recent history
    resurgam-update verify <bundle>       signature + hashes + version rules, changes nothing
    resurgam-update apply <bundle>        verify, back up, install, health-check, roll back on failure
    resurgam-update process-requests      run queued UI requests (resurgam-updater.service)

Apply sequence (each step name is the status file's ``step``):
verify -> backup -> stop -> load_images -> install -> host_setup -> migrate -> start -> health -> done,
and on any failure after the backup: rollback (links back, previous images up, DB restored only if
the Alembic revision changed, workers restarted).

``host_setup`` (since 0.2.2) runs the NEW release's own ``appliance/release/host-setup.sh`` -- so a
release can change host state (e.g. install the Proxmox file-restore packages it ships in the
bundle's ``host-packages/`` repository) without needing a newer updater first. Contract: ADR 0024,
"Host packages and the host-setup hook".
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from resurgam_api.updates import bundle

WORKERS = (
    "resurgam-collector-scheduler",
    "resurgam-file-index-worker",
    "resurgam-file-restore-worker",
    "resurgam-recovery-lab-scheduler",
)
COMPOSE_PROJECT = "appliance"
KEEP_BACKUPS = 5
KEEP_RELEASES = 3  # the current one plus the two before it (rollback targets / disk budget)
HISTORY_LIMIT = 100
CRASH_LOOP_RESTARTS = 3
# Relative to a release directory; shipped in source.tar.gz like everything else in the release.
HOST_SETUP_HOOK = Path("appliance") / "release" / "host-setup.sh"
HOST_SETUP_TIMEOUT = 1800
HOST_SETUP_LOG_LINES = 40
HOST_PROBE_UNIT = "resurgam-host-probe.service"
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_BUNDLE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_SQL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


class UpdateError(Exception):
    """A step failed. ``str(err)`` is operator-facing."""


def utcnow() -> str:
    return datetime.datetime.now(datetime.UTC).replace(microsecond=0).isoformat()


def atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise


def atomic_write_json(path: Path, doc: Any, mode: int = 0o644) -> None:
    atomic_write(path, (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode(), mode)


def atomic_symlink(link: Path, target: str) -> None:
    tmp = link.with_name(f".{link.name}.{os.getpid()}.tmp")
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()
    os.symlink(target, tmp)
    os.replace(tmp, link)


# --- layout ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Layout:
    """Every path the updater touches (ADR 0024 "Layout on the appliance"). Tests point it at tmp."""

    opt_root: Path = Path("/opt/resurgam")
    venvs: Path = Path("/opt/venvs")
    data: Path = Path("/var/lib/resurgam")
    systemd_dir: Path = Path("/etc/systemd/system")
    python: str = "/usr/bin/python3"

    @property
    def releases(self) -> Path:
        return self.opt_root / "releases"

    @property
    def current(self) -> Path:
        return self.opt_root / "current"

    @property
    def stable(self) -> Path:
        """Release-independent: .env, the compose-file link, first-boot.sh, the release public key."""
        return self.opt_root / "appliance"

    @property
    def env_file(self) -> Path:
        return self.stable / ".env"

    @property
    def compose_file(self) -> Path:
        return self.stable / "docker-compose.yml"

    @property
    def public_key(self) -> Path:
        return self.stable / "release-signing.pub"

    @property
    def venv_link(self) -> Path:
        return self.venvs / "resurgam-backend"

    def venv_for(self, version: str) -> Path:
        return self.venvs / f"resurgam-backend-{version}"

    @property
    def updates(self) -> Path:
        return self.data / "updates"

    @property
    def incoming(self) -> Path:
        return self.updates / "incoming"

    @property
    def requests(self) -> Path:
        return self.updates / "requests"

    @property
    def status(self) -> Path:
        return self.updates / "status"

    @property
    def history(self) -> Path:
        return self.updates / "history.json"

    @property
    def lock(self) -> Path:
        return self.updates / ".updater.lock"

    @property
    def backups(self) -> Path:
        return self.data / "backups"

    @property
    def work(self) -> Path:
        """Root-only scratch space -- NOT shared with the API container."""
        return self.data / "update-work"


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return values
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def set_env_value(path: Path, key: str, value: str) -> None:
    """Replace (or append) KEY=value in the shared .env, keeping everything else and mode 600."""
    lines = path.read_text().splitlines() if path.exists() else []
    out, found = [], False
    for line in lines:
        if line.strip().startswith(f"{key}="):
            if not found:
                out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{key}={value}")
    atomic_write(path, ("\n".join(out) + "\n").encode(), 0o600)


# --- host operations (the only place that shells out; faked in tests) -------------------------


class Host:
    """Thin wrappers around docker / systemctl / uv. Every method raises UpdateError on failure."""

    def __init__(self, layout: Layout) -> None:
        self.layout = layout

    # generic
    def run(
        self,
        cmd: list[str],
        *,
        stdin_path: Path | None = None,
        stdout_path: Path | None = None,
        timeout: float | None = 1800,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        with contextlib.ExitStack() as stack:
            stdin = stack.enter_context(open(stdin_path, "rb")) if stdin_path else subprocess.DEVNULL
            stdout = stack.enter_context(open(stdout_path, "wb")) if stdout_path else subprocess.PIPE
            try:
                proc = subprocess.run(  # fixed argv lists, never a shell
                    cmd, stdin=stdin, stdout=stdout, stderr=subprocess.PIPE, timeout=timeout, check=False
                )
            except FileNotFoundError as exc:
                raise UpdateError(f"Required tool {cmd[0]} is not installed on this host.") from exc
            except subprocess.TimeoutExpired as exc:
                raise UpdateError(f"'{' '.join(cmd[:4])} ...' did not finish within {timeout:.0f}s.") from exc
        out = proc.stdout.decode(errors="replace") if isinstance(proc.stdout, bytes) else ""
        err = proc.stderr.decode(errors="replace") if isinstance(proc.stderr, bytes) else ""
        result = subprocess.CompletedProcess(cmd, proc.returncode, out, err)
        if check and proc.returncode != 0:
            detail = (err.strip() or out.strip()).splitlines()[-5:]
            raise UpdateError(f"'{' '.join(cmd[:4])} ...' failed (exit {proc.returncode}): {' | '.join(detail)}")
        return result

    def compose(self, *args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        lay = self.layout
        base = [
            "docker", "compose", "--project-name", COMPOSE_PROJECT, "--project-directory", str(lay.stable),
            "--env-file", str(lay.env_file), "-f", str(lay.compose_file),
        ]  # fmt: skip
        return self.run([*base, *args], **kwargs)

    def _db(self) -> tuple[str, str]:
        env = read_env(self.layout.env_file)
        user, db = env.get("POSTGRES_USER") or "resurgam", env.get("POSTGRES_DB") or "resurgam"
        if not (_SQL_NAME_RE.match(user) and _SQL_NAME_RE.match(db)):
            raise UpdateError("POSTGRES_USER/POSTGRES_DB in .env are not plain identifiers.")
        return user, db

    # database
    def alembic_version(self) -> str | None:
        """The DB's Alembic revision(s), or None if it can't be read."""
        user, db = self._db()
        sql = "SELECT string_agg(version_num, ',' ORDER BY version_num) FROM alembic_version"
        proc = self.compose(
            "exec", "-T", "postgres", "psql", "-U", user, "-d", db, "-tAc", sql, check=False, timeout=60
        )
        value = proc.stdout.strip()
        return value if proc.returncode == 0 and value else None

    def pg_dump(self, dest: Path) -> None:
        user, db = self._db()
        partial = dest.with_name(dest.name + ".partial")
        try:
            fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.close(fd)
            self.compose("exec", "-T", "postgres", "pg_dump", "-U", user, "-Fc", db, stdout_path=partial)
            if partial.stat().st_size == 0:
                raise UpdateError("pg_dump produced an empty file.")
            # Prove the archive is readable before trusting it as the rollback point.
            self.compose("exec", "-T", "postgres", "pg_restore", "--list", stdin_path=partial, timeout=300)
            os.replace(partial, dest)
        finally:
            with contextlib.suppress(FileNotFoundError):
                partial.unlink()

    def restore_db(self, dump: Path) -> None:
        user, db = self._db()
        self.compose("stop", "backend", timeout=300)
        self.compose("up", "-d", "--wait", "postgres", timeout=300)
        psql = ["exec", "-T", "postgres", "psql", "-v", "ON_ERROR_STOP=1", "-U", user, "-d", "postgres", "-c"]
        self.compose(*psql, f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)', timeout=300)
        self.compose(*psql, f'CREATE DATABASE "{db}" OWNER "{user}"', timeout=300)
        self.compose(
            "exec", "-T", "postgres", "pg_restore", "--exit-on-error", "-U", user, "-d", db,
            stdin_path=dump, timeout=3600,
        )  # fmt: skip

    # docker / compose
    def docker_load(self, image_tar: Path) -> None:
        self.run(["docker", "load", "-i", str(image_tar)], timeout=3600)

    def image_exists(self, ref: str) -> bool:
        return self.run(["docker", "image", "inspect", ref], check=False, timeout=60).returncode == 0

    def remove_image(self, ref: str) -> None:
        self.run(["docker", "image", "rm", ref], check=False, timeout=300)

    def compose_up(self) -> None:
        self.compose("up", "-d", timeout=900)

    def service_running(self, service: str) -> bool:
        proc = self.compose("ps", "--status", "running", "--services", check=False, timeout=60)
        return proc.returncode == 0 and service in proc.stdout.split()

    def restart_count(self, service: str) -> int:
        """How often Docker has restarted the service's container (a crash-looping migration shows up here)."""
        ids = self.compose("ps", "-q", service, check=False, timeout=60).stdout.split()
        if not ids:
            return 0
        proc = self.run(["docker", "inspect", "--format", "{{.RestartCount}}", ids[0]], check=False, timeout=60)
        try:
            return int(proc.stdout.strip())
        except ValueError:
            return 0

    def backend_get_json(self, path: str) -> dict[str, Any] | None:
        """GET http://127.0.0.1:8001<path> from inside the backend container (bypasses Caddy/TLS/SNI)."""
        code = (
            "import sys,urllib.request;"
            "print(urllib.request.urlopen('http://127.0.0.1:8001'+sys.argv[1],timeout=5).read().decode())"
        )
        proc = self.compose("exec", "-T", "backend", "python", "-c", code, path, check=False, timeout=60)
        if proc.returncode != 0:
            return None
        try:
            doc = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return None
        return doc if isinstance(doc, dict) else None

    # systemd
    def systemctl(self, *args: str) -> None:
        self.run(["systemctl", *args], timeout=300)

    def try_restart(self, unit: str) -> None:
        """Best effort: restart ``unit`` if it is active (a missing/inactive unit is not an error)."""
        self.run(["systemctl", "try-restart", unit], check=False, timeout=300)

    def run_host_setup(self, hook: Path, args: list[str], env: dict[str, str]) -> tuple[int, list[str]]:
        """Run a release's host-setup hook as root; returns (exit code, combined output lines)."""
        try:
            proc = subprocess.run(  # fixed argv, never a shell string
                ["sh", str(hook), *args],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env={**os.environ, **env},
                timeout=HOST_SETUP_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise UpdateError(f"The host-setup hook did not finish within {HOST_SETUP_TIMEOUT}s.") from exc
        return proc.returncode, proc.stdout.decode(errors="replace").splitlines()

    def unit_active(self, unit: str) -> bool:
        return self.run(["systemctl", "is-active", "--quiet", unit], check=False, timeout=60).returncode == 0

    # venv
    def build_venv(self, venv: Path, wheels: Path) -> None:
        self.run(["uv", "venv", "--quiet", "--python", self.layout.python, str(venv)], timeout=600)
        self.run(
            [
                "uv", "pip", "install", "--quiet", "--python", str(venv / "bin" / "python"), "--offline",
                "--no-index", "--no-cache", "--find-links", str(wheels), "-r", str(wheels / "requirements.txt"),
            ],
            timeout=1800,
        )  # fmt: skip

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def monotonic(self) -> float:
        return time.monotonic()


# --- the run ----------------------------------------------------------------------------------


@dataclass
class ApplyState:
    """What the apply has changed so far -- drives exactly which rollback actions are needed."""

    prev_version: str
    new_version: str
    alembic_before: str | None = None
    backup_file: Path | None = None
    stopped_workers: list[str] = field(default_factory=list)
    release_created: bool = False
    venv_created: bool = False
    links_switched: bool = False
    compose_started: bool = False
    db_restored: bool = False


class Run:
    """One verify or apply, reporting into status/<request_id>.json as it goes."""

    def __init__(
        self,
        layout: Layout,
        host: Host,
        *,
        request_id: str,
        action: str,
        bundle_path: Path,
        requested_by: str,
        echo: bool = False,
        ready_timeout: float = 300,
        worker_settle: float = 10,
    ) -> None:
        self.layout, self.host = layout, host
        self.bundle_path = bundle_path
        self.echo = echo
        self.ready_timeout = ready_timeout
        self.worker_settle = worker_settle
        self.requested_by = requested_by
        self.status: dict[str, Any] = {
            "request_id": request_id,
            "action": action,
            "state": "running",
            "step": "verify",
            "from_version": None,
            "to_version": None,
            "message": "",
            "log": [],
            "manifest": None,
            "backup_file": None,
            "started_at": utcnow(),
            "finished_at": None,
        }
        self.work_dir = layout.work / request_id

    # reporting
    @property
    def status_path(self) -> Path:
        return self.layout.status / f"{self.status['request_id']}.json"

    def save(self) -> None:
        atomic_write_json(self.status_path, self.status)

    def log(self, message: str) -> None:
        line = f"{datetime.datetime.now(datetime.UTC).strftime('%H:%M:%SZ')} {message}"
        self.status["log"].append(line)
        if self.echo:
            print(line, flush=True)
        self.save()

    def step(self, name: str, message: str) -> None:
        self.status["step"] = name
        self.status["message"] = message
        self.log(f"[{name}] {message}")

    def finish(self, state: str, message: str) -> None:
        # step: "done" on success; otherwise it stays where the run stopped ("rollback" after one).
        self.status.update(state=state, message=message, finished_at=utcnow())
        if state == "succeeded":
            self.status["step"] = "done"
        self.log(f"[{state}] {message}")
        if self.status["action"] == "apply" and self.status["to_version"]:
            append_history(
                self.layout,
                {
                    "request_id": self.status["request_id"],
                    "from_version": self.status["from_version"],
                    "to_version": self.status["to_version"],
                    "state": state,
                    "message": message,
                    "backup_file": self.status["backup_file"],
                    "started_at": self.status["started_at"],
                    "finished_at": self.status["finished_at"],
                    "requested_by": self.requested_by,
                },
            )

    # entry point
    def execute(self) -> str:
        """Returns the final state. Never raises for an expected failure -- it is in the status file."""
        self.save()
        try:
            manifest, current = self._verify()
        except (bundle.BundleError, UpdateError) as exc:
            self._cleanup_work()
            self.finish("failed", f"Verification failed: {exc}")
            return "failed"
        if self.status["action"] == "verify":
            self._cleanup_work()
            n = len(manifest.components)
            self.finish(
                "succeeded",
                f"Bundle {manifest.version} is genuine: signature, all {n} components and the version rules "
                f"(running {current}, requires >= {manifest.min_upgrade_from}) check out.",
            )
            return "succeeded"
        try:
            return self._apply(manifest, current)
        finally:
            self._cleanup_work()

    def _verify(self) -> tuple[bundle.Manifest, str]:
        self.step("verify", f"Verifying {self.bundle_path.name}")
        current = current_version(self.layout)
        self.status["from_version"] = current
        if not self.bundle_path.is_file() or self.bundle_path.is_symlink():
            raise UpdateError(f"Bundle {self.bundle_path} does not exist or is not a regular file.")
        public_key = bundle.load_public_key_file(self.layout.public_key)
        with contextlib.suppress(FileNotFoundError):
            shutil.rmtree(self.work_dir)
        self.layout.work.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.layout.work, 0o700)
        manifest = bundle.extract_bundle(self.bundle_path, self.work_dir / "bundle", public_key)
        self.status["manifest"] = manifest.summary()
        self.status["to_version"] = manifest.version
        self.log(f"Signature valid; {len(manifest.components)} component hashes match")
        bundle.check_upgrade_allowed(manifest, current)
        self.log(f"Upgrade {current} -> {manifest.version} allowed (min_upgrade_from {manifest.min_upgrade_from})")
        return manifest, current

    def _apply(self, manifest: bundle.Manifest, current: str) -> str:
        state = ApplyState(prev_version=current, new_version=manifest.version)
        extracted = self.work_dir / "bundle"

        # 2. backup -- a failure here changed nothing, so no rollback.
        try:
            self.step("backup", "Backing up the database (pg_dump -Fc)")
            state.alembic_before = self.host.alembic_version()
            if state.alembic_before is None:
                raise UpdateError("Cannot read the database's Alembic revision -- is the postgres container up?")
            self.layout.backups.mkdir(parents=True, exist_ok=True)
            stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
            backup = self.layout.backups / f"pre-{manifest.version}-{stamp}.dump"
            self.host.pg_dump(backup)
            state.backup_file = backup
            self.status["backup_file"] = str(backup)
            self.log(f"Database backed up to {backup} (schema revision {state.alembic_before})")
            prune_backups(self.layout)
        except (UpdateError, OSError) as exc:
            self.finish("failed", f"Database backup failed, nothing was changed: {exc}")
            return "failed"

        try:
            # 3. stop the native workers
            self.step("stop", "Stopping the native workers")
            state.stopped_workers = [w for w in WORKERS if self.host.unit_active(w)]
            if state.stopped_workers:
                self.host.systemctl("stop", *state.stopped_workers)
            self.log(f"Stopped: {', '.join(state.stopped_workers) or 'none were running'}")

            # 4a. images
            self.step("load_images", "Loading the container images")
            for name in ("backend", "web"):
                self.host.docker_load(extracted / "images" / f"{name}.tar")
                if not self.host.image_exists(f"resurgam-{name}:{manifest.version}"):
                    raise UpdateError(f"images/{name}.tar did not contain resurgam-{name}:{manifest.version}.")
            self.log("Images loaded")

            # 4b. release tree + venv
            self.step("install", f"Installing release {manifest.version}")
            self._install_release(extracted / "source.tar.gz", manifest.version)
            state.release_created = True
            venv = self.layout.venv_for(manifest.version)
            if venv.exists():
                shutil.rmtree(venv)
            state.venv_created = True
            self.host.build_venv(venv, extracted / "wheels")
            self.log(f"Release in {self.layout.releases / manifest.version}, workers' venv in {venv}")

            # 4c. host setup -- the new release's own hook, before anything is switched
            self._host_setup(manifest, extracted, current)

            # 5. switch + compose up (the backend runs Alembic migrations on start)
            self.step("migrate", "Switching to the new release and starting it (database migrations run now)")
            state.links_switched = True
            self._switch_to(manifest.version)
            state.compose_started = True
            self.host.compose_up()
            self._wait_ready(manifest.version)
            self.log(f"API ready at {manifest.version} (schema revision {self.host.alembic_version()})")

            # 6. workers + health
            self.step("start", "Starting the native workers")
            if state.stopped_workers:
                self.host.systemctl("start", *state.stopped_workers)
            self.step("health", "Checking the API, the web container and the workers")
            self.host.sleep(self.worker_settle)
            dead = [w for w in state.stopped_workers if not self.host.unit_active(w)]
            if dead:
                raise UpdateError(f"Worker(s) did not stay running: {', '.join(dead)} (see journalctl -u <unit>).")
            if not self.host.service_running("web"):
                raise UpdateError("The web container is not running.")
            self._check_version(manifest.version)
        except (UpdateError, bundle.BundleError, OSError, tarfile.TarError) as exc:
            return self._rollback(state, exc)

        self.finish("succeeded", f"Updated {current} -> {manifest.version}.")
        with contextlib.suppress(OSError):
            prune_releases(self.layout, self.host)
        if self.bundle_path.parent == self.layout.incoming:
            with contextlib.suppress(OSError):
                self.bundle_path.unlink()
        return "succeeded"

    # helpers
    def _install_release(self, source_tar: Path, version: str) -> None:
        release = self.layout.releases / version
        partial = self.layout.releases / f".{version}.partial"
        for path in (release, partial):
            if path.exists():
                shutil.rmtree(path)
        partial.mkdir(parents=True)
        with tarfile.open(source_tar, "r:gz") as tf:
            tf.extractall(partial, filter="data")
        if not (partial / "appliance" / "docker-compose.yml").is_file():
            raise UpdateError("source.tar.gz has no appliance/docker-compose.yml.")
        os.replace(partial, release)

    def _host_setup(self, manifest: bundle.Manifest, extracted: Path, current: str) -> None:
        release = self.layout.releases / manifest.version
        hook = release / HOST_SETUP_HOOK
        self.step("host_setup", "Setting up the appliance host (the release's host-setup hook)")
        if not hook.is_file():
            self.log(f"Release {manifest.version} ships no {HOST_SETUP_HOOK} -- nothing to set up on the host")
            return
        args: list[str] = []
        if manifest.has_host_packages:
            package_list = extracted / bundle.HOST_PACKAGES_LIST
            repo = extracted / bundle.HOST_PACKAGES_DIR
            names = bundle.parse_package_list(package_list.read_text())
            debs = bundle.check_host_repo(repo, names)
            self.log(f"Host packages to ensure: {', '.join(names)} ({len(debs)} .debs in the bundle)")
            args = ["--repo", str(repo), "--packages", str(package_list)]
        code, lines = self.host.run_host_setup(
            hook,
            args,
            {
                "RESURGAM_FROM_VERSION": current,
                "RESURGAM_TO_VERSION": manifest.version,
                "RESURGAM_RELEASE_DIR": str(release),
            },
        )
        if len(lines) > HOST_SETUP_LOG_LINES:
            self.log(f"  ... ({len(lines) - HOST_SETUP_LOG_LINES} earlier lines of host-setup output omitted)")
        for line in lines[-HOST_SETUP_LOG_LINES:]:
            self.log(f"  {line}")
        if code != 0:
            raise UpdateError(
                f"The host-setup hook of {manifest.version} failed (exit {code}); see the log above. Host packages "
                "it already installed stay installed (they are not rolled back)."
            )
        self.log("Host setup complete")

    def _switch_to(self, version: str) -> None:
        lay = self.layout
        atomic_symlink(lay.current, f"releases/{version}")
        atomic_symlink(lay.venv_link, lay.venv_for(version).name)
        set_env_value(lay.env_file, "RESURGAM_VERSION", version)
        units = sorted((lay.releases / version / "backend" / "packaging" / "systemd").glob("*.service"))
        for unit in units:
            shutil.copyfile(unit, lay.systemd_dir / unit.name)
        self.host.systemctl("daemon-reload")
        # The host probe's report is what the API shows as file-restore capability; refresh it with
        # this release's probe code (and whatever host setup just installed).
        self.host.try_restart(HOST_PROBE_UNIT)
        self.log(f"current -> releases/{version}, venv -> {lay.venv_for(version).name}, RESURGAM_VERSION={version}")

    def _wait_ready(self, version: str) -> None:
        deadline = self.host.monotonic() + self.ready_timeout
        while True:
            doc = self.host.backend_get_json("/api/v1/readiness")
            if doc and doc.get("status") == "ready":
                return
            if self.host.monotonic() >= deadline:
                raise UpdateError(
                    f"The API ({version}) did not become ready within {self.ready_timeout:.0f}s -- typically a "
                    "failed database migration (see 'docker compose logs backend')."
                )
            # Its entrypoint exits on a failed migration and Docker restarts it -- no point waiting
            # out the whole timeout once it is clearly crash-looping.
            restarts = self.host.restart_count("backend")
            if restarts >= CRASH_LOOP_RESTARTS:
                raise UpdateError(
                    f"The API ({version}) keeps crashing on start ({restarts} restarts) -- typically a failed "
                    "database migration (see 'docker compose logs backend')."
                )
            self.host.sleep(3)

    def _check_version(self, version: str) -> None:
        doc = self.host.backend_get_json("/api/v1/version")
        reported = doc.get("version") if doc else None
        if reported != version:
            raise UpdateError(f"The API reports version {reported!r}, expected {version!r}.")

    def _rollback(self, state: ApplyState, cause: Exception) -> str:
        self.step("rollback", f"Update failed ({cause}); rolling back to {state.prev_version}")
        try:
            if state.links_switched:
                self._switch_to(state.prev_version)
            if state.compose_started:
                after = self.host.alembic_version()
                if after != state.alembic_before:
                    if state.backup_file is None:
                        raise UpdateError("the schema changed but there is no backup to restore")
                    self.log(f"Schema revision changed ({state.alembic_before} -> {after}); restoring the backup")
                    self.host.restore_db(state.backup_file)
                    state.db_restored = True
                    self.log(f"Database restored from {state.backup_file}")
                else:
                    self.log("No migration ran (schema revision unchanged) -- database left as is")
                self.host.compose_up()
                self._wait_ready(state.prev_version)
                self._check_version(state.prev_version)
            if state.stopped_workers:
                self.host.systemctl("start", *state.stopped_workers)
            self._discard_release(state)
        except (UpdateError, OSError) as exc:
            message = (
                f"Update to {state.new_version} failed ({cause}) AND the automatic rollback failed: {exc}. "
                f"Manual recovery: see /opt/resurgam/current/appliance/UPDATE.md (rollback section); "
                f"database backup: {state.backup_file}."
            )
            self.finish("failed", message)
            return "failed"
        restored = " (database restored from the pre-update backup)" if state.db_restored else ""
        self.finish("rolled_back", f"Update to {state.new_version} failed and was rolled back{restored}: {cause}")
        return "rolled_back"

    def _discard_release(self, state: ApplyState) -> None:
        if state.new_version == current_version(self.layout):
            return
        if state.release_created:
            shutil.rmtree(self.layout.releases / state.new_version, ignore_errors=True)
        if state.venv_created:
            shutil.rmtree(self.layout.venv_for(state.new_version), ignore_errors=True)
        for image in ("backend", "web"):
            self.host.remove_image(f"resurgam-{image}:{state.new_version}")

    def _cleanup_work(self) -> None:
        shutil.rmtree(self.work_dir, ignore_errors=True)


# --- shared state -----------------------------------------------------------------------------


def current_version(layout: Layout) -> str:
    try:
        target = os.readlink(layout.current)
    except FileNotFoundError as exc:
        raise UpdateError(f"{layout.current} does not exist -- this is not an installed RESURGAM appliance.") from exc
    except OSError as exc:
        raise UpdateError(
            f"{layout.current} is not a symlink -- this appliance predates the release layout (ADR 0024); "
            "reinstall it from a 0.2.0 or newer image once."
        ) from exc
    version = Path(target).name
    bundle.parse_version(version)
    return version


def read_history(layout: Layout) -> list[dict[str, Any]]:
    try:
        doc = json.loads(layout.history.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    return doc if isinstance(doc, list) else []


def append_history(layout: Layout, entry: dict[str, Any]) -> None:
    history = [entry, *read_history(layout)][:HISTORY_LIMIT]
    atomic_write_json(layout.history, history)


def prune_backups(layout: Layout) -> None:
    dumps = sorted(layout.backups.glob("pre-*.dump"), key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    for old in dumps[KEEP_BACKUPS:]:
        old.unlink()


def prune_releases(layout: Layout, host: Host) -> None:
    current = current_version(layout)
    running_venv = Path(sys.prefix).resolve()
    versions = []
    for path in layout.releases.iterdir():
        with contextlib.suppress(bundle.BundleError):
            versions.append((bundle.parse_version(path.name), path.name))
    keep = {name for _, name in sorted(versions, reverse=True)[:KEEP_RELEASES]} | {current}
    for _, name in versions:
        if name in keep or layout.venv_for(name).resolve() == running_venv:
            continue
        shutil.rmtree(layout.releases / name, ignore_errors=True)
        shutil.rmtree(layout.venv_for(name), ignore_errors=True)
        for image in ("backend", "web"):
            host.remove_image(f"resurgam-{image}:{name}")


@contextlib.contextmanager
def updater_lock(layout: Layout, *, wait: bool) -> Iterator[None]:
    layout.updates.mkdir(parents=True, exist_ok=True)
    with open(layout.lock, "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise UpdateError("Another update run is in progress -- wait for it to finish.") from exc
        yield


# --- request queue (resurgam-updater.service) -------------------------------------------------


def _fail_request(layout: Layout, request_id: str, action: str, message: str) -> None:
    atomic_write_json(
        layout.status / f"{request_id}.json",
        {
            "request_id": request_id, "action": action, "state": "failed", "step": "verify",
            "from_version": None, "to_version": None, "message": message, "log": [message], "manifest": None,
            "backup_file": None, "started_at": utcnow(), "finished_at": utcnow(),
        },
    )  # fmt: skip


def recover_interrupted(layout: Layout) -> None:
    """A *.claimed request left behind means the updater died mid-run (crash, reboot)."""
    for claimed in sorted(layout.requests.glob("*.claimed")):
        status_path = layout.status / f"{claimed.stem}.json"
        try:
            status = json.loads(status_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            status = {"request_id": claimed.stem, "action": "apply", "log": []}
        if status.get("state") in (None, "queued", "running"):
            message = (
                f"The updater was interrupted during step '{status.get('step', '?')}' (host crash or reboot). "
                "Check 'resurgam-update status'; if the appliance is not healthy, restore manually per UPDATE.md "
                f"(backup: {status.get('backup_file')})."
            )
            status.update(state="failed", message=message, finished_at=utcnow())
            status.setdefault("log", []).append(message)
            atomic_write_json(status_path, status)
        claimed.unlink()


def process_requests(layout: Layout, host: Host, **run_kwargs: Any) -> int:
    """Run every queued request, oldest first, one at a time. Returns how many were processed."""
    processed = 0
    with updater_lock(layout, wait=True):
        layout.requests.mkdir(parents=True, exist_ok=True)
        recover_interrupted(layout)
        while True:
            queued = sorted(layout.requests.glob("*.json"), key=lambda p: (p.stat().st_mtime, p.name))
            if not queued:
                return processed
            req = queued[0]
            request_id = req.stem
            claimed = req.with_suffix(".claimed")
            os.replace(req, claimed)
            processed += 1
            try:
                if not _REQUEST_ID_RE.match(request_id):
                    continue  # can't even name a status file for it safely
                try:
                    doc = json.loads(claimed.read_text())
                except (json.JSONDecodeError, UnicodeDecodeError):
                    _fail_request(layout, request_id, "apply", "The request file is not valid JSON.")
                    continue
                action = doc.get("action") if isinstance(doc, dict) else None
                name = doc.get("bundle") if isinstance(doc, dict) else None
                if action not in ("verify", "apply"):
                    _fail_request(layout, request_id, "apply", f"Unknown action {action!r} (verify or apply).")
                    continue
                if not isinstance(name, str) or not _BUNDLE_NAME_RE.match(name):
                    _fail_request(layout, request_id, action, "'bundle' must be a plain file name in incoming/.")
                    continue
                Run(
                    layout,
                    host,
                    request_id=request_id,
                    action=action,
                    bundle_path=layout.incoming / name,
                    requested_by=str(doc.get("requested_by") or "web UI"),
                    echo=True,
                    **run_kwargs,
                ).execute()
            finally:
                with contextlib.suppress(FileNotFoundError):
                    claimed.unlink()


# --- CLI --------------------------------------------------------------------------------------


def _console_user() -> str:
    user = os.environ.get("SUDO_USER") or getpass.getuser()
    return f"console ({user})"


def _status_summary(layout: Layout) -> dict[str, Any]:
    try:
        version: str | None = current_version(layout)
    except (UpdateError, bundle.BundleError) as exc:
        version = None
        error = str(exc)
    else:
        error = None
    releases = sorted(p.name for p in layout.releases.iterdir()) if layout.releases.is_dir() else []
    backups = (
        sorted((p.name for p in layout.backups.glob("pre-*.dump")), reverse=True) if layout.backups.is_dir() else []
    )
    return {
        "version": version,
        "error": error,
        "releases": releases,
        "venv": os.path.realpath(layout.venv_link) if layout.venv_link.exists() else None,
        "backups": backups,
        "history": read_history(layout)[:5],
    }


def main(argv: list[str] | None = None, layout: Layout | None = None, host: Host | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="resurgam-update", description="Verify and apply signed RESURGAM update bundles (ADR 0024)."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("status", help="installed version, releases, backups and recent update history")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p = sub.add_parser("verify", help="check a bundle's signature, hashes and version rules (changes nothing)")
    p.add_argument("bundle", type=Path)
    p = sub.add_parser("apply", help="verify, back up, install and health-check a bundle; roll back on failure")
    p.add_argument("bundle", type=Path)
    sub.add_parser("process-requests", help="run the requests queued by the web UI (used by resurgam-updater.service)")
    args = parser.parse_args(argv)
    layout = layout or Layout()
    host = host or Host(layout)

    if args.command == "status":
        summary = _status_summary(layout)
        if args.json:
            print(json.dumps(summary, indent=2))
        else:
            print(f"Installed version: {summary['version'] or 'unknown -- ' + str(summary['error'])}")
            print(f"Releases on disk:  {', '.join(summary['releases']) or '-'}")
            print(f"Workers' venv:     {summary['venv'] or '-'}")
            print(f"Backups ({layout.backups}): {', '.join(summary['backups']) or 'none yet'}")
            for entry in summary["history"]:
                print(f"  {entry.get('finished_at')}  {entry.get('from_version')} -> {entry.get('to_version')}  "
                      f"{entry.get('state')}: {entry.get('message')}")  # fmt: skip
        return 0

    if os.geteuid() != 0:
        print("resurgam-update must run as root (sudo resurgam-update ...).", file=sys.stderr)
        return 1
    try:
        if args.command == "process-requests":
            count = process_requests(layout, host)
            print(f"Processed {count} request(s).")
            return 0
        bundle_path = args.bundle.resolve()
        with updater_lock(layout, wait=False):
            run = Run(
                layout,
                host,
                request_id=f"cli-{datetime.datetime.now(datetime.UTC).strftime('%Y%m%dT%H%M%SZ')}",
                action=args.command,
                bundle_path=bundle_path,
                requested_by=_console_user(),
                echo=True,
            )
            state = run.execute()
    except UpdateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Result: {state} -- {run.status['message']}")
    return {"succeeded": 0, "rolled_back": 3}.get(state, 1)


if __name__ == "__main__":
    sys.exit(main())
