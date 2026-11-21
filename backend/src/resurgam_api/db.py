import logging
import time
from collections.abc import Callable, Generator

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from resurgam_api.config import get_settings


class Base(DeclarativeBase):
    pass


def build_engine(database_url: str | None = None):
    return create_engine(database_url or get_settings().database_url, pool_pre_ping=True)


_engine = None
_SessionLocal: sessionmaker | None = None


def _init_session_factory() -> sessionmaker:
    global _engine, _SessionLocal
    if _SessionLocal is None:
        _engine = build_engine()
        _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)
    return _SessionLocal


def get_session_factory() -> sessionmaker:
    """Public accessor for standalone processes with no FastAPI request
    lifecycle of their own -- e.g. `worker/file_restore_worker.py`, which
    needs to open its own short-lived sessions per polling cycle.
    """
    return _init_session_factory()


def get_db() -> Generator[Session, None, None]:
    session_factory = _init_session_factory()
    db = session_factory()
    try:
        yield db
    finally:
        db.close()


def wait_for_database(
    session_factory: sessionmaker,
    logger: logging.Logger,
    *,
    initial_delay: float = 2.0,
    max_delay: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Blocks until the database answers `SELECT 1`, retrying with
    backoff. For the native workers on the appliance: they start under
    systemd while Postgres runs in Docker, so at boot they can be up
    before the database is (confirmed live: one crash + restart per
    worker per boot)."""
    delay = initial_delay
    while True:
        try:
            with session_factory() as db:
                db.execute(text("SELECT 1"))
            return
        except OperationalError as exc:
            logger.warning("database not reachable yet, retrying in %.0fs (%s)", delay, exc.orig or exc)
            sleep(delay)
            delay = min(delay * 2, max_delay)
