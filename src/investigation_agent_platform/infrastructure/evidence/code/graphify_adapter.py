# src/investigation_agent_platform/infrastructure/evidence/code/graphify_adapter.py
import logging
from collections.abc import Sequence

import rustworkx as rx

from investigation_agent_platform.infrastructure.evidence.code.parser import CodeSymbol
from investigation_agent_platform.infrastructure.evidence.schema.sql import TableSchema
from investigation_agent_platform.ports.evidence.code import CodeLocation

logger = logging.getLogger(__name__)


class CodeSymbolGraphBuilder:
    """Combines custom TreeSitterParser symbol outputs into a Graphify-compatible Rustworkx structure."""

    def __init__(self) -> None:
        self.graph = rx.PyDiGraph()
        self._node_map: dict[str, int] = {}

    def add_file_symbols(self, file_path: str, symbols: Sequence[CodeSymbol]) -> None:
        """Ingests symbols from a single file and creates structural nodes & hierarchy edges."""
        # Add file node as a top-level container
        if file_path not in self._node_map:
            file_idx = self.graph.add_node(
                {
                    "id": file_path,
                    "node_type": "source_file",
                    "label": file_path,
                }
            )
            self._node_map[file_path] = file_idx
        else:
            file_idx = self._node_map[file_path]

        symbol_node_map: dict[str, str] = {}

        for sym in symbols:
            # Construct a deterministic unique identifier for the graph node
            node_id = (
                f"{file_path}::{sym.parent_symbol + '.' if sym.parent_symbol else ''}{sym.name}"
            )
            symbol_node_map[sym.name] = node_id

            sym_data = {
                "id": node_id,
                "name": sym.name,
                "symbol_type": sym.symbol_type,
                "file_path": file_path,
                "start_line": sym.start_line,
                "end_line": sym.end_line,
                "signature": sym.signature,
                "node_type": "code_symbol",
            }

            if node_id not in self._node_map:
                sym_idx = self.graph.add_node(sym_data)
                self._node_map[node_id] = sym_idx
            else:
                sym_idx = self._node_map[node_id]

            # Edge 1: Parent-Child structural hierarchy (Class -> Method)
            if sym.parent_symbol and sym.parent_symbol in symbol_node_map:
                parent_id = symbol_node_map[sym.parent_symbol]
                if parent_id in self._node_map:
                    parent_idx = self._node_map[parent_id]
                    self.graph.add_edge(parent_idx, sym_idx, {"relation": "CONTAINS"})
            else:
                # Top-level functions/classes belong directly to the source file
                self.graph.add_edge(file_idx, sym_idx, {"relation": "DEFINES"})

    def build_call_graph_edges(
        self, raw_code_bytes: bytes, file_path: str, symbols: Sequence[CodeSymbol]
    ) -> None:
        """
        Lightweight heuristic parser pass: Connects function call references (CALLS edges)
        between extracted symbols across the graph.
        """
        code_str = raw_code_bytes.decode("utf-8", errors="replace")

        file_idx = self._node_map.get(file_path)
        if file_idx is None:
            return

        all_symbol_nodes = [
            (idx, data)
            for idx in self.graph.node_indices()
            if isinstance(data := self.graph.get_node_data(idx), dict)
            and data.get("node_type") == "code_symbol"
        ]

        # Scan function signatures or body references to establish cross-call edges
        for node_idx, data in all_symbol_nodes:
            symbol_name = data.get("name")
            if symbol_name and symbol_name in code_str:
                # Add edge if a file/function references another known symbol
                self.graph.add_edge(file_idx, node_idx, {"relation": "REFERENCES"})

    def add_database_schema_nodes(self, tables: Sequence[TableSchema]) -> None:
        """Ingest pre-indexed database tables into the rustworkx graph.

        Idempotent: re-ingesting the same tables never duplicates nodes —
        existing ``db_table::`` / ``db_column::`` IDs are skipped, so the
        pipeline and retries can safely call this repeatedly.
        """
        for table in tables:
            tbl_node_id = f"db_table::{table.name}"
            tbl_idx = self._node_map.get(tbl_node_id)
            if tbl_idx is None:
                tbl_idx = self.graph.add_node(
                    {
                        "id": tbl_node_id,
                        "name": table.name,
                        "node_type": "database_table",
                    }
                )
                self._node_map[tbl_node_id] = tbl_idx

            for col in table.columns:
                col_node_id = f"db_column::{table.name}.{col.name}"
                if col_node_id in self._node_map:
                    continue
                col_idx = self.graph.add_node(
                    {
                        "id": col_node_id,
                        "name": col.name,
                        "data_type": col.data_type,
                        "node_type": "database_column",
                    }
                )
                self._node_map[col_node_id] = col_idx
                self.graph.add_edge(tbl_idx, col_idx, {"relation": "HAS_COLUMN"})

    def link_code_to_tables(self, file_path: str, function_name: str, table_name: str) -> bool:
        """Link one code symbol to one database table with an ``ACCESSES_TABLE`` edge.

        Resolution order: exact ``{file_path}::{function_name}`` match first
        (callers should pass the qualified name, e.g. ``ClassName.method``),
        then an unambiguous qualified-suffix match within the same file.
        Returns ``True`` when an edge was created, ``False`` when the symbol
        or table is absent or the name is ambiguous — ambiguity never links
        opportunistically.
        """
        tbl_node_id = f"db_table::{table_name}"
        if tbl_node_id not in self._node_map:
            logger.debug("link_code_to_tables: unknown table %s", table_name)
            return False

        exact_node_id = f"{file_path}::{function_name}"
        if exact_node_id in self._node_map:
            func_node_id = exact_node_id
        else:
            candidates = [
                node_id
                for node_id in self._node_map
                if node_id.startswith(f"{file_path}::")
                and (
                    node_id.endswith(f".{function_name}") or node_id.endswith(f"::{function_name}")
                )
            ]
            # Deduplicate: the exact form may already appear via the suffix rule.
            candidates = sorted(set(candidates))
            if len(candidates) != 1:
                logger.debug(
                    "link_code_to_tables: %d candidates for %s in %s; skipping",
                    len(candidates),
                    function_name,
                    file_path,
                )
                return False
            func_node_id = candidates[0]

        self.graph.add_edge(
            self._node_map[func_node_id],
            self._node_map[tbl_node_id],
            {"relation": "ACCESSES_TABLE"},
        )
        return True

    def link_evidence_to_tables(self, locations: Sequence[CodeLocation], table_name: str) -> int:
        """Link runtime-evidenced database operations to a known table.

        For each ``CodeLocation`` (a provenanced observation that some line of
        code accesses ``table_name``, e.g. from
        ``TreeSitterCodeIntelligenceProvider.find_database_operations``),
        finds the enclosing code-symbol node in the same file whose
        ``[start_line, end_line]`` range contains ``location.line_number``.

        Links only on an unambiguous match — exactly one enclosing symbol.
        Ambiguous (multiple enclosing candidates) or unmatched (no enclosing
        symbol, or unknown table) locations are skipped and debug-logged,
        never linked opportunistically. Returns the number of
        ``ACCESSES_TABLE`` edges created.
        """
        tbl_node_id = f"db_table::{table_name}"
        if tbl_node_id not in self._node_map:
            logger.debug("link_evidence_to_tables: unknown table %s", table_name)
            return 0

        linked = 0
        for location in locations:
            candidates = [
                node_id
                for node_id, idx in self._node_map.items()
                if isinstance(data := self.graph.get_node_data(idx), dict)
                and data.get("node_type") == "code_symbol"
                and data.get("file_path") == location.file_path
                and data.get("start_line") is not None
                and data.get("end_line") is not None
                and data["start_line"] <= location.line_number <= data["end_line"]
            ]
            if not candidates:
                logger.debug(
                    "link_evidence_to_tables: no enclosing symbol for %s:%d; skipping",
                    location.file_path,
                    location.line_number,
                )
                continue

            # Nesting (e.g. a method inside a class) means multiple symbols
            # legitimately enclose the same line; the innermost (smallest
            # range) is the real match. Only a tie at the smallest range is
            # genuine ambiguity (e.g. two same-named symbols) and refused.
            def _range_size(node_id: str) -> int:
                data = self.graph.get_node_data(self._node_map[node_id])
                return int(data["end_line"]) - int(data["start_line"])

            candidates.sort(key=_range_size)
            smallest = _range_size(candidates[0])
            innermost = [c for c in candidates if _range_size(c) == smallest]
            if len(innermost) != 1:
                logger.debug(
                    "link_evidence_to_tables: %d ambiguous innermost symbols for %s:%d; skipping",
                    len(innermost),
                    location.file_path,
                    location.line_number,
                )
                continue

            node_id = innermost[0]
            # node_id is "{file_path}::{qualified_name}"; passing the
            # qualified suffix through guarantees link_code_to_tables's
            # exact-match resolves to *this* node, not a same-named symbol
            # elsewhere in the file (the ambiguity case link_code_to_tables
            # itself refuses).
            qualified_name = node_id.split("::", 1)[1] if "::" in node_id else None
            if not qualified_name:
                continue

            if self.link_code_to_tables(location.file_path, qualified_name, table_name):
                linked += 1

        return linked
