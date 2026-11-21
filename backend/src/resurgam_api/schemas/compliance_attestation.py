from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

# Every dimension across both scopes: `POST /datastores/{id}/compliance-attestations`
# only accepts "immutable_copy"; `POST /customers/{id}/compliance-attestations`
# only accepts the other two -- enforced at the router (`_validate_dimension`
# in `api/v1/compliance_attestations.py`), not expressible per-scope in one
# shared schema, but a `Literal` here still beats a bare `str`: it shows the
# full valid vocabulary in `/docs` and rejects anything outside it up front.
AttestationDimension = Literal["immutable_copy", "production_copy_count", "media_type_diversity"]

AttestationValue = bool | int | list[str]


class CreateComplianceAttestationRequest(BaseModel):
    dimension: AttestationDimension = Field(
        description=(
            "Which 3-2-1-1-0 leg this attestation is for. A datastore-scoped "
            "request only accepts 'immutable_copy'; a customer-scoped request "
            "only accepts 'production_copy_count' or 'media_type_diversity' -- "
            "the wrong dimension for the scope is rejected with 400."
        )
    )
    value: AttestationValue = Field(
        description=(
            "Shape depends on `dimension`: a boolean for 'immutable_copy' "
            "(is this datastore write-immutable), an integer for "
            "'production_copy_count' (how many independent copies of the data "
            "exist), or a list of strings for 'media_type_diversity' (distinct "
            "storage media types in use, e.g. ['disk', 'tape'])."
        )
    )
    notes: str | None = Field(
        default=None, description="Free-text rationale for this attestation, shown alongside it in the report."
    )


class ComplianceAttestationOut(BaseModel):
    id: UUID = Field(description="This attestation row's own id.")
    scope_type: Literal["datastore", "customer"] = Field(description="What kind of object this attestation is about.")
    scope_id: UUID = Field(description="The datastore or customer id this attestation is about, per `scope_type`.")
    dimension: AttestationDimension = Field(
        description="Which 3-2-1-1-0 leg this attestation is for -- see the same field on CreateComplianceAttestationRequest."
    )
    value: AttestationValue = Field(
        description="The attested value, shaped per `dimension` -- see the same field on CreateComplianceAttestationRequest."
    )
    notes: str | None = Field(description="Free-text rationale supplied when this attestation was recorded, if any.")
    asserted_by_user_id: UUID = Field(
        description="The user who recorded this attestation -- never null; every row must have a real author."
    )
    asserted_by_display_name: str = Field(
        description="Display name of `asserted_by_user_id`, resolved for convenience."
    )
    created_at: datetime = Field(
        description=(
            "When this attestation was recorded -- also its only timestamp: rows in this table are "
            "append-only and never updated, so `created_at` is permanently accurate as the assertion time."
        )
    )
