"""Backend side of appliance updates (ADR 0024): the shared directory.

The backend container never touches Docker or systemd. It only

- writes uploaded bundles to ``<updates_dir>/incoming/<upload-id>.bundle``
  (streamed to a dot-prefixed ``.part`` file first, then renamed, so the
  host updater never sees a half-written bundle),
- writes ``<updates_dir>/requests/<request-id>.json`` atomically (dot-
  prefixed temp file + ``os.replace``) for the host-side
  ``resurgam-updater.path`` unit to pick up, and
- reads ``status/<request-id>.json``, ``history.json`` and the backup
  directory back.

Everything here is plain filesystem code with no FastAPI or DB imports, so
it can be tested against a tmp directory.
"""

import json
import os
import re
import tarfile
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# A tar header block is 512 bytes; POSIX ustar / GNU tar both put the
# "ustar" magic at offset 257. Enough to reject a non-tar upload after
# its first block instead of after gigabytes.
TAR_BLOCK_SIZE = 512
_TAR_MAGIC_OFFSET = 257
_TAR_MAGIC = b"ustar"

MANIFEST_MEMBER = "manifest.json"
SIGNATURE_MEMBER = "manifest.json.sig"
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024  # same bound as updates/bundle.py

ACTIVE_STATES = frozenset({"queued", "running"})
TERMINAL_STATES = frozenset({"succeeded", "failed", "rolled_back"})

# A request the host updater has not picked up within this long (no status
# file yet) no longer blocks a new apply -- the updater is evidently not
# running, and it re-verifies `version > current` anyway, so a second
# queued apply of the same bundle cannot apply twice.
UNPICKED_REQUEST_STALE_SECONDS = 30 * 60
# A "running" status file the updater has not rewritten for this long is
# treated as abandoned (the updater rewrites it at every step).
RUNNING_STATUS_STALE_SECONDS = 2 * 60 * 60

# UI requests use uuid4 hex; CLI-started runs on the host write
# status/cli-<timestamp>.json. Never a dot or slash (no path tricks).
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
# Operators may also copy a bundle into incoming/ by hand, so any plain
# file name ending in .bundle is accepted -- but never a path.
_BUNDLE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}\.bundle$")


class UpdatesError(Exception):
    """A problem the API layer maps onto a typed error response."""

    def __init__(self, code: str, message: str, status_code: int) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def file_time_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()


def is_valid_request_id(request_id: str) -> bool:
    return bool(_REQUEST_ID_RE.match(request_id))


def is_valid_bundle_name(name: str) -> bool:
    return bool(_BUNDLE_NAME_RE.match(name))


def looks_like_tar_header(block: bytes) -> bool:
    return len(block) >= _TAR_MAGIC_OFFSET + len(_TAR_MAGIC) and (
        block[_TAR_MAGIC_OFFSET : _TAR_MAGIC_OFFSET + len(_TAR_MAGIC)] == _TAR_MAGIC
    )


def _normalize_member(name: str) -> str:
    while name.startswith("./"):
        name = name[2:]
    return name


@dataclass
class BundleInspection:
    """What the backend can tell about a bundle without the host updater:
    the members are present and the manifest parses. Signature and
    component hashes are the host updater's authoritative verify step."""

    manifest: dict[str, Any] | None
    has_signature: bool
    problem: str | None


def inspect_bundle(path: Path) -> BundleInspection:
    """Open the (uncompressed) tar and read `manifest.json`. Iterating an
    uncompressed tar only reads the 512-byte headers and seeks over member
    data, so this stays cheap for a multi-GB bundle."""
    manifest_bytes: bytes | None = None
    has_signature = False
    try:
        with tarfile.open(path, mode="r:") as archive:
            for member in archive:
                name = _normalize_member(member.name)
                if name == SIGNATURE_MEMBER and member.isfile():
                    has_signature = True
                elif name == MANIFEST_MEMBER and member.isfile():
                    if member.size > _MAX_MANIFEST_BYTES:
                        return BundleInspection(None, has_signature, "manifest.json is implausibly large.")
                    extracted = archive.extractfile(member)
                    manifest_bytes = extracted.read() if extracted else None
                if manifest_bytes is not None and has_signature:
                    break
    except (tarfile.TarError, OSError) as exc:
        return BundleInspection(None, False, f"Not a readable tar archive ({exc}).")

    if manifest_bytes is None:
        return BundleInspection(None, has_signature, "The archive has no manifest.json.")
    if not has_signature:
        return BundleInspection(None, False, "The archive has no manifest.json.sig signature.")
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return BundleInspection(None, True, "manifest.json is not valid JSON.")
    if not isinstance(manifest, dict) or not isinstance(manifest.get("version"), str):
        return BundleInspection(None, True, "manifest.json has no version.")
    return BundleInspection(manifest, True, None)


class UpdatesStore:
    def __init__(self, updates_dir: str | Path, backups_dir: str | Path) -> None:
        self.root = Path(updates_dir)
        self.backups = Path(backups_dir)

    # --- availability ---------------------------------------------------

    def unavailable_reason(self) -> str | None:
        if not self.root.is_dir():
            return (
                f"Updates are only available on the RESURGAM appliance: the shared updates directory "
                f"{self.root} does not exist. On an appliance, check that it is mounted into the backend "
                "container (UPDATES_DIR)."
            )
        if not os.access(self.root, os.W_OK | os.X_OK):
            return (
                f"The updates directory {self.root} is not writable by the backend. Check the bind mount "
                "in docker-compose.yml is read-write and owned by the backend's user."
            )
        return None

    def require_available(self) -> None:
        reason = self.unavailable_reason()
        if reason:
            raise UpdatesError("updates_unavailable", reason, 503)
        for sub in ("incoming", "requests"):
            (self.root / sub).mkdir(exist_ok=True)

    @property
    def incoming(self) -> Path:
        return self.root / "incoming"

    @property
    def requests_dir(self) -> Path:
        return self.root / "requests"

    @property
    def status_dir(self) -> Path:
        return self.root / "status"

    # --- bundles ----------------------------------------------------------

    def new_upload_paths(self) -> tuple[Path, Path]:
        upload_id = uuid.uuid4().hex
        return self.incoming / f".{upload_id}.part", self.incoming / f"{upload_id}.bundle"

    def bundle_path(self, name: str) -> Path:
        if not is_valid_bundle_name(name):
            raise UpdatesError(
                "invalid_bundle_name",
                f"{name!r} is not a bundle file name (expected a plain name ending in .bundle, no path).",
                400,
            )
        path = self.incoming / name
        if not path.is_file():
            raise UpdatesError(
                "bundle_not_found",
                f"No uploaded bundle named {name!r}. Upload it first (or copy it into {self.incoming}).",
                404,
            )
        return path

    def list_bundles(self) -> list[dict[str, Any]]:
        bundles = []
        for path in self.incoming.glob("*.bundle"):
            if not path.is_file() or not is_valid_bundle_name(path.name):
                continue
            inspection = inspect_bundle(path)
            bundles.append(
                {
                    "name": path.name,
                    "size_bytes": path.stat().st_size,
                    "uploaded_at": file_time_iso(path),
                    "version": (inspection.manifest or {}).get("version"),
                    "problem": inspection.problem,
                }
            )
        bundles.sort(key=lambda b: b["uploaded_at"], reverse=True)
        return bundles

    def delete_bundle(self, name: str) -> None:
        path = self.bundle_path(name)
        now = time.time()
        for status in self.list_request_statuses():
            if status.get("bundle") == name and self._is_active(status, now):
                raise UpdatesError(
                    "bundle_in_use",
                    f"Bundle {name!r} is used by the {status.get('action')} request {status['request_id']} "
                    "that is still queued or running. Wait for it to finish, then delete the bundle.",
                    409,
                )
        path.unlink()

    # --- requests and status ---------------------------------------------

    def _read_json(self, path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            # The updater rewrites atomically, so this is a genuinely broken
            # file, not a half-written one; treat it as absent.
            return None

    def _write_json_atomic(self, path: Path, data: dict[str, Any]) -> None:
        # Dot-prefixed and not ending in .json, so the host's path unit
        # (which triggers on *.json) never sees a half-written file.
        tmp = path.with_name(f".{path.stem}.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)

    def _read_request(self, request_id: str) -> tuple[dict[str, Any] | None, Path | None]:
        """The request as written, also after the updater claimed it (it
        renames `<id>.json` to `<id>.claimed` and deletes it when done)."""
        for suffix in (".json", ".claimed"):
            path = self.requests_dir / f"{request_id}{suffix}"
            data = self._read_json(path)
            if isinstance(data, dict):
                return data, path
        return None, None

    def _status_for(self, request_id: str, request: dict[str, Any] | None) -> dict[str, Any] | None:
        status_path = self.status_dir / f"{request_id}.json"
        status = self._read_json(status_path)
        if isinstance(status, dict):
            merged = dict(status)
            merged["request_id"] = request_id
            # Tolerate a status file from a newer/older updater rather than
            # failing the whole response on one unexpected value.
            if merged.get("state") not in ACTIVE_STATES | TERMINAL_STATES:
                merged["state"] = "running"
            if not isinstance(merged.get("log"), list):
                merged["log"] = []
            merged["log"] = [str(line) for line in merged["log"]]
            if not isinstance(merged.get("manifest"), dict):
                merged["manifest"] = None
            if request:
                merged.setdefault("action", request.get("action"))
                merged.setdefault("bundle", request.get("bundle"))
                merged.setdefault("requested_by", request.get("requested_by"))
                merged.setdefault("requested_at", request.get("requested_at"))
            merged["_mtime"] = status_path.stat().st_mtime if status_path.exists() else None
            return merged
        if request is None:
            return None
        # Missing status file = the updater has not picked the request up yet.
        _, request_path = self._read_request(request_id)
        return {
            "request_id": request_id,
            "action": request.get("action"),
            "bundle": request.get("bundle"),
            "requested_by": request.get("requested_by"),
            "requested_at": request.get("requested_at"),
            "state": "queued",
            "step": None,
            "message": None,
            "log": [],
            "_mtime": request_path.stat().st_mtime if request_path and request_path.exists() else None,
        }

    def get_status(self, request_id: str) -> dict[str, Any]:
        if not is_valid_request_id(request_id):
            raise UpdatesError("invalid_request_id", f"{request_id!r} is not an update request id.", 400)
        request, _ = self._read_request(request_id)
        status = self._status_for(request_id, request)
        if status is None:
            raise UpdatesError("update_request_not_found", f"No update request {request_id}.", 404)
        status.pop("_mtime", None)
        return status

    def list_request_statuses(self) -> list[dict[str, Any]]:
        """Every known request (from requests/ and status/ -- the updater may
        remove a request file once it has picked it up), newest first."""
        ids: set[str] = set()
        for directory in (self.requests_dir, self.status_dir):
            if directory.is_dir():
                for pattern in ("*.json", "*.claimed"):
                    ids.update(p.stem for p in directory.glob(pattern) if is_valid_request_id(p.stem))
        statuses = []
        for request_id in ids:
            request, _ = self._read_request(request_id)
            status = self._status_for(request_id, request)
            if status is not None:
                statuses.append(status)
        statuses.sort(key=lambda s: s.get("requested_at") or s.get("started_at") or "", reverse=True)
        return statuses

    def _is_active(self, status: dict[str, Any], now: float) -> bool:
        state = status.get("state")
        if state not in ACTIVE_STATES:
            return False
        mtime = status.get("_mtime")
        if mtime is None:
            return True
        limit = UNPICKED_REQUEST_STALE_SECONDS if state == "queued" else RUNNING_STATUS_STALE_SECONDS
        return now - mtime < limit

    def active_apply(self) -> dict[str, Any] | None:
        now = time.time()
        for status in self.list_request_statuses():
            if status.get("action") == "apply" and self._is_active(status, now):
                return status
        return None

    def create_request(self, *, action: str, bundle: str, requested_by: str) -> dict[str, Any]:
        self.bundle_path(bundle)
        if action == "apply":
            active = self.active_apply()
            if active is not None:
                raise UpdatesError(
                    "update_already_in_progress",
                    f"Another update (request {active['request_id']}) is still {active['state']}. "
                    "Wait for it to finish before starting a new one.",
                    409,
                )
        request_id = uuid.uuid4().hex
        request = {
            "action": action,
            "bundle": bundle,
            "requested_by": requested_by,
            "requested_at": _now_iso(),
        }
        self._write_json_atomic(self.requests_dir / f"{request_id}.json", request)
        return self.get_status(request_id)

    # --- history and backups ---------------------------------------------

    def history(self) -> list[dict[str, Any]]:
        data = self._read_json(self.root / "history.json")
        if isinstance(data, dict):
            data = data.get("entries", data.get("history", []))
        if not isinstance(data, list):
            return []
        return [entry for entry in data if isinstance(entry, dict)]

    def list_backups(self) -> list[dict[str, Any]]:
        if not self.backups.is_dir():
            return []
        backups = []
        for path in self.backups.glob("*.dump"):
            if path.is_file():
                backups.append(
                    {"name": path.name, "size_bytes": path.stat().st_size, "created_at": file_time_iso(path)}
                )
        backups.sort(key=lambda b: b["created_at"], reverse=True)
        return backups
