from pydantic import BaseModel, Field

from resurgam_api.models.identity import ScopeType


class BreakglassLoginRequest(BaseModel):
    username: str
    password: str


class BreakglassLoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class LocalLoginRequest(BaseModel):
    email: str
    password: str


class LocalLoginResponse(BaseModel):
    """Exactly one of three outcomes: a real session (`access_token`), a
    hand-off to the MFA step (`mfa_required=True`, `challenge_token`), or --
    when an administrator made MFA mandatory and the account has no second
    factor yet -- forced enrollment (`mfa_enrollment_required=True`,
    `enrollment_token`).
    """

    mfa_required: bool = Field(description="True: verify a second factor with `challenge_token` next.")
    access_token: str | None = Field(default=None, description="The session token, when no further step is needed.")
    token_type: str = "bearer"
    expires_in: int | None = Field(
        default=None, description="Lifetime in seconds of `access_token` (or of `enrollment_token`)."
    )
    challenge_token: str | None = Field(
        default=None, description="Short-lived token for the /auth/mfa/verify-* and WebAuthn authenticate calls."
    )
    available_methods: list[str] = Field(
        default=[], description="Second factors the account can use: `totp`, `webauthn`, `recovery_code`."
    )
    mfa_enrollment_required: bool = Field(
        default=False,
        description="True: an administrator requires MFA and the account has none yet. Use `enrollment_token` as "
        "the Bearer token for /mfa/totp/enroll + /mfa/totp/confirm or /mfa/webauthn/register-options + "
        "/mfa/webauthn/register (optionally /mfa/recovery-codes/generate), then exchange it for a session with "
        "POST /auth/mfa/enrollment/complete. It reaches no other endpoint.",
    )
    enrollment_token: str | None = Field(
        default=None, description="The enrollment token when `mfa_enrollment_required` is true."
    )


class MfaEnrollmentCompleteRequest(BaseModel):
    enrollment_token: str = Field(description="The `enrollment_token` from POST /auth/local-login.")


class VerifyTotpRequest(BaseModel):
    challenge_token: str
    code: str


class VerifyRecoveryCodeRequest(BaseModel):
    challenge_token: str
    code: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class RoleAssignmentOut(BaseModel):
    role: str
    scope_type: ScopeType
    scope_id: str | None = None


class WhoAmIResponse(BaseModel):
    user_id: str
    email: str
    display_name: str
    is_breakglass: bool
    api_token_id: str | None = Field(
        default=None,
        description=(
            "Set when this request was authenticated with a personal API token instead of a login session: "
            "the token's id. `assignments` then lists only the roles the token grants (and the user still holds)."
        ),
    )
    assignments: list[RoleAssignmentOut]
