def test_get_branding_unconfigured_returns_defaults_with_no_auth(client):
    """No Authorization header at all -- the login page needs this
    before anyone is signed in.
    """
    response = client.get("/api/v1/branding")
    assert response.status_code == 200
    body = response.json()
    assert body == {"logo_configured": False, "primary_color": None, "accent_color": None, "app_name": None}


def test_get_branding_logo_404s_when_unconfigured(client):
    response = client.get("/api/v1/branding/logo")
    assert response.status_code == 404


def test_put_branding_requires_platform_admin(client):
    response = client.put("/api/v1/branding", data={"primary_color": "#112233"})
    assert response.status_code == 401


def test_put_branding_rejects_malformed_hex_color(client, platform_admin_headers):
    response = client.put("/api/v1/branding", data={"primary_color": "not-a-color"}, headers=platform_admin_headers)
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_color"


def test_put_branding_saves_colors_and_logo_roundtrips(client, platform_admin_headers):
    png_bytes = b"\x89PNG\r\n\x1a\nfake-but-good-enough-for-a-round-trip-test"
    response = client.put(
        "/api/v1/branding",
        files={"logo": ("logo.png", png_bytes, "image/png")},
        data={"primary_color": "#112233", "accent_color": "#aabbcc"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body == {"logo_configured": True, "primary_color": "#112233", "accent_color": "#aabbcc", "app_name": None}

    # Unauthenticated GETs now see the configured state.
    get_response = client.get("/api/v1/branding")
    assert get_response.json()["logo_configured"] is True

    logo_response = client.get("/api/v1/branding/logo")
    assert logo_response.status_code == 200
    assert logo_response.content == png_bytes
    assert logo_response.headers["content-type"] == "image/png"


def test_put_branding_omitting_logo_keeps_the_existing_one(client, platform_admin_headers):
    png_bytes = b"\x89PNG\r\n\x1a\noriginal-logo-bytes"
    client.put(
        "/api/v1/branding",
        files={"logo": ("logo.png", png_bytes, "image/png")},
        data={"primary_color": "#112233"},
        headers=platform_admin_headers,
    )

    # Second call sends only a color update, no logo field at all.
    response = client.put("/api/v1/branding", data={"accent_color": "#ddeeff"}, headers=platform_admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["logo_configured"] is True
    assert body["primary_color"] == "#112233"
    assert body["accent_color"] == "#ddeeff"

    logo_response = client.get("/api/v1/branding/logo")
    assert logo_response.content == png_bytes


def test_put_branding_records_audit_event(client, platform_admin_headers, db_session):
    from resurgam_api.models.audit import AuditEvent

    client.put("/api/v1/branding", data={"primary_color": "#112233"}, headers=platform_admin_headers)

    event = db_session.query(AuditEvent).filter_by(action="branding_settings_updated").one()
    assert event.target_type == "branding_settings"
    assert event.actor_user_id is not None


def _customize(client, headers):
    response = client.put(
        "/api/v1/branding",
        data={"primary_color": "#112233", "accent_color": "#445566"},
        files={"logo": ("logo.png", b"\x89PNG\r\n\x1a\nfake", "image/png")},
        headers=headers,
    )
    assert response.status_code == 200


def test_delete_branding_logo_keeps_colors(client, platform_admin_headers):
    _customize(client, platform_admin_headers)
    response = client.delete("/api/v1/branding/logo", headers=platform_admin_headers)
    assert response.status_code == 200
    assert response.json() == {
        "logo_configured": False,
        "primary_color": "#112233",
        "accent_color": "#445566",
        "app_name": None,
    }
    assert client.get("/api/v1/branding/logo").status_code == 404


def test_reset_branding_restores_defaults_and_is_audited(client, platform_admin_headers, db_session):
    from resurgam_api.models.audit import AuditEvent

    _customize(client, platform_admin_headers)
    response = client.delete("/api/v1/branding", headers=platform_admin_headers)
    assert response.status_code == 200
    assert response.json() == {"logo_configured": False, "primary_color": None, "accent_color": None, "app_name": None}
    assert client.get("/api/v1/branding").json() == response.json()
    assert db_session.query(AuditEvent).filter_by(action="branding_settings_reset").count() == 1
    # Resetting again (nothing customized) is harmless.
    assert client.delete("/api/v1/branding", headers=platform_admin_headers).status_code == 200


def test_reset_and_logo_removal_require_platform_admin(client):
    assert client.delete("/api/v1/branding").status_code == 401
    assert client.delete("/api/v1/branding/logo").status_code == 401


def test_app_name_set_cleared_and_reset(client, platform_admin_headers):
    response = client.put("/api/v1/branding", data={"app_name": "  Acme   Backup  "}, headers=platform_admin_headers)
    assert response.json()["app_name"] == "Acme Backup"
    assert client.get("/api/v1/branding").json()["app_name"] == "Acme Backup"
    # Omitted keeps it; reset_app_name goes back to RESURGAM.
    client.put("/api/v1/branding", data={"primary_color": "#112233"}, headers=platform_admin_headers)
    assert client.get("/api/v1/branding").json()["app_name"] == "Acme Backup"
    assert (
        client.put("/api/v1/branding", data={"reset_app_name": "true"}, headers=platform_admin_headers).json()[
            "app_name"
        ]
        is None
    )
    client.put("/api/v1/branding", data={"app_name": "Acme"}, headers=platform_admin_headers)
    assert client.delete("/api/v1/branding", headers=platform_admin_headers).json()["app_name"] is None
    too_long = client.put("/api/v1/branding", data={"app_name": "x" * 61}, headers=platform_admin_headers)
    assert too_long.status_code == 422


def test_put_branding_rejects_html_disguised_as_png(client, platform_admin_headers):
    """The declared content type and file name are ignored: an HTML file
    uploaded as image/png would otherwise be served from the API origin."""
    response = client.put(
        "/api/v1/branding",
        files={"logo": ("logo.png", b"<html><script>alert(localStorage)</script></html>", "image/png")},
        headers=platform_admin_headers,
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_logo"
    assert client.get("/api/v1/branding").json()["logo_configured"] is False


def test_put_branding_rejects_svg(client, platform_admin_headers):
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    response = client.put(
        "/api/v1/branding",
        files={"logo": ("logo.svg", svg, "image/svg+xml")},
        headers=platform_admin_headers,
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_logo"


def test_put_branding_rejects_oversized_logo(client, platform_admin_headers, test_settings):
    test_settings.branding_logo_max_bytes = 64
    response = client.put(
        "/api/v1/branding",
        files={"logo": ("logo.png", b"\x89PNG\r\n\x1a\n" + b"x" * 100, "image/png")},
        headers=platform_admin_headers,
    )
    assert response.status_code == 413
    assert response.json()["code"] == "logo_too_large"


def test_put_branding_stores_detected_type_not_declared_one(client, platform_admin_headers):
    webp = b"RIFF\x10\x00\x00\x00WEBPVP8 fake"
    response = client.put(
        "/api/v1/branding",
        files={"logo": ("logo.png", webp, "text/html")},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    logo = client.get("/api/v1/branding/logo")
    assert logo.headers["content-type"] == "image/webp"
    assert logo.headers["content-disposition"] == 'inline; filename="logo.webp"'


def test_get_branding_logo_sends_hardening_headers(client, platform_admin_headers):
    client.put(
        "/api/v1/branding",
        files={"logo": ("logo.jpg", b"\xff\xd8\xff\xe0fake-jpeg", "image/jpeg")},
        headers=platform_admin_headers,
    )
    logo = client.get("/api/v1/branding/logo")
    assert logo.status_code == 200
    assert logo.headers["content-type"] == "image/jpeg"
    assert logo.headers["x-content-type-options"] == "nosniff"
    assert logo.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert logo.headers["content-disposition"] == 'inline; filename="logo.jpg"'


def test_get_branding_logo_never_serves_legacy_html_as_html(client, db_session):
    """A row stored before the format check existed keeps its bytes but is
    never served with its stored (attacker-chosen) content type."""
    from resurgam_api.models.branding_settings import BrandingSettings

    db_session.add(BrandingSettings(logo=b"<html><script>x()</script></html>", logo_content_type="text/html"))
    db_session.commit()
    logo = client.get("/api/v1/branding/logo")
    assert logo.headers["content-type"] == "application/octet-stream"
    assert logo.headers["content-security-policy"] == "default-src 'none'; sandbox"
