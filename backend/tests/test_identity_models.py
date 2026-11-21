import pytest
from sqlalchemy.exc import IntegrityError

from resurgam_api.models.identity import Role, RoleKey, ScopeType, User, UserRoleAssignment
from resurgam_api.models.tenancy import Customer


def test_roles_are_seeded(db_session):
    keys = {role.key for role in db_session.query(Role).all()}
    assert keys == {role.value for role in RoleKey}


def test_duplicate_scoped_role_assignment_rejected(db_session):
    customer = Customer(name="Acme", slug="acme", debitor_number="10001", match_code="ACM")
    user = User(email="alice@example.com", display_name="Alice")
    db_session.add_all([customer, user])
    db_session.flush()

    role = db_session.query(Role).filter_by(key=RoleKey.RESTORE_OPERATOR.value).one()

    db_session.add(
        UserRoleAssignment(user_id=user.id, role_id=role.id, scope_type=ScopeType.CUSTOMER.value, scope_id=customer.id)
    )
    db_session.flush()

    db_session.add(
        UserRoleAssignment(user_id=user.id, role_id=role.id, scope_type=ScopeType.CUSTOMER.value, scope_id=customer.id)
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_duplicate_global_role_assignment_rejected(db_session):
    """Regression test for the NULL-scope_id gap: a plain 4-column unique
    constraint does not catch two global (scope_id IS NULL) assignments,
    since Postgres treats NULLs as distinct. The partial unique index added
    in the initial migration must catch this instead.
    """
    user = User(email="bob@example.com", display_name="Bob")
    db_session.add(user)
    db_session.flush()

    role = db_session.query(Role).filter_by(key=RoleKey.PLATFORM_ADMIN.value).one()

    db_session.add(
        UserRoleAssignment(user_id=user.id, role_id=role.id, scope_type=ScopeType.GLOBAL.value, scope_id=None)
    )
    db_session.flush()

    db_session.add(
        UserRoleAssignment(user_id=user.id, role_id=role.id, scope_type=ScopeType.GLOBAL.value, scope_id=None)
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
