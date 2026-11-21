import uuid

from sqlalchemy.orm import Session

from resurgam_api.auth.models import AuthenticatedUser
from resurgam_api.errors import ResurgamApiError
from resurgam_api.models.datastore import DatastoreCustomerAssignment
from resurgam_api.models.identity import RoleKey, ScopeType
from resurgam_api.models.inventory import BackupGroup


def visible_customer_ids(user: AuthenticatedUser) -> set[uuid.UUID] | None:
    """Customer ids the caller may see customer-scoped data for, or None
    meaning "all" (platform_admin). Manifest §7: every query must enforce
    effective scope server-side. Shared by every endpoint that filters by
    customer scope (datastores, customers, ...) rather than each
    reimplementing the same three lines.
    """
    if user.has_role(RoleKey.PLATFORM_ADMIN):
        return None
    return {a.scope_id for a in user.assignments if a.scope_type == ScopeType.CUSTOMER and a.scope_id is not None}


def restore_visible_customer_ids(user: AuthenticatedUser) -> set[uuid.UUID]:
    """Customer ids the caller holds `restore_operator` for, or every
    customer id they can see at all if they're `platform_admin` (checked
    by the caller separately -- mirrors `visible_customer_ids`'s `None`-
    means-"all" shape being handled by its own callers). Unlike
    `visible_customer_ids` (any customer-scoped role), this is for list
    views that hand back real backup content (e.g. file/directory names
    from inside a snapshot) rather than just plan/job metadata -- the
    same `restore_operator`-or-`platform_admin` bar `require_restore_access`
    already enforces for a single job, applied across every job this
    caller could otherwise see.
    """
    return {
        a.scope_id
        for a in user.assignments
        if a.role == RoleKey.RESTORE_OPERATOR and a.scope_type == ScopeType.CUSTOMER and a.scope_id is not None
    }


def effective_group_customer_id(
    group: BackupGroup, datastore_assignment: DatastoreCustomerAssignment | None
) -> uuid.UUID | None:
    """Which customer a backup group actually belongs to: its own
    `customer_id` override if set (not from the manifest — added for
    billing, so one shared datastore can hold VMs for different
    customers), otherwise whatever its datastore is assigned to, or None
    if neither is set.
    """
    if group.customer_id is not None:
        return group.customer_id
    return datastore_assignment.customer_id if datastore_assignment else None


def require_restore_access_for_customer(user: AuthenticatedUser, customer_id: uuid.UUID | None) -> None:
    """Same `restore_operator`-or-`platform_admin` check as
    `require_restore_access`, for callers that already have a resolved
    `customer_id` in hand (e.g. Recovery Lab's `ApplicationGroup`, which
    carries its own `customer_id` directly, unlike a `BackupGroup`, whose
    effective customer needs resolving through its datastore assignment).
    """
    allowed = user.has_role(RoleKey.PLATFORM_ADMIN) or user.has_role(
        RoleKey.RESTORE_OPERATOR, scope_type=ScopeType.CUSTOMER, scope_id=customer_id
    )
    if not allowed:
        raise ResurgamApiError(code="not_found", message="Not found.", status_code=404)


def require_customer_admin_access_for_customer(user: AuthenticatedUser, customer_id: uuid.UUID) -> None:
    """`customer_admin`-or-`platform_admin`, for endpoints where a
    customer's own admin should be able to act on their own data without
    needing platform-wide access -- first user of the `customer_admin`
    role in this codebase (compliance self-attestation: a customer's own
    admin is the one who actually knows their production copy count and
    media mix, raised directly by the user). Same 404-not-403 posture as
    `require_restore_access_for_customer`.
    """
    allowed = user.has_role(RoleKey.PLATFORM_ADMIN) or user.has_role(
        RoleKey.CUSTOMER_ADMIN, scope_type=ScopeType.CUSTOMER, scope_id=customer_id
    )
    if not allowed:
        raise ResurgamApiError(code="not_found", message="Not found.", status_code=404)


def require_restore_access(db: Session, user: AuthenticatedUser, group: BackupGroup) -> None:
    """`restore_operator` (manifest §7) or `platform_admin`, scoped to the
    group's *effective* customer. Shared by file-level restore
    (`api/v1/filerestore.py`) and guided VM/CT restore plans
    (`api/v1/restore_plans.py`) — both need the exact same "can this
    caller act on this snapshot's data" check. 404, not 403: don't confirm
    existence of an out-of-scope backup group to a caller who can't see it.
    """
    assignment = db.query(DatastoreCustomerAssignment).filter_by(datastore_id=group.datastore_id).one_or_none()
    effective_customer = effective_group_customer_id(group, assignment)
    require_restore_access_for_customer(user, effective_customer)
