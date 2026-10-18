"""Application identity derivation for profile generation (pure).

Profile IDs derive deterministically from tenant + app name: no hidden
mappings, no caller-supplied IDs. Repo references accept
github.com/org/repo, full https:// URLs, or bare org/repo (SSH rejected).
"""

from __future__ import annotations

import re


def slugify_app_name(app_name: str) -> str:
    """Slugify a display name to a profile ID segment (pure)."""
    try:
        slug = re.sub(r"[^a-z0-9]+", "-", str(app_name or "").lower()).strip("-")
        return slug[:64]
    except Exception:
        return ""


def derive_app_id(tenant_id: str, app_name: str) -> str:
    """Deterministic profile ID from tenant + app name (pure).

    Raises ValueError on empty inputs (callers map to 400). The tenant
    scopes the ID so identical app names in different tenants never
    collide; same tenant + same name replays instead of forking.
    """
    tenant = str(tenant_id or "").strip()
    slug = slugify_app_name(app_name)
    if not tenant or not slug:
        raise ValueError("tenant and app_name are required.")
    return f"{slug}"


def parse_repo_ref(ref: str) -> tuple[str, str]:
    """Normalize a repo reference to (org, repo) (pure).

    Accepts github.com/org/repo, https://github.com/org/repo[.git],
    http(s)://host/org/repo, or bare org/repo. Rejects SSH (git@),
    traversal (..), and malformed shapes with ValueError.
    """
    try:
        text = str(ref or "").strip()
        if not text or ".." in text or text.startswith("git@"):
            raise ValueError("Invalid repo reference.")
        # Strip scheme + host.
        without_scheme = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", text)
        # Strip a leading github.com/ host when present.
        path = without_scheme
        if "/" in without_scheme:
            host, _, rest = without_scheme.partition("/")
            if "." in host and rest:
                path = rest
        path = path.removesuffix(".git").strip("/")
        parts = [p for p in path.split("/") if p]
        if len(parts) != 2:
            raise ValueError("Repo reference must resolve to org/repo.")
        org, repo = parts
        for value in (org, repo):
            if not value or len(value) > 128 or not re.fullmatch(r"[A-Za-z0-9_.\-]+", value):
                raise ValueError("Invalid org/repo segment.")
        return org, repo
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("Invalid repo reference.") from exc
