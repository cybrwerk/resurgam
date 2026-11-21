from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class CredentialOut(BaseModel):
    """A PBS/PVE API token's `SecretReference`, never the decrypted value
    itself. Deliberately scoped to infra credentials only -- `mfa.py` also
    stores per-user TOTP secrets via `SecretReference`, which must never
    appear here (see `credentials.py`'s own query, which resolves rows
    *from* `PbsInstance`/`PveCluster`, not by listing every
    `SecretReference` row generically).
    """

    secret_reference_id: UUID
    description: str
    owner_type: str  # "pbs_instance" | "pve_cluster"
    owner_id: UUID
    owner_label: str
    created_at: datetime
    last_rotated_at: datetime = Field(
        description="When this token's value was last set -- equal to created_at until the first rotation."
    )


class RotateCredentialRequest(BaseModel):
    new_value: str
