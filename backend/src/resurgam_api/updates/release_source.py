"""Release sources for appliance updates (docs/adr/0026): find RESURGAM
releases on GitHub or GitLab and download their signed update bundle.

The operator pastes one repository URL (``https://github.com/owner/repo``
or ``https://gitlab.example.com/group/project``) and, for a private
repository, a personal access token. Everything provider-specific -- API
base URL, auth header, release/asset JSON shapes, how an asset is
downloaded -- stays in this module; callers only see :class:`ReleaseInfo`.

Trust does not come from here: a downloaded bundle goes through the same
Ed25519 signature check as an uploaded one (ADR 0024), so a compromised
repository or token can at most offer a bundle the updater then rejects.
The token is only ever sent to the repository's own API host, over the
API base's own scheme -- never to the object storage a download redirects
to (GitHub answers an asset download with a 302 to a pre-signed URL on
another host), and a download never follows a redirect from https to
plain http.

Like the rest of ``resurgam_api.updates`` this module has no FastAPI or
database imports.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx

from resurgam_api.updates.bundle import BundleError, compare_versions, parse_version

PROVIDERS = ("github", "gitlab")
GITHUB_PUBLIC_HOST = "github.com"
GITHUB_PUBLIC_API = "https://api.github.com"
# Enough releases to cover any realistic gap between the running version
# and the newest one; both APIs return newest first.
RELEASES_PER_PAGE = 50
_MAX_REDIRECTS = 5
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_DOWNLOAD_CHUNK = 1024 * 1024

# "v0.2.3", "0.2.3", "resurgam-0.2.3" -> 0.2.3. Only plain MAJOR.MINOR.PATCH
# is a release version (updates/bundle.py), anything else is not offered.
_TAG_VERSION_RE = re.compile(r"(?:^|[^0-9.])v?(\d+\.\d+\.\d+)$")


class ReleaseSourceError(Exception):
    """A problem talking to the release source, with a message meant for the
    operator (what failed and what to check)."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class RepoLocation:
    provider: str
    web_url: str
    api_base: str
    # GitHub: "owner/repo"; GitLab: the full project path "group/sub/project".
    path: str

    @property
    def api_host(self) -> str:
        return urlsplit(self.api_base).netloc.lower()

    @property
    def api_scheme(self) -> str:
        return urlsplit(self.api_base).scheme.lower()


@dataclass
class ReleaseInfo:
    version: str
    tag_name: str
    name: str | None
    released_at: str | None
    notes: str | None
    web_url: str | None
    prerelease: bool
    bundle_name: str | None
    bundle_size: int | None
    # Where the bundle is fetched from: the GitHub asset API URL (answers
    # with the binary or a redirect when asked for application/octet-stream)
    # or the GitLab release link URL.
    bundle_url: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def guess_provider(repository_url: str) -> str | None:
    """'github'/'gitlab' when the host name says so, else None (a
    self-hosted instance under a neutral name -- the operator picks)."""
    host = urlsplit(repository_url.strip()).netloc.lower()
    if "github" in host:
        return "github"
    if "gitlab" in host:
        return "gitlab"
    return None


def parse_repository_url(repository_url: str, provider: str) -> RepoLocation:
    """Derive the API base and repository path from the repository's web URL.

    GitHub: ``https://github.com/<owner>/<repo>`` uses ``api.github.com``; any
    other host is GitHub Enterprise Server (``https://<host>/api/v3``).
    GitLab: ``https://<host>/<group>/.../<project>`` uses ``https://<host>/api/v4``.
    A trailing ``.git``, a trailing slash and GitLab's ``/-/...`` page suffix
    (e.g. a pasted ``/-/releases`` URL) are ignored.
    """
    if provider not in PROVIDERS:
        raise ReleaseSourceError("invalid_provider", f"Unknown provider {provider!r}; expected github or gitlab.")
    parts = urlsplit(repository_url.strip())
    if parts.scheme not in ("https", "http") or not parts.netloc:
        raise ReleaseSourceError(
            "invalid_repository_url",
            f"{repository_url!r} is not a repository URL. Paste the repository's web address, "
            "e.g. https://github.com/owner/repository.",
        )
    path = parts.path.split("/-/", 1)[0].strip("/")
    path = path.removesuffix(".git")
    segments = [s for s in path.split("/") if s]
    host = parts.netloc.lower()
    origin = f"{parts.scheme}://{parts.netloc}"

    if provider == "github":
        if len(segments) < 2:
            raise ReleaseSourceError(
                "invalid_repository_url",
                f"{repository_url!r} does not name a GitHub repository -- expected https://{host}/<owner>/<repository>.",
            )
        owner, repo = segments[0], segments[1]
        api_base = (
            GITHUB_PUBLIC_API if host in (GITHUB_PUBLIC_HOST, f"www.{GITHUB_PUBLIC_HOST}") else f"{origin}/api/v3"
        )
        return RepoLocation("github", f"{origin}/{owner}/{repo}", api_base, f"{owner}/{repo}")

    if len(segments) < 2:
        raise ReleaseSourceError(
            "invalid_repository_url",
            f"{repository_url!r} does not name a GitLab project -- expected https://{host}/<group>/<project>.",
        )
    project = "/".join(segments)
    return RepoLocation("gitlab", f"{origin}/{project}", f"{origin}/api/v4", project)


def version_from_tag(tag: str) -> str | None:
    match = _TAG_VERSION_RE.search(tag.strip())
    if not match:
        return None
    try:
        parse_version(match[1])
    except BundleError:
        return None
    return match[1]


def _pick_bundle(assets: list[tuple[str, int | None, str]], version: str) -> tuple[str, int | None, str] | None:
    """The release's update bundle: ``resurgam-<version>.bundle`` (what
    ``build-bundle.sh`` produces), else the only ``*.bundle`` asset."""
    exact = [a for a in assets if a[0] == f"resurgam-{version}.bundle"]
    if exact:
        return exact[0]
    bundles = [a for a in assets if a[0].endswith(".bundle")]
    return bundles[0] if len(bundles) == 1 else None


class ReleaseSourceClient:
    def __init__(
        self,
        location: RepoLocation,
        token: str | None,
        *,
        tls_verify: bool = True,
        user_agent: str = "RESURGAM",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.location = location
        self.token = token or None
        self.tls_verify = tls_verify
        self.user_agent = user_agent
        self.transport = transport

    # --- plumbing ---------------------------------------------------------

    def _client(self) -> httpx.Client:
        return httpx.Client(
            verify=self.tls_verify,
            timeout=_TIMEOUT,
            transport=self.transport,
            follow_redirects=False,
            headers={"User-Agent": self.user_agent},
        )

    def _auth_headers(self, url: str) -> dict[str, str]:
        """The token, but only for the repository's own API host and only
        over the API base's scheme (never plain http when the API is https)."""
        parts = urlsplit(url)
        if (
            not self.token
            or parts.netloc.lower() != self.location.api_host
            or parts.scheme.lower() != self.location.api_scheme
        ):
            return {}
        if self.location.provider == "github":
            return {"Authorization": f"Bearer {self.token}"}
        return {"PRIVATE-TOKEN": self.token}

    def _api_headers(self) -> dict[str, str]:
        if self.location.provider == "github":
            return {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        return {"Accept": "application/json"}

    def _repo_api_path(self) -> str:
        if self.location.provider == "github":
            return f"/repos/{self.location.path}"
        return f"/projects/{quote(self.location.path, safe='')}"

    def _raise_for(self, response: httpx.Response, what: str, *, include_body: bool = True) -> None:
        status = response.status_code
        if status < 400:
            return
        provider = "GitHub" if self.location.provider == "github" else "GitLab"
        if status == 401:
            raise ReleaseSourceError(
                "token_rejected",
                f"{provider} rejected the access token while reading {what} (HTTP 401). "
                "It may be expired or revoked -- create a new one and save it here.",
            )
        if status == 403 and response.headers.get("x-ratelimit-remaining") == "0":
            raise ReleaseSourceError(
                "rate_limited",
                f"{provider}'s API rate limit is exhausted (HTTP 403). Without a token GitHub allows 60 requests "
                "per hour per IP; save an access token to raise the limit, or check again later.",
            )
        if status == 403:
            raise ReleaseSourceError(
                "token_forbidden",
                f"{provider} refused access to {what} (HTTP 403). The token lacks the needed permission -- "
                + (
                    "a fine-grained token needs read access to this repository's Contents."
                    if self.location.provider == "github"
                    else "it needs the read_api scope (downloading a package-registry asset may need api)."
                ),
            )
        if status == 404:
            raise ReleaseSourceError(
                "repository_not_found",
                f"{provider} answered 404 for {what}. Check the repository URL; a private repository also "
                "answers 404 when no token, or a token without access to it, is saved.",
            )
        if not include_body:
            # A download's error body comes from wherever the redirect chain
            # ended (object storage, a proxy) -- only the status is reported.
            raise ReleaseSourceError("source_error", f"{provider} answered HTTP {status} for {what}.")
        raise ReleaseSourceError("source_error", f"{provider} answered HTTP {status} for {what}: {response.text[:300]}")

    def _get_json(self, client: httpx.Client, path: str, what: str, params: dict | None = None) -> Any:
        url = self.location.api_base + path
        try:
            response = client.get(url, params=params, headers={**self._api_headers(), **self._auth_headers(url)})
        except httpx.HTTPError as exc:
            raise ReleaseSourceError(
                "source_unreachable",
                f"Could not reach {self.location.api_base} ({exc.__class__.__name__}: {exc}). Check the URL, DNS "
                "and that the appliance may reach it (proxy/firewall).",
            ) from exc
        self._raise_for(response, what)
        try:
            return response.json()
        except ValueError as exc:
            raise ReleaseSourceError(
                "source_error", f"{self.location.api_base} did not answer with JSON for {what} -- is it the right host?"
            ) from exc

    # --- operations -------------------------------------------------------

    def test(self) -> dict[str, Any]:
        """Read the repository itself: proves URL, reachability and token."""
        with self._client() as client:
            data = self._get_json(client, self._repo_api_path(), "the repository")
        if not isinstance(data, dict):
            raise ReleaseSourceError("source_error", "Unexpected answer for the repository.")
        if self.location.provider == "github":
            return {"full_name": data.get("full_name") or self.location.path, "private": bool(data.get("private"))}
        return {
            "full_name": data.get("path_with_namespace") or self.location.path,
            "private": data.get("visibility") not in (None, "public"),
        }

    def list_releases(self) -> list[ReleaseInfo]:
        """Published releases whose tag is a release version, newest first.
        Drafts (GitHub) and upcoming releases (GitLab) are left out."""
        with self._client() as client:
            data = self._get_json(
                client, f"{self._repo_api_path()}/releases", "the releases", params={"per_page": RELEASES_PER_PAGE}
            )
        if not isinstance(data, list):
            raise ReleaseSourceError("source_error", "Unexpected answer for the releases list.")
        releases = []
        for raw in data:
            if not isinstance(raw, dict):
                continue
            info = self._github_release(raw) if self.location.provider == "github" else self._gitlab_release(raw)
            if info is not None:
                releases.append(info)
        releases.sort(key=lambda r: parse_version(r.version), reverse=True)
        return releases

    def _github_release(self, raw: dict[str, Any]) -> ReleaseInfo | None:
        if raw.get("draft"):
            return None
        tag = str(raw.get("tag_name") or "")
        version = version_from_tag(tag)
        if version is None:
            return None
        assets = [
            (str(a.get("name") or ""), a.get("size"), str(a.get("url") or ""))
            for a in raw.get("assets") or []
            if isinstance(a, dict) and a.get("state", "uploaded") == "uploaded" and a.get("url")
        ]
        picked = _pick_bundle(assets, version)
        return ReleaseInfo(
            version=version,
            tag_name=tag,
            name=raw.get("name") or None,
            released_at=raw.get("published_at") or raw.get("created_at"),
            notes=raw.get("body") or None,
            web_url=raw.get("html_url"),
            prerelease=bool(raw.get("prerelease")),
            bundle_name=picked[0] if picked else None,
            bundle_size=picked[1] if picked and isinstance(picked[1], int) else None,
            bundle_url=picked[2] if picked else None,
        )

    def _gitlab_release(self, raw: dict[str, Any]) -> ReleaseInfo | None:
        if raw.get("upcoming_release"):
            return None
        tag = str(raw.get("tag_name") or "")
        version = version_from_tag(tag)
        if version is None:
            return None
        links = ((raw.get("assets") or {}).get("links")) or []
        assets = []
        for link in links:
            if not isinstance(link, dict) or not link.get("url"):
                continue
            url = str(link["url"])
            # direct_asset_url is a stable permalink on the GitLab host itself.
            direct = link.get("direct_asset_url")
            assets.append((str(link.get("name") or url.rsplit("/", 1)[-1]), None, str(direct or url)))
        picked = _pick_bundle(assets, version)
        web = (raw.get("_links") or {}).get("self")
        return ReleaseInfo(
            version=version,
            tag_name=tag,
            name=raw.get("name") or None,
            released_at=raw.get("released_at") or raw.get("created_at"),
            notes=raw.get("description") or None,
            web_url=web or f"{self.location.web_url}/-/releases/{quote(tag, safe='')}",
            prerelease=False,
            bundle_name=picked[0] if picked else None,
            bundle_size=None,
            bundle_url=picked[2] if picked else None,
        )

    def download(
        self,
        url: str,
        destination: Path,
        *,
        max_bytes: int,
        on_progress: Callable[[int, int | None], None] | None = None,
    ) -> int:
        """Stream one asset to ``destination``; returns the byte count.

        Redirects are followed by hand so the token is dropped as soon as
        a hop leaves the repository's API host, and only to https (plain
        http only between hops on the API host of a source configured as
        http://, never as a downgrade from https)."""
        current = url
        with self._client() as client:
            for _ in range(_MAX_REDIRECTS + 1):
                headers = {"Accept": "application/octet-stream", **self._auth_headers(current)}
                try:
                    with client.stream("GET", current, headers=headers) as response:
                        if response.is_redirect:
                            location = response.headers.get("location")
                            if not location:
                                raise ReleaseSourceError("source_error", "Download redirect without a Location.")
                            current = self._redirect_target(current, location)
                            continue
                        self._raise_for(response, "the bundle download", include_body=False)
                        declared = response.headers.get("content-length")
                        total = int(declared) if declared and declared.isdigit() else None
                        if total is not None and total > max_bytes:
                            raise ReleaseSourceError(
                                "bundle_too_large",
                                f"The bundle is {total} bytes; the limit is {max_bytes} bytes "
                                "(setting UPDATES_MAX_BUNDLE_BYTES).",
                            )
                        received = 0
                        with destination.open("wb") as handle:
                            for chunk in response.iter_bytes(_DOWNLOAD_CHUNK):
                                received += len(chunk)
                                if received > max_bytes:
                                    raise ReleaseSourceError(
                                        "bundle_too_large",
                                        f"The download exceeded the limit of {max_bytes} bytes "
                                        "(setting UPDATES_MAX_BUNDLE_BYTES).",
                                    )
                                handle.write(chunk)
                                if on_progress:
                                    on_progress(received, total)
                        return received
                except httpx.HTTPError as exc:
                    raise ReleaseSourceError(
                        "source_unreachable", f"The download failed ({exc.__class__.__name__}: {exc})."
                    ) from exc
        raise ReleaseSourceError("source_error", f"The download was redirected more than {_MAX_REDIRECTS} times.")

    def _redirect_target(self, current: str, location: str) -> str:
        target = urljoin(current, location)
        target_parts = urlsplit(target)
        scheme = target_parts.scheme.lower()
        if scheme == "https":
            return target
        if (
            scheme == "http"
            and urlsplit(current).scheme.lower() == "http"
            and self.location.api_scheme == "http"
            and target_parts.netloc.lower() == self.location.api_host
        ):
            return target
        raise ReleaseSourceError(
            "insecure_redirect",
            f"The download was redirected to {target_parts.scheme}://{target_parts.netloc}, which is not https. "
            "The download was stopped; check the repository host and any proxy in between.",
        )


def newer_releases(
    releases: list[ReleaseInfo], current_version: str, *, include_prereleases: bool
) -> list[ReleaseInfo]:
    """Releases above the running version (all of them when the running
    version is no release version, e.g. a dev checkout), newest first."""
    try:
        parse_version(current_version)
        running_is_release = True
    except BundleError:
        running_is_release = False
    return [
        r
        for r in releases
        if (include_prereleases or not r.prerelease)
        and (not running_is_release or compare_versions(r.version, current_version) > 0)
    ]
