"""Release update source (docs/adr/0026): check the configured GitHub/GitLab
repository for newer RESURGAM releases and download a release's bundle into
the shared updates directory, where the ADR 0024 verify/apply flow takes
over unchanged.

- ``run_check`` asks the source for releases and caches the newer ones on
  the `UpdateSourceSettings` row (manual "Check now", and
  ``run_due_check`` from the collector scheduler for automatic checks).
- ``start_download`` streams a bundle to ``incoming/.<id>.part`` in a
  background thread of the API process and renames it to
  ``incoming/resurgam-<version>-<id8>.bundle`` once its manifest and (when
  the release key is baked in) its signature check out. Progress lives in
  ``<updates_dir>/downloads/<id>.json`` -- a file, like the updater's own
  status files, so it survives a page reload and a lost thread (backend
  restart) shows up as a stale download instead of hanging forever; its
  leftover ``.part`` file is swept, and finished status files older than
  ``DOWNLOAD_RETENTION_DAYS`` are pruned.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.orm import Session

from resurgam_api.appliance_updates import UpdatesError, UpdatesStore, inspect_bundle, is_valid_request_id
from resurgam_api.models.secrets import SecretReference
from resurgam_api.models.update_source import UpdateSourceSettings
from resurgam_api.secrets.local_provider import LocalEncryptedSecretsProvider
from resurgam_api.secrets.provider import SecretNotFoundError
from resurgam_api.updates import bundle
from resurgam_api.updates.release_source import (
    ReleaseInfo,
    ReleaseSourceClient,
    ReleaseSourceError,
    newer_releases,
    parse_repository_url,
)
from resurgam_api.version import running_version

logger = logging.getLogger("resurgam_api.update_source")

TOKEN_REFERENCE_KEY = "update-source-token"
DOWNLOAD_STATES = ("running", "succeeded", "failed")
# The download thread rewrites its status file at least this often while
# bytes arrive; a "running" file untouched for DOWNLOAD_STALE_SECONDS means
# the thread is gone (backend restarted mid-download).
_PROGRESS_WRITE_SECONDS = 2.0
DOWNLOAD_STALE_SECONDS = 5 * 60
# While the bundle is inspected and its signature checked (no bytes arrive)
# a heartbeat keeps touching the status file this often.
_HEARTBEAT_SECONDS = 30.0
# Finished downloads stay listed this long, then their status file goes.
DOWNLOAD_RETENTION_DAYS = 7

# Tests swap in an httpx.MockTransport here.
TRANSPORT: httpx.BaseTransport | None = None


def get_update_source(db: Session) -> UpdateSourceSettings | None:
    return db.query(UpdateSourceSettings).first()


def resolve_token(db: Session, settings, row: UpdateSourceSettings) -> str | None:
    if row.token_ref_id is None:
        return None
    ref = db.get(SecretReference, row.token_ref_id)
    if ref is None:
        return None
    try:
        return LocalEncryptedSecretsProvider(db, settings.secrets_local_encryption_key).retrieve(ref.reference_key)
    except SecretNotFoundError:
        return None


def same_token_scope(row: UpdateSourceSettings, provider: str, repository_url: str) -> bool:
    """Whether the stored token (saved for ``row``) may be sent to the source
    named by ``provider`` + ``repository_url``: same provider and the same
    API base (scheme and host). Otherwise the token would travel to a host
    nobody saved it for."""
    if row.provider != provider:
        return False
    try:
        saved = parse_repository_url(row.repository_url, row.provider)
        given = parse_repository_url(repository_url, provider)
    except ReleaseSourceError:
        return False
    return (saved.api_scheme, saved.api_host) == (given.api_scheme, given.api_host)


def build_client(provider: str, repository_url: str, token: str | None, tls_verify: bool) -> ReleaseSourceClient:
    return ReleaseSourceClient(
        parse_repository_url(repository_url, provider),
        token,
        tls_verify=tls_verify,
        user_agent=f"RESURGAM/{running_version()}",
        transport=TRANSPORT,
    )


def client_for(db: Session, settings, row: UpdateSourceSettings) -> ReleaseSourceClient:
    return build_client(row.provider, row.repository_url, resolve_token(db, settings, row), row.tls_verify)


# --- checking ----------------------------------------------------------------------


def run_check(db: Session, settings, row: UpdateSourceSettings) -> UpdateSourceSettings:
    """Ask the source for releases and cache the result on ``row`` (committed).
    A failure is recorded in ``last_check_error`` and keeps the previous list,
    so a temporary outage does not hide an update already found."""
    current = running_version()
    try:
        releases = client_for(db, settings, row).list_releases()
    except ReleaseSourceError as exc:
        row.last_check_error = exc.message
    else:
        found = newer_releases(releases, current, include_prereleases=row.include_prereleases)
        row.available_releases = [r.to_dict() for r in found]
        row.latest_version = found[0].version if found else None
        row.last_check_error = None
    row.last_checked_at = datetime.now(UTC)
    db.commit()
    db.refresh(row)
    return row


def run_due_check(db: Session, settings) -> bool:
    """Collector-scheduler hook: one check when automatic checks are on and
    the interval has passed. Returns whether a check ran."""
    row = get_update_source(db)
    if row is None or not row.auto_check:
        return False
    if row.last_checked_at and datetime.now(UTC) - row.last_checked_at < timedelta(hours=row.check_interval_hours):
        return False
    run_check(db, settings, row)
    if row.last_check_error:
        logger.warning("update source check failed: %s", row.last_check_error)
    elif row.latest_version:
        logger.info("update source: version %s is available", row.latest_version)
    return True


def cached_releases(row: UpdateSourceSettings | None) -> list[dict[str, Any]]:
    """The cached list, minus anything no longer newer than what runs (it
    was cached before an update was applied)."""
    if row is None or not row.available_releases:
        return []
    current = running_version()
    infos = []
    for raw in row.available_releases:
        try:
            infos.append(ReleaseInfo(**raw))
        except TypeError:
            continue
    return [r.to_dict() for r in newer_releases(infos, current, include_prereleases=row.include_prereleases)]


def find_cached_release(row: UpdateSourceSettings | None, version: str) -> dict[str, Any] | None:
    return next((r for r in cached_releases(row) if r["version"] == version), None)


# --- downloading -------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _downloads_dir(store: UpdatesStore) -> Path:
    path = store.root / "downloads"
    path.mkdir(exist_ok=True)
    return path


def _write_status(store: UpdatesStore, status: dict[str, Any]) -> None:
    path = _downloads_dir(store) / f"{status['download_id']}.json"
    tmp = path.with_name(f".{path.stem}.tmp")
    tmp.write_text(json.dumps(status, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _part_path(store: UpdatesStore, download_id: str) -> Path:
    return store.incoming / f".{download_id}.part"


def _read_status(path: Path, store: UpdatesStore | None = None) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        mtime = path.stat().st_mtime
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("state") == "running" and time.time() - mtime > DOWNLOAD_STALE_SECONDS:
        data["state"] = "failed"
        data["message"] = "The download stopped making progress (the backend was probably restarted). Start it again."
        download_id = data.get("download_id")
        if store is not None and isinstance(download_id, str) and is_valid_request_id(download_id):
            _part_path(store, download_id).unlink(missing_ok=True)
    return data


def _sweep(store: UpdatesStore) -> None:
    """Remove ``.part`` files no running download owns (left behind by a
    backend restart mid-download) and status files of downloads that
    finished more than DOWNLOAD_RETENTION_DAYS ago."""
    downloads = _downloads_dir(store)
    cutoff = time.time() - DOWNLOAD_RETENTION_DAYS * 86400
    for path in downloads.glob("*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                data = _read_status(path, store)
                if data is None or data.get("state") != "running":
                    path.unlink(missing_ok=True)
        except OSError:
            continue
    if not store.incoming.is_dir():
        return
    for part in store.incoming.glob(".*.part"):
        download_id = part.name[1 : -len(".part")]
        status_path = downloads / f"{download_id}.json"
        data = _read_status(status_path, store) if is_valid_request_id(download_id) else None
        if data is None or data.get("state") != "running":
            part.unlink(missing_ok=True)


@contextlib.contextmanager
def _start_lock(store: UpdatesStore):
    """Serialises the check-for-a-running-download-then-start sequence across
    threads and API worker processes."""
    with (_downloads_dir(store) / ".start.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextlib.contextmanager
def _heartbeat(store: UpdatesStore, download_id: str):
    """Keep a running download from looking stale while no bytes arrive
    (bundle inspection, signature check): touch its status file."""
    path = _downloads_dir(store) / f"{download_id}.json"
    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(_HEARTBEAT_SECONDS):
            with contextlib.suppress(OSError):
                os.utime(path)

    thread = threading.Thread(target=beat, name=f"bundle-download-heartbeat-{download_id[:8]}", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()


def get_download(store: UpdatesStore, download_id: str) -> dict[str, Any]:
    if not is_valid_request_id(download_id):
        raise UpdatesError("invalid_download_id", f"{download_id!r} is not a download id.", 400)
    data = _read_status(_downloads_dir(store) / f"{download_id}.json", store)
    if data is None:
        raise UpdatesError("download_not_found", f"No bundle download {download_id}.", 404)
    return data


def list_downloads(store: UpdatesStore) -> list[dict[str, Any]]:
    _sweep(store)
    found = [d for p in _downloads_dir(store).glob("*.json") if (d := _read_status(p, store)) is not None]
    found.sort(key=lambda d: d.get("started_at") or "", reverse=True)
    return found


def active_download(store: UpdatesStore) -> dict[str, Any] | None:
    return next((d for d in list_downloads(store) if d.get("state") == "running"), None)


def start_download(
    store: UpdatesStore,
    settings,
    client: ReleaseSourceClient,
    release: dict[str, Any],
    requested_by: str,
    *,
    background: bool = True,
) -> dict[str, Any]:
    """Queue a bundle download; returns its initial status. One at a time."""
    if not release.get("bundle_url"):
        raise UpdatesError(
            "release_has_no_bundle",
            f"Release {release['version']} has no resurgam-{release['version']}.bundle asset attached, so it "
            "cannot be installed from here. Attach the bundle to the release (appliance/release/publish-release.sh).",
            409,
        )
    with _start_lock(store):
        running = active_download(store)
        if running is not None:
            raise UpdatesError(
                "download_in_progress",
                f"The bundle for {running.get('version')} is still downloading. Wait for it to finish.",
                409,
            )
        download_id = uuid.uuid4().hex
        status = {
            "download_id": download_id,
            "version": release["version"],
            "source_asset": release.get("bundle_name"),
            "state": "running",
            "bytes_done": 0,
            "bytes_total": release.get("bundle_size"),
            "bundle": None,
            "message": None,
            "requested_by": requested_by,
            "started_at": _now_iso(),
            "finished_at": None,
        }
        _write_status(store, status)
    args = (store, settings, client, release, dict(status))
    if background:
        threading.Thread(
            target=_run_download, args=args, name=f"bundle-download-{download_id[:8]}", daemon=True
        ).start()
    else:
        _run_download(*args)
    return get_download(store, download_id)


def _run_download(
    store: UpdatesStore, settings, client: ReleaseSourceClient, release: dict[str, Any], status: dict[str, Any]
) -> None:
    download_id = status["download_id"]
    part = _part_path(store, download_id)
    last_write = 0.0

    def progress(done: int, total: int | None) -> None:
        nonlocal last_write
        now = time.monotonic()
        if now - last_write >= _PROGRESS_WRITE_SECONDS:
            last_write = now
            status["bytes_done"] = done
            status["bytes_total"] = total or status.get("bytes_total")
            _write_status(store, status)

    try:
        received = client.download(
            release["bundle_url"], part, max_bytes=settings.updates_max_bundle_bytes, on_progress=progress
        )
        status["bytes_done"] = received
        status["bytes_total"] = received
        _write_status(store, status)
        with _heartbeat(store, download_id):
            _verify_download(settings, part, release, status)
        final = store.incoming / f"resurgam-{release['version']}-{download_id[:8]}.bundle"
        os.replace(part, final)
        status.update(state="succeeded", bundle=final.name, message=None, finished_at=_now_iso())
    except ReleaseSourceError as exc:
        status.update(state="failed", message=exc.message, finished_at=_now_iso())
    except Exception as exc:  # never leave a "running" status behind
        logger.exception("bundle download %s failed", download_id)
        status.update(state="failed", message=f"The download failed: {exc}", finished_at=_now_iso())
    finally:
        part.unlink(missing_ok=True)
        _write_status(store, status)


def _verify_download(settings, part: Path, release: dict[str, Any], status: dict[str, Any]) -> None:
    """Manifest version check and (when the release key is baked in) the
    signature pre-check; raises ReleaseSourceError with the reason."""
    inspection = inspect_bundle(part)
    if inspection.problem:
        raise ReleaseSourceError(
            "not_a_bundle", f"The downloaded file is not a RESURGAM update bundle: {inspection.problem}"
        )
    manifest_version = (inspection.manifest or {}).get("version")
    if manifest_version != release["version"]:
        raise ReleaseSourceError(
            "bundle_version_mismatch",
            f"The release is tagged {release['version']} but its bundle contains version {manifest_version}. "
            "The release has the wrong asset attached.",
        )
    key_path = Path(settings.release_public_key_file)
    if key_path.is_file():
        try:
            bundle.verify_bundle(part, bundle.load_public_key_file(key_path), None, check_hashes=False)
        except bundle.BundleError as exc:
            raise ReleaseSourceError("bundle_signature_invalid", f"The bundle was rejected: {exc}") from exc
        status["signature_prechecked"] = True
