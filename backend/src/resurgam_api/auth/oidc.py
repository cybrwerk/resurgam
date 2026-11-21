from dataclasses import dataclass
from typing import Literal

from authlib.integrations.starlette_client import OAuth
from sqlalchemy.orm import Session

from resurgam_api.config import Settings
from resurgam_api.models.identity import Role, RoleAssignmentSource, RoleKey, ScopeType, User, UserRoleAssignment
from resurgam_api.models.oidc_settings import OidcSettings
from resurgam_api.models.secrets import SecretReference
from resurgam_api.secrets.local_provider import LocalEncryptedSecretsProvider

_GROUP_PREFIX = "resurgam-"


class OidcProvisioningError(Exception):
    pass


@dataclass
class EffectiveOidcConfig:
    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str
    source: Literal["database", "environment"]


def resolve_effective_oidc_config(db: Session, settings: Settings) -> EffectiveOidcConfig:
    """Administration -> Integrations (manifest §6.3): a DB-backed
    `OidcSettings` row, when `enabled=True`, is authoritative over
    `Settings.oidc_*` (env/`.env`) -- deliberately not cached (matches
    ADR 0007's already-accepted "reconstruct on every login/callback,
    simplest correct thing for now" stance, not revisited here). No row,
    or a disabled row, falls back to env exactly as before this existed
    -- an existing deployment relying on `.env` is unaffected until an
    admin explicitly saves something through the new Integrations page.
    """
    row = db.query(OidcSettings).filter_by(enabled=True).one_or_none()
    if row is not None:
        client_secret = ""
        if row.client_secret_ref_id is not None:
            secret_ref = db.get(SecretReference, row.client_secret_ref_id)
            provider = LocalEncryptedSecretsProvider(db, settings.secrets_local_encryption_key)
            client_secret = provider.retrieve(secret_ref.reference_key)
        return EffectiveOidcConfig(
            issuer=row.issuer,
            client_id=row.client_id,
            client_secret=client_secret,
            redirect_uri=row.redirect_uri,
            source="database",
        )
    return EffectiveOidcConfig(
        issuer=settings.oidc_issuer,
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        redirect_uri=settings.oidc_redirect_uri,
        source="environment",
    )


def get_oauth_client(config: EffectiveOidcConfig) -> OAuth:
    """Authlib OAuth registration for authentik. `server_metadata_url` makes
    Authlib fetch the authorization/token/JWKS endpoints itself via OIDC
    discovery, and validate the ID token's issuer, audience, signature,
    expiry, and nonce per manifest §6.3 — no hand-rolled JWT validation.
    """
    oauth = OAuth()
    oauth.register(
        name="authentik",
        client_id=config.client_id,
        client_secret=config.client_secret,
        server_metadata_url=f"{config.issuer.rstrip('/')}/.well-known/openid-configuration",
        # code_challenge_method must be set explicitly — Authlib does not
        # enable PKCE by default, but manifest §6.3 requires it.
        client_kwargs={"scope": "openid email profile", "code_challenge_method": "S256"},
    )
    return oauth


def _role_keys_from_groups(groups: list[str]) -> set[RoleKey]:
    """Maps `resurgam-<role-key>` groups to RoleKey values. RoleKey values
    are underscore_separated ("platform_admin") but group names are
    naturally typed as kebab-case ("resurgam-platform-admin"), so hyphens
    are normalized to underscores before matching — confirmed necessary
    against a real authentik instance during manual testing, where a group
    named exactly that way otherwise silently matched nothing.
    """
    valid_keys = {role.value for role in RoleKey}
    role_keys = set()
    for group in groups:
        if not group.startswith(_GROUP_PREFIX):
            continue
        candidate = group[len(_GROUP_PREFIX) :].replace("-", "_")
        if candidate in valid_keys:
            role_keys.add(RoleKey(candidate))
    return role_keys


def _find_or_create_user(db: Session, claims: dict) -> User:
    subject = claims.get("sub")
    email = claims.get("email")
    if not subject:
        raise OidcProvisioningError("ID token is missing a 'sub' claim")
    if not email:
        raise OidcProvisioningError("ID token/userinfo is missing an 'email' claim")

    display_name = claims.get("name") or claims.get("preferred_username") or email

    user = db.query(User).filter_by(external_subject=subject).one_or_none()
    if user is None:
        user = db.query(User).filter_by(email=email).one_or_none()
        if user is not None:
            user.external_subject = subject  # adopt a pre-existing row by email

    if user is None:
        user = User(external_subject=subject, email=email, display_name=display_name)
        db.add(user)
        db.flush()

    return user


def _reconcile_oidc_roles(db: Session, user: User, groups: list[str]) -> None:
    """Grant global roles for `resurgam-<role-key>` groups present now;
    revoke previously OIDC-granted global roles whose group is no longer
    present. Manual-source assignments (e.g. break-glass) are never read or
    touched here.
    """
    desired = _role_keys_from_groups(groups)
    roles_by_key = {role.key: role for role in db.query(Role).all()}

    existing = (
        db.query(UserRoleAssignment)
        .filter_by(user_id=user.id, scope_type=ScopeType.GLOBAL.value, source=RoleAssignmentSource.OIDC.value)
        .all()
    )
    id_to_role = {role.id: role for role in roles_by_key.values()}
    existing_by_role_key = {RoleKey(id_to_role[a.role_id].key): a for a in existing if a.role_id in id_to_role}

    for role_key, assignment in existing_by_role_key.items():
        if role_key not in desired:
            db.delete(assignment)

    for role_key in desired:
        if role_key in existing_by_role_key:
            continue
        role = roles_by_key.get(role_key.value)
        if role is None:
            continue
        db.add(
            UserRoleAssignment(
                user_id=user.id,
                role_id=role.id,
                scope_type=ScopeType.GLOBAL.value,
                source=RoleAssignmentSource.OIDC.value,
            )
        )

    db.flush()


def provision_user_from_claims(db: Session, claims: dict) -> User:
    """Find-or-create the User for these validated OIDC claims and
    reconcile their OIDC-derived role assignments against current groups.
    Decoupled from the Authlib token exchange so it's unit-testable without
    mocking OAuth internals.
    """
    user = _find_or_create_user(db, claims)
    groups = claims.get("groups") or []
    _reconcile_oidc_roles(db, user, groups)
    return user
