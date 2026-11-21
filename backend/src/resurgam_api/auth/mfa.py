import secrets
import string

import bcrypt
import pyotp

_RECOVERY_CODE_ALPHABET = string.ascii_uppercase + string.digits
_RECOVERY_CODE_LENGTH = 10
_RECOVERY_CODE_COUNT = 10


def generate_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, *, account_email: str, issuer: str = "RESURGAM") -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=account_email, issuer_name=issuer)


def verify_totp_code(secret: str, code: str) -> bool:
    # valid_window=1 tolerates one 30s step of clock drift either side —
    # the standard tradeoff between usability and not widening the replay
    # window too far.
    return pyotp.TOTP(secret).verify(code, valid_window=1)


def generate_recovery_codes(count: int = _RECOVERY_CODE_COUNT) -> list[str]:
    """Plaintext codes for one-time display — callers must hash each with
    `hash_recovery_code` before storing and never persist the plaintext.
    """
    return [
        "".join(secrets.choice(_RECOVERY_CODE_ALPHABET) for _ in range(_RECOVERY_CODE_LENGTH)) for _ in range(count)
    ]


def hash_recovery_code(code: str) -> str:
    return bcrypt.hashpw(code.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_recovery_code(code: str, code_hash: str) -> bool:
    return bcrypt.checkpw(code.encode("utf-8"), code_hash.encode("utf-8"))
