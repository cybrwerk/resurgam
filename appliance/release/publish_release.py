"""Publish a built update bundle as a GitHub (or GitLab) release (docs/adr/0026),
so appliances with an update source find it under System Settings -> Updates.

Run after build-bundle.sh, from the repo root:

    GITHUB_TOKEN=... uv run --project backend python appliance/release/publish_release.py

The release is tagged v<VERSION> at the current HEAD (which must already be pushed),
titled "RESURGAM <VERSION>", and its notes are this version's CHANGELOG.md section.
The bundle is attached as resurgam-<VERSION>.bundle -- the name the appliance looks for.

Repository: --repository, else the `origin` remote. Provider: guessed from the host
(github/gitlab in the name), else --provider.
Tokens (on the release builder only, never on an appliance):
  GitHub  GITHUB_TOKEN  fine-grained with Contents: read and write (or classic `repo`)
  GitLab  GITLAB_TOKEN  personal access token with `api` (generic package upload + release)
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import httpx

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO / "backend" / "src"))
sys.path.insert(0, str(HERE))

from make_bundle import changelog_excerpt

from resurgam_api.updates.release_source import guess_provider, parse_repository_url

TIMEOUT = httpx.Timeout(60.0, connect=15.0, write=None)


def die(message: str) -> None:
    print(f"publish-release: {message}", file=sys.stderr)
    sys.exit(1)


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(REPO), *args], check=True, capture_output=True, text=True).stdout.strip()


def origin_web_url() -> str:
    url = git("remote", "get-url", "origin")
    if url.startswith("git@"):  # git@github.com:owner/repo.git
        host, _, path = url[4:].partition(":")
        url = f"https://{host}/{path}"
    return url


def check(response: httpx.Response, what: str) -> httpx.Response:
    if response.status_code >= 400:
        die(f"{what} failed: HTTP {response.status_code} {response.text[:500]}")
    return response


def publish_github(location, token: str, version: str, sha: str, notes: str, bundle: Path, args) -> str:
    api = location.api_base
    repo = f"{api}/repos/{location.path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "resurgam-publish-release",
    }
    tag = f"v{version}"
    with httpx.Client(headers=headers, timeout=TIMEOUT) as client:
        existing = client.get(f"{repo}/releases/tags/{tag}")
        if existing.status_code == 200:
            release = existing.json()
            print(f"==> Release {tag} exists ({release['html_url']})")
        else:
            if existing.status_code != 404:
                check(existing, f"looking up release {tag}")
            body = {
                "tag_name": tag,
                "target_commitish": sha,
                "name": f"RESURGAM {version}",
                "body": notes,
                "draft": False,
                "prerelease": args.prerelease,
            }
            release = check(client.post(f"{repo}/releases", json=body), f"creating release {tag}").json()
            print(f"==> Created release {tag} ({release['html_url']})")

        for asset in release.get("assets") or []:
            if asset["name"] == bundle.name:
                if not args.replace:
                    die(f"{bundle.name} is already attached to {tag}. Pass --replace to upload it again.")
                check(client.delete(f"{repo}/releases/assets/{asset['id']}"), "deleting the old asset")
        upload_url = release["upload_url"].split("{", 1)[0]
        size = bundle.stat().st_size
        print(f"==> Uploading {bundle.name} ({size / 1024**2:.0f} MiB)")
        with bundle.open("rb") as handle:
            check(
                client.post(
                    upload_url,
                    params={"name": bundle.name},
                    content=handle,
                    headers={"Content-Type": "application/octet-stream", "Content-Length": str(size)},
                ),
                "uploading the bundle",
            )
        return release["html_url"]


def publish_gitlab(location, token: str, version: str, sha: str, notes: str, bundle: Path, args) -> str:
    project = f"{location.api_base}/projects/{quote(location.path, safe='')}"
    headers = {"PRIVATE-TOKEN": token, "User-Agent": "resurgam-publish-release"}
    tag = f"v{version}"
    package_url = f"{project}/packages/generic/resurgam/{version}/{bundle.name}"
    with httpx.Client(headers=headers, timeout=TIMEOUT) as client:
        size = bundle.stat().st_size
        print(f"==> Uploading {bundle.name} ({size / 1024**2:.0f} MiB) to the generic package registry")
        with bundle.open("rb") as handle:
            check(
                client.put(package_url, content=handle, headers={"Content-Length": str(size)}), "uploading the bundle"
            )
        existing = client.get(f"{project}/releases/{quote(tag, safe='')}")
        link = {"name": bundle.name, "url": package_url, "link_type": "package", "direct_asset_path": f"/{bundle.name}"}
        if existing.status_code == 200:
            links = existing.json().get("assets", {}).get("links", [])
            if any(link_["name"] == bundle.name for link_ in links):
                print(f"==> Release {tag} already links {bundle.name} (package file replaced)")
            else:
                check(
                    client.post(f"{project}/releases/{quote(tag, safe='')}/assets/links", json=link), "adding the link"
                )
        else:
            body = {
                "tag_name": tag,
                "ref": sha,
                "name": f"RESURGAM {version}",
                "description": notes,
                "assets": {"links": [link]},
            }
            check(client.post(f"{project}/releases", json=body), f"creating release {tag}")
        return f"{location.web_url}/-/releases/{tag}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repository", help="Repository web URL (default: the origin remote).")
    parser.add_argument("--provider", choices=("github", "gitlab"), help="Default: guessed from the host.")
    parser.add_argument("--bundle", type=Path, help="Default: appliance/release/dist/resurgam-<VERSION>.bundle.")
    parser.add_argument("--prerelease", action="store_true", help="Mark the GitHub release as pre-release.")
    parser.add_argument("--replace", action="store_true", help="Replace a bundle already attached to the release.")
    args = parser.parse_args()

    version = (REPO / "VERSION").read_text().strip()
    bundle = args.bundle or HERE / "dist" / f"resurgam-{version}.bundle"
    if not bundle.is_file():
        die(f"{bundle} not found -- run appliance/release/build-bundle.sh first.")
    if bundle.name != f"resurgam-{version}.bundle":
        die(f"the bundle must be named resurgam-{version}.bundle (appliances look for that name).")

    repository = args.repository or origin_web_url()
    provider = args.provider or guess_provider(repository)
    if provider is None:
        die(f"cannot tell whether {repository} is GitHub or GitLab; pass --provider.")
    location = parse_repository_url(repository, provider)
    token = os.environ.get("GITHUB_TOKEN" if provider == "github" else "GITLAB_TOKEN", "")
    if not token:
        die(f"set {'GITHUB_TOKEN' if provider == 'github' else 'GITLAB_TOKEN'} (see --help for the permissions).")

    sha = git("rev-parse", "HEAD")
    if git("status", "--porcelain", "--untracked-files=no"):
        print("publish-release: warning: the working tree has uncommitted changes; the tag points at HEAD.")
    remote_has_sha = subprocess.run(
        ["git", "-C", str(REPO), "branch", "-r", "--contains", sha], capture_output=True, text=True, check=False
    ).stdout.strip()
    if not remote_has_sha:
        die(f"HEAD {sha[:12]} is not pushed yet; push it first so the release tag can point at it.")
    notes = changelog_excerpt((REPO / "CHANGELOG.md").read_text(), version)

    publish = publish_github if provider == "github" else publish_gitlab
    url = publish(location, token, version, sha, notes, bundle, args)
    print(f"==> Published {version}: {url}")


if __name__ == "__main__":
    main()
