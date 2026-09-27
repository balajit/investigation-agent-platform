# src/investigation_agent_platform/infrastructure/evidence/code/codeowners.py
"""CODEOWNERS-based team ownership lookup (ISSUE-6).

Lowest-confidence ownership tier: consulted only when static topology yields
no domain owner. Reads CODEOWNERS files from the tenant-authorized repository
checkout with the same traversal and symlink guards as the code intelligence
provider, so this path cannot become a filesystem-escape vector.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from investigation_agent_platform.application.investigation.validator import _has_traversal
from investigation_agent_platform.domain.common.exceptions import (
    SecurityPolicyViolationException,
)

logger = logging.getLogger(__name__)

# Candidate filenames in precedence order (first found wins).
CODEOWNERS_CANDIDATES = ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS", "OWNERS")

_MAX_CODEOWNERS_BYTES = 1_048_576


def _pattern_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a CODEOWNERS pattern to a regex (simplified GitHub semantics).

    Simplifications (documented): no `!` negation support; `*` never crosses
    `/`; `**` crosses directories; a pattern without `/` matches the basename
    at any depth; otherwise anchored at the repository root.
    """
    anchored = "/" in pattern.rstrip("/")
    # A trailing slash marks a directory pattern: it matches the directory
    # itself and everything beneath it (GitHub semantics).
    dir_only = pattern.endswith("/")
    core = pattern.rstrip("/")
    regex_parts: list[str] = []
    i = 0
    while i < len(core):
        char = core[i]
        if char == "*":
            if core[i : i + 2] == "**":
                regex_parts.append(".*")
                i += 2
                # A `/**/` segment also matches zero directories.
                if core[i : i + 1] == "/":
                    i += 1
            else:
                regex_parts.append("[^/]*")
                i += 1
        elif char == "?":
            regex_parts.append("[^/]")
            i += 1
        else:
            regex_parts.append(re.escape(char))
            i += 1
    body = "".join(regex_parts)
    if dir_only:
        body = f"{body}(?:/.*)?"
    if anchored:
        return re.compile(rf"^(?:\./)?{body}$")
    return re.compile(rf"(?:^|.*/){body}$")


def parse_codeowners(text: str) -> list[tuple[re.Pattern[str], list[str]]]:
    """Parse CODEOWNERS content into (pattern, owners) rules in file order."""
    rules: list[tuple[re.Pattern[str], list[str]]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) < 2:
            continue
        pattern, owners = parts[0], parts[1:]
        try:
            rules.append((_pattern_to_regex(pattern), owners))
        except re.error:
            logger.warning("Skipping invalid CODEOWNERS pattern: %s", pattern)
    return rules


def match_owner(rules: list[tuple[re.Pattern[str], list[str]]], file_path: str) -> str | None:
    """Last matching rule wins (GitHub semantics). Returns the last owner token."""
    normalized = file_path.replace("\\", "/").lstrip("./")
    matched: list[str] | None = None
    for pattern, owners in rules:
        if pattern.match(normalized):
            matched = owners
    if not matched:
        return None
    # Owner team identity: last token, `@` prefix stripped (user vs team
    # distinction is not modeled; the raw token is preserved in provenance).
    return matched[-1].lstrip("@")


class CodeownersResolver:
    """Resolves file ownership from CODEOWNERS with tenant scoping.

    ``tenant_allowlist`` maps tenant_id -> set of authorized repository
    locators (same shape as the code intelligence provider). ``locators``
    maps (tenant_id, repository_id) -> repository locator; when a mapping is
    absent the tier is gracefully unavailable (logged) rather than guessed.
    """

    def __init__(
        self,
        repo_base_path: str = "",
        tenant_allowlist: dict[str, set[str]] | None = None,
        locators: dict[tuple[str, str], str] | None = None,
    ) -> None:
        self._repo_base_path = Path(repo_base_path).resolve() if repo_base_path else None
        self._tenant_allowlist = tenant_allowlist
        self._locators = locators or {}

    def _resolve_repo_path(self, tenant_id: str, locator: str) -> Path | None:
        if not tenant_id or tenant_id == "anonymous":
            raise SecurityPolicyViolationException("Code access requires an authenticated tenant")
        if self._repo_base_path is None:
            logger.debug("CODEOWNERS tier unavailable: no repo base path configured")
            return None
        if _has_traversal(locator) or Path(locator).is_absolute():
            logger.warning("Repository locator traversal blocked: %s", locator)
            return None
        if self._tenant_allowlist is not None:
            allowed = self._tenant_allowlist.get(tenant_id, set())
            if locator not in allowed:
                raise SecurityPolicyViolationException(
                    f"Tenant '{tenant_id}' is not authorized for repository '{locator}'"
                )
        try:
            repo_path = (self._repo_base_path / locator).resolve()
        except Exception:
            return None
        try:
            if not repo_path.is_relative_to(self._repo_base_path):
                logger.warning("Repository path traversal blocked: %s", locator)
                return None
        except Exception:
            return None
        if repo_path.is_symlink() or not repo_path.is_dir():
            return None
        return repo_path

    def locator_for(self, tenant_id: str, repository_id: str) -> str | None:
        return self._locators.get((tenant_id, repository_id))

    async def resolve_owner(self, tenant_id: str, locator: str, file_path: str) -> str | None:
        """Return the owning team for a repository-relative file, or None."""
        import asyncio

        return await asyncio.to_thread(self._resolve_owner_sync, tenant_id, locator, file_path)

    def _resolve_owner_sync(self, tenant_id: str, locator: str, file_path: str) -> str | None:
        repo_path = self._resolve_repo_path(tenant_id, locator)
        if repo_path is None:
            return None
        if _has_traversal(file_path) or Path(file_path).is_absolute():
            logger.warning("CODEOWNERS lookup path traversal blocked: %s", file_path)
            return None
        for candidate in CODEOWNERS_CANDIDATES:
            codeowners_file = repo_path / candidate
            try:
                if not codeowners_file.is_file() or codeowners_file.is_symlink():
                    continue
                if codeowners_file.stat().st_size > _MAX_CODEOWNERS_BYTES:
                    logger.warning("CODEOWNERS file too large, skipping: %s", candidate)
                    continue
                text = codeowners_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            owner = match_owner(parse_codeowners(text), file_path)
            if owner:
                return owner
        return None
