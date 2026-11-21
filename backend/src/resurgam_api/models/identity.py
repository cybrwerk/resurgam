import uuid
from enum import Enum

from sqlalchemy import Boolean, ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from resurgam_api.db import Base
from resurgam_api.models.base import CreatedAtMixin, UUIDPrimaryKeyMixin


class RoleKey(str, Enum):
    """Roles from manifest §7."""

    VIEWER = "viewer"
    BACKUP_OPERATOR = "backup_operator"
    RESTORE_OPERATOR = "restore_operator"
    CUSTOMER_ADMIN = "customer_admin"
    AUDITOR = "auditor"
    PLATFORM_ADMIN = "platform_admin"


class ScopeType(str, Enum):
    """Scopes from manifest §7."""

    GLOBAL = "global"
    CUSTOMER = "customer"
    PBS_INSTANCE = "pbs_instance"
    DATASTORE = "datastore"
    PVE_CLUSTER = "pve_cluster"


class RoleAssignmentSource(str, Enum):
    """Where a role assignment came from. OIDC-sourced assignments are
    reconciled against the caller's current authentik groups on every
    login (granted/revoked); manual ones (e.g. break-glass, direct DB
    grants) are never touched by that reconciliation.
    """

    MANUAL = "manual"
    OIDC = "oidc"


class User(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "users"

    external_subject: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    is_breakglass: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Not from the manifest — a real local-user account (distinct from the
    # single break-glass credential, which stays env-configured, and from
    # OIDC users, who have no password at all). Null means "not a local
    # account" — only a user with this set can log in via
    # /auth/local-login, enroll MFA, or change their password.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Set by a platform_admin (Users & Roles -> "Require MFA"): a local
    # account with this set cannot get a session without a second factor.
    # Enforced at the next /auth/local-login -- a user with no TOTP/security
    # key gets only an enrollment token (`create_mfa_enrollment_token`) that
    # reaches the MFA enrollment endpoints, and the last factor cannot be
    # removed while it is set. Meaningless for break-glass and SSO users.
    mfa_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="false")


class Role(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "roles"

    key: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=False)


class UserRoleAssignment(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "user_role_assignments"
    __table_args__ = (
        # Covers customer/pbs_instance/datastore/pve_cluster scopes, where scope_id is set.
        UniqueConstraint("user_id", "role_id", "scope_type", "scope_id", name="uq_user_role_scope"),
        # Postgres treats NULL as distinct in a plain unique constraint, so the
        # constraint above does not catch duplicate *global* assignments
        # (scope_id IS NULL). A partial index closes that gap.
        Index(
            "uq_user_role_global_scope",
            "user_id",
            "role_id",
            unique=True,
            postgresql_where=text("scope_type = 'global'"),
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("roles.id", ondelete="CASCADE"), nullable=False
    )
    scope_type: Mapped[str] = mapped_column(String(50), nullable=False)
    scope_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default=RoleAssignmentSource.MANUAL.value)
