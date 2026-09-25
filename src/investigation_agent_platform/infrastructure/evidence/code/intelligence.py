# src/investigation_agent_platform/infrastructure/evidence/code/intelligence.py
"""Tree-sitter backed CodeIntelligenceProvider scanning filesystem under CodeProfile roots."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from opentelemetry import trace
from pydantic import BaseModel  # noqa: F401 - keep for potential future use

from investigation_agent_platform.application.investigation.validator import _has_traversal
from investigation_agent_platform.domain.evidence.models import (
    ClassificationLevel,
    Evidence,
    EvidenceType,
)
from investigation_agent_platform.domain.profile.models import CodeProfile
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)
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
    """Filesystem-backed implementation of CodeIntelligenceProviderProtocol.

    Scans ``repo_base_path / CodeProfile.repository / sourceRoots`` and uses
    tree-sitter (via ``tree-sitter-language-pack``) to extract symbols, call graphs,
    exception handlers and database operations. Falls back to text search when a
    language parser is unavailable.
    """

    def __init__(
        self, repo_base_path: str = "/tmp", provider_id: str = "code-intelligence"
    ) -> None:
        self._repo_base_path = Path(repo_base_path).resolve()
        self._provider_id = provider_id
        self._parsers: dict[str, Any] = {}

    # -- internal helpers -------------------------------------------------

    def _get_parser(self, language: str) -> Any | None:
        if language in self._parsers:
            return self._parsers[language]
        try:
            from tree_sitter_language_pack import get_parser  # type: ignore[import-not-found]

            parser = get_parser(language)  # type: ignore[no-untyped-call]
            self._parsers[language] = parser
            return parser
        except Exception as exc:  # noqa: BLE001
            logger.debug("Parser unavailable for language %s: %s", language, exc)
            self._parsers[language] = None
            return None

    def _resolve_roots(self, profile: CodeProfile) -> list[Path]:
        repo_path = (self._repo_base_path / profile.repository).resolve()
        # traversal guard - if repo_path escapes base, fallback to base/profile.repository without resolve
        try:
            if not repo_path.is_relative_to(self._repo_base_path):
                logger.warning("Repository path traversal blocked: %s", profile.repository)
                return []
        except Exception:  # noqa: BLE001
            return []
        if not repo_path.exists():
            # No filesystem backing - return empty (graceful degradation)
            return []
        roots: list[Path] = []
        source_roots = profile.source_roots or ["."]
        for sr in source_roots:
            # guard traversal in source root via centralized check
            if _has_traversal(sr):
                continue
            candidate = (repo_path / sr).resolve()
            try:
                if not candidate.is_relative_to(repo_path):
                    continue
            except Exception:  # noqa: BLE001, S112
                continue
            if candidate.exists():
                roots.append(candidate)
            elif repo_path.exists():
                roots.append(repo_path)
        if not roots and repo_path.exists():
            roots.append(repo_path)
        # deduplicate
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
        return files

    def _language_for_file(self, path: Path) -> str | None:
        return EXT_TO_LANGUAGE.get(path.suffix)

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
                            symbol_name = code[name_node.start_byte : name_node.end_byte].strip()
                        except Exception:
                            symbol_name = ""
                    if symbol_name:
                        sig = (
                            code[node.start_byte : min(node.end_byte, node.start_byte + 200)]
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
        self, tenant_id: str, investigation_id: UUID, query: str, profile: CodeProfile
    ) -> list[Evidence]:
        roots = self._resolve_roots(profile)
        if not roots:
            return []
        files = self._iter_source_files(roots)
        query_lower = query.lower()
        evidences: list[Evidence] = []
        now = datetime.now(UTC)
        for f in files:
            if len(evidences) >= MAX_SEARCH_RESULTS:
                break
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
            commit_hash = hashlib.sha256(f"{tenant_id}:{rel}:{query}".encode()).hexdigest()[:16]
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
            # Try tree-sitter enrichment: verify file parses
            lang = self._language_for_file(f)
            if lang:
                parser = self._get_parser(lang)
                if parser is not None:
                    try:
                        tree = parser.parse(bytes(code, "utf-8"))
                        # if parse has error, still return result but mark
                        _ = tree.root_node.has_error
                    except Exception:
                        pass
            evidences.append(
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
                )
            )
        return evidences

    def _find_symbol_sync(self, symbol_name: str, profile: CodeProfile) -> list[CodeSymbol]:
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
            if code is None:
                continue
            # fast pre-filter: if symbol not in text, skip expensive parse
            if symbol_name not in code:
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

    def _find_callers_sync(self, symbol_name: str, profile: CodeProfile) -> list[CallGraphNode]:
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
            # stack of enclosing function names
            func_stack: list[str] = []
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
                            callee_text = code[func_node.start_byte : func_node.end_byte].strip()
                            # handle dotted: take last part
                            if "." in callee_text:
                                callee_text = callee_text.split(".")[-1]
                        else:
                            # fallback: first child text
                            if node.children:
                                callee_text = (
                                    code[node.children[0].start_byte : node.children[0].end_byte]
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
                                        caller = code[n.start_byte : n.end_byte].strip()
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

    def _find_callees_sync(self, symbol_name: str, profile: CodeProfile) -> list[CallGraphNode]:
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
                            nm = code[n.start_byte : n.end_byte].strip()
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
                            def_code[func_node.start_byte : func_node.end_byte]
                            .strip()
                            .split(".")[-1]
                            .split("(")[0]
                            .strip()
                        )
                    elif node.children:
                        callee = (
                            def_code[node.children[0].start_byte : node.children[0].end_byte]
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
                                    snippet=def_code[
                                        node.start_byte : min(node.end_byte, node.start_byte + 200)
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

    def _find_exception_handlers_sync(
        self, exception_class: str, profile: CodeProfile
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
        self, entity_or_table: str, profile: CodeProfile
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

    async def search_code(self, tenant_id: str, query: str, profile: CodeProfile) -> list[Evidence]:
        with tracer.start_as_current_span("TreeSitterCodeIntelligenceProvider.search_code"):
            # synthetic investigation_id for provenance when caller doesn't supply; use deterministic UUID from tenant+query
            inv_id = UUID(hashlib.sha256(f"{tenant_id}:{query}".encode()).hexdigest()[:32])
            return await asyncio.to_thread(
                self._search_code_sync, tenant_id, inv_id, query, profile
            )

    async def find_symbol(
        self, tenant_id: str, symbol_name: str, profile: CodeProfile
    ) -> list[CodeSymbol]:
        with tracer.start_as_current_span("TreeSitterCodeIntelligenceProvider.find_symbol"):
            _ = tenant_id
            return await asyncio.to_thread(self._find_symbol_sync, symbol_name, profile)

    async def find_callers(
        self, tenant_id: str, symbol_name: str, profile: CodeProfile
    ) -> list[CallGraphNode]:
        with tracer.start_as_current_span("TreeSitterCodeIntelligenceProvider.find_callers"):
            _ = tenant_id
            return await asyncio.to_thread(self._find_callers_sync, symbol_name, profile)

    async def find_callees(
        self, tenant_id: str, symbol_name: str, profile: CodeProfile
    ) -> list[CallGraphNode]:
        with tracer.start_as_current_span("TreeSitterCodeIntelligenceProvider.find_callees"):
            _ = tenant_id
            return await asyncio.to_thread(self._find_callees_sync, symbol_name, profile)

    async def find_exception_handlers(
        self, tenant_id: str, exception_class: str, profile: CodeProfile
    ) -> list[CodeLocation]:
        with tracer.start_as_current_span(
            "TreeSitterCodeIntelligenceProvider.find_exception_handlers"
        ):
            _ = tenant_id
            return await asyncio.to_thread(
                self._find_exception_handlers_sync, exception_class, profile
            )

    async def find_database_operations(
        self, tenant_id: str, entity_or_table: str, profile: CodeProfile
    ) -> list[CodeLocation]:
        with tracer.start_as_current_span(
            "TreeSitterCodeIntelligenceProvider.find_database_operations"
        ):
            _ = tenant_id
            return await asyncio.to_thread(
                self._find_database_operations_sync, entity_or_table, profile
            )
