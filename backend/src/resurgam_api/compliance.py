import uuid

from sqlalchemy.orm import Session

from resurgam_api.models.compliance_attestation import ComplianceAttestation
from resurgam_api.models.leg_applicability import ThreeTwoOneOneZeroLegSetting


def latest_attestation(
    db: Session, scope_type: str, scope_id: uuid.UUID, dimension: str
) -> ComplianceAttestation | None:
    """The current value for a `(scope_type, scope_id, dimension)` triple
    is simply its most recent row -- `compliance_attestations` is
    append-only (migration 0030's trigger), so every prior assertion
    stays in the table as real history, never superseded in place.
    """
    return (
        db.query(ComplianceAttestation)
        .filter(
            ComplianceAttestation.scope_type == scope_type,
            ComplianceAttestation.scope_id == scope_id,
            ComplianceAttestation.dimension == dimension,
        )
        .order_by(ComplianceAttestation.created_at.desc())
        .first()
    )


def leg_applicability(db: Session, customer_id: uuid.UUID) -> dict[str, bool]:
    """Which 3-2-1-1-0 legs are in scope for this customer -- absence of a
    row for a leg means applicable (the default; an operator has to
    deliberately carve out an exception). Returns only the legs that have
    an explicit row; callers treat a missing key as `True`.
    """
    rows = db.query(ThreeTwoOneOneZeroLegSetting).filter(ThreeTwoOneOneZeroLegSetting.customer_id == customer_id).all()
    return {row.leg: row.applicable for row in rows}
