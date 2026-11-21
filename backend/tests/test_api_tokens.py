"""Personal API tokens (docs/adr/0029): creation rules, request-time
narrowing to the token's grants, read-only, IP allowlist, expiry,
revocation and the session-only management endpoints."""

from datetime import UTC, datetime, timedelta

import pytest

from resurgam_api.auth.api_tokens import MAX_LIFETIME_DAYS, hash_token
from resurgam_api.models.api_token import ApiToken
from resurgam_api.models.audit import AuditEvent

PASSWORD = "correct horse battery staple"


def _customer(client, admin_headers, name: str) -> str:
    slug = name.lower().replace(" ", "-")
    response = client.post(
        "/api/v1/customers",
        json={
            "name": name,
            "slug": slug,
            "debitor_number": f"D-{slug}",
            "match_code": slug[-1].upper() + slug[:2].upper(),
        },
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _user(client, admin_headers, email: str, roles: list[tuple[str, str, str | None]]) -> dict:
    """Local user with the given (role, scope_type, scope_id) assignments;
    returns session headers."""
    created = client.post(
        "/api/v1/users",
        json={"email": email, "display_name": email.split("@")[0], "password": PASSWORD},
        headers=admin_headers,
    )
    assert created.status_code == 200, created.text
    user_id = created.json()["id"]
    for role, scope_type, scope_id in roles:
        r = client.post(
            f"/api/v1/users/{user_id}/assign-role",
            json={"role": role, "scope_type": scope_type, "scope_id": scope_id},
            headers=admin_headers,
        )
        assert r.status_code == 200, r.text
    login = client.post("/api/v1/auth/local-login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}", "_user_id": user_id}


def _session(headers: dict) -> dict:
    return {"Authorization": headers["Authorization"]}


def _create_token(client, headers, *, grants, read_only=True, days=30, cidrs=None, name="test token"):
    return client.post(
        "/api/v1/api-tokens",
        json={
            "name": name,
            "expires_in_days": days,
            "read_only": read_only,
            "grants": grants,
            "allowed_cidrs": cidrs,
        },
        headers=_session(headers),
    )


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def setup(client, platform_admin_headers):
    a = _customer(client, platform_admin_headers, "Customer A")
    b = _customer(client, platform_admin_headers, "Customer B")
    admin = _user(client, platform_admin_headers, "admin@example.com", [("platform_admin", "global", None)])
    viewer_a = _user(client, platform_admin_headers, "viewer-a@example.com", [("viewer", "customer", a)])
    return {"a": a, "b": b, "admin": admin, "viewer_a": viewer_a, "breakglass": platform_admin_headers}


# --- creation -----------------------------------------------------------------


def test_create_returns_token_once_and_stores_only_hash(client, db_session, setup):
    response = _create_token(client, setup["admin"], grants=[{"role": "viewer", "scope_type": "global"}])
    assert response.status_code == 200, response.text
    body = response.json()
    token = body["token"]
    assert token.startswith("rsg_") and len(token) > 40
    assert token.startswith(body["token_prefix"])
    assert body["read_only"] is True and body["status"] == "active"

    row = db_session.get(ApiToken, body["id"])
    assert row.token_hash == hash_token(token)
    assert token not in str(row.__dict__)

    listed = client.get("/api/v1/api-tokens/mine", headers=_session(setup["admin"])).json()
    assert [t["id"] for t in listed] == [body["id"]]
    assert "token" not in listed[0]
    assert db_session.query(AuditEvent).filter_by(action="api_token_created").count() == 1


@pytest.mark.parametrize("days", [0, MAX_LIFETIME_DAYS + 1])
def test_lifetime_is_limited_to_18_months(client, setup, days):
    response = _create_token(client, setup["admin"], grants=[{"role": "viewer", "scope_type": "global"}], days=days)
    assert response.status_code == 422


def test_max_lifetime_is_accepted(client, setup):
    response = _create_token(
        client, setup["admin"], grants=[{"role": "viewer", "scope_type": "global"}], days=MAX_LIFETIME_DAYS
    )
    assert response.status_code == 200
    expires = datetime.fromisoformat(response.json()["expires_at"])
    assert expires - datetime.now(UTC) > timedelta(days=MAX_LIFETIME_DAYS - 1)


def test_user_can_only_grant_roles_they_hold(client, setup):
    ok = _create_token(
        client, setup["viewer_a"], grants=[{"role": "viewer", "scope_type": "customer", "scope_id": setup["a"]}]
    )
    assert ok.status_code == 200, ok.text

    for grant in (
        {"role": "viewer", "scope_type": "customer", "scope_id": setup["b"]},  # other customer
        {"role": "viewer", "scope_type": "global"},  # wider scope
        {"role": "restore_operator", "scope_type": "customer", "scope_id": setup["a"]},  # other role
    ):
        refused = _create_token(client, setup["viewer_a"], grants=[grant])
        assert refused.status_code == 403, grant
        assert refused.json()["code"] == "grant_not_allowed"


def test_platform_admin_can_grant_any_role_on_any_customer(client, setup):
    response = _create_token(
        client,
        setup["admin"],
        grants=[{"role": "restore_operator", "scope_type": "customer", "scope_id": setup["b"]}],
    )
    assert response.status_code == 200
    assert response.json()["grants"][0]["scope_name"] == "Customer B"


@pytest.mark.parametrize(
    "grant",
    [
        {"role": "viewer", "scope_type": "global", "scope_id": "00000000-0000-0000-0000-000000000001"},
        {"role": "viewer", "scope_type": "customer"},
        {"role": "platform_admin", "scope_type": "customer", "scope_id": "SCOPE_A"},
        {"role": "viewer", "scope_type": "customer", "scope_id": "00000000-0000-0000-0000-000000000001"},
    ],
)
def test_malformed_grants_are_rejected(client, setup, grant):
    if grant.get("scope_id") == "SCOPE_A":
        grant = {**grant, "scope_id": setup["a"]}
    response = _create_token(client, setup["admin"], grants=[grant])
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_grant"


def test_grantable_lists_own_assignments_or_everything_for_admin(client, setup):
    mine = client.get("/api/v1/api-tokens/grantable", headers=_session(setup["viewer_a"])).json()
    assert mine["can_grant_any"] is False
    assert [(o["role"], o["scope_name"]) for o in mine["options"]] == [("viewer", "Customer A")]
    assert mine["max_lifetime_days"] == MAX_LIFETIME_DAYS

    admin = client.get("/api/v1/api-tokens/grantable", headers=_session(setup["admin"])).json()
    assert admin["can_grant_any"] is True
    combos = {(o["role"], o["scope_type"], o["scope_name"]) for o in admin["options"]}
    assert ("platform_admin", "global", None) in combos
    assert ("restore_operator", "customer", "Customer B") in combos
    assert ("platform_admin", "customer", "Customer A") not in combos


def test_breakglass_cannot_create_tokens(client, setup):
    response = _create_token(client, setup["breakglass"], grants=[{"role": "viewer", "scope_type": "global"}])
    assert response.status_code == 403
    assert response.json()["code"] == "breakglass_cannot_create_api_token"


def test_invalid_cidr_is_rejected(client, setup):
    response = _create_token(
        client, setup["admin"], grants=[{"role": "viewer", "scope_type": "global"}], cidrs=["not-an-ip"]
    )
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_cidr"


# --- using a token ----------------------------------------------------------------


def test_token_is_narrowed_to_its_grants(client, setup):
    token = _create_token(
        client, setup["admin"], grants=[{"role": "viewer", "scope_type": "customer", "scope_id": setup["a"]}]
    ).json()["token"]

    whoami = client.get("/api/v1/auth/whoami", headers=_bearer(token)).json()
    assert whoami["api_token_id"] is not None
    assert [(a["role"], a["scope_id"]) for a in whoami["assignments"]] == [("viewer", setup["a"])]

    # The admin user sees every customer; the token only customer A.
    customers = client.get("/api/v1/customers", headers=_bearer(token)).json()
    assert [c["name"] for c in customers] == ["Customer A"]
    # ...and no platform-admin endpoint.
    assert client.get("/api/v1/users", headers=_bearer(token)).status_code == 403


def test_read_only_token_cannot_write(client, setup):
    token = _create_token(
        client, setup["admin"], grants=[{"role": "platform_admin", "scope_type": "global"}], read_only=True
    ).json()["token"]
    assert client.get("/api/v1/customers", headers=_bearer(token)).status_code == 200
    refused = client.post("/api/v1/customers", json={"name": "Nope", "slug": "nope"}, headers=_bearer(token))
    assert refused.status_code == 403
    assert refused.json()["code"] == "api_token_read_only"


def test_read_write_token_can_write_and_is_named_in_audit(client, db_session, setup):
    created = _create_token(
        client,
        setup["admin"],
        grants=[{"role": "platform_admin", "scope_type": "global"}],
        read_only=False,
        name="Provisioning",
    ).json()
    response = client.post(
        "/api/v1/customers",
        json={"name": "Via token", "slug": "via-token", "debitor_number": "D-9", "match_code": "VIA"},
        headers=_bearer(created["token"]),
    )
    assert response.status_code == 200, response.text

    event = (
        db_session.query(AuditEvent).filter_by(action="customer_created", target_id=response.json()["id"]).one_or_none()
    )
    assert event is not None
    assert "via API token Provisioning" in event.actor_label
    assert created["token_prefix"] in event.actor_label


def test_ip_allowlist_refuses_other_addresses(client, setup):
    open_token = _create_token(client, setup["admin"], grants=[{"role": "viewer", "scope_type": "global"}]).json()
    limited = _create_token(
        client, setup["admin"], grants=[{"role": "viewer", "scope_type": "global"}], cidrs=["192.0.2.10"]
    ).json()
    assert limited["allowed_cidrs"] == ["192.0.2.10/32"]
    assert client.get("/api/v1/auth/whoami", headers=_bearer(open_token["token"])).status_code == 200
    # Starlette's TestClient connects as "testclient" -- not an allowed address.
    refused = client.get("/api/v1/auth/whoami", headers=_bearer(limited["token"]))
    assert refused.status_code == 403
    assert refused.json()["code"] == "api_token_ip_not_allowed"


def test_ip_allowlist_uses_forwarded_for_only_when_trusted(client, setup, test_settings):
    token = _create_token(
        client, setup["admin"], grants=[{"role": "viewer", "scope_type": "global"}], cidrs=["192.0.2.0/24"]
    ).json()["token"]
    headers = {**_bearer(token), "X-Forwarded-For": "198.51.100.7, 192.0.2.10"}

    test_settings.trust_proxy_headers = False
    assert client.get("/api/v1/auth/whoami", headers=headers).status_code == 403

    test_settings.trust_proxy_headers = True
    assert client.get("/api/v1/auth/whoami", headers=headers).status_code == 200
    spoofed = {**_bearer(token), "X-Forwarded-For": "192.0.2.10, 198.51.100.7"}
    assert client.get("/api/v1/auth/whoami", headers=spoofed).status_code == 403


def test_last_used_is_recorded(client, db_session, setup, test_settings):
    created = _create_token(client, setup["admin"], grants=[{"role": "viewer", "scope_type": "global"}]).json()
    test_settings.trust_proxy_headers = True
    client.get("/api/v1/auth/whoami", headers={**_bearer(created["token"]), "X-Forwarded-For": "203.0.113.5"})
    row = db_session.get(ApiToken, created["id"])
    db_session.refresh(row)
    assert row.last_used_at is not None
    assert row.last_used_ip == "203.0.113.5"


def test_expired_and_unknown_tokens_are_rejected(client, db_session, setup):
    created = _create_token(client, setup["admin"], grants=[{"role": "viewer", "scope_type": "global"}]).json()
    row = db_session.get(ApiToken, created["id"])
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()

    for token in (created["token"], "rsg_" + "x" * 43):
        response = client.get("/api/v1/auth/whoami", headers=_bearer(token))
        assert response.status_code == 401
        assert response.json()["code"] == "invalid_api_token"


def test_losing_a_role_removes_it_from_the_token(client, setup, platform_admin_headers):
    token = _create_token(
        client, setup["viewer_a"], grants=[{"role": "viewer", "scope_type": "customer", "scope_id": setup["a"]}]
    ).json()["token"]
    assert len(client.get("/api/v1/auth/whoami", headers=_bearer(token)).json()["assignments"]) == 1

    user_id = setup["viewer_a"]["_user_id"]
    assignments = client.get(f"/api/v1/users/{user_id}/role-assignments", headers=platform_admin_headers).json()
    removed = client.post(
        f"/api/v1/users/{user_id}/unassign-role",
        json={"assignment_id": assignments[0]["id"]},
        headers=platform_admin_headers,
    )
    assert removed.status_code == 204
    assert client.get("/api/v1/auth/whoami", headers=_bearer(token)).json()["assignments"] == []


def test_disabled_user_tokens_stop_working(client, setup, platform_admin_headers):
    token = _create_token(
        client, setup["viewer_a"], grants=[{"role": "viewer", "scope_type": "customer", "scope_id": setup["a"]}]
    ).json()["token"]
    client.post(f"/api/v1/users/{setup['viewer_a']['_user_id']}/disable", headers=platform_admin_headers)
    assert client.get("/api/v1/auth/whoami", headers=_bearer(token)).status_code == 401


def test_tokens_cannot_manage_tokens_or_change_password(client, setup):
    token = _create_token(
        client, setup["admin"], grants=[{"role": "platform_admin", "scope_type": "global"}], read_only=False
    ).json()["token"]
    for method, url, body in (
        ("get", "/api/v1/api-tokens/mine", None),
        (
            "post",
            "/api/v1/api-tokens",
            {"name": "x", "expires_in_days": 1, "grants": [{"role": "viewer", "scope_type": "global"}]},
        ),
        (
            "post",
            "/api/v1/auth/change-password",
            {"current_password": PASSWORD, "new_password": "another long password"},
        ),
    ):
        response = (
            getattr(client, method)(url, json=body, headers=_bearer(token))
            if body
            else getattr(client, method)(url, headers=_bearer(token))
        )
        assert response.status_code == 403, url
        assert response.json()["code"] == "session_required"
    # MFA endpoints only take login sessions, never tokens.
    assert client.get("/api/v1/mfa/status", headers=_bearer(token)).status_code == 401


# --- revocation and admin view ----------------------------------------------------


def test_owner_revokes_token(client, db_session, setup):
    created = _create_token(
        client, setup["viewer_a"], grants=[{"role": "viewer", "scope_type": "customer", "scope_id": setup["a"]}]
    ).json()
    assert client.delete(f"/api/v1/api-tokens/{created['id']}", headers=_session(setup["viewer_a"])).status_code == 204
    assert client.get("/api/v1/auth/whoami", headers=_bearer(created["token"])).status_code == 401
    assert client.get("/api/v1/api-tokens/mine", headers=_session(setup["viewer_a"])).json() == []
    assert client.delete(f"/api/v1/api-tokens/{created['id']}", headers=_session(setup["viewer_a"])).status_code == 404
    assert db_session.query(AuditEvent).filter_by(action="api_token_revoked").count() == 1


def test_other_users_cannot_see_or_revoke_a_token(client, setup):
    created = _create_token(client, setup["admin"], grants=[{"role": "viewer", "scope_type": "global"}]).json()
    assert client.delete(f"/api/v1/api-tokens/{created['id']}", headers=_session(setup["viewer_a"])).status_code == 404
    assert client.get("/api/v1/api-tokens", headers=_session(setup["viewer_a"])).status_code == 403


def test_admin_lists_and_revokes_everyones_tokens(client, setup):
    created = _create_token(
        client, setup["viewer_a"], grants=[{"role": "viewer", "scope_type": "customer", "scope_id": setup["a"]}]
    ).json()
    listed = client.get("/api/v1/api-tokens", headers=_session(setup["admin"])).json()
    assert [(t["id"], t["user_email"]) for t in listed] == [(created["id"], "viewer-a@example.com")]
    assert client.delete(f"/api/v1/api-tokens/{created['id']}", headers=_session(setup["admin"])).status_code == 204
    assert client.get("/api/v1/auth/whoami", headers=_bearer(created["token"])).status_code == 401


def test_session_logins_are_not_labelled_as_token(client, db_session, setup):
    """The token label lives on the per-request session; a later session
    request on the same DB session must not inherit it."""
    token = _create_token(
        client, setup["admin"], grants=[{"role": "platform_admin", "scope_type": "global"}], read_only=False
    ).json()["token"]
    by_token = client.post(
        "/api/v1/customers",
        json={"name": "By token", "slug": "by-token", "debitor_number": "D-7", "match_code": "BYT"},
        headers=_bearer(token),
    ).json()["id"]
    by_session = client.post(
        "/api/v1/customers",
        json={"name": "By session", "slug": "by-session", "debitor_number": "D-8", "match_code": "BYS"},
        headers=_session(setup["admin"]),
    ).json()["id"]

    def label(customer_id):
        return (
            db_session.query(AuditEvent).filter_by(action="customer_created", target_id=customer_id).one().actor_label
        )

    assert "via API token" in label(by_token)
    assert "via API token" not in label(by_session)
