import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from resurgam_api.auth.dependency import get_current_user
from resurgam_api.auth.models import AuthenticatedUser
from resurgam_api.auth.scope import require_restore_access_for_customer, visible_customer_ids
from resurgam_api.db import get_db
from resurgam_api.errors import ResurgamApiError
from resurgam_api.models.application_group import ApplicationGroup, ApplicationGroupMember
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.inventory import BackupGroup
from resurgam_api.models.restore_test import RestoreTestPlan, RestoreTestPlanMember
from resurgam_api.models.tenancy import Customer
from resurgam_api.schemas.application_group import (
    ApplicationGroupCreateRequest,
    ApplicationGroupMemberOut,
    ApplicationGroupOut,
    ApplicationGroupUpdateRequest,
)

router = APIRouter(prefix="/application-groups", tags=["application-groups"])


def _compute_next_scheduled_run_at(schedule_enabled: bool, interval_hours: int | None) -> datetime | None:
    """Mirrors `file_index.py`'s own `_compute_next_scheduled_run_at`: the
    first automatic run waits out one full interval, same as every
    subsequent one -- not fired immediately on enabling the schedule.
    """
    if not schedule_enabled or interval_hours is None:
        return None
    return datetime.now(UTC) + timedelta(hours=interval_hours)


def _record_audit(
    db: Session, request: Request, *, actor_user_id, action: str, target_id, customer_id=None, detail=None
) -> None:
    db.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            action=action,
            target_type="application_group",
            target_id=str(target_id),
            customer_id=customer_id,
            correlation_id=getattr(request.state, "correlation_id", None),
            detail=detail,
        )
    )


def _out(db: Session, group: ApplicationGroup) -> ApplicationGroupOut:
    members = (
        db.query(ApplicationGroupMember)
        .filter_by(application_group_id=group.id)
        .order_by(ApplicationGroupMember.startup_order)
        .all()
    )
    # A member's `backup_group_superseded` flag depends on the *referenced*
    # BackupGroup's own state, not the member row itself -- batch-loaded
    # here rather than one query per member.
    superseded_by_group_id = dict(
        db.query(BackupGroup.id, BackupGroup.superseded_at)
        .filter(BackupGroup.id.in_({m.backup_group_id for m in members}))
        .all()
    )
    return ApplicationGroupOut(
        id=group.id,
        customer_id=group.customer_id,
        name=group.name,
        created_at=group.created_at,
        members=[
            ApplicationGroupMemberOut(
                id=m.id,
                backup_group_id=m.backup_group_id,
                startup_order=m.startup_order,
                check_types=m.check_types,
                expected_ip=m.expected_ip,
                backup_group_superseded=superseded_by_group_id.get(m.backup_group_id) is not None,
            )
            for m in members
        ],
        lab_network_mode=group.lab_network_mode,
        lab_subnet_cidr=group.lab_subnet_cidr,
        lab_gateway_ip=group.lab_gateway_ip,
        lab_dns_ip=group.lab_dns_ip,
        lab_dhcp_range_start=group.lab_dhcp_range_start,
        lab_dhcp_range_end=group.lab_dhcp_range_end,
        lab_schedule_enabled=group.lab_schedule_enabled,
        lab_schedule_interval_hours=group.lab_schedule_interval_hours,
        lab_next_scheduled_run_at=group.lab_next_scheduled_run_at,
        lab_schedule_pve_cluster_id=group.lab_schedule_pve_cluster_id,
        lab_schedule_target_node=group.lab_schedule_target_node,
        lab_schedule_target_storage=group.lab_schedule_target_storage,
    )


def _get_group_or_404(db: Session, group_id: uuid.UUID, user: AuthenticatedUser) -> ApplicationGroup:
    group = db.get(ApplicationGroup, group_id)
    if group is None:
        raise ResurgamApiError(code="not_found", message="Application group not found.", status_code=404)
    visible = visible_customer_ids(user)
    if visible is not None and group.customer_id not in visible:
        raise ResurgamApiError(code="not_found", message="Application group not found.", status_code=404)
    return group


def _replace_members(db: Session, group: ApplicationGroup, members_in) -> None:
    """Reconciles the group's members against `members_in` by `backup_group_id`
    identity, rather than the delete-everything-then-recreate this
    originally did. That naive approach crashed with an unhandled
    `IntegrityError` (surfaced to the browser as an opaque CORS failure,
    since Starlette's CORSMiddleware never gets a chance to add headers to
    a response that dies mid-request) on the very first edit to any group
    that had ever run in Recovery Lab -- `RestoreTestPlanMember.
    application_group_member_id` is deliberately `RESTRICT` (same
    reasoning `delete_application_group` below already applies at the
    whole-group level), so deleting a member row a real Lab run still
    references is refused at the DB level, not silently allowed.

    A member kept across the edit (same `backup_group_id`) is updated in
    place, preserving its `id` and therefore any `RestoreTestPlanMember`
    history pointing at it. A member actually being removed still can't
    carry run history -- surfaced as a clear 409 here, matching
    `delete_application_group`'s own precedent, instead of a raw 500.
    """
    existing = db.query(ApplicationGroupMember).filter_by(application_group_id=group.id).all()
    existing_by_backup_group = {m.backup_group_id: m for m in existing}
    incoming_backup_group_ids = {member.backup_group_id for member in members_in}

    for existing_member in existing:
        if existing_member.backup_group_id in incoming_backup_group_ids:
            continue
        has_run_history = (
            db.query(RestoreTestPlanMember).filter_by(application_group_member_id=existing_member.id).first()
            is not None
        )
        if has_run_history:
            raise ResurgamApiError(
                code="application_group_member_has_runs",
                message="A member with Recovery Lab run history can't be removed from this group.",
                status_code=409,
            )
        db.delete(existing_member)

    for member in members_in:
        if db.get(BackupGroup, member.backup_group_id) is None:
            raise ResurgamApiError(code="backup_group_not_found", message="Backup group not found.", status_code=400)
        existing_member = existing_by_backup_group.get(member.backup_group_id)
        if existing_member is not None:
            existing_member.startup_order = member.startup_order
            existing_member.check_types = member.check_types
            existing_member.expected_ip = member.expected_ip
        else:
            db.add(
                ApplicationGroupMember(
                    application_group_id=group.id,
                    backup_group_id=member.backup_group_id,
                    startup_order=member.startup_order,
                    check_types=member.check_types,
                    expected_ip=member.expected_ip,
                )
            )


@router.post("", response_model=ApplicationGroupOut)
def create_application_group(
    body: ApplicationGroupCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> ApplicationGroupOut:
    require_restore_access_for_customer(user, body.customer_id)
    if db.get(Customer, body.customer_id) is None:
        raise ResurgamApiError(code="customer_not_found", message="Customer not found.", status_code=400)

    group = ApplicationGroup(
        customer_id=body.customer_id,
        name=body.name,
        created_by_user_id=user.user_id,
        lab_network_mode=body.lab_network_mode,
        lab_subnet_cidr=body.lab_subnet_cidr,
        lab_gateway_ip=body.lab_gateway_ip,
        lab_dns_ip=body.lab_dns_ip,
        lab_dhcp_range_start=body.lab_dhcp_range_start,
        lab_dhcp_range_end=body.lab_dhcp_range_end,
        lab_schedule_enabled=body.lab_schedule_enabled,
        lab_schedule_interval_hours=body.lab_schedule_interval_hours,
        lab_next_scheduled_run_at=_compute_next_scheduled_run_at(
            body.lab_schedule_enabled, body.lab_schedule_interval_hours
        ),
        lab_schedule_pve_cluster_id=body.lab_schedule_pve_cluster_id,
        lab_schedule_target_node=body.lab_schedule_target_node,
        lab_schedule_target_storage=body.lab_schedule_target_storage,
    )
    db.add(group)
    db.flush()
    _replace_members(db, group, body.members)

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="application_group_created",
        target_id=group.id,
        customer_id=group.customer_id,
        detail={"name": group.name, "member_count": len(body.members)},
    )
    db.commit()
    db.refresh(group)
    return _out(db, group)


@router.get("", response_model=list[ApplicationGroupOut])
def list_application_groups(
    db: Session = Depends(get_db), user: AuthenticatedUser = Depends(get_current_user)
) -> list[ApplicationGroupOut]:
    visible = visible_customer_ids(user)
    query = db.query(ApplicationGroup).order_by(ApplicationGroup.name)
    if visible is not None:
        query = query.filter(ApplicationGroup.customer_id.in_(visible))
    return [_out(db, g) for g in query.all()]


@router.get("/{group_id}", response_model=ApplicationGroupOut)
def get_application_group(
    group_id: uuid.UUID, db: Session = Depends(get_db), user: AuthenticatedUser = Depends(get_current_user)
) -> ApplicationGroupOut:
    return _out(db, _get_group_or_404(db, group_id, user))


@router.patch("/{group_id}", response_model=ApplicationGroupOut)
def update_application_group(
    group_id: uuid.UUID,
    body: ApplicationGroupUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> ApplicationGroupOut:
    group = _get_group_or_404(db, group_id, user)
    require_restore_access_for_customer(user, group.customer_id)

    if body.name is not None:
        group.name = body.name
    if body.members is not None:
        _replace_members(db, group, body.members)
    if body.lab_network_mode is not None:
        group.lab_network_mode = body.lab_network_mode
    if body.lab_subnet_cidr is not None:
        group.lab_subnet_cidr = body.lab_subnet_cidr
    if body.lab_gateway_ip is not None:
        group.lab_gateway_ip = body.lab_gateway_ip
    if body.lab_dns_ip is not None:
        group.lab_dns_ip = body.lab_dns_ip
    if body.lab_dhcp_range_start is not None:
        group.lab_dhcp_range_start = body.lab_dhcp_range_start
    if body.lab_dhcp_range_end is not None:
        group.lab_dhcp_range_end = body.lab_dhcp_range_end

    schedule_fields_touched = body.model_dump(
        include={"lab_schedule_enabled", "lab_schedule_interval_hours"}, exclude_unset=True
    )
    if body.lab_schedule_enabled is not None:
        group.lab_schedule_enabled = body.lab_schedule_enabled
    if body.lab_schedule_interval_hours is not None:
        group.lab_schedule_interval_hours = body.lab_schedule_interval_hours
    if body.lab_schedule_pve_cluster_id is not None:
        group.lab_schedule_pve_cluster_id = body.lab_schedule_pve_cluster_id
    if body.lab_schedule_target_node is not None:
        group.lab_schedule_target_node = body.lab_schedule_target_node
    if body.lab_schedule_target_storage is not None:
        group.lab_schedule_target_storage = body.lab_schedule_target_storage

    # Validated against the group's *effective* post-merge state, not the
    # raw (partial) body -- a PATCH need not resend fields set earlier.
    # Mirrors `ApplicationGroupCreateRequest`'s own `_validate_lab_schedule`.
    if group.lab_schedule_enabled and (
        group.lab_schedule_interval_hours is None
        or group.lab_schedule_pve_cluster_id is None
        or not group.lab_schedule_target_node
        or not group.lab_schedule_target_storage
    ):
        raise ResurgamApiError(
            code="invalid_lab_schedule",
            message="lab_schedule_interval_hours, lab_schedule_pve_cluster_id, lab_schedule_target_node and "
            "lab_schedule_target_storage must all be set before enabling the schedule.",
            status_code=400,
        )
    if schedule_fields_touched:
        if group.lab_schedule_enabled and group.lab_next_scheduled_run_at is None:
            group.lab_next_scheduled_run_at = _compute_next_scheduled_run_at(
                group.lab_schedule_enabled, group.lab_schedule_interval_hours
            )
        elif not group.lab_schedule_enabled:
            group.lab_next_scheduled_run_at = None

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="application_group_updated",
        target_id=group.id,
        customer_id=group.customer_id,
    )
    db.commit()
    db.refresh(group)
    return _out(db, group)


@router.delete("/{group_id}", status_code=204)
def delete_application_group(
    group_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> None:
    group = _get_group_or_404(db, group_id, user)
    require_restore_access_for_customer(user, group.customer_id)

    if db.query(RestoreTestPlan).filter_by(application_group_id=group.id).first() is not None:
        raise ResurgamApiError(
            code="application_group_has_runs",
            message="This application group has Recovery Lab runs recorded against it and cannot be deleted.",
            status_code=409,
        )

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="application_group_deleted",
        target_id=group.id,
        customer_id=group.customer_id,
    )
    db.delete(group)
    db.commit()
