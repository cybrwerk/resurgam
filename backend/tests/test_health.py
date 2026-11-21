def test_health_ok(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_ok_with_db_up(client):
    response = client.get("/api/v1/readiness")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "database": "ok"}


def test_version(client):
    response = client.get("/api/v1/version")
    assert response.status_code == 200
    assert "version" in response.json()


def test_correlation_id_header_echoed(client):
    response = client.get("/api/v1/health", headers={"X-Request-ID": "test-correlation-id"})
    assert response.headers["X-Request-ID"] == "test-correlation-id"


def test_correlation_id_generated_when_absent(client):
    response = client.get("/api/v1/health")
    assert response.headers["X-Request-ID"]
