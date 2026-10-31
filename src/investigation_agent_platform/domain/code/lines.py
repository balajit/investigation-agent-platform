# src/investigation_agent_platform/domain/code/lines.py
"""Source line splitting that matches git, IDEs and tree-sitter row counting."""

from __future__ import annotations


def split_source_lines(text: str) -> list[str]:
    """Split on ``\\n`` only; fold a trailing ``\\r`` (CRLF); drop the final empty segment.

    ``""`` has no lines; ``"a"`` and ``"a\\n"`` both have one. Form feed,
    U+2028/2029, ``\\x85`` and a lone ``\\r`` inside a line stay inside it.
    """
    if not text:
        return []
    parts = text.split("\n")
    if parts[-1] == "":
        parts.pop()
    return [part.removesuffix("\r") for part in parts]
