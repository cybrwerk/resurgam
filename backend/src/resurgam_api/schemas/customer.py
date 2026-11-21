from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CustomerCreateRequest(BaseModel):
    name: str = Field(description="Display name shown throughout the UI and on reports.")
    slug: str = Field(
        pattern=r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$",
        max_length=100,
        description="Stable, URL-safe identity key (lowercase letters, digits, hyphens). Not editable after creation.",
    )
    debitor_number: str = Field(
        min_length=1,
        max_length=50,
        description="Accounts-receivable / debtor number (Debitorennummer). Must be unique.",
    )
    match_code: str = Field(
        min_length=3,
        max_length=3,
        description="Three-letter mnemonic lookup code (Matchcode), case-insensitive on input but stored uppercased. Must be unique.",
    )

    @field_validator("match_code")
    @classmethod
    def _match_code_letters_uppercased(cls, value: str) -> str:
        normalized = value.upper()
        if not normalized.isalpha():
            raise ValueError("match_code must be exactly three letters")
        return normalized


class CustomerUpdateRequest(BaseModel):
    """Partial update — an omitted field leaves the current value
    unchanged. `slug` is not editable here — it's used as this
    customer's stable identity key elsewhere (e.g. import flows keyed
    by slug in the future); changing it is a bigger, separate decision
    than a simple field edit, not something to fold into this endpoint.

    `report_retention_days` is the one field on this model meant to be
    *clearable* back to null (null means "use the platform-wide floor")
    -- the endpoint checks `model_fields_set` for it instead of `is not
    None`, same fix as PATCH /pve-clusters's `lab_sdn_zone_name`.
    """

    name: str | None = Field(default=None, description="New display name. Omit to leave unchanged.")
    debitor_number: str | None = Field(
        default=None,
        min_length=1,
        max_length=50,
        description="New debtor number. Omit to leave unchanged. Must be unique.",
    )
    match_code: str | None = Field(
        default=None,
        min_length=3,
        max_length=3,
        description="New three-letter match code. Omit to leave unchanged. Must be unique.",
    )
    report_retention_days: int | None = Field(
        default=None,
        ge=1,
        description="How long to keep this customer's generated report PDFs, in days. Send null to clear back to the platform-wide floor. Must be at least the platform floor if set.",
    )

    @field_validator("match_code")
    @classmethod
    def _match_code_letters_uppercased(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.upper()
        if not normalized.isalpha():
            raise ValueError("match_code must be exactly three letters")
        return normalized


class CustomerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="This customer's own id.")
    name: str = Field(description="Display name shown throughout the UI and on reports.")
    slug: str = Field(description="Stable, URL-safe identity key.")
    debitor_number: str = Field(description="Accounts-receivable / debtor number (Debitorennummer).")
    match_code: str = Field(description="Three-letter mnemonic lookup code (Matchcode).")
    disabled: bool = Field(
        description="Whether this customer is disabled. A disabled customer's data stays intact but the customer is excluded from active-customer views."
    )
    created_at: datetime = Field(description="When this customer was created.")
    report_retention_days: int | None = Field(
        description="This customer's own report-retention override, in days. Null means the platform-wide floor applies -- see GET /instance-settings."
    )


class AssignGroupCustomerRequest(BaseModel):
    customer_id: UUID


class CustomerImportRowError(BaseModel):
    row: int
    message: str


class CustomerImportResult(BaseModel):
    created: int
    updated: int
    errors: list[CustomerImportRowError]
