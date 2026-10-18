# src/investigation_agent_platform/infrastructure/topology/ownership.py
"""Ownership derivation for Layer 3 organizational identity (ISSUE-7).

Two independent, narrow sources feed the ``GitOrganization``/``Domain``
identities Neo4j needs to answer ownership queries — neither is
hand-maintained Cypher:

- ``GitOrganization`` is parsed from the repository's git locator (clone
  URL or ``org/repo`` path) — the org the repository already belongs to on
  its hosting provider.
- ``Domain`` (team ownership) is derived from the repository's CODEOWNERS
  catch-all rule (the ``*`` pattern), reusing the same parser the
  query-time CODEOWNERS fallback tier (ISSUE-6) already uses.

Both derivations fail closed to ``None`` rather than guessing: an
unparseable locator or a CODEOWNERS file with no catch-all rule leaves the
corresponding identity unset, and callers must not manufacture one.

Scope note: this derives one **repository-level** domain (the CODEOWNERS
catch-all owner), not a per-file-pattern domain. Per-file precision beyond
the catch-all rule remains the query-time ``CodeownersResolver`` (ISSUE-6)
fallback tier, unchanged.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from investigation_agent_platform.domain.topology.models import GitOrganizationIdentity
from investigation_agent_platform.infrastructure.evidence.code.codeowners import (
    CODEOWNERS_CANDIDATES,
    match_owner,
    parse_codeowners,
)

logger = logging.getLogger(__name__)

_MAX_CODEOWNERS_BYTES = 1_048_576

# Recognized locator shapes:
#   https://github.com/org/repo(.git)
#   http://gitlab.example.com/org/sub/repo.git
#   git@github.com:org/repo.git
#   ssh://git@host/org/repo.git
_URL_SCHEME_RE = re.compile(
    r"^[a-z][a-z0-9+.\-]*://(?:[^@/]+@)?(?P<host>[^/]+)/(?P<path>.+)$", re.IGNORECASE
)
_SCP_LIKE_RE = re.compile(r"^[^@/]+@(?P<host>[^:/]+):(?P<path>.+)$")


def derive_git_organization(
    tenant_id: str, repository_locator: str
) -> GitOrganizationIdentity | None:
    """Parse a repository locator into a ``GitOrganizationIdentity``.

    Recognizes URL locators (``https://host/org/repo``), scp-like locators
    (``git@host:org/repo``), and bare ``org/repo`` paths. Returns ``None``
    when the locator does not resolve to a recognizable ``org/repo`` shape
    (e.g. a bare local filesystem path with no org segment) — never a
    manufactured placeholder org.
    """
    locator = (repository_locator or "").strip()
    if not locator:
        return None

    host = ""
    path = locator
    scp_match = _SCP_LIKE_RE.match(locator)
    url_match = _URL_SCHEME_RE.match(locator)
    if scp_match:
        host, path = scp_match.group("host"), scp_match.group("path")
    elif url_match:
        host, path = url_match.group("host"), url_match.group("path")
    elif locator.startswith("/") or locator.startswith("~"):
        # An absolute filesystem path is not an "org/repo" locator, even
        # though it would otherwise split into >= 2 segments.
        logger.debug("Locator looks like a filesystem path, not org/repo: %s", repository_locator)
        return None
    # else: bare "org/repo" path form — host stays "", path is the locator.

    path = path.removesuffix(".git").strip("/")
    segments = [seg for seg in path.split("/") if seg]
    if len(segments) < 2:
        # No recognizable "org/repo" shape (e.g. a bare filesystem path).
        logger.debug("Locator has no org segment: %s", repository_locator)
        return None

    org_segment = segments[0]
    host_lower = host.lower()
    if "github" in host_lower:
        provider = "github"
    elif "gitlab" in host_lower:
        provider = "gitlab"
    elif host_lower:
        provider = host_lower
    else:
        provider = "git"
    git_org_id = f"{provider}:{org_segment}".lower()

    return GitOrganizationIdentity(
        tenant_id=tenant_id,
        git_org_id=git_org_id,
        name=org_segment,
        provider=provider,
    )


def derive_repository_domain(repo_checkout_path: Path) -> str | None:
    """Read the repository's CODEOWNERS catch-all (``*``) rule as its default domain.

    Applies the same file-size cap and symlink guard as the query-time
    ``CodeownersResolver``. Returns ``None`` when no CODEOWNERS file exists
    or none of its rules resolve for the catch-all path ``"/"``.
    """
    for candidate in CODEOWNERS_CANDIDATES:
        codeowners_file = repo_checkout_path / candidate
        try:
            if not codeowners_file.is_file() or codeowners_file.is_symlink():
                continue
            if codeowners_file.stat().st_size > _MAX_CODEOWNERS_BYTES:
                logger.warning("CODEOWNERS file too large, skipping: %s", candidate)
                continue
            text = codeowners_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        owner = match_owner(parse_codeowners(text), "/")
        if owner:
            return owner
    return None
