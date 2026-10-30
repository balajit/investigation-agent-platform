# src/investigation_agent_platform/infrastructure/topology/payload.py
"""AST payload construction from a local checkout (moved from scripts/ingest_github.py).

Single home for deterministic tree-sitter payload building: the Temporal
projection activity and scripts/cognee_pilot.py share this module (no forks).
Only stdlib + platform imports; no GitHub API, no network.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from pathlib import Path
from uuid import uuid4

logger = logging.getLogger(__name__)

SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "vendor",
        "third_party",
        "third-party",
        "bower_components",
        "__pycache__",
        ".venv",
        "venv",
        ".tox",
        "dist",
        "build",
        "out",
        "target",
        ".next",
        ".nuxt",
        "coverage",
        ".pytest_cache",
        ".mypy_cache",
    }
)
SKIP_SUFFIXES = (".min.js", ".bundle.js", ".map", ".pb.go", ".d.ts")
SKIP_FILES = ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock")
MAX_FILE_BYTES = 524_288  # mirrors TreeSitterParser.MAX_CODE_SIZE_BYTES
MAX_FILES_PER_REPO = 20_000  # mirrors ASTTopologyPayload source_files cap
MAX_NODE_SYMBOLS = 200_000  # mirrors ASTTopologyPayload ast_nodes cap
MAX_EDGES_PER_REPO = 200_000  # mirrors ASTTopologyPayload calls/references cap

# Language dispatch: authoritative EXT_TO_LANGUAGE first, small loader-only
# extras for extensions the provider does not map. Unknown extensions fall
# back to MODULE-only file entries (counted, no symbols).
_EXTRA_EXT_TO_LANGUAGE = {
    ".jsx": "javascript",
    ".php": "php",
    ".rb": "ruby",
    ".sh": "bash",
    ".sql": "sql",
    ".swift": "swift",
    ".kt": "kotlin",
}

_IMPORT_RE = re.compile(
    r"^\s*(?:import\s+(?:static\s+)?([\w.*]+)|from\s+([\w.]+)\s+import\s+)", re.MULTILINE
)
_JS_IMPORT_RE = re.compile(r"""^\s*import\s+(?:.*?\s+from\s+)?['"]([^'"]+)['"]""", re.MULTILINE)
_FROM_TABLE_RE = re.compile(r"\bFROM\s+[\"'`\[]?([a-zA-Z_][\w.]*)", re.IGNORECASE)


def walk_source_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn in SKIP_FILES or fn.endswith(SKIP_SUFFIXES):
                continue
            p = Path(dirpath) / fn
            try:
                if p.stat().st_size == 0 or p.stat().st_size > MAX_FILE_BYTES:
                    continue
                with p.open("rb") as fh:
                    if b"\x00" in fh.read(8192):
                        continue
            except OSError:
                continue
            files.append(p)
            if len(files) >= MAX_FILES_PER_REPO:
                return files
    return files


class PayloadTruncated(Exception):
    """Raised when a repo exceeds payload caps; the caller marks FAILED, never partial-READY."""


def _parser_version() -> str:
    try:
        from importlib.metadata import version

        return f"tree-sitter-language-pack=={version('tree-sitter-language-pack')}"
    except Exception:
        return "unknown"


async def build_payload_for_repo(
    checkout: Path,
    tenant_id: str,
    application_id: str,
    repository_id: str,
    sha: str,
) -> tuple[object, bool]:
    """Build one real `ASTTopologyPayload` for a checked-out repo revision.

    Returns (payload, truncated). Symbol identity uses `ASTNodeIdentity.create`
    (deterministic uuid5); CALLS edges persist only when both endpoints are
    macro-tier nodes in this same payload; REFERENCES resolve unique same-repo
    module imports; ACCESSES_TABLE edges come from SQL/ORM-context lines.
    Anything unresolvable is skipped with a diagnostic, never guessed.
    """
    from investigation_agent_platform.domain.topology.models import (
        ASTNodeIdentity,
        ASTTopologyPayload,
        CallEdgeInput,
        CallResolutionStatus,
        DatabaseAccessEdgeInput,
        ParseDiagnostic,
        ReferenceEdgeInput,
        SourceFileIdentity,
    )
    from investigation_agent_platform.infrastructure.evidence.code.intelligence import (
        EXT_TO_LANGUAGE,
        ORM_PATTERNS,
        SQL_KEYWORDS,
    )
    from investigation_agent_platform.infrastructure.evidence.code.micro import (
        _TREE_SITTER_KIND_MAP,  # single source of truth for grammar-kind mapping
    )
    from investigation_agent_platform.infrastructure.evidence.code.parser import TreeSitterParser

    files = await asyncio.to_thread(walk_source_files, checkout)
    parser = TreeSitterParser()
    parser_version = _parser_version()
    ext_map = {**EXT_TO_LANGUAGE, **_EXTRA_EXT_TO_LANGUAGE}

    source_files: list[SourceFileIdentity] = []
    ast_nodes: list[ASTNodeIdentity] = []
    calls: list[CallEdgeInput] = []
    references: list[ReferenceEdgeInput] = []
    db_accesses: list[DatabaseAccessEdgeInput] = []
    diagnostics: list[ParseDiagnostic] = []
    truncated = len(files) >= MAX_FILES_PER_REPO

    # Per-file symbol tables for parent linkage and edge resolution.
    file_lines: dict[str, list[str]] = {}
    file_qual_index: dict[str, dict[str, ASTNodeIdentity]] = {}
    module_index: dict[str, object] = {}

    for path in files:
        rel = path.relative_to(checkout).as_posix()
        try:
            raw = path.read_bytes()
            text = raw.decode("utf-8", errors="replace")
        except OSError as exc:
            diagnostics.append(
                ParseDiagnostic(file_path=rel[:1024], message=f"unreadable: {exc}"[:1024])
            )
            continue
        lines = text.splitlines()
        file_lines[rel] = lines
        content_hash = hashlib.sha256(raw).hexdigest()
        language = ext_map.get(path.suffix.lower(), "unknown")
        source_files.append(
            SourceFileIdentity(
                tenant_id=tenant_id,
                repository_id=repository_id,
                revision=sha,
                path=rel,
                language=language[:64] if language != "unknown" else "unknown",
                content_hash=content_hash[:128],
            )
        )
        qual_index: dict[str, ASTNodeIdentity] = {}
        file_qual_index[rel] = qual_index

        # One MODULE node per file (dotted path identity).
        dotted = rel.rsplit(".", 1)[0].replace("/", ".").replace("-", "_")
        try:
            module_node = ASTNodeIdentity.create(
                tenant_id=tenant_id,
                repository_id=repository_id,
                revision=sha,
                name=path.stem,
                qualified_name=dotted,
                node_type=__import__(
                    "investigation_agent_platform.domain.topology.models",
                    fromlist=["TopologyNodeType"],
                ).TopologyNodeType.MODULE,
                file_path=rel,
                start_line=1,
                end_line=max(1, len(lines)),
                parser_version=parser_version[:64],
            )
        except ValueError as exc:
            diagnostics.append(ParseDiagnostic(file_path=rel[:1024], message=str(exc)[:1024]))
            continue
        ast_nodes.append(module_node)
        module_index[dotted] = module_node
        module_index[dotted.replace(".", "/")] = module_node
        module_index.setdefault(f"stem:{path.stem}", []).append(module_node)
        qual_index[dotted] = module_node

        if language == "unknown":
            continue  # MODULE-only fallback: counted, no symbols.
        try:
            symbols = await parser.parse_symbols(text, language)
        except Exception as exc:  # per-file containment, never whole-repo fail
            diagnostics.append(ParseDiagnostic(file_path=rel[:1024], message=str(exc)[:1024]))
            continue
        for sym in symbols:
            if len(ast_nodes) >= MAX_NODE_SYMBOLS:
                truncated = True
                break
            from investigation_agent_platform.domain.topology.models import TopologyNodeType

            node_type: TopologyNodeType | None = _TREE_SITTER_KIND_MAP.get(sym.symbol_type)  # type: ignore[assignment]
            if node_type is None:
                diagnostics.append(
                    ParseDiagnostic(
                        file_path=rel[:1024],
                        message=f"unmapped grammar kind '{sym.symbol_type}' for '{sym.name}'; skipped"[
                            :1024
                        ],
                        severity="WARNING",
                    )
                )
                continue
            qualified = f"{sym.parent_symbol}.{sym.name}" if sym.parent_symbol else sym.name
            parent_id = None
            if sym.parent_symbol:
                parent = qual_index.get(sym.parent_symbol) or next(
                    (n for q, n in qual_index.items() if q.endswith(f".{sym.parent_symbol}")), None
                )
                parent_id = parent.node_id if parent is not None else None
            try:
                node = ASTNodeIdentity.create(
                    tenant_id=tenant_id,
                    repository_id=repository_id,
                    revision=sha,
                    name=sym.name,
                    qualified_name=qualified,
                    node_type=node_type,
                    file_path=rel,
                    start_line=sym.start_line,
                    end_line=sym.end_line,
                    start_column=sym.start_column,
                    end_column=sym.end_column,
                    signature=sym.signature,
                    parent_node_id=parent_id,
                    parser_version=parser_version[:64],
                )
            except ValueError as exc:
                diagnostics.append(ParseDiagnostic(file_path=rel[:1024], message=str(exc)[:1024]))
                continue
            ast_nodes.append(node)
            qual_index.setdefault(qualified, node)

    macro_ids = {n.node_id for n in ast_nodes if n.granularity == "macro"}
    macro_by_file: dict[str, dict[str, list[ASTNodeIdentity]]] = {}
    for node in ast_nodes:
        if node.node_id not in macro_ids or node.node_type.value == "MODULE":  # type: ignore[union-attr]
            continue
        bucket = macro_by_file.setdefault(node.file_path, {}).setdefault(node.name, [])
        bucket.append(node)

    for rel, name_map in macro_by_file.items():
        lines = file_lines.get(rel, [])
        # Unique names only: ambiguous callees are skipped, never guessed.
        unique = {name: nodes[0] for name, nodes in name_map.items() if len(nodes) == 1}
        for caller_name, callers in name_map.items():
            for caller in callers:
                if len(calls) + len(references) >= MAX_EDGES_PER_REPO:
                    truncated = True
                    break
                body = "\n".join(lines[caller.start_line - 1 : caller.end_line])
                seen: set[str] = set()
                for callee_name, callee in unique.items():
                    if callee_name == caller_name or callee_name in seen:
                        continue
                    match = re.search(r"\b" + re.escape(callee_name) + r"\s*\(", body)
                    if match is None:
                        continue
                    seen.add(callee_name)
                    line_no = caller.start_line + body[: match.start()].count("\n")
                    calls.append(
                        CallEdgeInput(
                            caller_node_id=caller.node_id,
                            callee_node_id=callee.node_id,
                            callee_qualified_name=callee.qualified_name,
                            call_site_file=rel,
                            call_site_line=line_no,
                            resolution_status=CallResolutionStatus.RESOLVED,
                            confidence=0.7,
                        )
                    )
                    if len(calls) + len(references) >= MAX_EDGES_PER_REPO:
                        truncated = True
                        break

        # REFERENCES from imports, resolved to unique same-repo MODULE nodes only.
        module_node = next(
            (n for n in ast_nodes if n.file_path == rel and n.node_type.value == "MODULE"), None
        )  # type: ignore[union-attr]
        if module_node is None:
            continue
        imported: dict[str, int] = {}
        full_text = "\n".join(lines)
        for m in _IMPORT_RE.finditer(full_text):
            mod = (m.group(1) or m.group(2) or "").strip().rstrip(";")
            if mod and mod != "static":
                imported[mod] = full_text.count("\n", 0, m.start()) + 1
        for m in _JS_IMPORT_RE.finditer(full_text):
            spec = m.group(1)
            if spec.startswith("."):
                mod = Path(rel).parent.joinpath(spec).as_posix().replace("/", ".").replace("-", "_")
                mod = re.sub(r"\.+$", "", mod.rsplit(".", 1)[0] if "." in spec else mod)
                imported[mod.split(".")[-1]] = full_text.count("\n", 0, m.start()) + 1
        for mod, line_no in imported.items():
            if len(calls) + len(references) >= MAX_EDGES_PER_REPO:
                truncated = True
                break
            # Resolution order: exact dotted/slashed module path, then a
            # unique same-stem file — first segment (Python package root),
            # then last segment (Java imported class). External packages and
            # ambiguous stems are skipped, never guessed.
            candidate = module_index.get(mod) or module_index.get(mod.replace(".", "/"))
            target = candidate if not isinstance(candidate, list) else None
            if target is None:
                parts = mod.split(".")
                for stem in dict.fromkeys([parts[0], parts[-1]]):
                    stems = module_index.get(f"stem:{stem}", [])
                    stems = [n for n in stems if isinstance(n, ASTNodeIdentity)]
                    if len(stems) == 1:
                        target = stems[0]
                        break
            if target is None or target.node_id == module_node.node_id:
                continue
            references.append(
                ReferenceEdgeInput(
                    source_node_id=module_node.node_id,
                    target_node_id=target.node_id,
                    reference_type="IMPORTS",
                    file_path=rel,
                    line_number=line_no,
                    confidence=0.8,
                )
            )

        # ACCESSES_TABLE from SQL/ORM-context lines, enclosed-symbol resolution.
        for idx, line in enumerate(lines, start=1):
            if not (SQL_KEYWORDS.search(line) or ORM_PATTERNS.search(line)):
                continue
            tm = _FROM_TABLE_RE.search(line)
            if tm is None:
                continue
            table = tm.group(1).lower()[:256]
            enclosing = None
            for node in ast_nodes:
                if (
                    node.file_path == rel
                    and node.node_id in macro_ids
                    and node.start_line <= idx <= node.end_line
                ):
                    if enclosing is None or (node.end_line - node.start_line) < (
                        enclosing.end_line - enclosing.start_line
                    ):
                        enclosing = node
            if enclosing is None:
                continue
            if len(calls) + len(db_accesses) >= MAX_EDGES_PER_REPO:
                truncated = True
                break
            op = "SELECT" if "SELECT" in line.upper() else "READ"
            db_accesses.append(
                DatabaseAccessEdgeInput(
                    source_node_id=enclosing.node_id,
                    target_entity_or_table=table,
                    operation_type=op[:64],
                    query_fingerprint=hashlib.sha256(line.encode()).hexdigest()[:64],
                )
            )

    canonical = json.dumps(
        {
            "files": sorted(f.path for f in source_files),
            "nodes": sorted(str(n.node_id) for n in ast_nodes),
            "edges": sorted(
                [f"{c.caller_node_id}>{c.callee_node_id}" for c in calls]
                + [f"{r.source_node_id}>{r.target_node_id}" for r in references]
            ),
            "revision": sha,
        },
        sort_keys=True,
    )
    payload = ASTTopologyPayload(
        schema_version="v1",
        parser_version=parser_version[:64],
        tenant_id=tenant_id,
        application_id=application_id,
        repository_id=repository_id,
        revision=sha,
        snapshot_id=uuid4(),
        source_files=source_files[:20000],
        ast_nodes=ast_nodes[:200000],
        calls=calls[:200000],
        references=references[:200000],
        database_accesses=db_accesses[:20000],
        diagnostics=diagnostics[:1000],
        payload_hash=hashlib.sha256(canonical.encode()).hexdigest(),
    )
    return payload, truncated
