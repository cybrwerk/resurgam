"""Release update source (ADR 0026): repository URL parsing, the GitHub and
GitLab release clients against an httpx.MockTransport, the settings API with
its write-only token, the cached check, and downloading a bundle into the
shared updates directory."""

import io
import json
import tarfile
import time

import httpx
import pytest

from resurgam_api import update_source as source_module
from resurgam_api.updates.release_source import (
    ReleaseSourceClient,
    ReleaseSourceError,
    guess_provider,
    parse_repository_url,
    version_from_tag,
)

GITHUB_URL = "https://github.com/cybrwerk/resurgam"
RELEASES_API = "https://api.github.com/repos/cybrwerk/resurgam/releases"
ASSET_API = "https://api.github.com/repos/cybrwerk/resurgam/releases/assets/7"
STORAGE_URL = "https://objects.githubusercontent.com/signed/resurgam-0.3.0.bundle?sig=abc"


def _bundle_bytes(version: str = "0.3.0") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, data in (
            (
                "manifest.json",
                json.dumps({"format": 1, "version": version, "min_upgrade_from": "0.2.0", "components": {}}).encode(),
            ),
            ("manifest.json.sig", b"\x00" * 64),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _github_release(tag: str, *, draft=False, prerelease=False, bundle=True) -> dict:
    version = tag.lstrip("v")
    assets = (
        [{"name": f"resurgam-{version}.bundle", "size": 1234, "state": "uploaded", "url": ASSET_API}] if bundle else []
    )
    return {
        "tag_name": tag,
        "name": f"RESURGAM {version}",
        "draft": draft,
        "prerelease": prerelease,
        "published_at": "2026-10-09T10:00:00Z",
        "body": f"Notes for {version}",
        "html_url": f"{GITHUB_URL}/releases/tag/{tag}",
        "assets": assets,
    }


class FakeGitHub:
    """Records every request; serves repo, releases, the asset redirect and the storage file."""

    def __init__(self, releases: list[dict] | None = None, bundle: bytes | None = None, status: int = 200):
        self.releases = releases if releases is not None else [_github_release("v0.3.0"), _github_release("v0.2.3")]
        self.bundle = bundle if bundle is not None else _bundle_bytes()
        self.status = status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if self.status != 200:
            return httpx.Response(self.status, json={"message": "nope"})
        if url.startswith(RELEASES_API + "?") or url == RELEASES_API:
            return httpx.Response(200, json=self.releases)
        if url == ASSET_API:
            return httpx.Response(302, headers={"location": STORAGE_URL})
        if url == STORAGE_URL:
            return httpx.Response(200, content=self.bundle)
        if url == "https://api.github.com/repos/cybrwerk/resurgam":
            return httpx.Response(200, json={"full_name": "cybrwerk/resurgam", "private": True})
        return httpx.Response(404, json={"message": "Not Found"})


@pytest.fixture
def fake_github(monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(source_module, "TRANSPORT", httpx.MockTransport(fake))
    return fake


@pytest.fixture
def running_020(monkeypatch):
    monkeypatch.setenv("RESURGAM_VERSION", "0.2.3")


@pytest.fixture
def updates_dir(tmp_path, test_settings):
    updates = tmp_path / "updates"
    updates.mkdir()
    test_settings.updates_dir = str(updates)
    test_settings.backups_dir = str(tmp_path / "backups")
    test_settings.release_public_key_file = str(tmp_path / "no-release-key.pub")
    return updates


# --- URL parsing ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "provider", "api_base", "path"),
    [
        (GITHUB_URL, "github", "https://api.github.com", "cybrwerk/resurgam"),
        (GITHUB_URL + ".git", "github", "https://api.github.com", "cybrwerk/resurgam"),
        (GITHUB_URL + "/releases", "github", "https://api.github.com", "cybrwerk/resurgam"),
        ("https://ghe.corp.example/team/resurgam", "github", "https://ghe.corp.example/api/v3", "team/resurgam"),
        ("https://gitlab.com/group/sub/resurgam/", "gitlab", "https://gitlab.com/api/v4", "group/sub/resurgam"),
        ("https://git.example.com/ops/resurgam/-/releases", "gitlab", "https://git.example.com/api/v4", "ops/resurgam"),
    ],
)
def test_parse_repository_url(url, provider, api_base, path):
    location = parse_repository_url(url, provider)
    assert (location.api_base, location.path) == (api_base, path)


@pytest.mark.parametrize("url", ["github.com/a/b", "https://github.com/onlyowner", "ftp://github.com/a/b"])
def test_parse_repository_url_rejects(url):
    with pytest.raises(ReleaseSourceError):
        parse_repository_url(url, "github")


def test_guess_provider_and_tag_versions():
    assert guess_provider(GITHUB_URL) == "github"
    assert guess_provider("https://gitlab.example.com/a/b") == "gitlab"
    assert guess_provider("https://git.example.com/a/b") is None
    assert version_from_tag("v0.3.0") == "0.3.0"
    assert version_from_tag("resurgam-1.10.2") == "1.10.2"
    assert version_from_tag("nightly") is None
    assert version_from_tag("v0.3.0-rc1") is None


# --- clients ----------------------------------------------------------------------


def test_github_lists_releases_skipping_drafts_and_unversioned_tags():
    fake = FakeGitHub(
        releases=[
            _github_release("v0.4.0", draft=True),
            _github_release("v0.3.1", prerelease=True),
            _github_release("v0.3.0"),
            _github_release("v0.2.10", bundle=False),
            {**_github_release("v9.9.9"), "tag_name": "nightly"},
        ]
    )
    client = ReleaseSourceClient(
        parse_repository_url(GITHUB_URL, "github"), "ghp_secret", transport=httpx.MockTransport(fake)
    )
    releases = client.list_releases()
    assert [r.version for r in releases] == ["0.3.1", "0.3.0", "0.2.10"]
    assert releases[0].prerelease is True
    assert releases[1].bundle_name == "resurgam-0.3.0.bundle" and releases[1].bundle_url == ASSET_API
    assert releases[2].bundle_name is None
    request = fake.requests[0]
    assert request.headers["authorization"] == "Bearer ghp_secret"
    assert request.headers["x-github-api-version"] == "2022-11-28"


def test_download_drops_the_token_on_the_redirect_to_storage(tmp_path):
    fake = FakeGitHub()
    client = ReleaseSourceClient(
        parse_repository_url(GITHUB_URL, "github"), "ghp_secret", transport=httpx.MockTransport(fake)
    )
    received = client.download(ASSET_API, tmp_path / "b.bundle", max_bytes=10_000_000)
    assert received == len(fake.bundle)
    asset_request, storage_request = fake.requests
    assert asset_request.headers["authorization"] == "Bearer ghp_secret"
    assert asset_request.headers["accept"] == "application/octet-stream"
    assert "authorization" not in storage_request.headers


def test_download_enforces_the_size_limit(tmp_path):
    client = ReleaseSourceClient(
        parse_repository_url(GITHUB_URL, "github"), None, transport=httpx.MockTransport(FakeGitHub())
    )
    with pytest.raises(ReleaseSourceError) as excinfo:
        client.download(ASSET_API, tmp_path / "b.bundle", max_bytes=100)
    assert excinfo.value.code == "bundle_too_large"


def test_gitlab_uses_private_token_and_encoded_project_path():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=[
                {
                    "tag_name": "v0.3.0",
                    "name": "0.3.0",
                    "description": "Notes",
                    "released_at": "2026-10-09T10:00:00Z",
                    "upcoming_release": False,
                    "assets": {
                        "links": [
                            {
                                "name": "resurgam-0.3.0.bundle",
                                "url": "https://gitlab.example.com/api/v4/projects/5/packages/generic/resurgam/0.3.0/resurgam-0.3.0.bundle",
                                "direct_asset_url": "https://gitlab.example.com/ops/resurgam/-/releases/v0.3.0/downloads/resurgam-0.3.0.bundle",
                            }
                        ]
                    },
                },
                {"tag_name": "v0.4.0", "upcoming_release": True, "assets": {"links": []}},
            ],
        )

    client = ReleaseSourceClient(
        parse_repository_url("https://gitlab.example.com/ops/resurgam", "gitlab"),
        "glpat-secret",
        transport=httpx.MockTransport(handler),
    )
    releases = client.list_releases()
    assert [r.version for r in releases] == ["0.3.0"]
    assert releases[0].bundle_url.endswith("/-/releases/v0.3.0/downloads/resurgam-0.3.0.bundle")
    assert seen[0].url.raw_path.startswith(b"/api/v4/projects/ops%2Fresurgam/releases")
    assert seen[0].headers["private-token"] == "glpat-secret"


@pytest.mark.parametrize(
    ("status", "code"), [(401, "token_rejected"), (403, "token_forbidden"), (404, "repository_not_found")]
)
def test_http_errors_become_operator_messages(status, code):
    client = ReleaseSourceClient(
        parse_repository_url(GITHUB_URL, "github"), None, transport=httpx.MockTransport(FakeGitHub(status=status))
    )
    with pytest.raises(ReleaseSourceError) as excinfo:
        client.list_releases()
    assert excinfo.value.code == code


# --- settings API -----------------------------------------------------------------


def _save(client, headers, **overrides):
    body = {"provider": "github", "repository_url": GITHUB_URL, "token": "ghp_secret", **overrides}
    return client.put("/api/v1/updates/source", json=body, headers=headers)


def test_unconfigured_source_prefills_resurgams_repository(client, platform_admin_headers):
    response = client.get("/api/v1/updates/source", headers=platform_admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is False
    assert body["provider"] == "github"
    assert body["repository_url"] == GITHUB_URL


def test_token_is_write_only_and_three_way(client, platform_admin_headers, db_session, test_settings):
    response = _save(client, platform_admin_headers)
    assert response.status_code == 200, response.text
    assert response.json()["token_configured"] is True
    assert "ghp_secret" not in response.text

    row = source_module.get_update_source(db_session)
    assert source_module.resolve_token(db_session, test_settings, row) == "ghp_secret"

    body = {"provider": "github", "repository_url": GITHUB_URL, "auto_check": False}
    response = client.put("/api/v1/updates/source", json=body, headers=platform_admin_headers)
    assert response.json()["token_configured"] is True
    assert response.json()["auto_check"] is False

    response = client.put("/api/v1/updates/source", json={**body, "token": None}, headers=platform_admin_headers)
    assert response.json()["token_configured"] is False


def test_save_rejects_a_url_that_names_no_repository(client, platform_admin_headers):
    response = _save(client, platform_admin_headers, repository_url="https://github.com/cybrwerk")
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_repository_url"


def test_test_endpoint_uses_body_token_before_saving(client, platform_admin_headers, fake_github):
    response = client.post(
        "/api/v1/updates/source/test",
        json={"provider": "github", "repository_url": GITHUB_URL, "token": "ghp_new"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "repository": "cybrwerk/resurgam",
        "private": True,
        "releases_found": 2,
        "latest_release": "0.3.0",
        "error": None,
        "stored_token_withheld": False,
    }
    assert all(r.headers["authorization"] == "Bearer ghp_new" for r in fake_github.requests)


def test_test_endpoint_reports_failure_as_ok_false(client, platform_admin_headers, fake_github):
    fake_github.status = 401
    response = client.post(
        "/api/v1/updates/source/test",
        json={"provider": "github", "repository_url": GITHUB_URL, "token": "bad"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "rejected the access token" in response.json()["error"]


def test_check_caches_newer_releases_and_keeps_them_on_failure(
    client, platform_admin_headers, fake_github, running_020
):
    assert client.post("/api/v1/updates/source/check", headers=platform_admin_headers).status_code == 409
    _save(client, platform_admin_headers)
    body = client.post("/api/v1/updates/source/check", headers=platform_admin_headers).json()
    assert body["latest_version"] == "0.3.0"
    assert [r["version"] for r in body["available_releases"]] == ["0.3.0"]
    assert body["last_check_error"] is None and body["last_checked_at"]

    fake_github.status = 500
    body = client.post("/api/v1/updates/source/check", headers=platform_admin_headers).json()
    assert "HTTP 500" in body["last_check_error"]
    assert [r["version"] for r in body["available_releases"]] == ["0.3.0"]


def test_cached_releases_hide_versions_no_longer_newer(
    client, platform_admin_headers, fake_github, running_020, monkeypatch
):
    _save(client, platform_admin_headers)
    client.post("/api/v1/updates/source/check", headers=platform_admin_headers)
    monkeypatch.setenv("RESURGAM_VERSION", "0.3.0")
    body = client.get("/api/v1/updates/source", headers=platform_admin_headers).json()
    assert body["available_releases"] == [] and body["latest_version"] is None


def test_scheduler_check_respects_auto_check_and_interval(
    client, platform_admin_headers, fake_github, running_020, db_session, test_settings
):
    _save(client, platform_admin_headers, auto_check=False)
    assert source_module.run_due_check(db_session, test_settings) is False
    _save(client, platform_admin_headers, auto_check=True, check_interval_hours=6)
    assert source_module.run_due_check(db_session, test_settings) is True
    assert source_module.run_due_check(db_session, test_settings) is False  # checked just now


def _wait_for_download(client, headers, download_id: str) -> dict:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status = client.get(f"/api/v1/updates/source/downloads/{download_id}", headers=headers).json()
        if status["state"] != "running":
            return status
        time.sleep(0.05)
    raise AssertionError("download did not finish")


def test_download_lands_in_incoming_and_is_listed_as_a_bundle(
    client, platform_admin_headers, fake_github, running_020, updates_dir
):
    _save(client, platform_admin_headers)
    client.post("/api/v1/updates/source/check", headers=platform_admin_headers)
    response = client.post(
        "/api/v1/updates/source/downloads", json={"version": "0.3.0"}, headers=platform_admin_headers
    )
    assert response.status_code == 202, response.text
    status = _wait_for_download(client, platform_admin_headers, response.json()["download_id"])
    assert status["state"] == "succeeded", status
    assert status["bundle"].startswith("resurgam-0.3.0-") and status["bundle"].endswith(".bundle")
    bundles = client.get("/api/v1/updates/bundles", headers=platform_admin_headers).json()
    assert [(b["name"], b["version"]) for b in bundles] == [(status["bundle"], "0.3.0")]
    assert not list((updates_dir / "incoming").glob(".*.part"))


def test_download_rejects_a_bundle_whose_manifest_version_differs(
    client, platform_admin_headers, fake_github, running_020, updates_dir
):
    fake_github.bundle = _bundle_bytes("0.2.9")
    _save(client, platform_admin_headers)
    client.post("/api/v1/updates/source/check", headers=platform_admin_headers)
    response = client.post(
        "/api/v1/updates/source/downloads", json={"version": "0.3.0"}, headers=platform_admin_headers
    )
    status = _wait_for_download(client, platform_admin_headers, response.json()["download_id"])
    assert status["state"] == "failed"
    assert "contains version 0.2.9" in status["message"]
    assert not list((updates_dir / "incoming").iterdir())


def test_download_of_unknown_or_bundleless_release(
    client, platform_admin_headers, fake_github, running_020, updates_dir
):
    fake_github.releases = [_github_release("v0.3.0", bundle=False)]
    _save(client, platform_admin_headers)
    client.post("/api/v1/updates/source/check", headers=platform_admin_headers)
    missing = client.post("/api/v1/updates/source/downloads", json={"version": "0.9.0"}, headers=platform_admin_headers)
    assert missing.status_code == 404
    no_bundle = client.post(
        "/api/v1/updates/source/downloads", json={"version": "0.3.0"}, headers=platform_admin_headers
    )
    assert no_bundle.status_code == 409
    assert no_bundle.json()["code"] == "release_has_no_bundle"


def test_download_needs_the_appliance(client, platform_admin_headers, test_settings, tmp_path):
    test_settings.updates_dir = str(tmp_path / "missing")
    response = client.post(
        "/api/v1/updates/source/downloads", json={"version": "0.3.0"}, headers=platform_admin_headers
    )
    assert response.status_code == 503


def test_endpoints_require_platform_admin(client):
    assert client.get("/api/v1/updates/source").status_code == 401
    assert client.put("/api/v1/updates/source", json={}).status_code == 401
    assert client.post("/api/v1/updates/source/check").status_code == 401


# --- token scope, redirects, download housekeeping (review fixes) ------------------


def test_test_endpoint_uses_the_stored_token_only_for_the_saved_host(client, platform_admin_headers, fake_github):
    _save(client, platform_admin_headers)
    seen: list[httpx.Request] = []

    def elsewhere(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(404, json={"message": "Not Found"})

    # Same host: the stored token is used.
    response = client.post(
        "/api/v1/updates/source/test",
        json={"provider": "github", "repository_url": GITHUB_URL},
        headers=platform_admin_headers,
    )
    assert response.json()["ok"] is True and response.json()["stored_token_withheld"] is False
    assert fake_github.requests[-1].headers["authorization"] == "Bearer ghp_secret"

    # Another host: the test runs without it and says so.
    source_module.TRANSPORT = httpx.MockTransport(elsewhere)
    response = client.post(
        "/api/v1/updates/source/test",
        json={"provider": "gitlab", "repository_url": "https://attacker.example/group/project"},
        headers=platform_admin_headers,
    )
    assert response.json()["ok"] is False
    assert response.json()["stored_token_withheld"] is True
    assert seen and all("private-token" not in r.headers and "authorization" not in r.headers for r in seen)


def test_save_clears_the_token_when_the_api_host_changes(client, platform_admin_headers, db_session, test_settings):
    _save(client, platform_admin_headers)
    # Another repository on the same host keeps the token.
    body = {"provider": "github", "repository_url": "https://github.com/cybrwerk/other"}
    response = client.put("/api/v1/updates/source", json=body, headers=platform_admin_headers)
    assert response.json()["token_configured"] is True and response.json()["token_cleared"] is False

    body = {"provider": "github", "repository_url": "https://ghe.corp.example/team/resurgam"}
    response = client.put("/api/v1/updates/source", json=body, headers=platform_admin_headers)
    assert response.status_code == 200
    assert response.json()["token_configured"] is False
    assert response.json()["token_cleared"] is True
    row = source_module.get_update_source(db_session)
    assert source_module.resolve_token(db_session, test_settings, row) is None

    # A new token sent with the host change is simply stored.
    response = _save(client, platform_admin_headers, token="ghp_other")
    assert response.json()["token_configured"] is True and response.json()["token_cleared"] is False


def test_token_is_not_sent_over_plain_http_to_the_api_host():
    client = ReleaseSourceClient(parse_repository_url(GITHUB_URL, "github"), "ghp_secret")
    assert client._auth_headers(ASSET_API) == {"Authorization": "Bearer ghp_secret"}
    assert client._auth_headers(ASSET_API.replace("https://", "http://")) == {}


def test_download_refuses_a_redirect_to_plain_http(tmp_path):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if str(request.url) == ASSET_API:
            return httpx.Response(302, headers={"location": ASSET_API.replace("https://", "http://")})
        return httpx.Response(200, content=b"bundle")

    client = ReleaseSourceClient(
        parse_repository_url(GITHUB_URL, "github"), "ghp_secret", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(ReleaseSourceError) as excinfo:
        client.download(ASSET_API, tmp_path / "b.bundle", max_bytes=1000)
    assert excinfo.value.code == "insecure_redirect"
    assert len(seen) == 1


def test_download_error_does_not_echo_the_response_body(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == ASSET_API:
            return httpx.Response(302, headers={"location": STORAGE_URL})
        return httpx.Response(500, text="<Error>secret-signed-url-details</Error>")

    client = ReleaseSourceClient(
        parse_repository_url(GITHUB_URL, "github"), None, transport=httpx.MockTransport(handler)
    )
    with pytest.raises(ReleaseSourceError) as excinfo:
        client.download(ASSET_API, tmp_path / "b.bundle", max_bytes=1000)
    assert "HTTP 500" in excinfo.value.message
    assert "secret-signed-url-details" not in excinfo.value.message


def test_changing_include_prereleases_drops_the_cached_list(
    client, platform_admin_headers, fake_github, running_020, db_session
):
    fake_github.releases = [_github_release("v0.3.1", prerelease=True), _github_release("v0.3.0")]
    _save(client, platform_admin_headers, include_prereleases=True)
    body = client.post("/api/v1/updates/source/check", headers=platform_admin_headers).json()
    assert [r["version"] for r in body["available_releases"]] == ["0.3.1", "0.3.0"]

    body = _save(client, platform_admin_headers, include_prereleases=False).json()
    assert body["available_releases"] == [] and body["last_checked_at"] is None

    # Even a list cached with pre-releases is filtered by the row's setting.
    row = source_module.get_update_source(db_session)
    row.available_releases = [
        r.to_dict()
        for r in ReleaseSourceClient(
            parse_repository_url(GITHUB_URL, "github"), None, transport=httpx.MockTransport(fake_github)
        ).list_releases()
    ]
    db_session.commit()
    assert [r["version"] for r in source_module.cached_releases(row)] == ["0.3.0"]


def _store(updates_dir):
    from resurgam_api.appliance_updates import UpdatesStore

    store = UpdatesStore(str(updates_dir), str(updates_dir.parent / "backups"))
    store.incoming.mkdir(parents=True, exist_ok=True)
    return store


def test_stale_download_part_files_and_old_statuses_are_swept(updates_dir):
    import os

    store = _store(updates_dir)
    downloads = updates_dir / "downloads"
    downloads.mkdir(exist_ok=True)
    old = time.time() - source_module.DOWNLOAD_STALE_SECONDS - 60
    ancient = time.time() - (source_module.DOWNLOAD_RETENTION_DAYS + 1) * 86400

    stale_id, live_id, orphan_id, done_id = "a" * 32, "b" * 32, "c" * 32, "d" * 32
    for download_id, state, mtime in (
        (stale_id, "running", old),
        (live_id, "running", None),
        (done_id, "succeeded", ancient),
    ):
        path = downloads / f"{download_id}.json"
        path.write_text(json.dumps({"download_id": download_id, "state": state, "started_at": "x"}))
        if mtime is not None:
            os.utime(path, (mtime, mtime))
    for download_id in (stale_id, live_id, orphan_id):
        (store.incoming / f".{download_id}.part").write_bytes(b"partial")

    downloads_listed = source_module.list_downloads(store)
    states = {d["download_id"]: d["state"] for d in downloads_listed}
    assert states == {stale_id: "failed", live_id: "running"}
    assert not (store.incoming / f".{stale_id}.part").exists()
    assert not (store.incoming / f".{orphan_id}.part").exists()
    assert (store.incoming / f".{live_id}.part").exists()
    assert not (downloads / f"{done_id}.json").exists()


def test_heartbeat_keeps_the_status_file_fresh(updates_dir, monkeypatch):
    import os

    store = _store(updates_dir)
    monkeypatch.setattr(source_module, "_HEARTBEAT_SECONDS", 0.02)
    download_id = "e" * 32
    path = updates_dir / "downloads" / f"{download_id}.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"download_id": download_id, "state": "running"}))
    old = time.time() - source_module.DOWNLOAD_STALE_SECONDS - 60
    os.utime(path, (old, old))
    with source_module._heartbeat(store, download_id):
        time.sleep(0.2)
    assert source_module.get_download(store, download_id)["state"] == "running"


def test_second_download_start_is_refused_while_one_runs(updates_dir, test_settings):
    store = _store(updates_dir)
    release = {"version": "0.3.0", "bundle_url": ASSET_API, "bundle_name": "resurgam-0.3.0.bundle"}

    class NeverCalled:
        pass

    source_module._write_status(store, {"download_id": "f" * 32, "state": "running", "version": "0.3.0"})
    with pytest.raises(source_module.UpdatesError) as excinfo:
        source_module.start_download(store, test_settings, NeverCalled(), release, "admin@example.com")
    assert excinfo.value.code == "download_in_progress"
