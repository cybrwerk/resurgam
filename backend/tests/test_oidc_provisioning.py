import pytest

from resurgam_api.auth.oidc import OidcProvisioningError, provision_user_from_claims
from resurgam_api.models.identity import (
    Role,
    RoleAssignmentSource,
    RoleKey,
    ScopeType,
    User,
    UserRoleAssignment,
)


def _assignments_for(db_session, user_id):
    return db_session.query(UserRoleAssignment).filter_by(user_id=user_id).all()


def test_creates_new_user_from_claims(db_session):
    claims = {"sub": "auth|abc123", "email": "alice@example.com", "name": "Alice Example"}

    user = provision_user_from_claims(db_session, claims)

    assert user.external_subject == "auth|abc123"
    assert user.email == "alice@example.com"
    assert user.display_name == "Alice Example"
    assert user.is_breakglass is False


def test_second_login_matches_by_external_subject(db_session):
    claims = {"sub": "auth|abc123", "email": "alice@example.com", "name": "Alice"}
    first = provision_user_from_claims(db_session, claims)

    second = provision_user_from_claims(db_session, {**claims, "name": "Alice Updated"})

    assert second.id == first.id
    assert db_session.query(User).count() == 1


def test_adopts_existing_user_by_email_when_subject_unset(db_session):
    existing = User(email="bob@example.com", display_name="Bob (manual)")
    db_session.add(existing)
    db_session.flush()

    user = provision_user_from_claims(db_session, {"sub": "auth|bob-sub", "email": "bob@example.com"})

    assert user.id == existing.id
    assert user.external_subject == "auth|bob-sub"
    assert db_session.query(User).count() == 1


def test_missing_sub_raises(db_session):
    with pytest.raises(OidcProvisioningError):
        provision_user_from_claims(db_session, {"email": "no-sub@example.com"})


def test_missing_email_raises(db_session):
    with pytest.raises(OidcProvisioningError):
        provision_user_from_claims(db_session, {"sub": "auth|no-email"})


def test_grants_role_for_hyphenated_multiword_group_name(db_session):
    """Regression test: RoleKey values are underscore_separated
    (platform_admin) but a real authentik group is naturally typed
    kebab-case (resurgam-platform-admin) — confirmed against a live
    instance to silently match nothing before hyphens were normalized.
    """
    user = provision_user_from_claims(
        db_session,
        {"sub": "auth|admin1", "email": "admin@example.com", "groups": ["resurgam-platform-admin"]},
    )

    assignments = _assignments_for(db_session, user.id)
    assert len(assignments) == 1
    role = db_session.get(Role, assignments[0].role_id)
    assert role.key == RoleKey.PLATFORM_ADMIN.value


def test_grants_global_role_for_matching_group(db_session):
    user = provision_user_from_claims(
        db_session, {"sub": "auth|viewer1", "email": "viewer@example.com", "groups": ["resurgam-viewer"]}
    )

    assignments = _assignments_for(db_session, user.id)
    assert len(assignments) == 1
    role = db_session.get(Role, assignments[0].role_id)
    assert role.key == RoleKey.VIEWER.value
    assert assignments[0].scope_type == ScopeType.GLOBAL.value
    assert assignments[0].source == RoleAssignmentSource.OIDC.value


def test_ignores_unrelated_groups(db_session):
    user = provision_user_from_claims(
        db_session,
        {"sub": "auth|x", "email": "x@example.com", "groups": ["some-other-app-group", "resurgam-viewer"]},
    )

    assignments = _assignments_for(db_session, user.id)
    assert len(assignments) == 1


def test_revokes_role_when_group_removed_on_later_login(db_session):
    claims = {"sub": "auth|viewer2", "email": "viewer2@example.com", "groups": ["resurgam-viewer"]}
    user = provision_user_from_claims(db_session, claims)
    assert len(_assignments_for(db_session, user.id)) == 1

    provision_user_from_claims(db_session, {**claims, "groups": []})

    assert _assignments_for(db_session, user.id) == []


def test_grants_multiple_roles_and_updates_on_change(db_session):
    claims = {
        "sub": "auth|multi",
        "email": "multi@example.com",
        "groups": ["resurgam-viewer", "resurgam-backup_operator"],
    }
    user = provision_user_from_claims(db_session, claims)
    role_keys = {RoleKey(db_session.get(Role, a.role_id).key) for a in _assignments_for(db_session, user.id)}
    assert role_keys == {RoleKey.VIEWER, RoleKey.BACKUP_OPERATOR}

    provision_user_from_claims(db_session, {**claims, "groups": ["resurgam-platform_admin"]})
    role_keys_after = {RoleKey(db_session.get(Role, a.role_id).key) for a in _assignments_for(db_session, user.id)}
    assert role_keys_after == {RoleKey.PLATFORM_ADMIN}


def test_manual_assignment_never_touched_by_reconciliation(db_session):
    claims = {"sub": "auth|manual-test", "email": "manual@example.com", "groups": []}
    user = provision_user_from_claims(db_session, claims)

    role = db_session.query(Role).filter_by(key=RoleKey.AUDITOR.value).one()
    manual_assignment = UserRoleAssignment(
        user_id=user.id,
        role_id=role.id,
        scope_type=ScopeType.GLOBAL.value,
        source=RoleAssignmentSource.MANUAL.value,
    )
    db_session.add(manual_assignment)
    db_session.commit()

    # log in again with no resurgam-* groups at all
    provision_user_from_claims(db_session, claims)

    remaining = _assignments_for(db_session, user.id)
    assert len(remaining) == 1
    assert remaining[0].source == RoleAssignmentSource.MANUAL.value
    assert remaining[0].role_id == role.id
