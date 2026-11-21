import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from resurgam_api.api.v1.customers import get_customer_in_scope_or_404
from resurgam_api.api.v1.datastores import get_datastore_in_scope_or_404
from resurgam_api.auth.dependency import get_current_user, require_role
from resurgam_api.auth.models import AuthenticatedUser
from resurgam_api.auth.scope import require_customer_admin_access_for_customer
from resurgam_api.db import get_db
from resurgam_api.errors import ResurgamApiError
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.compliance_attestation import ComplianceAttestation
from resurgam_api.models.identity import RoleKey, User
from resurgam_api.schemas.compliance_attestation import ComplianceAttestationOut, CreateComplianceAttestationRequest

router = APIRouter(tags=["compliance-attestations"])

_require_platform_admin = require_role(RoleKey.PLATFORM_ADMIN)

_DATASTORE_DIMENSIONS: dict[str, type] = {"immutable_copy": bool}
_CUSTOMER_DIMENSIONS: dict[str, type] = {"production_copy_count": int, "media_type_diversity": list}


def _validate_dimension(dimension: str, value, allowed: dict[str, type]) -> None:
    expected_type = allowed.get(dimension)
    if expected_type is None:
        raise ResurgamApiError(
            code="invalid_dimension",
            message=f"Unknown dimension {dimension!r}. Expected one of: {', '.join(sorted(allowed))}.",
            status_code=400,
        )
    # bool is a subclass of int in Python -- an explicit isinstance(x, int)
    # check would silently accept a bool for an int-typed dimension.
    if expected_type is bool and not isinstance(value, bool):
        raise ResurgamApiError(
            code="invalid_value", message=f"{dimension!r} requires a boolean value.", status_code=400
        )
    if expected_type is int and (isinstance(value, bool) or not isinstance(value, int)):
        raise ResurgamApiError(
            code="invalid_value", message=f"{dimension!r} requires an integer value.", status_code=400
        )
    if expected_type is list and not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
        raise ResurgamApiError(
            code="invalid_value", message=f"{dimension!r} requires a list of strings.", status_code=400
        )


def _record_audit(
    db: Session, request: Request, *, actor_user_id, scope_type: str, scope_id: uuid.UUID, dimension: str
) -> None:
    db.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            action="compliance_attestation_recorded",
            target_type=f"{scope_type}_compliance_attestation",
            target_id=str(scope_id),
            correlation_id=getattr(request.state, "correlation_id", None),
            detail={"dimension": dimension},
        )
    )


def _to_out(db: Session, attestation: ComplianceAttestation) -> ComplianceAttestationOut:
    user = db.get(User, attestation.asserted_by_user_id)
    return ComplianceAttestationOut(
        id=attestation.id,
        scope_type=attestation.scope_type,
        scope_id=attestation.scope_id,
        dimension=attestation.dimension,
        value=attestation.value,
        notes=attestation.notes,
        asserted_by_user_id=attestation.asserted_by_user_id,
        asserted_by_display_name=user.display_name if user else "—",
        created_at=attestation.created_at,
    )


def _list_history(db: Session, scope_type: str, scope_id: uuid.UUID) -> list[ComplianceAttestationOut]:
    rows = (
        db.query(ComplianceAttestation)
        .filter(ComplianceAttestation.scope_type == scope_type, ComplianceAttestation.scope_id == scope_id)
        .order_by(ComplianceAttestation.created_at.desc())
        .all()
    )
    return [_to_out(db, row) for row in rows]


@router.get("/datastores/{datastore_id}/compliance-attestations", response_model=list[ComplianceAttestationOut])
def list_datastore_compliance_attestations(
    datastore_id: uuid.UUID, db: Session = Depends(get_db), user: AuthenticatedUser = Depends(get_current_user)
) -> list[ComplianceAttestationOut]:
    """Full attestation history for this datastore, newest first --
    every row ever recorded, never just the current value, since the
    table is append-only (see `POST` on this same path). Any caller who
    can see this datastore may list its history; only `platform_admin`
    can add to it.
    """
    get_datastore_in_scope_or_404(db, datastore_id, user)
    return _list_history(db, "datastore", datastore_id)


@router.post("/datastores/{datastore_id}/compliance-attestations", response_model=ComplianceAttestationOut)
def create_datastore_compliance_attestation(
    datastore_id: uuid.UUID,
    body: CreateComplianceAttestationRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(_require_platform_admin),
) -> ComplianceAttestationOut:
    """`platform_admin` only -- datastores are platform-managed
    infrastructure, same gate as every other datastore-config write
    (`api/v1/datastores.py`).
    """
    get_datastore_in_scope_or_404(db, datastore_id, user)
    _validate_dimension(body.dimension, body.value, _DATASTORE_DIMENSIONS)
    attestation = ComplianceAttestation(
        scope_type="datastore",
        scope_id=datastore_id,
        dimension=body.dimension,
        value=body.value,
        notes=body.notes,
        asserted_by_user_id=user.user_id,
    )
    db.add(attestation)
    _record_audit(
        db, request, actor_user_id=user.user_id, scope_type="datastore", scope_id=datastore_id, dimension=body.dimension
    )
    db.commit()
    db.refresh(attestation)
    return _to_out(db, attestation)


@router.get("/customers/{customer_id}/compliance-attestations", response_model=list[ComplianceAttestationOut])
def list_customer_compliance_attestations(
    customer_id: uuid.UUID, db: Session = Depends(get_db), user: AuthenticatedUser = Depends(get_current_user)
) -> list[ComplianceAttestationOut]:
    """Full attestation history for this customer (both `dimension`
    values mixed together, newest first) -- every row ever recorded,
    never just the current value, since the table is append-only (see
    `POST` on this same path). Any caller who can see this customer may
    list its history; only `platform_admin` or the customer's own
    `customer_admin` can add to it.
    """
    get_customer_in_scope_or_404(db, customer_id, user)
    return _list_history(db, "customer", customer_id)


@router.post("/customers/{customer_id}/compliance-attestations", response_model=ComplianceAttestationOut)
def create_customer_compliance_attestation(
    customer_id: uuid.UUID,
    body: CreateComplianceAttestationRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> ComplianceAttestationOut:
    """`platform_admin` OR that customer's own `customer_admin` -- a
    customer's own admin is the one who actually knows their production
    copy count and media mix.
    """
    customer = get_customer_in_scope_or_404(db, customer_id, user)
    require_customer_admin_access_for_customer(user, customer.id)
    _validate_dimension(body.dimension, body.value, _CUSTOMER_DIMENSIONS)
    attestation = ComplianceAttestation(
        scope_type="customer",
        scope_id=customer_id,
        dimension=body.dimension,
        value=body.value,
        notes=body.notes,
        asserted_by_user_id=user.user_id,
    )
    db.add(attestation)
    _record_audit(
        db, request, actor_user_id=user.user_id, scope_type="customer", scope_id=customer_id, dimension=body.dimension
    )
    db.commit()
    db.refresh(attestation)
    return _to_out(db, attestation)
