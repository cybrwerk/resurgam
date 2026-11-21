def test_validation_error_names_the_field_and_rule(client, platform_admin_headers):
    response = client.post(
        "/api/v1/customers",
        json={"name": "Acme AG", "slug": "Acme AG", "debitor_number": "D-1", "match_code": "ACM"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert body["message"].startswith("slug: ")
    assert body["details"]["errors"][0]["field"] == "slug"
    assert body["correlation_id"]


def test_validation_error_never_echoes_submitted_values(client, platform_admin_headers):
    # A wrong-typed field next to a secret-looking value: the response must
    # explain the problem without repeating what was sent.
    response = client.post(
        "/api/v1/pbs-instances",
        json={
            "display_name": "x",
            "host": "pbs.example",
            "port": "not-a-port",
            "api_token": "root@pam!t:super-secret-value",
        },
        headers=platform_admin_headers,
    )
    assert response.status_code == 422
    assert "super-secret-value" not in response.text
    assert "not-a-port" not in response.text
