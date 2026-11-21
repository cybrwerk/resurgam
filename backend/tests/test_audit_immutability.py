import pytest
from sqlalchemy.exc import DBAPIError

from resurgam_api.models.audit import AuditEvent


def test_audit_event_cannot_be_updated(db_session):
    event = AuditEvent(action="test_action", detail={"note": "original"})
    db_session.add(event)
    db_session.flush()

    event.detail = {"note": "tampered"}
    with pytest.raises(DBAPIError, match="immutable"):
        db_session.flush()


def test_audit_event_cannot_be_deleted(db_session):
    event = AuditEvent(action="test_action")
    db_session.add(event)
    db_session.flush()

    db_session.delete(event)
    with pytest.raises(DBAPIError, match="immutable"):
        db_session.flush()
