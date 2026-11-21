import base64

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# Bumping this would re-derive every subkey differently -- never change it
# without a corresponding re-encryption migration (see 0047's own).
_HKDF_INFO = b"resurgam-secret-purpose-subkey-v1"


def derive_purpose_key(master_key: str, purpose: str) -> bytes:
    """HKDF-SHA256-derives a Fernet-compatible (32-byte, urlsafe-base64)
    subkey from the one master `SECRETS_LOCAL_ENCRYPTION_KEY`, salted by
    `purpose` (a `SecretPurpose` value). Deterministic -- the same
    `(master_key, purpose)` pair always derives the same subkey, so this
    needs no storage of its own, and both `LocalEncryptedSecretsProvider`
    and the one-time re-encryption migration (0047) call this exact
    function rather than each re-implementing the derivation.

    This isolates *ciphertext*, not the master key itself: `purpose`
    values are public (literal strings in this module's own source), so
    anyone holding the master key can already derive every subkey. What
    it actually buys: a confused-deputy bug (code that decrypts a
    `SecretReference` under the wrong assumed purpose) fails loudly --
    Fernet's `InvalidToken` -- instead of silently succeeding against the
    wrong secret class.
    """
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=purpose.encode("utf-8"), info=_HKDF_INFO)
    raw = hkdf.derive(master_key.encode("utf-8"))
    return base64.urlsafe_b64encode(raw)
