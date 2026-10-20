"""Deterministic, revision-pinned Java field initializer/default resolution."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Any, Literal

from investigation_agent_platform.domain.code.models import (
    CodeFactResolution,
    CodeFactStatus,
    CodeValueSemantics,
)
from investigation_agent_platform.domain.evidence.models import EvidenceType

_FIELD_TYPES = frozenset({"field_declaration"})
_CLASS_TYPES = frozenset({"class_declaration"})
_INTEGER_SUFFIX_RE = re.compile(r"(?i)[l]$")
_FLOAT_SUFFIX_RE = re.compile(r"(?i)[fd]$")


@dataclass(frozen=True)
class _ParsedLine:
    number: int
    text: str


def _java_source_lines(evidence: Any) -> tuple[list[_ParsedLine], str | None]:
    """Return verified numbered source lines or a fail-closed reason."""
    provenance = getattr(evidence, "provenance", None)
    fingerprint = getattr(provenance, "query_fingerprint", None)
    location = getattr(provenance, "source_location", None)
    attrs = getattr(evidence, "attributes", None) or {}
    operation = str(getattr(fingerprint, "operation", "") or "").upper()
    revision = str(getattr(location, "revision", "") or "")
    start = getattr(location, "line_start", None)
    end = getattr(location, "line_end", None)
    if (
        str(getattr(getattr(evidence, "evidence_type", None), "value", "")).upper()
        != EvidenceType.SOURCE_CODE.value
    ):
        return [], "not_source_code_evidence"
    if operation not in {"GET_CODE", "GET_SOURCE", "READ_FILE"}:
        return [], "not_a_source_read"
    if not isinstance(attrs, dict) or attrs.get("read_mode") not in (None, "range"):
        return [], "source_read_not_range_pinned"
    if attrs.get("discovery_only"):
        return [], "source_read_is_discovery_only"
    if not revision or revision in {"HEAD", "UNPINNED"} or not getattr(location, "file_path", None):
        return [], "missing_immutable_source_provenance"
    if not isinstance(start, int) or not isinstance(end, int) or start < 1 or end < start:
        return [], "missing_source_line_window"
    if attrs.get("truncated"):
        return [], "source_range_truncated"

    parsed: list[_ParsedLine] = []
    for offset, raw in enumerate(str(getattr(evidence, "content_snippet", "") or "").splitlines()):
        match = re.match(r"^\s*(\d+)\| ?(.*)$", raw)
        if match:
            parsed.append(_ParsedLine(number=int(match.group(1)), text=match.group(2)))
        else:
            # Range GET_CODE evidence is expected to carry numbered lines.
            return [], "source_range_missing_numbered_lines"
    if not parsed or parsed[0].number < start or parsed[-1].number > end:
        return [], "source_lines_outside_provenance_window"
    return parsed, None


def _node_text(node: Any, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _qualified_class_name(node: Any, source: bytes) -> str:
    names: list[str] = []
    current = node
    while current is not None:
        if current.type in _CLASS_TYPES:
            name = current.child_by_field_name("name")
            if name is None:
                return ""
            names.append(_node_text(name, source))
        current = current.parent
    return ".".join(reversed(names))


def _literal_value(expression: str, field_type: str) -> str | None:
    """Normalize a conservative Java literal subset; do not evaluate code."""
    raw = expression.strip()
    if raw.startswith("(") and raw.endswith(")"):
        # Parentheses are accepted only when they wrap the entire expression.
        raw = raw[1:-1].strip()
    if raw in {"true", "false"}:
        return raw if field_type == "boolean" else None
    if raw == "null":
        return (
            "null"
            if field_type
            not in {"boolean", "byte", "short", "int", "long", "char", "float", "double"}
            else None
        )
    if raw.startswith("'") and raw.endswith("'") and field_type == "char":
        try:
            value = ast.literal_eval(raw)
        except (SyntaxError, ValueError):
            return None
        return f"U+{ord(value):04X}" if isinstance(value, str) and len(value) == 1 else None
    if raw.startswith('"') and raw.endswith('"') and field_type in {"String", "java.lang.String"}:
        try:
            value = ast.literal_eval(raw)
        except (SyntaxError, ValueError):
            return None
        return value if isinstance(value, str) else None

    numeric = raw.replace("_", "")
    if numeric[:1] in {"+", "-"}:
        sign, numeric = numeric[0], numeric[1:]
    else:
        sign = ""
    if field_type in {"byte", "short", "int", "long", "char"}:
        numeric = _INTEGER_SUFFIX_RE.sub("", numeric)
        try:
            base = (
                0
                if numeric.lower().startswith(("0x", "0b", "0o"))
                else (8 if len(numeric) > 1 and numeric.startswith("0") else 10)
            )
            number = int(numeric, base)
        except ValueError:
            return None
        if sign == "-":
            number = -number
        elif sign == "+":
            number = +number
        if field_type == "char":
            return f"U+{number:04X}" if 0 <= number <= 0xFFFF else None
        return str(number)
    if field_type in {"float", "double"}:
        numeric = _FLOAT_SUFFIX_RE.sub("", numeric)
        try:
            value = float(sign + numeric)
        except ValueError:
            return None
        return repr(value)
    return None


def resolve_java_field_fact(
    evidence: Any,
    field_name: str,
    *,
    declaring_type: str | None = None,
) -> CodeFactResolution:
    """Resolve one Java class field from exact source-read evidence.

    The function reads no files and follows no references. Its only source of
    facts is the supplied, revision-pinned `GET_CODE` evidence.
    """
    lines, reason = _java_source_lines(evidence)
    provenance = getattr(evidence, "provenance", None)
    location = getattr(provenance, "source_location", None)
    source_path = str(getattr(location, "file_path", "") or "")
    revision = str(getattr(location, "revision", "") or "")
    identifier = str(getattr(location, "identifier", "") or "")
    repository = identifier
    if source_path and identifier.endswith("/" + source_path):
        repository = identifier[: -(len(source_path) + 1)]
    elif identifier == source_path:
        repository = ""
    evidence_id = getattr(evidence, "evidence_id", None)
    evidence_ids = (evidence_id,) if evidence_id is not None else ()

    def unresolved(code: str) -> CodeFactResolution:
        fallback_location = location
        if fallback_location is None:
            from investigation_agent_platform.domain.provenance.models import SourceLocation

            fallback_location = SourceLocation(system="Git", identifier=source_path or "unknown")
        return CodeFactResolution(
            status=CodeFactStatus.UNRESOLVED,
            repository=repository or "unknown",
            revision=revision or None,
            declaring_type=declaring_type or "unknown",
            field_name=field_name or "unknown",
            field_type="unknown",
            field_category="INSTANCE_FIELD",
            value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
            source_evidence_ids=evidence_ids,
            declaration_location=fallback_location,
            limitation_codes=(code,),
        )

    if reason:
        # An unresolved result still needs valid provenance; if provenance is
        # absent, the caller must handle this as analyzer failure, not a fact.
        return unresolved(reason)
    assert location is not None
    if not field_name or not field_name.strip():
        return unresolved("missing_field_name")
    if not source_path.lower().endswith(".java"):
        return unresolved("not_a_java_source_file")
    if not repository:
        return unresolved("repository_identity_unavailable")

    source_text = "\n".join(line.text for line in lines)
    source_bytes = source_text.encode("utf-8")
    try:
        from tree_sitter_language_pack import get_parser

        tree = get_parser("java").parse(source_bytes)
    except Exception as exc:
        return unresolved(f"java_parser_unavailable:{type(exc).__name__}")
    if tree.root_node.has_error:
        return unresolved("java_syntax_error_or_incomplete_source_window")

    declarations: list[tuple[Any, Any, Any, Any]] = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "field_declaration":
            class_node = node.parent.parent if node.parent is not None else None
            if class_node is not None and class_node.type in _CLASS_TYPES:
                declarators = [
                    child for child in node.children if child.type == "variable_declarator"
                ]
                for declarator in declarators:
                    name_node = declarator.child_by_field_name("name")
                    if name_node is not None and _node_text(name_node, source_bytes) == field_name:
                        declarations.append((node, declarator, class_node, name_node))
        stack.extend(reversed(node.children))

    if declaring_type:
        declarations = [
            item
            for item in declarations
            if _qualified_class_name(item[2], source_bytes).endswith(declaring_type)
        ]
    if len(declarations) != 1:
        return unresolved("field_not_found" if not declarations else "ambiguous_field_declaration")

    declaration, declarator, class_node, name_node = declarations[0]
    type_node = declaration.child_by_field_name("type")
    if type_node is None:
        return unresolved("field_type_unavailable")
    field_type = _node_text(type_node, source_bytes).strip()
    modifiers = next((child for child in declaration.children if child.type == "modifiers"), None)
    modifier_text = _node_text(modifiers, source_bytes) if modifiers is not None else ""
    field_category: Literal["INSTANCE_FIELD", "STATIC_FIELD"] = (
        "STATIC_FIELD" if re.search(r"\bstatic\b", modifier_text) else "INSTANCE_FIELD"
    )
    type_base = field_type.replace("[]", "").split("<", 1)[0].strip()
    value_node = declarator.child_by_field_name("value")
    raw_initializer = (
        _node_text(value_node, source_bytes).strip() if value_node is not None else None
    )
    value: str | None
    if raw_initializer is None:
        if field_type == "boolean":
            value, semantics = "false", CodeValueSemantics.LANGUAGE_DEFAULT
        elif field_type in {"byte", "short", "int", "long"}:
            value, semantics = "0", CodeValueSemantics.LANGUAGE_DEFAULT
        elif field_type in {"float", "double"}:
            value, semantics = "0.0", CodeValueSemantics.LANGUAGE_DEFAULT
        elif field_type == "char":
            value, semantics = "U+0000", CodeValueSemantics.LANGUAGE_DEFAULT
        elif "[]" in field_type or type_base not in {
            "boolean",
            "byte",
            "short",
            "int",
            "long",
            "char",
            "float",
            "double",
        }:
            value, semantics = "null", CodeValueSemantics.LANGUAGE_DEFAULT
        else:
            return unresolved("unsupported_default_type")
    else:
        value = _literal_value(raw_initializer, field_type)
        if value is None:
            return CodeFactResolution(
                status=CodeFactStatus.PARTIAL,
                repository=repository,
                revision=revision,
                declaring_type=_qualified_class_name(class_node, source_bytes),
                field_name=field_name,
                field_type=field_type,
                field_category=field_category,
                value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
                raw_initializer=raw_initializer,
                source_evidence_ids=evidence_ids,
                declaration_location=location,
                limitation_codes=("unsupported_initializer_expression",),
            )
        semantics = CodeValueSemantics.EXPLICIT_INITIALIZER

    if value is None:
        return unresolved("value_resolution_failed")
    first_line = lines[0].number
    declaration_line = first_line + declaration.start_point[0]
    declaration_location = location.model_copy(
        update={"line_start": declaration_line, "line_end": first_line + declaration.end_point[0]}
    )
    return CodeFactResolution(
        status=CodeFactStatus.RESOLVED,
        repository=repository,
        revision=revision,
        declaring_type=_qualified_class_name(class_node, source_bytes),
        field_name=field_name,
        field_type=field_type,
        field_category=field_category,
        value_semantics=semantics,
        raw_initializer=raw_initializer,
        resolved_value=value,
        source_evidence_ids=evidence_ids,
        declaration_location=declaration_location,
    )
