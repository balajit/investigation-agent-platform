# src/investigation_agent_platform/application/analysis/python/config.py
"""Python configuration-declaration resolution (declarations only, never values).

``X = os.getenv("KEY", default)`` proves the declared default and names the
environment key that may override it. It never proves the effective runtime
value: every resolved result carries ``effective_value_requires_runtime``,
and secret values are never read (only key names cross the boundary).
Non-config assignments yield UNRESOLVED so dispatcher chains continue.
"""

from __future__ import annotations

from typing import Any

from investigation_agent_platform.application.analysis.failure import failure_code
from investigation_agent_platform.application.analysis.python import _parse as _p
from investigation_agent_platform.application.analysis.python import expressions as _expr
from investigation_agent_platform.application.analysis.python._provenance import (
    python_source_lines,
    repository_of,
)
from investigation_agent_platform.domain.code.models import (
    CodeFactResolutionV2,
    CodeFactStatus,
    CodeValueSemantics,
    JavaFactCategory,
    SemanticStage,
)

_ENV_CALLS = frozenset({"getenv", "get", "getint", "getboolean", "getfloat"})

#: Transparent wrappers around an env access: ``int(os.getenv("N", "3"))``
#: still declares the same key and default. One layer only; nested
#: computation beyond conversion stays unsupported.
_CONVERTERS = frozenset({"int", "float", "str", "bool"})


def _env_access(value_node: Any, source_bytes: bytes) -> tuple[str, str | None] | None:
    """Return (kind, key) for ``os.getenv(KEY[, default])``-shaped calls.

    kind is ``"getenv"`` (has-or-may-have default positionally) or
    ``"subscript"`` for ``os.environ[KEY]``. Returns None when the value is
    not an environment access.
    """
    if value_node.type == "call":
        func = value_node.child_by_field_name("function")
        if func is None:
            named = [c for c in value_node.children if c.is_named]
            func = named[0] if named else None
        if func is None:
            return None
        text = _p.node_text(func, source_bytes).strip()
        attr = text.rsplit(".", 1)[-1]
        if attr in _CONVERTERS:
            inner = [c for c in value_node.children if c.is_named]
            for candidate in inner:
                if candidate.type == "call" and candidate != func:
                    found = _env_access(candidate, source_bytes)
                    if found is not None:
                        return found
            args = value_node.child_by_field_name("arguments")
            if args is not None:
                for candidate in args.children:
                    if candidate.is_named and candidate.type == "call":
                        found = _env_access(candidate, source_bytes)
                        if found is not None:
                            return found
            return None
        if attr not in _ENV_CALLS:
            return None
        args = value_node.child_by_field_name("arguments")
        params = [
            c for c in (args.children if args is not None else value_node.children) if c.is_named
        ]
        if not params:
            return None
        key_node = params[0]
        if key_node.type != "string":
            return None
        from investigation_agent_platform.application.analysis.python import literals as _lit

        key, limitation = _lit.parse_string_literal(_p.node_text(key_node, source_bytes).strip())
        if limitation is not None or key is None:
            return None
        return ("getenv", key)
    if value_node.type == "subscript":
        base = _p.node_text(value_node, source_bytes)
        if "environ" not in base:
            return None
        return ("subscript", None)
    return None


def _unwrap_converter(value_node: Any, source_bytes: bytes) -> Any:
    """Step through one transparent ``int(...)``-style wrapper to the env call."""
    if value_node.type != "call":
        return value_node
    func = value_node.child_by_field_name("function")
    if func is None:
        named = [c for c in value_node.children if c.is_named]
        func = named[0] if named else None
    if func is None:
        return value_node
    if _p.node_text(func, source_bytes).strip().rsplit(".", 1)[-1] not in _CONVERTERS:
        return value_node
    args = value_node.child_by_field_name("arguments")
    holders = args.children if args is not None else value_node.children
    for candidate in holders:
        if candidate.is_named and candidate.type == "call":
            return candidate
    return value_node


def _subscript_key(value_node: Any, source_bytes: bytes) -> str | None:
    for child in value_node.children:
        if child.type == "string":
            from investigation_agent_platform.application.analysis.python import literals as _lit

            key, limitation = _lit.parse_string_literal(_p.node_text(child, source_bytes).strip())
            if limitation is None and key is not None:
                return key
    return None


def resolve_python_config_fact(
    evidence: Any,
    symbol: str,
    *,
    language_version: object = None,
) -> CodeFactResolutionV2:
    """Resolve a config declaration's default and override-key chain."""
    from investigation_agent_platform.application.analysis.python import literals as _lit
    from investigation_agent_platform.domain.provenance.models import SourceLocation

    lines, reason = python_source_lines(evidence)
    provenance = getattr(evidence, "provenance", None)
    location = getattr(provenance, "source_location", None)
    source_path = str(getattr(location, "file_path", "") or "")
    revision = str(getattr(location, "revision", "") or "")
    identifier = str(getattr(location, "identifier", "") or "")
    repository = repository_of(identifier, source_path)
    evidence_id = getattr(evidence, "evidence_id", None)
    evidence_ids = (evidence_id,) if evidence_id is not None else ()
    version_label, version_assumed = _lit.resolve_language_version(language_version)
    version_assumptions = ("assumed_default_language_version",) if version_assumed else ()

    def unresolved(code: str, *, exc: BaseException | None = None) -> CodeFactResolutionV2:
        return CodeFactResolutionV2(
            status=CodeFactStatus.UNRESOLVED,
            repository=repository or "unknown",
            revision=revision or None,
            fact_category=JavaFactCategory.CONFIGURATION_FACT,
            semantic_stage=SemanticStage.DECLARATION,
            field_name=symbol,
            subject_identity=symbol,
            value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
            language_version=version_label,
            analysis_method="tree-sitter-range-v1",
            source_evidence_ids=tuple(e for e in evidence_ids if e is not None),
            declaration_location=SourceLocation(system="Git", identifier="unknown"),
            limitation_codes=(code,),
            assumptions=() if exc is None else (f"exception_type={type(exc).__name__}",),
            parser_version=_p.parser_version(),
        )

    if reason:
        return unresolved(reason)
    assert location is not None
    if not symbol or not symbol.strip():
        return unresolved("missing_symbol_name")
    symbol = symbol.strip()
    if not source_path.lower().endswith(".py"):
        return unresolved("not_a_python_source_file")
    if not repository:
        return unresolved("repository_identity_unavailable")

    source_bytes = "\n".join(line.text for line in lines).encode("utf-8")
    try:
        tree = _p.parse_python(source_bytes)
    except Exception as exc:
        return unresolved(failure_code(exc), exc=exc)
    if _p.has_syntax_error(tree.root_node):
        return unresolved("python_syntax_error_or_incomplete_source_window")

    def loc_of(node: Any) -> Any:
        return location.model_copy(
            update={
                "line_start": lines[0].number + node.start_point[0],
                "line_end": lines[0].number + node.end_point[0],
            }
        )

    for node in _p.walk(tree.root_node):
        if node.type != "assignment":
            continue
        named = [c for c in node.children if c.is_named]
        if not named or named[0].type != "identifier":
            continue
        if _p.node_text(named[0], source_bytes).strip() != symbol:
            continue
        if len(named) < 2:
            continue
        value_node = named[-1]
        target_node = _unwrap_converter(value_node, source_bytes)
        access = _env_access(target_node, source_bytes)
        if access is None and target_node != value_node:
            continue
        if access is None:
            continue
        kind, key = access
        if kind == "subscript":
            key = _subscript_key(target_node, source_bytes)
        raw = _p.node_text(value_node, source_bytes).strip()
        env_keys = [key] if key else []
        # Default is the second positional arg (getenv/get) if evaluable.
        # Converter wrappers were already stepped through above.
        default_node: Any = None
        args_holder = target_node.child_by_field_name("arguments")
        siblings = (
            [c for c in args_holder.children if c.is_named]
            if args_holder is not None
            else [c for c in target_node.children if c.is_named][1:]
        )
        if len(siblings) >= 2:
            candidate = siblings[1]
            if candidate.type != "keyword_argument":
                default_node = candidate
        if default_node is not None:
            evaluated, limitation = _expr.evaluate(default_node, source_bytes)
            if limitation is not None or evaluated is None:
                return CodeFactResolutionV2(
                    status=CodeFactStatus.PARTIAL,
                    repository=repository,
                    revision=revision,
                    fact_category=JavaFactCategory.CONFIGURATION_FACT,
                    semantic_stage=SemanticStage.DECLARATION,
                    field_name=symbol,
                    subject_identity=symbol,
                    value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
                    language_version=version_label,
                    analysis_method="tree-sitter-range-v1",
                    raw_initializer=raw,
                    source_evidence_ids=evidence_ids,
                    declaration_location=loc_of(node),
                    limitation_codes=(limitation or "unsupported_default_expression",),
                    assumptions=("effective_value_requires_runtime",),
                    structured_fact={
                        "env_keys": env_keys,
                        "precedence": ["environment", "declared_default"],
                    },
                    parser_version=_p.parser_version(),
                )
            return CodeFactResolutionV2(
                status=CodeFactStatus.RESOLVED,
                repository=repository,
                revision=revision,
                fact_category=JavaFactCategory.CONFIGURATION_FACT,
                semantic_stage=SemanticStage.DECLARATION,
                field_name=symbol,
                subject_identity=symbol,
                value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
                language_version=version_label,
                analysis_method="tree-sitter-range-v1",
                raw_initializer=raw,
                resolved_value=_expr.format_value(evaluated),
                source_evidence_ids=evidence_ids,
                declaration_location=loc_of(node),
                limitation_codes=(),
                assumptions=(*version_assumptions, "effective_value_requires_runtime"),
                structured_fact={
                    "declared_default": _expr.format_value(evaluated),
                    "env_keys": env_keys,
                    "precedence": ["environment", "declared_default"],
                },
                parser_version=_p.parser_version(),
            )
        return CodeFactResolutionV2(
            status=CodeFactStatus.PARTIAL,
            repository=repository,
            revision=revision,
            fact_category=JavaFactCategory.CONFIGURATION_FACT,
            semantic_stage=SemanticStage.DECLARATION,
            field_name=symbol,
            subject_identity=symbol,
            value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
            language_version=version_label,
            analysis_method="tree-sitter-range-v1",
            raw_initializer=raw,
            source_evidence_ids=evidence_ids,
            declaration_location=loc_of(node),
            limitation_codes=("undeclared_default_requires_runtime",),
            assumptions=("effective_value_requires_runtime",),
            structured_fact={"env_keys": env_keys, "precedence": ["environment"]},
            parser_version=_p.parser_version(),
        )
    return unresolved("not_a_config_declaration")
