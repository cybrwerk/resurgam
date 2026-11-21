from typing import Literal

from pydantic import BaseModel, Field, field_validator

UpdateSourceProvider = Literal["github", "gitlab"]
DownloadState = Literal["running", "succeeded", "failed"]

_PROVIDER_DESCRIPTION = (
    "Where the repository lives: `github` (github.com, or a GitHub Enterprise Server host) or `gitlab` "
    "(gitlab.com or a self-hosted GitLab). The UI pre-selects it from the URL's host name."
)
_URL_DESCRIPTION = (
    "The repository's web address as shown in the browser, e.g. `https://github.com/cybrwerk/resurgam` "
    "or `https://gitlab.example.com/group/project`. The API address is derived from it; a trailing `.git` or a "
    "pasted `/-/releases` page is fine."
)
_TOKEN_DESCRIPTION = (
    "Personal access token for a private repository. GitHub: a fine-grained token with read-only access to this "
    "repository's Contents (or a classic token with `repo`). GitLab: `read_api` (an asset in GitLab's package "
    "registry may need `api`). Public repositories need none -- but without a token GitHub allows only 60 API "
    "requests per hour per IP."
)


def _strip_url(value: str) -> str:
    value = value.strip()
    if not value.lower().startswith(("https://", "http://")):
        raise ValueError("expected an http(s) URL, e.g. https://github.com/owner/repository")
    return value


class UpdateSourceSettingsRequest(BaseModel):
    provider: UpdateSourceProvider = Field(description=_PROVIDER_DESCRIPTION)
    repository_url: str = Field(min_length=1, max_length=500, description=_URL_DESCRIPTION)
    token: str | None = Field(
        default=None,
        max_length=1000,
        description=_TOKEN_DESCRIPTION + " Omit to keep the stored token unchanged; send null explicitly to remove it.",
    )
    tls_verify: bool = Field(
        default=True,
        description="Verify the server's TLS certificate. Turn off only for a self-hosted server with a private CA "
        "you trust -- the bundle signature is checked either way, but the token would travel unprotected.",
    )
    include_prereleases: bool = Field(
        default=False,
        description="Also offer releases GitHub marks as pre-release. GitLab has no such flag; every release counts.",
    )
    auto_check: bool = Field(
        default=True,
        description="Check for new releases automatically (by the collector scheduler) every "
        "`check_interval_hours`. Nothing is downloaded or installed automatically.",
    )
    check_interval_hours: int = Field(default=24, ge=1, le=720, description="Hours between automatic checks (1-720).")

    @field_validator("repository_url")
    @classmethod
    def _url(cls, value: str) -> str:
        return _strip_url(value)


class UpdateSourceTestRequest(BaseModel):
    provider: UpdateSourceProvider = Field(description=_PROVIDER_DESCRIPTION)
    repository_url: str = Field(min_length=1, max_length=500, description=_URL_DESCRIPTION)
    token: str | None = Field(
        default=None,
        max_length=1000,
        description=_TOKEN_DESCRIPTION + " Omit to test with the stored token (if any); send null to test without one.",
    )
    tls_verify: bool = Field(default=True, description="Verify the server's TLS certificate.")

    @field_validator("repository_url")
    @classmethod
    def _url(cls, value: str) -> str:
        return _strip_url(value)


class UpdateSourceReleaseOut(BaseModel):
    version: str = Field(description="Release version derived from the tag (e.g. tag `v0.3.0` -> `0.3.0`).")
    tag_name: str = Field(description="The release's git tag as named on GitHub/GitLab.")
    name: str | None = Field(default=None, description="The release title, if one was set.")
    released_at: str | None = Field(default=None, description="When the release was published (ISO 8601).")
    notes: str | None = Field(default=None, description="Release notes (Markdown) as written on GitHub/GitLab.")
    web_url: str | None = Field(default=None, description="Link to the release page in the browser.")
    prerelease: bool = Field(default=False, description="Marked as pre-release on GitHub (always false on GitLab).")
    bundle_name: str | None = Field(
        default=None,
        description="File name of the attached update bundle (`resurgam-<version>.bundle`). Null when the release "
        "has no bundle attached -- it is then shown but cannot be installed from here.",
    )
    bundle_size: int | None = Field(default=None, description="Bundle size in bytes, when the source reports it.")


class UpdateSourceDownloadOut(BaseModel):
    download_id: str = Field(
        description="Id of this download (32 hex characters); poll GET /updates/source/downloads/{id}."
    )
    version: str = Field(description="Release version being downloaded.")
    source_asset: str | None = Field(default=None, description="Asset file name on GitHub/GitLab.")
    state: DownloadState = Field(
        description="`running` while bytes arrive, `succeeded` once the bundle is in incoming/ (verify it next), "
        "`failed` with the reason in `message`."
    )
    bytes_done: int = Field(description="Bytes received so far.")
    bytes_total: int | None = Field(default=None, description="Expected size in bytes, when known.")
    bundle: str | None = Field(
        default=None,
        description="When succeeded: the bundle's file name in incoming/, to pass as `bundle` to POST /updates/requests.",
    )
    signature_prechecked: bool = Field(
        default=False,
        description="True when the downloaded bundle's signature already matched the release key baked into the "
        "backend image. The host updater's verify stays authoritative.",
    )
    message: str | None = Field(default=None, description="Why the download failed, when it did.")
    requested_by: str | None = Field(default=None, description="Email of the admin who started it.")
    started_at: str | None = Field(default=None, description="When the download started (ISO 8601).")
    finished_at: str | None = Field(default=None, description="When it succeeded or failed (ISO 8601).")


class UpdateSourceOut(BaseModel):
    configured: bool = Field(
        description="Whether an update source has been saved. False: every other field is a default."
    )
    provider: UpdateSourceProvider = Field(description=_PROVIDER_DESCRIPTION)
    repository_url: str = Field(description="The saved repository web address (empty when not configured).")
    token_configured: bool = Field(description="Whether an access token is stored. The token itself is never returned.")
    tls_verify: bool = Field(description="Whether the server's TLS certificate is verified.")
    include_prereleases: bool = Field(description="Whether GitHub pre-releases are offered too.")
    auto_check: bool = Field(description="Whether new releases are looked for automatically.")
    check_interval_hours: int = Field(description="Hours between automatic checks.")
    current_version: str = Field(description="The running RESURGAM version releases are compared against.")
    last_checked_at: str | None = Field(default=None, description="When the source was last checked (ISO 8601).")
    last_check_error: str | None = Field(
        default=None,
        description="Why the last check failed, null when it succeeded. The release list then still shows the "
        "result of the last successful check.",
    )
    latest_version: str | None = Field(
        default=None, description="The newest release above the running version, null when up to date."
    )
    available_releases: list[UpdateSourceReleaseOut] = Field(
        default_factory=list, description="Releases newer than the running version, newest first."
    )
    active_download: UpdateSourceDownloadOut | None = Field(
        default=None, description="The bundle download in progress, if any (lets the UI resume after a reload)."
    )
    token_cleared: bool = Field(
        default=False,
        description="Only in the answer to PUT /updates/source: true when the save moved the source to another "
        "provider or API host without a new token, so the stored token was removed instead of being sent to a "
        "host it was not saved for. Enter the token for the new source again if it needs one.",
    )


class UpdateSourceTestOut(BaseModel):
    ok: bool = Field(description="Whether the repository and its releases could be read.")
    repository: str | None = Field(default=None, description="The repository's full name as the source reports it.")
    private: bool | None = Field(default=None, description="Whether the repository is private.")
    releases_found: int | None = Field(
        default=None, description="Published releases with a version tag (MAJOR.MINOR.PATCH) found."
    )
    latest_release: str | None = Field(default=None, description="Newest release version found, newer or not.")
    error: str | None = Field(default=None, description="What failed and what to check, when `ok` is false.")
    stored_token_withheld: bool = Field(
        default=False,
        description="True when `token` was omitted and a token is stored, but for another provider or API host "
        "than the one tested: the test ran without a token instead of sending the stored one elsewhere.",
    )


class UpdateSourceDownloadCreate(BaseModel):
    version: str = Field(
        min_length=1,
        max_length=64,
        description="Release version to download, one of `available_releases[].version` from GET /updates/source.",
    )
