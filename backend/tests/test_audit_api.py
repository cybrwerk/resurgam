from datetime import UTC, datetime


def _insert_event(db_session, *, action, target_type="customer", target_id="t-1", occurred_at=None, actor_user_id=None):
    from resurgam_api.models.audit import AuditEvent

    event = AuditEvent(
        actor_user_id=actor_user_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        occurred_at=occurred_at or datetime.now(UTC),
    )
    db_session.add(event)
    return event


def test_platform_admin_lists_audit_events(client, platform_admin_headers, db_session):
    _insert_event(db_session, action="customer_created")
    _insert_event(db_session, action="customer_disabled")
    db_session.commit()

    response = client.get("/api/v1/audit-events", headers=platform_admin_headers)
    assert response.status_code == 200
    actions = {e["action"] for e in response.json()}
    assert {"customer_created", "customer_disabled"}.issubset(actions)


def test_non_platform_admin_is_forbidden(client, db_session, test_settings):
    from resurgam_api.auth.sessions import create_session_token
    from resurgam_api.models.identity import Role, RoleKey, ScopeType, User, UserRoleAssignment

    user = User(email="viewer@example.com", display_name="Viewer")
    db_session.add(user)
    db_session.flush()
    role = db_session.query(Role).filter_by(key=RoleKey.VIEWER.value).one()
    db_session.add(UserRoleAssignment(user_id=user.id, role_id=role.id, scope_type=ScopeType.GLOBAL.value))
    db_session.commit()

    token, _ = create_session_token(user.id, test_settings)
    response = client.get("/api/v1/audit-events", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403


def test_orders_most_recent_first(client, platform_admin_headers, db_session):
    _insert_event(db_session, action="a-older", occurred_at=datetime(2026, 1, 1, tzinfo=UTC))
    _insert_event(db_session, action="b-newer", occurred_at=datetime(2026, 2, 1, tzinfo=UTC))
    db_session.commit()

    response = client.get("/api/v1/audit-events", headers=platform_admin_headers)
    body = response.json()
    # other fixtures (e.g. logging in as platform_admin) also write real
    # audit events with "now" timestamps, so assert relative order rather
    # than absolute position.
    actions = [e["action"] for e in body]
    assert actions.index("b-newer") < actions.index("a-older")


def test_action_filter_applies_before_limit(client, platform_admin_headers, db_session):
    for i in range(3):
        _insert_event(db_session, action="noise", occurred_at=datetime(2026, 3, i + 1, tzinfo=UTC))
    _insert_event(db_session, action="target-action", occurred_at=datetime(2026, 1, 1, tzinfo=UTC))
    db_session.commit()

    response = client.get(
        "/api/v1/audit-events", params={"action": "target-action", "limit": 1}, headers=platform_admin_headers
    )
    body = response.json()
    assert len(body) == 1
    assert body[0]["action"] == "target-action"


def test_target_type_filter(client, platform_admin_headers, db_session):
    _insert_event(db_session, action="a", target_type="restore_plan", target_id="rp-1")
    _insert_event(db_session, action="b", target_type="customer", target_id="c-1")
    db_session.commit()

    response = client.get(
        "/api/v1/audit-events", params={"target_type": "restore_plan"}, headers=platform_admin_headers
    )
    body = response.json()
    assert all(e["target_type"] == "restore_plan" for e in body)
    assert len(body) == 1


def test_limit_rejects_out_of_range(client, platform_admin_headers):
    response = client.get("/api/v1/audit-events", params={"limit": 0}, headers=platform_admin_headers)
    assert response.status_code == 422

    response = client.get("/api/v1/audit-events", params={"limit": 5001}, headers=platform_admin_headers)
    assert response.status_code == 422


def test_actor_label_is_snapshotted_at_write_time(client, platform_admin_headers, db_session):
    """CISO audit finding (2026-09-23): `actor_user_id` alone goes NULL
    if its user is ever removed (`ON DELETE SET NULL`) -- `actor_label`
    must already have captured who it was at the moment the action
    happened, independent of whatever the `users` row says later.

    Doesn't exercise an actual user deletion here: this codebase has no
    hard-delete-user endpoint at all (only disable/enable, `users.py`),
    and a real attempt surfaced a second, deeper finding worth recording
    on its own -- `ON DELETE SET NULL` on `actor_user_id` fires an
    UPDATE against `audit_events` as part of the cascade, which the
    table's own append-only immutability trigger then rejects outright.
    So today that FK action is unreachable AND would fail loudly if it
    ever were reachable (e.g. a future GDPR erasure flow) -- an
    inherent tension between "immutable audit trail" and "the referenced
    user can go away" that a real hard-delete path would need to resolve
    by never issuing a raw DELETE against `users` for anyone with audit
    history, not by relying on the FK's cascade at all.
    """
    from resurgam_api.models.identity import User

    user = User(email="offboarded@example.com", display_name="Offboarded Operator")
    db_session.add(user)
    db_session.flush()
    event = _insert_event(db_session, action="restore_plan_created", actor_user_id=user.id)
    db_session.commit()

    assert event.actor_label == "Offboarded Operator (offboarded@example.com)"
    assert event.actor_is_breakglass is False

    response = client.get(
        "/api/v1/audit-events", params={"action": "restore_plan_created"}, headers=platform_admin_headers
    )
    body = next(e for e in response.json() if e["id"] == str(event.id))
    assert body["actor_label"] == "Offboarded Operator (offboarded@example.com)"


def test_actor_label_flags_breakglass_actor(client, platform_admin_headers, db_session):
    from resurgam_api.models.identity import User

    user = User(email="admin@breakglass.example", display_name="Break-glass (test)", is_breakglass=True)
    db_session.add(user)
    db_session.flush()
    event = _insert_event(db_session, action="restore_plan_created", actor_user_id=user.id)
    db_session.commit()

    assert event.actor_is_breakglass is True
