import datetime
import hashlib
import http.server
import json
import os
import re
import ssl
import threading
from pathlib import Path

import bcrypt
import psycopg
import pytest
from alembic.config import Config
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from alembic import command
from resurgam_api.config import Settings, get_settings
from resurgam_api.db import get_db
from resurgam_api.main import create_app

BACKEND_ROOT = Path(__file__).parent.parent

# Deliberately a *different* database name than the interactive dev
# database (docker-compose.dev.yml / .env.example both default to
# "resurgam"), on the same Postgres server. Running tests against the same
# database an interactively-run server writes to lets real dev-session rows
# (e.g. manual curl smoke-testing) leak into test assertions that count
# rows — this happened once during development and produced flaky failures.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://resurgam:resurgam-dev-only@localhost:55432/resurgam_test",
)

TEST_BREAKGLASS_USERNAME = "test-admin"
TEST_BREAKGLASS_PASSWORD = "test-password"


def _ensure_database_exists(database_url: str) -> None:
    match = re.match(r"^(.*)://(.*)@([^/]+)/([^/?]+)", database_url)
    if not match:
        raise ValueError(f"cannot parse database URL: {database_url}")
    _, userinfo, hostport, dbname = match.groups()

    admin_conninfo = f"postgresql://{userinfo}@{hostport}/postgres"
    with psycopg.connect(admin_conninfo, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{dbname}"')


@pytest.fixture(scope="session")
def engine():
    _ensure_database_exists(TEST_DATABASE_URL)

    alembic_cfg = Config(str(BACKEND_ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(alembic_cfg, "head")

    eng = create_engine(TEST_DATABASE_URL)
    yield eng
    eng.dispose()


@pytest.fixture
def db_session(engine):
    """One SAVEPOINT-backed session per test; the outer transaction is
    rolled back at teardown so tests never leave data behind, even though
    application code calls session.commit() (which only releases/restarts
    the SAVEPOINT under join_transaction_mode='create_savepoint').
    """
    connection = engine.connect()
    outer_transaction = connection.begin()
    session_factory = sessionmaker(bind=connection, join_transaction_mode="create_savepoint")
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        outer_transaction.rollback()
        connection.close()


@pytest.fixture
def test_settings() -> Settings:
    password_hash = bcrypt.hashpw(TEST_BREAKGLASS_PASSWORD.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    return Settings(
        database_url=TEST_DATABASE_URL,
        breakglass_enabled=True,
        breakglass_username=TEST_BREAKGLASS_USERNAME,
        breakglass_password_hash=password_hash,
        session_secret="test-only-session-secret-at-least-32-bytes-long",
        # A fixed, valid Fernet key so encrypted-secret round-trips are
        # reproducible across test runs; not used outside tests.
        secrets_local_encryption_key="PIjcEQaYJrbfZSCc5nPrYO-qID45lH3qxeKsKt_ln44=",
        recovery_lab_public_backend_url="https://resurgam.test.invalid/api/v1",
    )


@pytest.fixture
def client(db_session, test_settings):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_settings] = lambda: test_settings
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def platform_admin_headers(client, db_session) -> dict:
    """Log in via break-glass, grant the caller a global platform_admin
    assignment directly in the test DB, and return Authorization headers.
    Shared by any test needing an authenticated platform_admin caller.
    """
    from resurgam_api.models.identity import Role, RoleKey, ScopeType, UserRoleAssignment

    login = client.post(
        "/api/v1/auth/breakglass-login",
        json={"username": TEST_BREAKGLASS_USERNAME, "password": TEST_BREAKGLASS_PASSWORD},
    )
    token = login.json()["access_token"]
    user_id = client.get("/api/v1/auth/whoami", headers={"Authorization": f"Bearer {token}"}).json()["user_id"]

    # Break-glass login grants itself global platform_admin (auth/breakglass.py);
    # kept explicit here so the fixture's guarantee doesn't hinge on that.
    role = db_session.query(Role).filter_by(key=RoleKey.PLATFORM_ADMIN.value).one()
    has_role = (
        db_session.query(UserRoleAssignment)
        .filter_by(user_id=user_id, role_id=role.id, scope_type=ScopeType.GLOBAL.value)
        .first()
    )
    if has_role is None:
        db_session.add(UserRoleAssignment(user_id=user_id, role_id=role.id, scope_type=ScopeType.GLOBAL.value))
        db_session.commit()

    return {"Authorization": f"Bearer {token}"}


class _FakeVersionHandler(http.server.BaseHTTPRequestHandler):
    """Serves a fake PBS/PVE `/api2/json/version` response for TLS-pinning
    and connectivity tests — both use the same path/response shape.
    """

    def do_GET(self):  # http.server's naming convention
        if self.path == "/api2/json/version":
            body = b'{"data": {"version": "4.2.1"}}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):  # noqa: A002 - matches base class signature
        pass


@pytest.fixture
def self_signed_cert(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
    )

    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    fingerprint = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    return str(cert_path), str(key_path), fingerprint


@pytest.fixture
def local_tls_server(self_signed_cert):
    """A real local TLS server backed by a freshly generated self-signed
    cert, for tests that must prove fingerprint pinning against an actual
    handshake rather than a mock.
    """
    cert_path, key_path, fingerprint = self_signed_cert

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=cert_path, keyfile=key_path)

    httpd = http.server.HTTPServer(("127.0.0.1", 0), _FakeVersionHandler)
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    host, port = httpd.server_address

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield host, port, fingerprint
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


# Shared by the lab provisioning and pairing tests: the image's shipped lab
# packages (/app/artifacts), with VERSION-matching fake .debs.
@pytest.fixture
def artifacts(tmp_path, test_settings):
    """A shipped-artifacts directory like the image's /app/artifacts."""
    directory = tmp_path / "artifacts"
    directory.mkdir()
    version = (Path(__file__).resolve().parents[2] / "VERSION").read_text().strip()
    entries = []
    for name in ("resurgam-lab-agent", "resurgam-lab-prober"):
        filename = f"{name}_{version}-1_amd64.deb"
        data = f"fake {name} package".encode() * 100
        (directory / filename).write_bytes(data)
        entries.append(
            {
                "name": name,
                "version": f"{version}-1",
                "architecture": "amd64",
                "filename": filename,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
            }
        )
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "release_version": version,
                "debian_release": "trixie",
                "debian_version_id": "13",
                "artifacts": entries,
            }
        )
    )
    test_settings.lab_artifacts_dir = str(directory)
    return {e["name"]: e for e in entries}
