# src/investigation_agent_platform/infrastructure/evidence/code/pipeline.py
import asyncio
import json
import logging
from collections.abc import Sequence
from pathlib import Path

import rustworkx as rx

from investigation_agent_platform.infrastructure.evidence.code.graphify_adapter import (
    CodeSymbolGraphBuilder,
)
from investigation_agent_platform.infrastructure.evidence.code.parser import TreeSitterParser
from investigation_agent_platform.infrastructure.evidence.schema.sql import SchemaParser

logger = logging.getLogger(__name__)


class CodebaseGraphPipeline:
    """Pipeline orchestration combining custom AST Parsing with Graphify graph generation."""

    def __init__(self, parser: TreeSitterParser) -> None:
        self.parser = parser
        self.builder = CodeSymbolGraphBuilder()
        self.schema_parser = SchemaParser()

    async def build_repository_graph(
        self, repo_root: Path, language: str = "python", ddl_paths: Sequence[Path] | None = None
    ) -> rx.PyDiGraph:
        """Scans directory, parses ASTs in parallel, and forms Graphify knowledge structure.

        When ``ddl_paths`` is provided, each SQL DDL file is parsed with
        ``SchemaParser`` and its tables/columns are ingested into the same
        graph via ``CodeSymbolGraphBuilder.add_database_schema_nodes``,
        enabling ``link_code_to_tables`` to connect code symbols to the
        database resources they access. Defaults to ``None`` so existing
        callers are unaffected.
        """

        # Supported file extensions map
        ext_map = {"python": "*.py", "javascript": "*.js", "typescript": "*.ts"}
        glob_pattern = ext_map.get(language, "*.py")

        file_paths = list(repo_root.rglob(glob_pattern))
        logger.info(f"Starting AST parsing for {len(file_paths)} files via TreeSitterParser...")

        async def _process_file(file_path: Path) -> None:
            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
                symbols = await self.parser.parse_symbols(content, language)
                rel_path = str(file_path.relative_to(repo_root))

                # Update Graph Builder
                self.builder.add_file_symbols(rel_path, symbols)
                self.builder.build_call_graph_edges(content.encode("utf-8"), rel_path, symbols)
            except Exception as e:
                logger.warning(f"Failed to process file {file_path}: {e}")

        # Process AST parsing concurrently off-thread using gather
        await asyncio.gather(*[_process_file(fp) for fp in file_paths])

        if ddl_paths:
            for ddl_path in ddl_paths:
                try:
                    tables = self.schema_parser.parse_ddl_file(ddl_path)
                    self.builder.add_database_schema_nodes(tables)
                    logger.info(
                        "Ingested %d database tables from %s",
                        len(tables),
                        ddl_path,
                    )
                except Exception as e:
                    logger.warning(f"Failed to ingest schema file {ddl_path}: {e}")

        logger.info("AST parsing complete. Graph node count: %d", self.builder.graph.num_nodes())
        return self.builder.graph

    def export_graphify_format(self, output_file: Path) -> None:
        """Exports graph to standard Graphify JSON format for downstream agent tools."""
        nodes = []
        for idx in self.builder.graph.node_indices():
            ndata = self.builder.graph.get_node_data(idx)
            if isinstance(ndata, dict):
                nodes.append(ndata)
            else:
                nodes.append({"id": str(idx), "label": str(ndata)})

        links = []
        for src_idx, dst_idx, edata in self.builder.graph.weighted_edge_list():
            src_data = self.builder.graph.get_node_data(src_idx)
            dst_data = self.builder.graph.get_node_data(dst_idx)

            src_id = (
                src_data.get("id", str(src_idx)) if isinstance(src_data, dict) else str(src_idx)
            )
            dst_id = (
                dst_data.get("id", str(dst_idx)) if isinstance(dst_data, dict) else str(dst_idx)
            )

            link_item = {"source": src_id, "target": dst_id}
            if isinstance(edata, dict):
                link_item.update(edata)
            links.append(link_item)

        data = {"nodes": nodes, "links": links}
        output_file.parent.mkdir(parents=True, exist_ok=True)

        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        logger.info("Successfully exported Graphify artifact to %s", output_file)
