"""GitHub discovery for profile generation (P1).

Lists repos for an org (optional topic filter) via REST and detects
multi-module layouts from file listings. Token precedence: IAP_GITHUB_TOKEN
env first, then the `gh` CLI keychain (`gh auth token`) so operators who
already authenticated `gh` configure nothing twice. The token value is never
logged or persisted by this module. Pure parsing split from I/O for unit
tests. stdlib urllib only.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"
_TIMEOUT_SECONDS = 20
_PER_PAGE = 100


@dataclass(frozen=True)
class DiscoveredRepo:
    """One GitHub repo relevant to profile generation."""

    org: str
    name: str
    default_branch: str
    language: str | None
    clone_url: str


@dataclass(frozen=True)
class RepoModule:
    """One build module inside a repo (multi-module aware)."""

    path: str
    build_system: str


def _gh_cli_token() -> str:
    """Token from the `gh` CLI keychain, or "" when unavailable.

    Subprocess only, stderr suppressed, strict timeout; the value flows
    straight into the Authorization header — never logged, never stored.
    """
    try:
        import subprocess

        proc = subprocess.run(
            ["gh", "auth", "token", "--hostname", "github.com"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def _auth_headers() -> dict[str, str]:
    token = os.environ.get("IAP_GITHUB_TOKEN", "").strip() or _gh_cli_token()
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "investigation-agent-platform",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _get_json(url: str) -> Any:
    request = urllib.request.Request(url, headers=_auth_headers())
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise PermissionError(
                "GitHub authentication failed; check IAP_GITHUB_TOKEN or `gh auth login`."
            ) from exc
        if exc.code == 404:
            raise LookupError(f"GitHub resource not found: {url}") from exc
        raise ConnectionError(f"GitHub request failed ({exc.code}): {url}") from exc
    except Exception as exc:
        raise ConnectionError(f"GitHub request failed: {url}") from exc


def discover_repos(
    org: str, topic: str | None = None, max_repos: int = 100
) -> list[DiscoveredRepo]:
    """List an org's repos, optionally filtered by topic (I/O).

    Paginates the org repos endpoint; filters client-side. Bounded.
    Raises PermissionError/LookupError/ConnectionError (never returns
    partial silently — empty list means empty org/filter).
    """
    org = str(org or "").strip()
    if not org or "/" in org or ".." in org:
        raise ValueError("Invalid GitHub org name.")
    found: list[DiscoveredRepo] = []
    page = 1
    try:
        while len(found) < max(1, min(max_repos, 1000)):
            url = (
                f"{_GITHUB_API}/orgs/{urllib.parse.quote(org)}/repos"
                f"?per_page={_PER_PAGE}&page={page}&type=all"
            )
            payload = _get_json(url)
            if not isinstance(payload, list) or not payload:
                break
            for entry in payload:
                try:
                    if not isinstance(entry, dict):
                        continue
                    if topic:
                        topics = entry.get("topics", []) or []
                        if str(topic).lower() not in [str(t).lower() for t in topics]:
                            continue
                    found.append(
                        DiscoveredRepo(
                            org=org,
                            name=str(entry.get("name", "")),
                            default_branch=str(entry.get("default_branch", "") or "main"),
                            language=entry.get("language", None),
                            clone_url=str(entry.get("clone_url", "") or ""),
                        )
                    )
                    if len(found) >= max_repos:
                        break
                except Exception:
                    continue
            if len(payload) < _PER_PAGE:
                break
            page += 1
    except (PermissionError, LookupError):
        raise
    except Exception as exc:
        raise ConnectionError(f"GitHub org scan failed for '{org}'.") from exc
    return found


_BUILD_FILES = {
    "pom.xml": "maven",
    "build.gradle": "gradle",
    "build.gradle.kts": "gradle",
    "package.json": "npm",
    "go.mod": "go",
    "Cargo.toml": "cargo",
    "pyproject.toml": "python",
    "setup.py": "python",
}


def detect_modules(file_paths: list[str]) -> list[RepoModule]:
    """Detect build modules from a repo file listing (pure).

    A module root is any directory containing a known build file.
    Nested build files yield nested modules (filtered to outermost +
    direct children? No — every build-file dir is a module; the assembler
    dedupes overlapping source roots). Sorted, deterministic. Dot-directories
    and common build-output dirs are ignored as roots.
    """
    try:
        modules: dict[str, str] = {}
        for raw in file_paths or []:
            try:
                path = str(raw or "").strip().lstrip("./")
                if not path:
                    continue
                parts = path.split("/")
                filename = parts[-1]
                build_system = _BUILD_FILES.get(filename, "")
                if not build_system:
                    continue
                # Root-level build file = the repo itself is the module.
                root = "/".join(parts[:-1]) if len(parts) >= 2 else "."
                if root != "." and (not root or root.startswith(".")):
                    continue
                if any(
                    segment in ("build", "target", "node_modules", "dist", "out")
                    for segment in root.split("/")
                ):
                    continue
                modules.setdefault(root, build_system)
            except Exception:
                continue
        return [RepoModule(path=root, build_system=modules[root]) for root in sorted(modules)]
    except Exception:
        return []
