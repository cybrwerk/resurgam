from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class TotpEnrollResponse(BaseModel):
    secret: str
    otpauth_uri: str


class TotpConfirmRequest(BaseModel):
    code: str


class WebauthnCredentialOut(BaseModel):
    id: UUID
    nickname: str
    created_at: datetime


class MfaStatusOut(BaseModel):
    totp_enabled: bool
    webauthn_credentials: list[WebauthnCredentialOut]
    recovery_codes_remaining: int
    mfa_required: bool = Field(
        default=False,
        description="True when an administrator requires MFA for this account: the last authenticator app or "
        "security key cannot be removed, and a login without one goes through forced enrollment.",
    )


class RecoveryCodesOut(BaseModel):
    codes: list[str]


class WebauthnRegisterOptionsRequest(BaseModel):
    nickname: str


class WebauthnCeremonyOptionsOut(BaseModel):
    options: dict[str, Any]
    ceremony_token: str


class WebauthnRegisterRequest(BaseModel):
    ceremony_token: str
    credential: dict[str, Any]


class WebauthnAuthenticateOptionsRequest(BaseModel):
    challenge_token: str


class WebauthnAuthenticateRequest(BaseModel):
    ceremony_token: str
    credential: dict[str, Any]
