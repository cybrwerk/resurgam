def _create_customer(db_session, name="Acme", slug="acme"):
    from resurgam_api.models.tenancy import Customer

    customer = Customer(
        name=name, slug=slug, debitor_number=f"debitor-{slug}", match_code=slug[:3].upper().ljust(3, "X")
    )
    db_session.add(customer)
    db_session.flush()
    db_session.commit()
    return customer.id


def _create_customer_scoped_viewer(db_session, test_settings, customer_id) -> dict:
    from resurgam_api.auth.sessions import create_session_token
    from resurgam_api.models.identity import Role, RoleKey, ScopeType, User, UserRoleAssignment

    user = User(email=f"viewer-{customer_id}@example.com", display_name="Scoped Viewer")
    db_session.add(user)
    db_session.flush()

    role = db_session.query(Role).filter_by(key=RoleKey.VIEWER.value).one()
    db_session.add(
        UserRoleAssignment(user_id=user.id, role_id=role.id, scope_type=ScopeType.CUSTOMER.value, scope_id=customer_id)
    )
    db_session.commit()

    token, _ = create_session_token(user.id, test_settings)
    return {"Authorization": f"Bearer {token}"}


def _plan_payload(**overrides):
    payload = {
        "customer_id": None,
        "name": "Standard",
        "price_per_vm_month": "10.00",
        "price_per_gb_month": "0.0500",
        "currency": "CHF",
    }
    payload.update(overrides)
    return payload


def test_create_global_plan(client, platform_admin_headers):
    response = client.post("/api/v1/pricing-plans", json=_plan_payload(), headers=platform_admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["customer_id"] is None
    assert body["price_per_vm_month"] == "10.00"


def test_create_second_global_plan_rejected(client, platform_admin_headers):
    first = client.post("/api/v1/pricing-plans", json=_plan_payload(), headers=platform_admin_headers)
    assert first.status_code == 200
    second = client.post("/api/v1/pricing-plans", json=_plan_payload(name="Other"), headers=platform_admin_headers)
    assert second.status_code == 409
    assert second.json()["code"] == "pricing_plan_global_exists"


def test_create_customer_override_plan(client, platform_admin_headers, db_session):
    customer_id = _create_customer(db_session)
    response = client.post(
        "/api/v1/pricing-plans",
        json=_plan_payload(customer_id=str(customer_id), name="Enterprise", price_per_vm_month="8.00"),
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["customer_id"] == str(customer_id)


def test_create_second_plan_for_same_customer_rejected(client, platform_admin_headers, db_session):
    customer_id = _create_customer(db_session)
    first = client.post(
        "/api/v1/pricing-plans", json=_plan_payload(customer_id=str(customer_id)), headers=platform_admin_headers
    )
    assert first.status_code == 200
    second = client.post(
        "/api/v1/pricing-plans", json=_plan_payload(customer_id=str(customer_id)), headers=platform_admin_headers
    )
    assert second.status_code == 409
    assert second.json()["code"] == "pricing_plan_customer_exists"


def test_create_rejects_unknown_customer(client, platform_admin_headers):
    response = client.post(
        "/api/v1/pricing-plans",
        json=_plan_payload(customer_id="00000000-0000-0000-0000-000000000000"),
        headers=platform_admin_headers,
    )
    assert response.status_code == 400
    assert response.json()["code"] == "customer_not_found"


def test_create_requires_platform_admin(client, db_session, test_settings):
    customer_id = _create_customer(db_session)
    viewer_headers = _create_customer_scoped_viewer(db_session, test_settings, customer_id)
    response = client.post("/api/v1/pricing-plans", json=_plan_payload(), headers=viewer_headers)
    assert response.status_code == 403


def test_global_plan_invisible_to_non_admin(client, platform_admin_headers, db_session, test_settings):
    client.post("/api/v1/pricing-plans", json=_plan_payload(), headers=platform_admin_headers)
    customer_id = _create_customer(db_session)
    viewer_headers = _create_customer_scoped_viewer(db_session, test_settings, customer_id)

    response = client.get("/api/v1/pricing-plans", headers=viewer_headers)
    assert response.status_code == 200
    assert response.json() == []


def test_customer_scoped_viewer_sees_only_own_plan(client, platform_admin_headers, db_session, test_settings):
    mine = _create_customer(db_session, "Mine", "mine")
    other = _create_customer(db_session, "Other", "other")
    client.post("/api/v1/pricing-plans", json=_plan_payload(customer_id=str(mine)), headers=platform_admin_headers)
    client.post("/api/v1/pricing-plans", json=_plan_payload(customer_id=str(other)), headers=platform_admin_headers)

    viewer_headers = _create_customer_scoped_viewer(db_session, test_settings, mine)
    response = client.get("/api/v1/pricing-plans", headers=viewer_headers)
    assert response.status_code == 200
    plans = response.json()
    assert len(plans) == 1
    assert plans[0]["customer_id"] == str(mine)


def test_update_plan_price(client, platform_admin_headers):
    """UX-audit finding (2026-09-24): only create/delete existed, so a price
    correction meant losing the plan's created_at and momentarily leaving
    the customer with none at all.
    """
    plan = client.post("/api/v1/pricing-plans", json=_plan_payload(), headers=platform_admin_headers).json()

    response = client.patch(
        f"/api/v1/pricing-plans/{plan['id']}",
        json={"price_per_vm_month": "12.50"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["price_per_vm_month"] == "12.50"
    assert body["name"] == "Standard"
    assert body["id"] == plan["id"]
    assert body["created_at"] == plan["created_at"]


def test_update_plan_partial_leaves_other_fields_unchanged(client, platform_admin_headers):
    plan = client.post("/api/v1/pricing-plans", json=_plan_payload(), headers=platform_admin_headers).json()

    response = client.patch(
        f"/api/v1/pricing-plans/{plan['id']}", json={"name": "Renamed"}, headers=platform_admin_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Renamed"
    assert body["price_per_vm_month"] == plan["price_per_vm_month"]
    assert body["price_per_gb_month"] == plan["price_per_gb_month"]


def test_update_plan_not_found(client, platform_admin_headers):
    response = client.patch(
        "/api/v1/pricing-plans/00000000-0000-0000-0000-000000000000",
        json={"name": "X"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 404


def test_update_plan_requires_platform_admin(client, db_session, test_settings, platform_admin_headers):
    plan = client.post("/api/v1/pricing-plans", json=_plan_payload(), headers=platform_admin_headers).json()
    customer_id = _create_customer(db_session)
    viewer_headers = _create_customer_scoped_viewer(db_session, test_settings, customer_id)

    response = client.patch(f"/api/v1/pricing-plans/{plan['id']}", json={"name": "Hacked"}, headers=viewer_headers)
    assert response.status_code == 403


def test_delete_plan(client, platform_admin_headers):
    plan = client.post("/api/v1/pricing-plans", json=_plan_payload(), headers=platform_admin_headers).json()
    response = client.delete(f"/api/v1/pricing-plans/{plan['id']}", headers=platform_admin_headers)
    assert response.status_code == 204
    assert client.get("/api/v1/pricing-plans", headers=platform_admin_headers).json() == []
