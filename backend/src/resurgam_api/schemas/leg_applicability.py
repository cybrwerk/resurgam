from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

ThreeTwoOneOneZeroLeg = Literal["copies", "media_types", "offsite", "immutable", "zero_errors"]

ALL_LEGS: tuple[ThreeTwoOneOneZeroLeg, ...] = ("copies", "media_types", "offsite", "immutable", "zero_errors")


class UpdateLegApplicabilityRequest(BaseModel):
    applicable: bool = Field(
        description=(
            "False marks this leg out of contractual scope for this customer -- it will report "
            "'not_applicable' in the Recovery Evidence report instead of 'not_met'. True (the default "
            "for any leg with no stored row) puts it back in scope."
        )
    )


class LegApplicabilityOut(BaseModel):
    leg: ThreeTwoOneOneZeroLeg = Field(description="Which 3-2-1-1-0 leg this applicability setting is for.")
    applicable: bool = Field(
        description="Whether this leg counts against this customer's report. True is the default when no row has ever been stored."
    )
    updated_by_display_name: str | None = Field(
        description="Who last changed this setting -- null if it has never been explicitly set (still applicable by default)."
    )
    updated_at: datetime | None = Field(
        description="When this setting was last changed -- null if it has never been explicitly set."
    )
