"""PBS client-side encryption keys, stored by fingerprint (ADR 0031, stage 2).

- A keyfile is kept exactly as uploaded (PBS JSON format) in the credential
  store, so downloading it returns a file `proxmox-backup-client --keyfile`
  and a PVE storage's `encryption-key` accept as is.
- Every read of a backup uses the key whose fingerprint that backup carries
  (`BackupSnapshot.key_fingerprint`, from PBS's own metadata) -- and no key at
  all for an unencrypted backup. Found live (2026-11-22): passing a key for an
  unencrypted backup makes `proxmox-backup-client` refuse it ("manifest has no
  signature, but an encryption key is set"), which is why ADR 0022's one key
  per datastore broke every unencrypted backup in that datastore.
- Exports are encrypted with a passphrase (scrypt + AES-256-GCM); never
  plaintext.
"""

from __future__ import annotations

import base64
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from sqlalchemy.orm import Session

from resurgam_api.config import Settings
from resurgam_api.models.datastore import Datastore
from resurgam_api.models.encryption_key import EncryptionKey
from resurgam_api.models.inventory import BackupSnapshot
from resurgam_api.models.secrets import SecretPurpose, SecretReference
from resurgam_api.secrets.local_provider import LocalEncryptedSecretsProvider

EXPORT_FORMAT = "resurgam-encryption-keys"
EXPORT_VERSION = 1
# scrypt cost for exports: ~64 MiB, a fraction of a second -- an export is made
# rarely and only by a platform admin, so it can afford a slow KDF.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**16, 8, 1
MIN_EXPORT_PASSPHRASE_LENGTH = 12


class KeyfileError(ValueError):
    """An uploaded or imported keyfile RESURGAM cannot use. `code` goes into
    the API error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class EncryptionKeyMissingError(Exception):
    """A backup is encrypted with a key RESURGAM does not hold."""

    def __init__(self, fingerprint: str) -> None:
        self.fingerprint = fingerprint
        super().__init__(
            f"This backup is encrypted with key {fingerprint}, which RESURGAM does not have. "
            "Upload it under Administration -> Encryption keys."
        )


@dataclass(frozen=True)
class ParsedKeyfile:
    fingerprint: str
    kdf: str
    created_at: datetime | None


def parse_keyfile(content: str) -> ParsedKeyfile:
    """Checks a PBS keyfile (the JSON `proxmox-backup-client key create` and PVE
    write) and returns its fingerprint, KDF and creation time. PBS stores the
    fingerprint inside the file; PVE itself reads it from there
    (`PVE::Storage::PBSPlugin::on_add_hook`)."""
    try:
        data = json.loads(content)
    except (TypeError, ValueError) as exc:
        raise KeyfileError("invalid_keyfile", "This is not a PBS keyfile (not valid JSON).") from exc
    if not isinstance(data, dict) or not isinstance(data.get("data"), str):
        raise KeyfileError("invalid_keyfile", "This is not a PBS keyfile (no 'data' field).")
    fingerprint = data.get("fingerprint")
    if not isinstance(fingerprint, str) or len(fingerprint.split(":")) != 32:
        raise KeyfileError(
            "keyfile_without_fingerprint",
            "This keyfile carries no fingerprint. Re-export it with a current proxmox-backup-client "
            "(`proxmox-backup-client key show` prints the fingerprint).",
        )
    kdf_raw = data.get("kdf")
    if kdf_raw is None:
        kdf = "none"
    elif isinstance(kdf_raw, dict) and kdf_raw:
        kdf = next(iter(kdf_raw)).lower()
    else:
        kdf = str(kdf_raw).lower()
    if kdf != "none":
        raise KeyfileError(
            "password_protected_key",
            "This key is protected with a passphrase, which RESURGAM does not support yet. PVE and vzdump use keys "
            "without passphrase; to convert one, run `proxmox-backup-client key change-passphrase --kdf none "
            "<keyfile>` on a copy and upload that.",
        )
    created_at = None
    if isinstance(data.get("created"), str):
        try:
            created_at = datetime.fromisoformat(data["created"])
        except ValueError:
            created_at = None
    return ParsedKeyfile(fingerprint=fingerprint.lower(), kdf=kdf, created_at=created_at)


def _provider(db: Session, settings: Settings) -> LocalEncryptedSecretsProvider:
    return LocalEncryptedSecretsProvider(db, settings.secrets_local_encryption_key)


def store_key(
    db: Session,
    settings: Settings,
    content: str,
    *,
    label: str = "",
    customer_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    source: str = "upload",
) -> EncryptionKey:
    """Adds a keyfile to the store. Raises `KeyfileError` for an unusable file
    or a fingerprint that is already stored."""
    parsed = parse_keyfile(content)
    if db.query(EncryptionKey).filter_by(fingerprint=parsed.fingerprint).one_or_none() is not None:
        raise KeyfileError("key_exists", f"A key with fingerprint {parsed.fingerprint} is already stored.")
    reference_key = f"encryption-key-{uuid.uuid4()}"
    secret_ref = SecretReference(
        reference_key=reference_key,
        description=f"PBS encryption keyfile {parsed.fingerprint}",
        purpose=SecretPurpose.PBS_ENCRYPTION_KEY,
    )
    db.add(secret_ref)
    db.flush()
    _provider(db, settings).store(reference_key, content)
    key = EncryptionKey(
        fingerprint=parsed.fingerprint,
        label=label.strip(),
        customer_id=customer_id,
        keyfile_secret_ref_id=secret_ref.id,
        kdf=parsed.kdf,
        key_created_at=parsed.created_at,
        source=source,
        created_by_user_id=user_id,
    )
    db.add(key)
    db.flush()
    return key


def keyfile_content(db: Session, settings: Settings, key: EncryptionKey) -> str:
    secret_ref = db.get(SecretReference, key.keyfile_secret_ref_id)
    return _provider(db, settings).retrieve(secret_ref.reference_key)


def delete_key(db: Session, settings: Settings, key: EncryptionKey) -> None:
    secret_ref = db.get(SecretReference, key.keyfile_secret_ref_id)
    db.delete(key)
    db.flush()
    if secret_ref is not None:
        _provider(db, settings).delete(secret_ref.reference_key)
        db.delete(secret_ref)
        db.flush()


def keyfile_for_fingerprint(db: Session, settings: Settings, fingerprint: str | None) -> str | None:
    """The keyfile for `fingerprint`; None for no fingerprint (an unencrypted
    backup). Raises `EncryptionKeyMissingError` when the key is not stored."""
    if not fingerprint:
        return None
    key = db.query(EncryptionKey).filter_by(fingerprint=fingerprint.lower()).one_or_none()
    if key is None:
        raise EncryptionKeyMissingError(fingerprint)
    return keyfile_content(db, settings, key)


def keyfile_for_snapshot(db: Session, settings: Settings, snapshot: BackupSnapshot) -> str | None:
    """The key a read of `snapshot` must pass: exactly the one its fingerprint
    names, or none for an unencrypted backup (ADR 0031)."""
    return keyfile_for_fingerprint(db, settings, snapshot.key_fingerprint)


def require_key_for_snapshot(db: Session, snapshot: BackupSnapshot | None) -> None:
    """Raises `EncryptionKeyMissingError` when `snapshot` is encrypted with a
    key RESURGAM does not hold -- for endpoints that start work on a backup
    (Instant Recovery, Disk Mounts, lab runs), so they refuse up front."""
    if snapshot is None or not snapshot.key_fingerprint:
        return
    if not db.query(EncryptionKey.id).filter_by(fingerprint=snapshot.key_fingerprint.lower()).first():
        raise EncryptionKeyMissingError(snapshot.key_fingerprint)


def fingerprints_without_key(db: Session, fingerprints: set[str] | list[str]) -> list[str]:
    wanted = {fp.lower() for fp in fingerprints if fp}
    if not wanted:
        return []
    stored = {row[0] for row in db.query(EncryptionKey.fingerprint).filter(EncryptionKey.fingerprint.in_(wanted))}
    return sorted(wanted - stored)


def adopt_datastore_keys(db: Session, settings: Settings) -> int:
    """Moves ADR 0022 per-datastore keys into the store, by their fingerprint,
    and detaches them from the datastore. Idempotent; a key already in the store
    is only detached. Returns how many datastores were handled."""
    handled = 0
    provider = _provider(db, settings)
    for datastore in db.query(Datastore).filter(Datastore.encryption_key_secret_ref_id.isnot(None)).all():
        secret_ref = db.get(SecretReference, datastore.encryption_key_secret_ref_id)
        content = provider.retrieve(secret_ref.reference_key)
        try:
            parsed = parse_keyfile(content)
        except KeyfileError:
            continue  # leave it where it is; the datastore page still shows it as configured
        if db.query(EncryptionKey).filter_by(fingerprint=parsed.fingerprint).one_or_none() is None:
            store_key(db, settings, content, label=f"From datastore {datastore.name}", source="datastore")
        datastore.encryption_key_secret_ref_id = None
        db.flush()
        provider.delete(secret_ref.reference_key)
        db.delete(secret_ref)
        db.flush()
        handled += 1
    return handled


# --- export / import ---------------------------------------------------------


def _kdf(salt: bytes, n: int, r: int, p: int) -> Scrypt:
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p)


def export_keys(entries: list[dict], passphrase: str) -> bytes:
    """`entries`: [{fingerprint, label, keyfile}]. Returns a JSON document with
    the entries encrypted under a key derived from `passphrase`."""
    if len(passphrase) < MIN_EXPORT_PASSPHRASE_LENGTH:
        raise KeyfileError(
            "passphrase_too_short", f"The passphrase needs at least {MIN_EXPORT_PASSPHRASE_LENGTH} characters."
        )
    salt, nonce = os.urandom(16), os.urandom(12)
    key = _kdf(salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P).derive(passphrase.encode("utf-8"))
    plaintext = json.dumps({"keys": entries}).encode("utf-8")
    header = {"format": EXPORT_FORMAT, "version": EXPORT_VERSION, "count": len(entries)}
    aad = json.dumps(header, sort_keys=True).encode("utf-8")
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad)
    document = {
        **header,
        "kdf": {
            "name": "scrypt",
            "n": _SCRYPT_N,
            "r": _SCRYPT_R,
            "p": _SCRYPT_P,
            "salt": base64.b64encode(salt).decode(),
        },
        "cipher": "aes-256-gcm",
        "nonce": base64.b64encode(nonce).decode(),
        "ciphertext": base64.b64encode(ciphertext).decode(),
    }
    return json.dumps(document, indent=2).encode("utf-8")


def read_export(content: bytes | str, passphrase: str) -> list[dict]:
    """Decrypts an `export_keys` document. Raises `KeyfileError` for a wrong
    passphrase or a damaged file (AES-GCM detects both)."""
    try:
        document = json.loads(content)
        if document.get("format") != EXPORT_FORMAT or document.get("version") != EXPORT_VERSION:
            raise KeyfileError("invalid_export", "This is not a RESURGAM encryption key export.")
        kdf = document["kdf"]
        if kdf.get("name") != "scrypt":
            raise KeyfileError("invalid_export", "Unsupported key derivation in this export.")
        header = {k: document[k] for k in ("format", "version", "count")}
        aad = json.dumps(header, sort_keys=True).encode("utf-8")
        key = _kdf(base64.b64decode(kdf["salt"]), int(kdf["n"]), int(kdf["r"]), int(kdf["p"])).derive(
            passphrase.encode("utf-8")
        )
        plaintext = AESGCM(key).decrypt(
            base64.b64decode(document["nonce"]), base64.b64decode(document["ciphertext"]), aad
        )
    except KeyfileError:
        raise
    except InvalidTag as exc:
        raise KeyfileError("wrong_passphrase", "Wrong passphrase, or the export file is damaged.") from exc
    except (ValueError, KeyError, TypeError) as exc:
        raise KeyfileError("invalid_export", "This is not a readable RESURGAM encryption key export.") from exc
    entries = json.loads(plaintext).get("keys", [])
    if not isinstance(entries, list):
        raise KeyfileError("invalid_export", "This export contains no key list.")
    return entries


def api_error_for_missing_key(exc: EncryptionKeyMissingError):
    """The API error every endpoint uses for a backup whose key is missing."""
    from resurgam_api.errors import ResurgamApiError

    return ResurgamApiError(
        code="encryption_key_missing",
        message=str(exc),
        status_code=409,
        details={"key_fingerprint": exc.fingerprint},
    )


def ensure_key_for_snapshot(db: Session, snapshot: BackupSnapshot | None) -> None:
    """`require_key_for_snapshot` as an API error (409 `encryption_key_missing`)."""
    try:
        require_key_for_snapshot(db, snapshot)
    except EncryptionKeyMissingError as exc:
        raise api_error_for_missing_key(exc) from exc
