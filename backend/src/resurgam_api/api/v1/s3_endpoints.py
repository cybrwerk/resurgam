"""PBS S3 endpoints (ADR 0033): the object stores S3-backed datastores live on,
and RESURGAM's audited mark of which of them stand on site."""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from resurgam_api.auth.dependency import get_current_user, require_role
from resurgam_api.auth.models import AuthenticatedUser
from resurgam_api.db import get_db
from resurgam_api.errors import ResurgamApiError
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.datastore import Datastore, PbsS3Endpoint
from resurgam_api.models.identity import RoleKey, User
from resurgam_api.models.pbs import PbsInstance
from resurgam_api.schemas.s3_endpoints import S3EndpointLocationRequest, S3EndpointOut

router = APIRouter(prefix="/s3-endpoints", tags=["s3-endpoints"])

_require_platform_admin = require_role(RoleKey.PLATFORM_ADMIN)


def _to_out(db: Session, row: PbsS3Endpoint) -> S3EndpointOut:
    instance = db.get(PbsInstance, row.pbs_instance_id)
    author = db.get(User, row.on_site_changed_by_user_id) if row.on_site_changed_by_user_id else None
    datastores = (
        db.query(Datastore.name)
        .filter_by(pbs_instance_id=row.pbs_instance_id, backend_type="s3", s3_endpoint_name=row.name)
        .filter(Datastore.removed_from_pbs_at.is_(None))
        .order_by(Datastore.name)
        .all()
    )
    return S3EndpointOut(
        id=row.id,
        pbs_instance_id=row.pbs_instance_id,
        pbs_instance_name=instance.display_name if instance else "?",
        name=row.name,
        endpoint=row.endpoint,
        port=row.port,
        region=row.region,
        path_style=row.path_style,
        datastores=[d[0] for d in datastores],
        on_site=row.on_site,
        on_site_reason=row.on_site_reason,
        on_site_changed_at=row.on_site_changed_at,
        on_site_changed_by=author.display_name if author else None,
        removed_from_pbs_at=row.removed_from_pbs_at,
    )


@router.get("", response_model=list[S3EndpointOut])
def list_s3_endpoints(
    db: Session = Depends(get_db), user: AuthenticatedUser = Depends(get_current_user)
) -> list[S3EndpointOut]:
    """Every S3 endpoint of every connected PBS instance, as the Collector last
    read it (only instances with at least one S3-backed datastore are asked).
    Platform admins and global auditors only -- the endpoints are shared
    infrastructure, not one customer's."""
    if not (user.has_role(RoleKey.PLATFORM_ADMIN) or user.has_role(RoleKey.AUDITOR)):
        raise ResurgamApiError(
            code="forbidden", message="Only platform admins and auditors can list S3 endpoints.", status_code=403
        )
    rows = db.query(PbsS3Endpoint).order_by(PbsS3Endpoint.name).all()
    return [_to_out(db, row) for row in rows]


@router.put("/{endpoint_id}/location", response_model=S3EndpointOut)
def set_s3_endpoint_location(
    endpoint_id: uuid.UUID,
    body: S3EndpointLocationRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(_require_platform_admin),
) -> S3EndpointOut:
    """Mark an S3 endpoint on site (its datastores stop counting as offsite for
    3-2-1) or offsite again. A reason is required and recorded, with who and
    when, on the endpoint and in the audit log (`s3_endpoint_location_changed`).
    Takes effect on the next read of the 3-2-1 Gaps page, alarms and reports.
    Platform admins only."""
    row = db.get(PbsS3Endpoint, endpoint_id)
    if row is None:
        raise ResurgamApiError(code="not_found", message="S3 endpoint not found.", status_code=404)
    previous = row.on_site
    row.on_site = body.on_site
    row.on_site_reason = body.reason
    row.on_site_changed_at = datetime.now(UTC)
    row.on_site_changed_by_user_id = user.user_id
    db.add(
        AuditEvent(
            actor_user_id=user.user_id,
            action="s3_endpoint_location_changed",
            target_type="s3_endpoint",
            target_id=str(row.id),
            correlation_id=getattr(request.state, "correlation_id", None),
            detail={
                "pbs_instance_id": str(row.pbs_instance_id),
                "name": row.name,
                "endpoint": row.endpoint,
                "on_site_before": previous,
                "on_site": body.on_site,
                "reason": body.reason,
            },
        )
    )
    db.commit()
    db.refresh(row)
    return _to_out(db, row)
