from pydantic import BaseModel, Field


class BrandingSettingsOut(BaseModel):
    logo_configured: bool = Field(
        description="Whether a logo has been uploaded. The logo bytes themselves are never returned here -- fetch them from GET /branding/logo."
    )
    primary_color: str | None = Field(
        default=None,
        description="Hex color (#RRGGBB) used for headings and primary buttons in the web GUI and PDF reports. Null means RESURGAM's own default is used.",
    )
    accent_color: str | None = Field(
        default=None,
        description="Hex color (#RRGGBB) used for borders and secondary accents in the web GUI and PDF reports. Null means RESURGAM's own default is used.",
    )
    app_name: str | None = Field(
        default=None,
        description="The product name shown in the browser tab, the top bar, on the login page and in PDF report "
        "footers instead of 'RESURGAM'. Null means RESURGAM's own name is used.",
    )
