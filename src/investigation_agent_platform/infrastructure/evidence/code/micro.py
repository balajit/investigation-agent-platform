# src/investigation_agent_platform/infrastructure/evidence/code/micro.py
"""On-demand micro-tier symbol resolution (ISSUE-3).

Parses the working-tree file at query time for symbols too fine-grained to
persist (see ``MACRO_NODE_TYPES``). Bounded (file-size cap, timeout) and
tenant-scoped like the existing provider path. The returned symbol identity
is deterministic (same ``ASTNodeIdentity.create`` path as ingestion); only
the *source* differs, which is why callers record the resolution tier
separately rather than trusting this output alone for ownership.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from investigation_agent_platform.application.investigation.validator import _has_traversal
from investigation_agent_platform.domain.common.exceptions import (
    SecurityPolicyViolationException,
)
from investigation_agent_platform.domain.topology.models import (
    ASTNodeIdentity,
    MicroSymbolMatch,
    TopologyNodeType,
)
from investigation_agent_platform.infrastructure.evidence.code.intelligence import (
    EXT_TO_LANGUAGE,
    MAX_FILE_BYTES,
    SYMBOL_NODE_TYPES,
)

logger = logging.getLogger(__name__)

# Tree-sitter grammar node types mapped to canonical topology types. Only
# named declarations are mapped; unmatched grammar kinds are skipped (they
# cannot be typed, so they cannot be identified deterministically).
_TREE_SITTER_KIND_MAP: dict[str, TopologyNodeType] = {
    "function_definition": TopologyNodeType.FUNCTION,
    "function_declaration": TopologyNodeType.FUNCTION,
    "function": TopologyNodeType.FUNCTION,
    "method_definition": TopologyNodeType.METHOD,
    "method_declaration": TopologyNodeType.METHOD,
    "class_definition": TopologyNodeType.CLASS,
    "class_declaration": TopologyNodeType.CLASS,
    "interface_declaration": TopologyNodeType.INTERFACE,
    "enum_declaration": TopologyNodeType.ENUM,
    "constructor": TopologyNodeType.CONSTRUCTOR,
}

_REVISION_CHECK_TIMEOUT_SECONDS = 5.0


class MicroSymbolResolver:
    """Resolves the most-specific enclosing symbol by parsing on demand.

    ``tenant_allowlist`` maps tenant_id -> set of authorized repository
    locators (same shape as the code intelligence provider). ``locators``
    maps (tenant_id, repository_id) -> repository locator; absent mappings
    make the tier gracefully unavailable rather than guessed.
    """

    def __init__(
        self,
        repo_base_path: str = "",
        tenant_allowlist: dict[str, set[str]] | None = None,
        locators: dict[tuple[str, str], str] | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._repo_base_path = Path(repo_base_path).resolve() if repo_base_path else None
        self._tenant_allowlist = tenant_allowlist
        self._locators = locators or {}
        self._timeout_seconds = timeout_seconds
        self._parsers: dict[str, Any] = {}

    def locator_for(self, tenant_id: str, repository_id: str) -> str | None:
        return self._locators.get((tenant_id, repository_id))

    async def resolve_micro_symbol(
        self,
        tenant_id: str,
        repository_id: str,
        locator: str,
        revision: str,
        file_path: str,
        line_number: int,
    ) -> MicroSymbolMatch | None:
        """Parse the working-tree file and return the innermost enclosing symbol."""
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    self._resolve_sync,
                    tenant_id,
                    repository_id,
                    locator,
                    revision,
                    file_path,
                    line_number,
                ),
                timeout=self._timeout_seconds,
            )
        except TimeoutError:
            logger.warning(
                "Micro symbol resolution timed out",
                extra={"tenant_id": tenant_id, "file_path": file_path},
            )
            return None

    def _resolve_sync(
        self,
        tenant_id: str,
        repository_id: str,
        locator: str,
        revision: str,
        file_path: str,
        line_number: int,
    ) -> MicroSymbolMatch | None:
        repo_path = self._resolve_repo_path(tenant_id, locator)
        if repo_path is None:
            return None
        if _has_traversal(file_path) or Path(file_path).is_absolute():
            logger.warning("Micro lookup path traversal blocked: %s", file_path)
            return None
        target = (repo_path / file_path).resolve()
        try:
            if not target.is_relative_to(repo_path):
                return None
        except Exception:
            return None
        if target.is_symlink() or not target.is_file():
            return None
        try:
            if target.stat().st_size > MAX_FILE_BYTES:
                logger.warning("Micro lookup file too large: %s", file_path)
                return None
            code = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

        language = EXT_TO_LANGUAGE.get(target.suffix)
        if language is None:
            return None
        parser = self._get_parser(language)
        if parser is None:
            return None
        try:
            tree = parser.parse(bytes(code, "utf-8"))
        except Exception:
            return None

        best: tuple[int, Any] | None = None  # (range_size, node)
        cursor = tree.walk()
        visited_children = False
        depth = 0
        while True:
            if not visited_children:
                node = cursor.node
                if node.type in SYMBOL_NODE_TYPES and node.type in _TREE_SITTER_KIND_MAP:
                    start_line = node.start_point[0] + 1
                    end_line = node.end_point[0] + 1
                    if start_line <= line_number <= end_line:
                        size = end_line - start_line
                        if best is None or size < best[0]:
                            best = (size, node)
                if depth < 100 and cursor.goto_first_child():
                    depth += 1
                    continue
            if cursor.goto_next_sibling():
                visited_children = False
                continue
            if not cursor.goto_parent():
                break
            depth -= 1
            visited_children = True

        if best is None:
            return None
        _, node = best
        name = self._node_name(code, node)
        if not name:
            return None
        node_type = _TREE_SITTER_KIND_MAP[node.type]
        identity = ASTNodeIdentity.create(
            tenant_id=tenant_id,
            repository_id=repository_id,
            revision=revision,
            name=name,
            qualified_name=name,  # micro scope: flat name (documented limitation)
            node_type=node_type,
            file_path=file_path,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            start_column=node.start_point[1],
            end_column=node.end_point[1],
            parser_version="tree-sitter-micro",
        )
        return MicroSymbolMatch(
            node=identity,
            revision_verified=self._verify_revision(repo_path, revision),
        )

    @staticmethod
    def _node_name(code: str, node: Any) -> str:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            for child in node.children:
                if child.type == "identifier" or child.type.endswith("identifier"):
                    name_node = child
                    break
        if name_node is None:
            return ""
        try:
            return code[name_node.start_byte : name_node.end_byte].strip()
        except Exception:
            return ""

    def _get_parser(self, language: str) -> Any | None:
        if language in self._parsers:
            return self._parsers[language]
        try:
            from tree_sitter_language_pack import get_parser  # type: ignore[import-not-found]

            parser = get_parser(language)  # type: ignore[no-untyped-call]
            self._parsers[language] = parser
            return parser
        except Exception as exc:
            logger.debug("Parser unavailable for language %s: %s", language, exc)
            self._parsers[language] = None
            return None

    def _resolve_repo_path(self, tenant_id: str, locator: str) -> Path | None:
        if not tenant_id or tenant_id == "anonymous":
            raise SecurityPolicyViolationException("Code access requires an authenticated tenant")
        if self._repo_base_path is None:
            logger.debug("Micro tier unavailable: no repo base path configured")
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
                return None
        except Exception:
            return None
        if repo_path.is_symlink() or not repo_path.is_dir():
            return None
        return repo_path

    def _verify_revision(self, repo_path: Path, revision: str) -> bool:
        """Best-effort check that the working tree matches the requested revision.

        Runs synchronously (this method executes on a worker thread via
        ``asyncio.to_thread``). Any failure (not a git checkout, timeout,
        mismatch) yields False rather than raising. A False result marks the
        match approximate — it never blocks it.
        """
        import subprocess

        if not revision or len(revision) < 7:
            return False
        try:
            completed = subprocess.run(
                ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
                capture_output=True,
                timeout=_REVISION_CHECK_TIMEOUT_SECONDS,
                check=False,
            )
            head = completed.stdout.decode("utf-8", errors="replace").strip()
            if len(head) < 7:
                return False
            return head.startswith(revision) or revision.startswith(head)
        except Exception:
            return False
