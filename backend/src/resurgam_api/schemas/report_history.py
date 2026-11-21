from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field


class ReportHistoryOut(BaseModel):
    """One past generation of a report, metadata only -- the PDF bytes
    themselves are never included here (fetch them from
    GET /reporting/recovery-evidence/history/{id}/pdf); this shape is
    for a list view.
    """

    id: UUID = Field(
        description="This history entry's own id -- pass it to GET .../history/{id}/pdf to re-download the exact bytes."
    )
    report_type: str = Field(description="Which kind of report this is. Currently always 'recovery_evidence'.")
    customer_id: UUID | None = Field(
        description="Which customer this report was generated for. Null if that customer has since been deleted."
    )
    date_from: date = Field(description="Start of the period this report covered.")
    date_to: date = Field(description="End of the period this report covered.")
    framework: bool = Field(description="Whether this generation included the ISO27001/SOC2/ISO9001 control mapping.")
    generated_by_user_id: UUID | None = Field(
        description="Who generated this report. Null if that user has since been deleted."
    )
    sha256_checksum: str = Field(
        description="SHA256 hex digest of the exact PDF bytes stored for this entry -- matches a locally-computed checksum of the re-downloaded file."
    )
    created_at: datetime = Field(
        description="When this report was generated -- also when it will expire under this customer's effective retention period."
    )
