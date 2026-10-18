import asyncio
import logging
from typing import Any

from opentelemetry import trace
from pydantic import BaseModel, ConfigDict

from investigation_agent_platform.domain.common.exceptions import ExecutionError

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

MAX_AST_DEPTH = 100
MAX_CODE_SIZE_BYTES = 524_288  # 512 KB limit for parsing


class CodeSymbol(BaseModel):
    """Structured code symbol extracted from AST parsing."""

    model_config = ConfigDict(frozen=True)

    name: str
    symbol_type: str
    start_line: int
    start_column: int
    end_line: int
    end_column: int
    signature: str = ""
    parent_symbol: str | None = None


class TreeSitterParser:
    """Asynchronous Tree-Sitter AST parser running off-thread for non-blocking analysis."""

    def __init__(self) -> None:
        self._parsers: dict[str, Any] = {}

    def _get_parser(self, language: str) -> Any:
        if language not in self._parsers:
            try:
                # Upgraded to tree_sitter_language_pack for maintained C bindings
                from tree_sitter_language_pack import get_parser  # type: ignore[import-not-found]

                self._parsers[language] = get_parser(language)
            except Exception as exc:
                logger.error(
                    "Tree-sitter parser initialization failed",
                    extra={"context": {"language": language}},
                )
                raise ExecutionError(f"Unsupported language parser '{language}': {exc}") from exc
        return self._parsers[language]

    async def parse_symbols(self, code_content: str, language: str) -> list[CodeSymbol]:
        with tracer.start_as_current_span("TreeSitterParser.parse_symbols"):
            code_bytes = code_content.encode("utf-8")
            if len(code_bytes) > MAX_CODE_SIZE_BYTES:
                raise ExecutionError(
                    f"Code size exceeds maximum limit of {MAX_CODE_SIZE_BYTES} bytes"
                )
            return await asyncio.to_thread(self._parse_symbols_sync, code_bytes, language)

    def _parse_symbols_sync(self, code_bytes: bytes, language: str) -> list[CodeSymbol]:
        parser = self._get_parser(language)
        tree = parser.parse(code_bytes)

        symbols: list[CodeSymbol] = []
        cursor = tree.walk()
        visited_children = False
        depth = 0

        target_types = {
            "method_declaration",
            "function_definition",
            "async_function_definition",
            "class_declaration",
            "class_definition",
            "interface_declaration",
            "function_declarator",
        }

        # Track parent stack to populate parent_symbol
        parent_stack: list[tuple[str, int]] = []  # (symbol_name, depth)

        while True:
            if not visited_children:
                node = cursor.node
                if node.type in target_types:
                    name_node = node.child_by_field_name("name")

                    # FIX 1: Extract string slices strictly from raw bytes, then decode
                    if name_node:
                        symbol_name = code_bytes[name_node.start_byte : name_node.end_byte].decode(
                            "utf-8", errors="replace"
                        )
                    else:
                        symbol_name = "anonymous"

                    # Determine parent symbol from active stack
                    current_parent = parent_stack[-1][0] if parent_stack else None

                    # Extract line signature securely from bytes
                    sig_bytes = code_bytes[
                        node.start_byte : min(node.end_byte, node.start_byte + 128)
                    ]
                    signature_str = sig_bytes.decode("utf-8", errors="replace").split("\n")[0]

                    symbols.append(
                        CodeSymbol(
                            name=symbol_name,
                            symbol_type=node.type,
                            start_line=node.start_point[0] + 1,
                            start_column=node.start_point[1],
                            end_line=node.end_point[0] + 1,
                            end_column=node.end_point[1],
                            signature=signature_str,
                            parent_symbol=current_parent,
                        )
                    )

                    # FIX 2: Push current symbol onto parent tracking stack
                    parent_stack.append((symbol_name, depth))

                if depth < MAX_AST_DEPTH and cursor.goto_first_child():
                    depth += 1
                    continue

            if cursor.goto_next_sibling():
                visited_children = False
                # Pop symbols from stack if sibling is reached at current depth
                while parent_stack and parent_stack[-1][1] >= depth:
                    parent_stack.pop()
                continue

            if not cursor.goto_parent():
                break

            depth -= 1
            visited_children = True
            # Pop stack when unwinding parent nodes
            while parent_stack and parent_stack[-1][1] > depth:
                parent_stack.pop()

        return symbols
