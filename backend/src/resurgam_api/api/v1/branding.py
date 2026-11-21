import re

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from sqlalchemy.orm import Session

from resurgam_api.auth.dependency import require_role
from resurgam_api.branding import get_branding_settings
from resurgam_api.config import Settings, get_settings
from resurgam_api.db import get_db
from resurgam_api.errors import ResurgamApiError
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.branding_settings import BrandingSettings
from resurgam_api.models.identity import RoleKey
from resurgam_api.schemas.branding import BrandingSettingsOut

router = APIRouter(prefix="/branding", tags=["branding"])

_require_platform_admin = require_role(RoleKey.PLATFORM_ADMIN)

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# The logo is served without authentication from the API's own origin, so
# only raster formats a browser can never run script from are accepted --
# detected from the bytes, never from the client-supplied content type
# (an SVG or an HTML file renamed .png would otherwise be stored XSS).
_LOGO_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
# Headers for GET /branding/logo: no sniffing, no script, no frame -- even a
# legacy SVG logo stored before the format check opened directly in the
# browser cannot run anything.
_LOGO_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; sandbox",
}


def detect_logo_type(data: bytes) -> str | None:
    """'image/png', 'image/jpeg' or 'image/webp' by magic bytes, else None."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _record_audit(db: Session, request: Request, *, actor_user_id, action: str = "branding_settings_updated") -> None:
    db.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            action=action,
            target_type="branding_settings",
            target_id=None,
            correlation_id=getattr(request.state, "correlation_id", None),
        )
    )


def _to_out(settings: BrandingSettings | None) -> BrandingSettingsOut:
    if settings is None:
        return BrandingSettingsOut(logo_configured=False, primary_color=None, accent_color=None, app_name=None)
    return BrandingSettingsOut(
        logo_configured=settings.logo is not None,
        primary_color=settings.primary_color,
        accent_color=settings.accent_color,
        app_name=settings.app_name,
    )


@router.get("", response_model=BrandingSettingsOut)
def get_branding(db: Session = Depends(get_db)) -> BrandingSettingsOut:
    """Deliberately **no auth required** -- the login page and the
    sidebar of an unauthenticated session both need to know what to
    render before anyone has signed in, the same reasoning any public
    login page's branding always has to be unauthenticated. Never
    returns the logo bytes themselves, only whether one is configured.
    """
    return _to_out(get_branding_settings(db))


@router.get("/logo")
def get_branding_logo(db: Session = Depends(get_db)) -> Response:
    """Deliberately **no auth required**, same reasoning as `GET /branding`.
    Served with the content type detected from the stored bytes (PNG, JPEG
    or WebP), `X-Content-Type-Options: nosniff`, `Content-Disposition:
    inline` and `Content-Security-Policy: default-src 'none'; sandbox`, so
    opening it directly in the browser can never run script. 404 when no
    logo is configured."""
    settings = get_branding_settings(db)
    if settings is None or settings.logo is None:
        raise ResurgamApiError(code="not_found", message="No logo configured.", status_code=404)
    media_type = detect_logo_type(settings.logo)
    if media_type is None:
        # Stored before uploads were format-checked: an SVG keeps rendering in
        # <img> tags (where it never runs script); anything else is opaque.
        media_type = "image/svg+xml" if settings.logo_content_type == "image/svg+xml" else "application/octet-stream"
    extension = _LOGO_EXTENSIONS.get(media_type, "svg" if media_type == "image/svg+xml" else "bin")
    return Response(
        content=settings.logo,
        media_type=media_type,
        headers={**_LOGO_SECURITY_HEADERS, "Content-Disposition": f'inline; filename="logo.{extension}"'},
    )


@router.put("", response_model=BrandingSettingsOut)
def update_branding(
    request: Request,
    logo: UploadFile | None = File(
        default=None,
        description="New logo image: PNG, JPEG or WebP (recognised by its content, not its name or declared type; "
        "SVG is refused), at most BRANDING_LOGO_MAX_BYTES (default 1 MB). Omit to keep the current logo "
        "unchanged; remove it with DELETE /branding/logo.",
    ),
    primary_color: str | None = Form(
        default=None, description="Hex color (#RRGGBB). Omit to keep the current value unchanged."
    ),
    accent_color: str | None = Form(
        default=None, description="Hex color (#RRGGBB). Omit to keep the current value unchanged."
    ),
    app_name: str | None = Form(
        default=None,
        max_length=60,
        description="Product name shown instead of 'RESURGAM' (browser tab, top bar, login page, PDF report footer), "
        "up to 60 characters. Omit to keep the current value.",
    ),
    reset_app_name: bool = Form(
        default=False, description="True: go back to 'RESURGAM' as the product name (ignores `app_name`)."
    ),
    db: Session = Depends(get_db),
    app_settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> BrandingSettingsOut:
    """`platform_admin` only. Answers 400 `invalid_logo` for anything but a
    PNG, JPEG or WebP image and 413 `logo_too_large` above the size limit.
    Omitting `logo` keeps whatever is already
    stored (mirrors `UpdateOidcSettingsRequest.client_secret`'s "omit to
    keep current" convention). To go back to RESURGAM's own look use
    DELETE /branding (everything) or DELETE /branding/logo (logo only).
    """
    for color in (primary_color, accent_color):
        if color is not None and not _HEX_COLOR_RE.match(color):
            raise ResurgamApiError(
                code="invalid_color", message=f"{color!r} is not a valid #RRGGBB hex color.", status_code=400
            )

    logo_bytes: bytes | None = None
    logo_type: str | None = None
    if logo is not None:
        limit = app_settings.branding_logo_max_bytes
        logo_bytes = logo.file.read(limit + 1)
        if len(logo_bytes) > limit:
            raise ResurgamApiError(
                code="logo_too_large",
                message=f"The logo is larger than {limit} bytes. Upload a smaller PNG, JPEG or WebP image.",
                status_code=413,
            )
        logo_type = detect_logo_type(logo_bytes)
        if logo_type is None:
            raise ResurgamApiError(
                code="invalid_logo",
                message="The logo must be a PNG, JPEG or WebP image (SVG is not accepted).",
                status_code=400,
            )

    settings = get_branding_settings(db)
    if settings is None:
        settings = BrandingSettings()
        db.add(settings)

    if logo_bytes is not None:
        settings.logo = logo_bytes
        settings.logo_content_type = logo_type
    if primary_color is not None:
        settings.primary_color = primary_color
    if accent_color is not None:
        settings.accent_color = accent_color
    if reset_app_name:
        settings.app_name = None
    elif app_name is not None:
        settings.app_name = " ".join(app_name.split()) or None

    _record_audit(db, request, actor_user_id=user.user_id)
    db.commit()
    db.refresh(settings)
    return _to_out(settings)


@router.delete("", response_model=BrandingSettingsOut)
def reset_branding(
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(_require_platform_admin),
) -> BrandingSettingsOut:
    """`platform_admin` only. Restores RESURGAM's default look: removes the
    custom logo, both custom colors and the custom app title (the white-label settings row is
    deleted; absence means "use the defaults"). Audited."""
    settings = get_branding_settings(db)
    if settings is not None:
        db.delete(settings)
    _record_audit(db, request, actor_user_id=user.user_id, action="branding_settings_reset")
    db.commit()
    return _to_out(None)


@router.delete("/logo", response_model=BrandingSettingsOut)
def delete_branding_logo(
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(_require_platform_admin),
) -> BrandingSettingsOut:
    """`platform_admin` only. Removes the custom logo, keeping the colors --
    the login page and sidebar show RESURGAM's own lockup again. Audited."""
    settings = get_branding_settings(db)
    if settings is not None:
        settings.logo = None
        settings.logo_content_type = None
    _record_audit(db, request, actor_user_id=user.user_id, action="branding_logo_removed")
    db.commit()
    return _to_out(settings)
