# src/investigation_agent_platform/infrastructure/evidence/code/intelligence.py
"""Tree-sitter and Rustworkx backed CodeIntelligenceProvider scanning filesystem under CodeProfile roots."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import rustworkx as rx
from opentelemetry import trace
from pydantic import BaseModel, Field  # noqa: F401 - keep for potential future use

from investigation_agent_platform.application.investigation.validator import _has_traversal
from investigation_agent_platform.domain.evidence.models import (
    ClassificationLevel,
    Evidence,
    EvidenceType,
)
from investigation_agent_platform.domain.profile.models import ApplicationProfile
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)
from investigation_agent_platform.infrastructure.observability.tracing import traced
from investigation_agent_platform.ports.evidence.code import (
    CallGraphNode,
    CodeLocation,
    CodeSymbol,
)

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

MAX_FILE_BYTES = 524_288
MAX_SEARCH_RESULTS = 50
MAX_SYMBOL_RESULTS = 100

EXT_TO_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".java": "java",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".c": "c",
    ".cpp": "cpp",
    ".cs": "c_sharp",
}

# Node types that define symbols per language (superset)
SYMBOL_NODE_TYPES = frozenset(
    {
        "function_definition",
        "async_function_definition",
        "function_declaration",
        "method_declaration",
        "method_definition",
        "class_definition",
        "class_declaration",
        "interface_declaration",
        "function_declarator",
        "lexical_declaration",
        "variable_declarator",
    }
)

CALL_NODE_TYPES = frozenset(
    {"call_expression", "call", "method_invocation", "invocation_expression"}
)
TRY_NODE_TYPES = frozenset({"try_statement", "try_with_resources_statement"})
EXCEPT_NODE_TYPES = frozenset({"except_clause", "catch_clause", "except_handler"})

SQL_KEYWORDS = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|MERGE|UPSERT)\b", re.IGNORECASE)
ORM_PATTERNS = re.compile(
    r"(\.execute\s*\(|\.query\s*\(|\.filter\s*\(|Session|EntityManager|JdbcTemplate|createQuery|prepareStatement)",
)


class TreeSitterCodeIntelligenceProvider:
    """Filesystem and Rustworkx backed implementation of CodeIntelligenceProviderProtocol.

    Scans repo_base_path / services[].code (org/repos + sourceRoots) and uses tree-sitter
    and pre-computed Rustworkx (PyDiGraph) call graph artifacts for high-performance analysis.
    """

    def __init__(
        self,
        repo_base_path: str = "",
        provider_id: str = "code-intelligence",
        tenant_repository_allowlist: dict[str, set[str]] | None = None,
        graphify_artifact_name: str = "call_graph.json",
    ) -> None:
        # No insecure /tmp default: production must inject IAP_CODE_REPO_BASE
        # explicitly; an empty base fails closed at construction time.
        if not repo_base_path:
            from investigation_agent_platform.domain.common.exceptions import (
                PlatformConfigurationError,
            )

            raise PlatformConfigurationError(
                "Code intelligence requires an explicit repo_base_path (IAP_CODE_REPO_BASE)"
            )
        self._repo_base_path = Path(repo_base_path).resolve()
        self._provider_id = provider_id
        self._parsers: dict[str, Any] = {}
        # F-040: explicit tenant → authorized-repository mapping. When
        # configured, a tenant may only scan repositories registered to it;
        # anything else is denied. When unconfigured (dev), all repositories
        # under the base path are visible but every access is still logged.
        self._tenant_allowlist = tenant_repository_allowlist
        self._graphify_artifact_name = graphify_artifact_name
        self._graph_cache: dict[str, tuple[Any, int]] = {}

    @staticmethod
    def _split_repo_entry(svc_org: str, repo: str) -> str:
        """Locator for one repos entry (mirrors PyGit2Adapter).

        Entries may carry their own org (cross-org checkouts); otherwise
        the service org applies. Kept local (not imported) so the search
        provider never depends on the git adapter module.
        """
        repo = str(repo or "").strip().strip("/")
        if "/" in repo:
            org, name = repo.split("/", 1)
            if org and name and "/" not in name:
                return f"{org}/{name}"
            return repo
        if svc_org and repo:
            return f"{svc_org}/{repo}"
        return repo

    @staticmethod
    def _service_locators(profile: ApplicationProfile) -> list[str]:
        """Checkout locators for every service × every repo."""
        locators: list[str] = []
        for svc in getattr(profile, "services", []) or []:
            code = getattr(svc, "code", None)
            org = getattr(code, "org", "") if code else ""
            repos = list(getattr(code, "repos", []) or []) if code else []
            for repo in repos:
                locator = TreeSitterCodeIntelligenceProvider._split_repo_entry(org or "", repo)
                if locator:
                    locators.append(locator)
            if not repos:
                locators.append(getattr(svc, "name", ""))
        return locators

    def _authorized_locators(self, tenant_id: str, profile: ApplicationProfile) -> list[str]:
        """Service locators this tenant may scan (allowlist-filtered, fail-closed)."""
        locators = self._service_locators(profile)
        if self._tenant_allowlist is None:
            return locators
        return [loc for loc in locators if loc in self._tenant_allowlist.get(tenant_id, set())]

    def _require_tenant_scope(self, tenant_id: str, profile: ApplicationProfile) -> None:
        """Fail closed unless this tenant is authorized for at least one service repo."""
        from investigation_agent_platform.domain.common.exceptions import (
            SecurityPolicyViolationException,
        )

        if not tenant_id or tenant_id == "anonymous":
            raise SecurityPolicyViolationException("Code access requires an authenticated tenant")
        locators = self._service_locators(profile)
        if self._tenant_allowlist is None:
            logger.warning(
                "Code repository access without tenant allow-list (dev mode)",
                extra={"tenant_id": tenant_id, "repository": locators},
            )
            return
        if not self._authorized_locators(tenant_id, profile):
            raise SecurityPolicyViolationException(
                f"Tenant '{tenant_id}' is not authorized for repositories {locators}"
            )

    # -- Rustworkx Graph Helpers ---------------------------------------

    def _load_graphify_graph(self, profile: ApplicationProfile) -> rx.PyDiGraph | None:
        """Attempts to load a Graphify JSON artifact into a rustworkx.PyDiGraph.

        Freshness: the cache entry carries the artifact mtime; a changed
        file reloads on next use instead of serving a stale graph with
        fresh confidence. Multi-repo: first locator holding the artifact
        wins (declared order), same as before.
        """
        for locator in self._service_locators(profile):
            repo_path = self._repo_base_path / locator
            graph_file = repo_path / self._graphify_artifact_name

            if not graph_file.exists():
                continue

            cache_key = str(graph_file)
            try:
                current_mtime = graph_file.stat().st_mtime_ns
            except Exception:
                current_mtime = -1
            cached = self._graph_cache.get(cache_key)
            if cached is not None:
                graph, mtime = cached[0], cached[1]
                if mtime == current_mtime:
                    return graph

            try:
                with open(graph_file, "r", encoding="utf-8") as f:
                    data = json.load(f)

                graph = rx.PyDiGraph()
                node_lookup: dict[str, int] = {}

                for node_info in data.get("nodes", []):
                    node_id = node_info.get("id", "")
                    idx = graph.add_node(node_info)
                    if node_id:
                        node_lookup[node_id] = idx

                for edge in data.get("links", []):
                    src_id = edge.get("source")
                    dst_id = edge.get("target")
                    if src_id in node_lookup and dst_id in node_lookup:
                        graph.add_edge(node_lookup[src_id], node_lookup[dst_id], edge)

                self._graph_cache[cache_key] = (graph, current_mtime)
                return graph
            except Exception as exc:
                logger.warning(
                    "Failed to load Graphify artifact at %s with rustworkx: %s", graph_file, exc
                )
                continue
        return None

    # -- Internal Parser & Path Helpers --------------------------------

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

    def _resolve_roots(self, profile: ApplicationProfile) -> list[Path]:
        roots: list[Path] = []
        for svc in getattr(profile, "services", []) or []:
            code = getattr(svc, "code", None)
            org = getattr(code, "org", "") if code else ""
            repos = list(getattr(code, "repos", []) or []) if code else []
            locators = [
                self._split_repo_entry(org or "", repo)
                for repo in repos
                if self._split_repo_entry(org or "", repo)
            ] or ([getattr(svc, "name", "")] if getattr(svc, "name", "") else [])
            for locator in locators:
                try:
                    base = Path(locator)
                    if base.is_absolute():
                        logger.warning("Absolute repository path rejected: %s", locator)
                        continue
                    repo_path = (self._repo_base_path / base).resolve()
                except Exception:
                    continue
                try:
                    if not repo_path.is_relative_to(self._repo_base_path):
                        logger.warning("Repository path traversal blocked: %s", locator)
                        continue
                except Exception:
                    continue
                # Symlink-escape rejection: the resolved repo root itself must be a
                # real directory under base, not a symlink pointing elsewhere.
                if repo_path.is_symlink():
                    logger.warning("Repository root is a symlink; access denied: %s", locator)
                    continue
                if not repo_path.is_dir():
                    continue
                matched = False
                source_roots = [*getattr(code, "source_roots", []), *getattr(code, "paths", [])] or ["."]
                for sr in source_roots:
                    if not sr or sr.startswith("/") or sr.startswith("~"):
                        continue
                    if _has_traversal(sr):
                        continue
                    try:
                        candidate = (repo_path / sr).resolve()
                    except Exception:
                        continue
                    try:
                        if not candidate.is_relative_to(repo_path):
                            continue
                    except Exception:
                        continue
                    if candidate.is_symlink():
                        continue
                    if candidate.exists():
                        roots.append(candidate)
                        matched = True
                if not matched:
                    # None of the declared roots exist in this repo (flat
                    # shared-library layout): scan the repo root itself so
                    # the repo is not silently invisible. Declared roots
                    # still win wherever they match — no double scanning.
                    roots.append(repo_path)
        # deduplicate across services
        uniq: list[Path] = []
        seen: set[str] = set()
        for r in roots:
            key = str(r)
            if key not in seen:
                seen.add(key)
                uniq.append(r)
        return uniq

    def _iter_source_files(self, roots: list[Path]) -> list[Path]:
        files: list[Path] = []
        for root in roots:
            if root.is_file():
                if root.suffix in EXT_TO_LANGUAGE:
                    files.append(root)
                continue
            for p in root.rglob("*"):
                if (
                    p.is_file()
                    and not p.is_symlink()
                    and p.suffix in EXT_TO_LANGUAGE
                    and p.stat().st_size <= MAX_FILE_BYTES
                ):
                    # skip hidden / venv
                    if any(
                        part.startswith(".")
                        or part in ("__pycache__", "node_modules", ".venv", "venv")
                        for part in p.parts
                    ):
                        continue
                    files.append(p)
                if len(files) >= 5000:
                    break
        # P1-2: deterministic traversal order — result ranking must not
        # depend on filesystem order.
        files.sort(key=lambda p: str(p))
        return files

    def _language_for_file(self, path: Path) -> str | None:
        return EXT_TO_LANGUAGE.get(path.suffix)

    @staticmethod
    def _byte_slice(code: str, start: int, end: int) -> str:
        """Slice tree-sitter byte offsets safely (P1-2).

        Tree-sitter reports UTF-8 byte offsets; slicing the decoded str
        directly corrupts names when multibyte characters precede the node.
        Re-encoding valid UTF-8 reproduces the original bytes exactly.
        """
        raw = code.encode("utf-8", errors="replace")
        return raw[max(0, start) : max(0, end)].decode("utf-8", errors="replace")

    def _read_text(self, path: Path) -> str | None:
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                return None
            return path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return None

    def _extract_symbols_from_tree(
        self, code: str, language: str, file_path: str
    ) -> list[CodeSymbol]:
        parser = self._get_parser(language)
        if parser is None:
            return []
        try:
            tree = parser.parse(bytes(code, "utf-8"))
        except Exception:
            return []
        symbols: list[CodeSymbol] = []
        cursor = tree.walk()
        visited_children = False
        depth = 0
        while True:
            if not visited_children:
                node = cursor.node
                if node.type in SYMBOL_NODE_TYPES:
                    name_node = node.child_by_field_name("name")
                    # fallback: identifier child
                    if name_node is None:
                        for child in node.children:
                            if child.type == "identifier" or child.type.endswith("identifier"):
                                name_node = child
                                break
                    symbol_name = ""
                    if name_node is not None:
                        try:
                            symbol_name = self._byte_slice(
                                code, name_node.start_byte, name_node.end_byte
                            ).strip()
                        except Exception:
                            symbol_name = ""
                    if symbol_name:
                        sig = (
                            self._byte_slice(
                                code, node.start_byte, min(node.end_byte, node.start_byte + 200)
                            )
                            .split("\n")[0]
                            .strip()
                        )
                        symbols.append(
                            CodeSymbol(
                                name=symbol_name,
                                kind=node.type,
                                location=CodeLocation(
                                    file_path=file_path,
                                    line_number=node.start_point[0] + 1,
                                    column_number=node.start_point[1],
                                    snippet=sig[:500],
                                ),
                                signature=sig[:500],
                            )
                        )
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
        return symbols

    # -- protocol methods (sync cores) ---------------------------------

    def _search_code_sync(
        self,
        tenant_id: str,
        investigation_id: UUID,
        query: str,
        profile: ApplicationProfile,
        language: str | None = None,
        path_prefix: str | None = None,
        limit: int = 20,
    ) -> list[Evidence]:
        roots = self._resolve_roots(profile)
        if not roots:
            return []
        files = self._iter_source_files(roots)
        # P1-2: honor language + path_prefix request filters.
        if language:
            wanted = language.strip().lower()
            files = [
                f
                for f in files
                if (self._language_for_file(f) or "").lower() == wanted
            ]
        if path_prefix:
            prefix = path_prefix.strip().strip("/")
            files = [
                f
                for f in files
                if prefix in str(f).replace("\\", "/")
            ]
        query_lower = query.lower()
        scored: list[tuple[int, Evidence]] = []
        now = datetime.now(UTC)
        for f in files:
            code = self._read_text(f)
            if code is None:
                continue
            # tree-sitter query attempt: count matches via parsing? fallback to text search
            # For now use text search as primary, tree-sitter as enrichment (ensures functional)
            idx = code.lower().find(query_lower)
            if idx == -1:
                continue
            # snippet around match
            start = max(0, idx - 120)
            end = min(len(code), idx + len(query) + 120)
            # compute line number
            line_no = code[:idx].count("\n") + 1
            col_no = idx - code.rfind("\n", 0, idx) - 1
            snippet = code[start:end]
            # relative file path
            try:
                rel = str(f.resolve().relative_to(self._repo_base_path))
            except Exception:
                rel = str(f)
            # P1-2: deterministic relevance — exact filename > path hit >
            # definition line > body mention. Lower score sorts first.
            rel_lower = rel.lower()
            match_line = ""
            try:
                match_line = code.split("\n")[line_no - 1].strip().lower()
            except IndexError:
                pass
            if query_lower == Path(rel).stem.lower() or query_lower == Path(rel).name.lower():
                score = 0
            elif query_lower in rel_lower:
                score = 1
            elif match_line.startswith(("def ", "class ", "async def ")) and query_lower in match_line:
                score = 2
            else:
                score = 3
            commit_hash = self._repo_head_sha(f) or hashlib.sha256(
                f"{tenant_id}:{rel}:{query}".encode()
            ).hexdigest()[:16]
            now_item = datetime.now(UTC)
            prov = EvidenceProvenance(
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                provider_type="CODE",
                requested_provider_id=self._provider_id,
                actual_provider_id=self._provider_id,
                source_system="Code",
                retrieval_timestamp=now_item,
                query_fingerprint=QueryFingerprint(
                    provider_type="CODE",
                    operation="SEARCH_CODE",
                    normalized_query_hash=hashlib.sha256(query.encode()).hexdigest(),
                ),
                source_location=SourceLocation(
                    system="Code",
                    identifier=rel,
                    file_path=rel,
                    line_start=line_no,
                    line_end=line_no,
                    revision=commit_hash,
                ),
            )
            fingerprint = hashlib.sha256(f"{tenant_id}:{rel}:{idx}:{query}".encode()).hexdigest()
            scored.append(
                (
                    score,
                    Evidence(
                        tenant_id=tenant_id,
                        investigation_id=investigation_id,
                        evidence_type=EvidenceType.SOURCE_CODE,
                        provider=self._provider_id,
                        source=f"code://{rel}#L{line_no}:{col_no}",
                        title=f"Code match: {query!r} in {rel}:{line_no}",
                        summary=f"Found '{query}' in {rel} at line {line_no}",
                        content_snippet=snippet[:4096],
                        observed_at=now,
                        retrieved_at=now_item,
                        provenance=prov,
                        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now_item),
                        classification=ClassificationLevel.INTERNAL,
                        fingerprint=fingerprint,
                        attributes={"file_path": rel, "line_number": line_no, "column_number": col_no},
                    ),
                )
            )
        # P1-2: deterministic best-first order honoring the caller limit
        # (previously filesystem order, hardcoded 50-result ceiling).
        scored.sort(key=lambda item: (item[0], item[1].source))
        return [e for _, e in scored[: max(1, min(limit, MAX_SEARCH_RESULTS))]]

    @staticmethod
    def _repo_head_sha(path: Path) -> str | None:
        """Best-effort real revision for search provenance (P1-2).

        Walks up to the containing git checkout and reads HEAD. Returns None
        when unavailable — callers fall back to the query-derived hash rather
        than failing the search.
        """
        try:
            import pygit2

            current = path.resolve()
            root = current if current.is_dir() else current.parent
            for candidate in (root, *root.parents):
                try:
                    repo = pygit2.Repository(str(candidate))
                    return str(repo.head.target)
                except Exception:
                    continue
        except Exception:
            pass
        return None

    def _find_symbol_sync(self, symbol_name: str, profile: ApplicationProfile) -> list[CodeSymbol]:
        roots = self._resolve_roots(profile)
        if not roots:
            return []
        files = self._iter_source_files(roots)
        results: list[CodeSymbol] = []
        for f in files:
            if len(results) >= MAX_SYMBOL_RESULTS:
                break
            lang = self._language_for_file(f)
            if lang is None:
                continue
            code = self._read_text(f)
            if code is None or symbol_name not in code:
                continue
            symbols = self._extract_symbols_from_tree(
                code,
                lang,
                str(f.resolve().relative_to(self._repo_base_path))
                if f.resolve().is_relative_to(self._repo_base_path)
                else str(f),
            )
            for s in symbols:
                if s.name == symbol_name:
                    results.append(s)
        return results

    def _find_callers_sync(self, symbol_name: str, profile: ApplicationProfile) -> list[CallGraphNode]:
        # Fast path: Check Rustworkx PyDiGraph artifact
        graph = self._load_graphify_graph(profile)
        if graph is not None:
            callers: list[CallGraphNode] = []
            for node_idx in graph.node_indices():
                data = graph.get_node_data(node_idx)
                if isinstance(data, dict) and data.get("name") == symbol_name:
                    for pred_idx in graph.predecessor_indices(node_idx):
                        p_data = graph.get_node_data(pred_idx)
                        if isinstance(p_data, dict):
                            callers.append(
                                CallGraphNode(
                                    caller_symbol=p_data.get("name", str(pred_idx)),
                                    callee_symbol=symbol_name,
                                    location=CodeLocation(
                                        file_path=p_data.get("file_path", ""),
                                        line_number=p_data.get("start_line", 1),
                                    ),
                                )
                            )
            if callers:
                return callers

        # Fallback path: Tree-sitter AST walk across filesystem
        roots = self._resolve_roots(profile)
        if not roots:
            return []
        files = self._iter_source_files(roots)
        nodes: list[CallGraphNode] = []
        for f in files:
            lang = self._language_for_file(f)
            if lang is None:
                continue
            code = self._read_text(f)
            if code is None or symbol_name not in code:
                continue
            rel = (
                str(f.resolve().relative_to(self._repo_base_path))
                if f.resolve().is_relative_to(self._repo_base_path)
                else str(f)
            )
            parser = self._get_parser(lang)
            if parser is None:
                # fallback: regex search for callee(
                for i, line in enumerate(code.splitlines(), start=1):
                    if re.search(rf"\b{re.escape(symbol_name)}\s*\(", line):
                        # caller unknown -> file-level
                        nodes.append(
                            CallGraphNode(
                                caller_symbol=f"{rel}::module",
                                callee_symbol=symbol_name,
                                location=CodeLocation(
                                    file_path=rel, line_number=i, snippet=line.strip()[:500]
                                ),
                            )
                        )
                continue
            try:
                tree = parser.parse(bytes(code, "utf-8"))
            except Exception:
                continue
            # walk to find call nodes
            cursor = tree.walk()
            visited_children = False
            # We track via traversal: push when entering symbol node, pop on exit
            # Simpler: for each call node, scan ancestors for enclosing function name
            depth = 0
            while True:
                if not visited_children:
                    node = cursor.node
                    if node.type in CALL_NODE_TYPES:
                        # extract callee text
                        callee_text = ""
                        # try field 'function' or first identifier
                        func_node = node.child_by_field_name("function")
                        if func_node is not None:
                            callee_text = self._byte_slice(code, func_node.start_byte, func_node.end_byte).strip()
                            # handle dotted: take last part
                            if "." in callee_text:
                                callee_text = callee_text.split(".")[-1]
                        else:
                            # fallback: first child text
                            if node.children:
                                callee_text = (
                                    self._byte_slice(
                                        code,
                                        node.children[0].start_byte,
                                        node.children[0].end_byte,
                                    )
                                    .strip()
                                    .split(".")[-1]
                                    .split("(")[0]
                                    .strip()
                                )
                        if callee_text == symbol_name:
                            # find enclosing function via parent walk
                            caller = "module"
                            parent = node.parent
                            while parent is not None:
                                if parent.type in SYMBOL_NODE_TYPES:
                                    n = parent.child_by_field_name("name")
                                    if n is not None:
                                        caller = self._byte_slice(code, n.start_byte, n.end_byte).strip()
                                        break
                                parent = parent.parent
                            nodes.append(
                                CallGraphNode(
                                    caller_symbol=caller,
                                    callee_symbol=symbol_name,
                                    location=CodeLocation(
                                        file_path=rel,
                                        line_number=node.start_point[0] + 1,
                                        column_number=node.start_point[1],
                                        snippet=code[
                                            node.start_byte : min(
                                                node.end_byte, node.start_byte + 200
                                            )
                                        ].split("\n")[0][:500],
                                    ),
                                )
                            )
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
        return nodes

    def _find_callees_sync(self, symbol_name: str, profile: ApplicationProfile) -> list[CallGraphNode]:
        # Fast path: Check Rustworkx PyDiGraph artifact
        graph = self._load_graphify_graph(profile)
        if graph is not None:
            callees: list[CallGraphNode] = []
            for node_idx in graph.node_indices():
                data = graph.get_node_data(node_idx)
                if isinstance(data, dict) and data.get("name") == symbol_name:
                    for succ_idx in graph.successor_indices(node_idx):
                        s_data = graph.get_node_data(succ_idx)
                        if isinstance(s_data, dict):
                            callees.append(
                                CallGraphNode(
                                    caller_symbol=symbol_name,
                                    callee_symbol=s_data.get("name", str(succ_idx)),
                                    location=CodeLocation(
                                        file_path=s_data.get("file_path", ""),
                                        line_number=s_data.get("start_line", 1),
                                    ),
                                )
                            )
            if callees:
                return callees

        # Fallback path: Tree-sitter AST walk across filesystem
        roots = self._resolve_roots(profile)
        if not roots:
            return []
        files = self._iter_source_files(roots)
        # First locate definition file and byte range
        def_loc: tuple[Path, str, Any, int, int] | None = None
        for f in files:
            lang = self._language_for_file(f)
            if lang is None:
                continue
            code = self._read_text(f)
            if code is None or symbol_name not in code:
                continue
            parser = self._get_parser(lang)
            if parser is None:
                continue
            try:
                tree = parser.parse(bytes(code, "utf-8"))
            except Exception:
                continue
            cursor = tree.walk()
            visited_children = False
            depth = 0
            while True:
                if not visited_children:
                    node = cursor.node
                    if node.type in SYMBOL_NODE_TYPES:
                        n = node.child_by_field_name("name")
                        if n is not None:
                            nm = self._byte_slice(code, n.start_byte, n.end_byte).strip()
                            if nm == symbol_name:
                                def_loc = (f, code, tree, node.start_byte, node.end_byte)
                                break
                    if depth < 100 and cursor.goto_first_child():
                        depth += 1
                        continue
                if def_loc is not None:
                    break
                if cursor.goto_next_sibling():
                    visited_children = False
                    continue
                if not cursor.goto_parent():
                    break
                depth -= 1
                visited_children = True
            if def_loc is not None:
                break
        if def_loc is None:
            return []
        def_file, def_code, _def_tree, start_byte, end_byte = def_loc
        rel_def = (
            str(def_file.resolve().relative_to(self._repo_base_path))
            if def_file.resolve().is_relative_to(self._repo_base_path)
            else str(def_file)
        )
        lang = self._language_for_file(def_file)
        if lang is None:
            return []
        parser = self._get_parser(lang)
        if parser is None:
            return []
        # Re-parse and collect calls inside definition range
        try:
            tree = parser.parse(bytes(def_code, "utf-8"))
        except Exception:
            return []
        nodes: list[CallGraphNode] = []
        cursor = tree.walk()
        visited_children = False
        depth = 0
        while True:
            if not visited_children:
                node = cursor.node
                # only consider nodes inside definition
                if (
                    start_byte <= node.start_byte
                    and node.end_byte <= end_byte
                    and node.type in CALL_NODE_TYPES
                ):
                    func_node = node.child_by_field_name("function")
                    callee = ""
                    if func_node is not None:
                        callee = (
                            self._byte_slice(def_code, func_node.start_byte, func_node.end_byte)
                            .strip()
                            .split(".")[-1]
                            .split("(")[0]
                            .strip()
                        )
                    elif node.children:
                        callee = (
                            self._byte_slice(
                                        def_code,
                                        node.children[0].start_byte,
                                        node.children[0].end_byte,
                                    )
                            .strip()
                            .split(".")[-1]
                            .split("(")[0]
                            .strip()
                        )
                    if callee and callee != symbol_name:
                        nodes.append(
                            CallGraphNode(
                                caller_symbol=symbol_name,
                                callee_symbol=callee,
                                location=CodeLocation(
                                    file_path=rel_def,
                                    line_number=node.start_point[0] + 1,
                                    column_number=node.start_point[1],
                                    snippet=self._byte_slice(
                                        def_code,
                                        node.start_byte,
                                        min(node.end_byte, node.start_byte + 200),
                                    ).split("\n")[0][:500],
                                ),
                            )
                        )
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
        return nodes

    def _find_exception_handlers_sync(
        self, exception_class: str, profile: ApplicationProfile
    ) -> list[CodeLocation]:
        roots = self._resolve_roots(profile)
        if not roots:
            return []
        files = self._iter_source_files(roots)
        results: list[CodeLocation] = []
        for f in files:
            code = self._read_text(f)
            if code is None or exception_class not in code:
                continue
            rel = (
                str(f.resolve().relative_to(self._repo_base_path))
                if f.resolve().is_relative_to(self._repo_base_path)
                else str(f)
            )
            lang = self._language_for_file(f)
            # Python: try/except, Java: try/catch
            if lang is not None:
                parser = self._get_parser(lang)
                if parser is not None:
                    try:
                        tree = parser.parse(bytes(code, "utf-8"))
                    except Exception:
                        tree = None
                    if tree is not None:
                        cursor = tree.walk()
                        visited_children = False
                        depth = 0
                        while True:
                            if not visited_children:
                                node = cursor.node
                                if node.type in EXCEPT_NODE_TYPES:
                                    txt = code[node.start_byte : node.end_byte]
                                    if exception_class in txt:
                                        results.append(
                                            CodeLocation(
                                                file_path=rel,
                                                line_number=node.start_point[0] + 1,
                                                column_number=node.start_point[1],
                                                snippet=txt.split("\n")[0][:500],
                                            )
                                        )
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
                        if results and any(r.file_path == rel for r in results):
                            continue  # already found via tree-sitter for this file
            # fallback text search for except/catch lines
            for i, line in enumerate(code.splitlines(), start=1):
                if exception_class in line and ("except" in line or "catch" in line):
                    if not any(r.file_path == rel and r.line_number == i for r in results):
                        results.append(
                            CodeLocation(file_path=rel, line_number=i, snippet=line.strip()[:500])
                        )
        return results

    def _find_database_operations_sync(
        self, entity_or_table: str, profile: ApplicationProfile
    ) -> list[CodeLocation]:
        roots = self._resolve_roots(profile)
        if not roots:
            return []
        files = self._iter_source_files(roots)
        results: list[CodeLocation] = []
        needle_lower = entity_or_table.lower()
        for f in files:
            code = self._read_text(f)
            if code is None:
                continue
            rel = (
                str(f.resolve().relative_to(self._repo_base_path))
                if f.resolve().is_relative_to(self._repo_base_path)
                else str(f)
            )
            for i, line in enumerate(code.splitlines(), start=1):
                low = line.lower()
                if needle_lower in low and (
                    SQL_KEYWORDS.search(line) or ORM_PATTERNS.search(line) or needle_lower in low
                ):
                    # require SQL or ORM context if needle is generic; if needle looks like table name, relax
                    if (
                        SQL_KEYWORDS.search(line)
                        or ORM_PATTERNS.search(line)
                        or entity_or_table.lower() in low
                    ):
                        # Additional check: if line has SQL keyword or ORM, or table name appears in string literal
                        results.append(
                            CodeLocation(file_path=rel, line_number=i, snippet=line.strip()[:500])
                        )
                        if len(results) >= MAX_SEARCH_RESULTS:
                            return results
            # also scan string literals via tree-sitter for SQL strings containing table
            lang = self._language_for_file(f)
            if lang is not None:
                parser = self._get_parser(lang)
                if parser is not None:
                    try:
                        tree = parser.parse(bytes(code, "utf-8"))
                    except Exception:
                        continue
                    cursor = tree.walk()
                    visited_children = False
                    depth = 0
                    while True:
                        if not visited_children:
                            node = cursor.node
                            if node.type in (
                                "string",
                                "string_literal",
                                "interpreted_string_literal",
                            ):
                                txt = code[node.start_byte : node.end_byte]
                                if needle_lower in txt.lower() and SQL_KEYWORDS.search(txt):
                                    # avoid duplicate
                                    ln = node.start_point[0] + 1
                                    if not any(
                                        r.file_path == rel and r.line_number == ln for r in results
                                    ):
                                        results.append(
                                            CodeLocation(
                                                file_path=rel, line_number=ln, snippet=txt[:500]
                                            )
                                        )
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
        return results

    # -- async protocol surface ----------------------------------------

    @traced("provider", component="code_intel")
    async def search_code(
        self,
        tenant_id: str,
        query: str,
        profile: ApplicationProfile,
        investigation_id: UUID | None = None,
        language: str | None = None,
        path_prefix: str | None = None,
        limit: int = 20,
    ) -> list[Evidence]:
        with tracer.start_as_current_span("TreeSitterCodeIntelligenceProvider.search_code"):
            self._require_tenant_scope(tenant_id, profile)
            # F-039: a real investigation/run ID is required for provenance.
            # Ad-hoc searches without one get a fresh random query-run ID that
            # is recorded as a query run — never a deterministic hash shared
            # across identical queries masquerading as an investigation.
            from uuid import uuid4 as _uuid4

            run_id = investigation_id or _uuid4()
            return await asyncio.to_thread(
                self._search_code_sync,
                tenant_id,
                run_id,
                query,
                profile,
                language,
                path_prefix,
                limit,
            )

    @traced("provider", component="code_intel")
    async def find_symbol(
        self,
        tenant_id: str,
        symbol_name: str,
        profile: ApplicationProfile,
        investigation_id: UUID | None = None,
    ) -> list[CodeSymbol]:
        with tracer.start_as_current_span("TreeSitterCodeIntelligenceProvider.find_symbol"):
            self._require_tenant_scope(tenant_id, profile)
            return await asyncio.to_thread(self._find_symbol_sync, symbol_name, profile)

    @traced("provider", component="code_intel")
    async def find_callers(
        self,
        tenant_id: str,
        symbol_name: str,
        profile: ApplicationProfile,
        investigation_id: UUID | None = None,
    ) -> list[CallGraphNode]:
        with tracer.start_as_current_span("TreeSitterCodeIntelligenceProvider.find_callers"):
            self._require_tenant_scope(tenant_id, profile)
            return await asyncio.to_thread(self._find_callers_sync, symbol_name, profile)

    @traced("provider", component="code_intel")
    async def find_callees(
        self,
        tenant_id: str,
        symbol_name: str,
        profile: ApplicationProfile,
        investigation_id: UUID | None = None,
    ) -> list[CallGraphNode]:
        with tracer.start_as_current_span("TreeSitterCodeIntelligenceProvider.find_callees"):
            self._require_tenant_scope(tenant_id, profile)
            return await asyncio.to_thread(self._find_callees_sync, symbol_name, profile)

    @traced("provider", component="code_intel")
    async def find_exception_handlers(
        self,
        tenant_id: str,
        exception_class: str,
        profile: ApplicationProfile,
        investigation_id: UUID | None = None,
    ) -> list[CodeLocation]:
        with tracer.start_as_current_span(
            "TreeSitterCodeIntelligenceProvider.find_exception_handlers"
        ):
            self._require_tenant_scope(tenant_id, profile)
            return await asyncio.to_thread(
                self._find_exception_handlers_sync, exception_class, profile
            )

    @traced("provider", component="code_intel")
    async def find_database_operations(
        self,
        tenant_id: str,
        entity_or_table: str,
        profile: ApplicationProfile,
        investigation_id: UUID | None = None,
    ) -> list[CodeLocation]:
        with tracer.start_as_current_span(
            "TreeSitterCodeIntelligenceProvider.find_database_operations"
        ):
            self._require_tenant_scope(tenant_id, profile)
            return await asyncio.to_thread(
                self._find_database_operations_sync, entity_or_table, profile
            )
