from sqlalchemy.orm import Session

from resurgam_api.models.branding_settings import BrandingSettings


def get_branding_settings(db: Session) -> BrandingSettings | None:
    """The singleton branding row, or `None` if the operator has never
    configured one -- callers treat absence as "use RESURGAM's own
    defaults" (no env-var fallback to merge, unlike `OidcSettings`).
    """
    return db.query(BrandingSettings).first()
