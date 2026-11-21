from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from resurgam_api.models.secrets import LocalSecretValue, SecretReference
from resurgam_api.secrets.key_derivation import derive_purpose_key
from resurgam_api.secrets.provider import SecretNotFoundError


class LocalEncryptedSecretsProvider:
    """Interim SecretsProvider implementation (docs/adr/0005): Fernet
    symmetric encryption with a server-held key, stored in
    `local_secret_values`. Not a substitute for a real secrets provider
    (OpenBao) — the key lives in the same server config as the database
    credentials, so this only protects against someone reading the database
    directly, not someone with full server access.

    Each call encrypts/decrypts under a subkey derived from the one
    master key plus the target `SecretReference.purpose` (looked up here,
    not caller-supplied — see `SecretReference.purpose`'s own docstring
    for why: a call site can't get the purpose wrong if it never states
    one). `store`/`retrieve`/`delete`'s own signatures are unchanged, so
    none of this provider's ~20 call sites across the codebase needed to
    change.
    """

    def __init__(self, db: Session, encryption_key: str) -> None:
        if not encryption_key:
            raise RuntimeError("SECRETS_LOCAL_ENCRYPTION_KEY is not configured")
        self._db = db
        self._master_key = encryption_key
        self._fernet_cache: dict[str, Fernet] = {}

    def _fernet_for_purpose(self, purpose: str) -> Fernet:
        fernet = self._fernet_cache.get(purpose)
        if fernet is None:
            fernet = Fernet(derive_purpose_key(self._master_key, purpose))
            self._fernet_cache[purpose] = fernet
        return fernet

    def _fernet_for_reference_key(self, reference_key: str) -> Fernet:
        ref = self._db.query(SecretReference).filter_by(reference_key=reference_key).one_or_none()
        if ref is None:
            raise SecretNotFoundError(reference_key)
        return self._fernet_for_purpose(ref.purpose)

    def store(self, reference_key: str, plaintext: str) -> None:
        ciphertext = self._fernet_for_reference_key(reference_key).encrypt(plaintext.encode("utf-8"))
        existing = self._db.get(LocalSecretValue, reference_key)
        if existing is not None:
            existing.ciphertext = ciphertext
        else:
            self._db.add(LocalSecretValue(reference_key=reference_key, ciphertext=ciphertext))
        self._db.flush()

    def retrieve(self, reference_key: str) -> str:
        row = self._db.get(LocalSecretValue, reference_key)
        if row is None:
            raise SecretNotFoundError(reference_key)
        fernet = self._fernet_for_reference_key(reference_key)
        try:
            return fernet.decrypt(row.ciphertext).decode("utf-8")
        except InvalidToken as exc:
            raise SecretNotFoundError(reference_key) from exc

    def delete(self, reference_key: str) -> None:
        row = self._db.get(LocalSecretValue, reference_key)
        if row is not None:
            self._db.delete(row)
            self._db.flush()
