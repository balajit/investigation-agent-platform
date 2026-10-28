# src/investigation_agent_platform/application/analysis/java/dataflow.py
"""Deterministic bounded Java data-flow resolution (Phase 4, D5).

Reports reaching definitions, early-return enumeration, and
definite-assignment checks for one local variable or parameter inside a
single in-window method. ``data_flow_refs`` carry structured
definition-use references with exact line/column ranges. Anything needing
interprocedural aliasing, stream laziness, or concurrent visibility
returns ``PARTIAL``; references outside the window abstain.
"""

from __future__ import annotations

from typing import Any

from investigation_agent_platform.application.analysis.java import _parse as _p
from investigation_agent_platform.application.analysis.java import literals as _lit
from investigation_agent_platform.application.analysis.java._provenance import (
    java_source_lines,
    repository_of,
)
from investigation_agent_platform.domain.code.models import (
    CodeFactResolutionV2,
    CodeFactStatus,
    CodeValueSemantics,
    JavaFactCategory,
    SemanticStage,
)

_CONCURRENCY_MARKERS = (
    "synchronized",
    "volatile ",
    "Atomic",
    "CompletableFuture",
    "Executor",
    "new Thread",
    "Thread(",
)

_STREAM_MARKERS = (".stream(", ".parallelStream(", "Stream<", "Stream.")


def _unresolved(
    repository: str,
    revision: str | None,
    subject: str,
    code: str,
    evidence_ids: tuple[Any, ...],
    language_version: str | None,
    *,
    partial: bool = False,
) -> CodeFactResolutionV2:
    from investigation_agent_platform.domain.provenance.models import SourceLocation

    return CodeFactResolutionV2(
        status=CodeFactStatus.PARTIAL if partial else CodeFactStatus.UNRESOLVED,
        repository=repository or "unknown",
        revision=revision or None,
        fact_category=JavaFactCategory.DATA_FLOW_FACT,
        semantic_stage=SemanticStage.DECLARATION,
        declaring_type="unknown",
        subject_identity=subject or "unknown",
        value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
        language_version=language_version,
        analysis_method="tree-sitter-range-v1",
        source_evidence_ids=tuple(e for e in evidence_ids if e is not None),
        declaration_location=SourceLocation(system="Git", identifier="unknown"),
        limitation_codes=(code,),
        parser_version=_p.parser_version(),
    )


def _span(first_line: int, node: Any) -> dict[str, int]:
    (row_start, col_start) = node.start_point
    (row_end, col_end) = node.end_point
    return {
        "line_start": first_line + row_start,
        "col_start": col_start,
        "line_end": first_line + row_end,
        "col_end": col_end,
    }


def _same(a: Any, b: Any) -> bool:
    """Structural node identity (tree-sitter wrappers lack stable ``is``)."""
    return a.type == b.type and a.start_byte == b.start_byte and a.end_byte == b.end_byte


def _owns(method_node: Any, node: Any) -> bool:
    current: Any = node
    while current is not None:
        if _same(current, method_node):
            return True
        current = current.parent
    return False


def _enclosed_in_other_method(method_node: Any, node: Any) -> bool:
    """True when the identifier lives in a different method/constructor body."""
    current: Any = node.parent
    while current is not None:
        if current.type in {"method_declaration", "constructor_declaration"}:
            return not _same(current, method_node)
        current = current.parent
    return False


def _rebound_in_other_method(
    method_node: Any, node: Any, symbol_name: str, source_bytes: bytes
) -> bool:
    """True when another method body binds the same name (separate binding).

    A same-name identifier in a scope that declares nothing is not a
    rebinding — it is an unresolvable reference and still fails closed.
    """
    current: Any = node.parent
    scope: Any | None = None
    while current is not None:
        if current.type in {"method_declaration", "constructor_declaration"}:
            scope = current
            break
        current = current.parent
    if scope is None or _same(scope, method_node):
        return False
    for child in _p.walk(scope):
        if child.type == "variable_declarator":
            name_node = child.child_by_field_name("name")
            if name_node is not None and _p.node_text(name_node, source_bytes) == symbol_name:
                return True
        elif child.type in {"formal_parameter", "spread_parameter"}:
            target = child
            if child.type == "spread_parameter":
                target = next((c for c in child.children if c.type == "variable_declarator"), child)
            name_node = target.child_by_field_name("name")
            text = _p.node_text(name_node, source_bytes) if name_node is not None else ""
            if not text and target is child:
                text = _p.node_text(child, source_bytes).split()[-1]
            if text == symbol_name:
                return True
    return False


def _resolve_java_data_flow_legacy(
    evidence: Any,
    symbol_name: str,
    *,
    method_scope: str | None = None,
    language_version: object = None,
) -> CodeFactResolutionV2:
    """Resolve bounded local data flow for one symbol in one method."""
    lines, reason = java_source_lines(evidence)
    provenance = getattr(evidence, "provenance", None)
    location = getattr(provenance, "source_location", None)
    source_path = str(getattr(location, "file_path", "") or "")
    revision = str(getattr(location, "revision", "") or "")
    identifier = str(getattr(location, "identifier", "") or "")
    repository = repository_of(identifier, source_path)
    evidence_id = getattr(evidence, "evidence_id", None)
    evidence_ids = (evidence_id,) if evidence_id is not None else ()
    level = _lit.normalize_source_level(language_version)
    level_text = str(level) if level is not None else None
    subject = symbol_name or "unknown"

    if reason:
        return _unresolved(repository, revision, subject, reason, evidence_ids, level_text)
    assert location is not None
    if not symbol_name or not symbol_name.strip():
        return _unresolved(
            repository, revision, subject, "missing_symbol_name", evidence_ids, level_text
        )
    if not source_path.lower().endswith(".java"):
        return _unresolved(
            repository, revision, subject, "not_a_java_source_file", evidence_ids, level_text
        )
    if not repository:
        return _unresolved(
            repository,
            revision,
            subject,
            "repository_identity_unavailable",
            evidence_ids,
            level_text,
        )

    window_text = "\n".join(line.text for line in lines)
    source_bytes = window_text.encode("utf-8")
    try:
        tree = _p.parse_java(source_bytes)
    except Exception as exc:
        return _unresolved(
            repository,
            revision,
            subject,
            f"java_parser_unavailable:{type(exc).__name__}",
            evidence_ids,
            level_text,
        )
    if tree.root_node.has_error:
        return _unresolved(
            repository,
            revision,
            subject,
            "java_syntax_error_or_incomplete_source_window",
            evidence_ids,
            level_text,
        )

    package = _p.package_name(tree.root_node, source_bytes)
    methods: list[tuple[Any, str]] = []
    for node in _p.walk(tree.root_node):
        if node.type not in {"method_declaration", "constructor_declaration"}:
            continue
        name_node = node.child_by_field_name("name")
        label = _p.node_text(name_node, source_bytes) if name_node is not None else ""
        if method_scope and not _p.matches_declaring_type(label, method_scope):
            continue
        methods.append((node, label or "anonymous"))

    hosting: list[tuple[Any, str, Any, str]] = []
    for method_node, label in methods:
        for node in _p.walk(method_node):
            if node.type == "variable_declarator":
                name_node = node.child_by_field_name("name")
                if name_node is not None and _p.node_text(name_node, source_bytes) == symbol_name:
                    hosting.append((method_node, label, node, "local"))
            elif node.type in {"formal_parameter", "spread_parameter"}:
                target = (
                    node
                    if node.type != "spread_parameter"
                    else next((c for c in node.children if c.type == "variable_declarator"), node)
                )
                name_node = target.child_by_field_name("name")
                text = _p.node_text(name_node, source_bytes) if name_node is not None else ""
                if not text and target is node:
                    text = _p.node_text(node, source_bytes).split()[-1]
                if text == symbol_name:
                    hosting.append((method_node, label, node, "parameter"))

    if len(hosting) != 1:
        code = "local_not_found" if not hosting else "ambiguous_local_declaration"
        return _unresolved(repository, revision, subject, code, evidence_ids, level_text)
    method_node, method_label, decl_node, decl_kind = hosting[0]

    method_text = _p.node_text(method_node, source_bytes)
    if any(marker in method_text for marker in _CONCURRENCY_MARKERS):
        return _unresolved(
            repository,
            revision,
            subject,
            "concurrent_visibility",
            evidence_ids,
            level_text,
            partial=True,
        )
    if any(marker in method_text for marker in _STREAM_MARKERS):
        return _unresolved(
            repository,
            revision,
            subject,
            "stream_laziness",
            evidence_ids,
            level_text,
            partial=True,
        )

    first_line = lines[0].number
    definitions: list[dict[str, Any]] = []
    uses: list[dict[str, Any]] = []
    early_returns: list[dict[str, int]] = []
    def_spans: set[tuple[int, int, int]] = set()
    use_spans: set[tuple[int, int, int]] = set()

    def _record_use(kind: str, span: dict[str, int]) -> None:
        key = (span["line_start"], span["col_start"], span["col_end"])
        if key in def_spans or key in use_spans:
            return
        use_spans.add(key)
        uses.append({"kind": kind, **span})

    value_node = decl_node.child_by_field_name("value")
    decl_span = _span(first_line, decl_node)
    if decl_kind == "local" and value_node is not None:
        definitions.append({"kind": "initializer", **decl_span})
        def_spans.add((decl_span["line_start"], decl_span["col_start"], decl_span["col_end"]))
    elif decl_kind == "parameter":
        definitions.append({"kind": "parameter_binding", **decl_span})
        def_spans.add((decl_span["line_start"], decl_span["col_start"], decl_span["col_end"]))

    def _record_def(kind: str, span: dict[str, int]) -> None:
        definitions.append({"kind": kind, **span})
        def_spans.add((span["line_start"], span["col_start"], span["col_end"]))

    for node in _p.walk(method_node):
        if _same(node, decl_node) or _owns(decl_node, node):
            continue
        if node.type == "return_statement":
            early_returns.append(_span(first_line, node))
            for child in _p.walk(node):
                if child.type == "identifier" and _p.node_text(child, source_bytes) == symbol_name:
                    _record_use("return_use", _span(first_line, child))
            continue
        if node.type == "assignment_expression":
            left = node.child_by_field_name("left")
            if left is not None and _p.node_text(left, source_bytes).strip() == symbol_name:
                operator = "".join(
                    _p.node_text(c, source_bytes)
                    for c in node.children
                    if not c.is_named and c.type != ";"
                ).strip()
                kind = "assignment" if operator.strip() == "=" else "compound_assignment"
                _record_def(kind, _span(first_line, left))
                if kind == "compound_assignment":
                    _record_use("read_modify_write", _span(first_line, left))
                for child in _p.walk(node.child_by_field_name("right") or node):
                    if (
                        child.type == "identifier"
                        and _p.node_text(child, source_bytes) == symbol_name
                        and not _same(child, left)
                    ):
                        _record_use("value_use", _span(first_line, child))
                continue
        if node.type == "update_expression":
            text = _p.node_text(node, source_bytes)
            if symbol_name in text:
                for child in node.children:
                    if (
                        child.type == "identifier"
                        and _p.node_text(child, source_bytes) == symbol_name
                    ):
                        span = _span(first_line, child)
                        _record_def("update", span)
                        _record_use("read_modify_write", span)
            continue
        if node.type == "method_invocation":
            args_node = node.child_by_field_name("arguments")
            if args_node is not None:
                for arg in args_node.children:
                    if not arg.is_named:
                        continue
                    for child in _p.walk(arg):
                        if (
                            child.type == "identifier"
                            and _p.node_text(child, source_bytes) == symbol_name
                        ):
                            return _unresolved(
                                repository,
                                revision,
                                subject,
                                "interprocedural_aliasing",
                                evidence_ids,
                                level_text,
                                partial=True,
                            )
            continue
        if node.type == "identifier" and _p.node_text(node, source_bytes) == symbol_name:
            parent = node.parent
            if parent is not None and parent.type == "variable_declarator":
                continue
            if parent is not None and parent.type == "formal_parameter":
                continue
            _record_use("value_use", _span(first_line, node))

    for node in _p.walk(tree.root_node):
        if node.type == "identifier" and _p.node_text(node, source_bytes) == symbol_name:
            if not _owns(method_node, node):
                if _rebound_in_other_method(method_node, node, symbol_name, source_bytes):
                    # Same name bound in a different method body: a separate
                    # binding, not an escaping reference. Ignored by design.
                    continue
                return _unresolved(
                    repository,
                    revision,
                    subject,
                    "out_of_window_reference",
                    evidence_ids,
                    level_text,
                    partial=True,
                )

    ordered_defs = sorted(definitions, key=lambda d: (d["line_start"], d["col_start"]))
    ordered_uses = sorted(uses, key=lambda d: (d["line_start"], d["col_start"]))
    definite = True
    if not ordered_defs:
        definite = False
    else:
        first_def = (ordered_defs[0]["line_start"], ordered_defs[0]["col_start"])
        for use in ordered_uses:
            if (use["line_start"], use["col_start"]) < first_def:
                definite = False
                break
            if not any(
                (d["line_start"], d["col_start"]) <= (use["line_start"], use["col_start"])
                for d in ordered_defs
            ):
                definite = False
                break

    qualified_subject = f"{method_label}.{symbol_name}"
    if package:
        qualified_subject = f"{package}.{qualified_subject}"
    declaration_location = location.model_copy(
        update={
            "line_start": decl_span["line_start"],
            "line_end": decl_span["line_end"],
        }
    )
    refs: list[str] = []
    for use in ordered_uses:
        reaching = [
            d
            for d in ordered_defs
            if (d["line_start"], d["col_start"]) <= (use["line_start"], use["col_start"])
        ]
        for definition in reaching[-1:]:
            refs.append(
                f"{repository}@{revision}:{source_path}:"
                f"def({definition['line_start']}:{definition['col_start']}"
                f"-{definition['col_end']})"
                f"->use({use['line_start']}:{use['col_start']}-{use['col_end']})"
            )
    if not ordered_uses and ordered_defs:
        definition = ordered_defs[-1]
        refs.append(
            f"{repository}@{revision}:{source_path}:"
            f"def({definition['line_start']}:{definition['col_start']}"
            f"-{definition['col_end']})->use(none)"
        )

    if not definite:
        return CodeFactResolutionV2(
            status=CodeFactStatus.PARTIAL,
            repository=repository,
            revision=revision,
            fact_category=JavaFactCategory.DATA_FLOW_FACT,
            semantic_stage=SemanticStage.DECLARATION,
            declaring_type=method_label or "unknown",
            subject_identity=qualified_subject,
            value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
            language_version=level_text,
            analysis_method="tree-sitter-range-v1",
            structured_fact={
                "kind": "local_data_flow",
                "symbol": symbol_name,
                "method": method_label,
                "definitions": ordered_defs,
                "uses": ordered_uses,
                "early_returns": sorted(early_returns, key=lambda r: r["line_start"]),
                "definite_assignment": "unestablished",
            },
            data_flow_refs=tuple(refs),
            source_evidence_ids=evidence_ids,
            declaration_location=declaration_location,
            limitation_codes=("use_before_definition",),
            parser_version=_p.parser_version(),
        )

    return CodeFactResolutionV2(
        status=CodeFactStatus.RESOLVED,
        repository=repository,
        revision=revision,
        fact_category=JavaFactCategory.DATA_FLOW_FACT,
        semantic_stage=SemanticStage.DECLARATION,
        declaring_type=method_label or "unknown",
        subject_identity=qualified_subject,
        value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
        language_version=level_text,
        analysis_method="tree-sitter-range-v1",
        structured_fact={
            "kind": "local_data_flow",
            "symbol": symbol_name,
            "method": method_label,
            "definitions": ordered_defs,
            "uses": ordered_uses,
            "early_returns": sorted(early_returns, key=lambda r: r["line_start"]),
            "definite_assignment": "established",
        },
        data_flow_refs=tuple(refs),
        source_evidence_ids=evidence_ids,
        declaration_location=declaration_location,
        parser_version=_p.parser_version(),
    )


def resolve_java_data_flow(
    evidence: Any,
    symbol_name: str,
    *,
    method_scope: str | None = None,
    language_version: object = None,
) -> CodeFactResolutionV2:
    """Resolve bounded data flow from the shared Java window model."""
    lines, reason = java_source_lines(evidence)
    provenance = getattr(evidence, "provenance", None)
    location = getattr(provenance, "source_location", None)
    source_path = str(getattr(location, "file_path", "") or "")
    revision = str(getattr(location, "revision", "") or "")
    identifier = str(getattr(location, "identifier", "") or "")
    repository = repository_of(identifier, source_path)
    evidence_id = getattr(evidence, "evidence_id", None)
    evidence_ids = (evidence_id,) if evidence_id is not None else ()
    level = _lit.normalize_source_level(language_version)
    level_text = str(level) if level is not None else None
    subject = symbol_name or "unknown"

    if reason:
        return _unresolved(repository, revision, subject, reason, evidence_ids, level_text)
    assert location is not None
    if not symbol_name or not symbol_name.strip():
        return _unresolved(repository, revision, subject, "missing_symbol_name", evidence_ids, level_text)
    if not source_path.lower().endswith(".java"):
        return _unresolved(repository, revision, subject, "not_a_java_source_file", evidence_ids, level_text)
    if not repository:
        return _unresolved(
            repository, revision, subject, "repository_identity_unavailable", evidence_ids, level_text
        )

    from investigation_agent_platform.application.analysis.java import model as _model

    window_text = "\n".join(line.text for line in lines)
    try:
        code_model = _model.build_java_model(
            window_text.encode("utf-8"),
            file_path=source_path,
            repository=repository,
            revision=revision,
            language_version=language_version,
        )
    except Exception as exc:
        return _unresolved(
            repository,
            revision,
            subject,
            f"java_parser_unavailable:{type(exc).__name__}",
            evidence_ids,
            level_text,
        )
    if code_model.has_syntax_error:
        return _unresolved(
            repository,
            revision,
            subject,
            "java_syntax_error_or_incomplete_source_window",
            evidence_ids,
            level_text,
        )

    bindings = [binding for binding in code_model.dataflow_bindings if binding.symbol == symbol_name]
    if method_scope:
        bindings = [
            binding
            for binding in bindings
            if _p.matches_declaring_type(binding.method_name, method_scope)
        ]
    if len(bindings) != 1:
        code = "local_not_found" if not bindings else "ambiguous_local_declaration"
        return _unresolved(repository, revision, subject, code, evidence_ids, level_text)
    binding = bindings[0]
    method = next(
        (item for item in code_model.methods if item.name == binding.method_name), None
    )
    if method is None:
        return _unresolved(repository, revision, subject, "local_not_found", evidence_ids, level_text)
    method_identity = binding.method_identity
    method_events = [
        event
        for event in code_model.dataflow_events
        if event.method_identity == method_identity and event.symbol == symbol_name
    ]
    method_text = window_text[binding.span.line_start - 1 :] if window_text else ""
    if any(marker in method_text for marker in _CONCURRENCY_MARKERS):
        return _unresolved(
            repository, revision, subject, "concurrent_visibility", evidence_ids, level_text, partial=True
        )
    if any(marker in method_text for marker in _STREAM_MARKERS):
        return _unresolved(
            repository, revision, subject, "stream_laziness", evidence_ids, level_text, partial=True
        )

    first_line = lines[0].number
    definitions: list[dict[str, Any]] = []
    uses: list[dict[str, Any]] = []
    early_returns: list[dict[str, int]] = []
    if binding.kind == "local" and binding.has_initializer:
        definitions.append(
            {"kind": "initializer", "line_start": first_line + binding.span.line_start - 1,
             "col_start": 0, "line_end": first_line + binding.span.line_end - 1, "col_end": 0}
        )
    elif binding.kind == "parameter":
        definitions.append(
            {"kind": "parameter_binding", "line_start": first_line + binding.span.line_start - 1,
             "col_start": 0, "line_end": first_line + binding.span.line_end - 1, "col_end": 0}
        )
    for event in method_events:
        span = {
            "line_start": first_line + event.span.line_start - 1,
            "col_start": 0,
            "line_end": first_line + event.span.line_end - 1,
            "col_end": 0,
        }
        if event.kind in {"assignment", "compound_assignment", "update"}:
            definitions.append({"kind": event.kind, **span})
        if event.kind in {"value_use", "return_use", "read_modify_write"}:
            uses.append({"kind": event.kind, **span})
        if event.kind == "early_return":
            early_returns.append(span)
    definitions.sort(key=lambda item: (item["line_start"], item["col_start"]))
    uses.sort(key=lambda item: (item["line_start"], item["col_start"]))
    if any(event.kind == "method_argument" for event in method_events):
        return _unresolved(
            repository, revision, subject, "interprocedural_aliasing", evidence_ids, level_text, partial=True
        )
    other_bindings = [
        item
        for item in code_model.dataflow_bindings
        if item.symbol == symbol_name and item.method_identity != method_identity
    ]
    if other_bindings and any(
        event.symbol == symbol_name and event.method_identity != method_identity
        for event in code_model.dataflow_events
    ):
        return _unresolved(
            repository, revision, subject, "out_of_window_reference", evidence_ids, level_text, partial=True
        )
    definite = bool(definitions)
    if definitions:
        first_def = (definitions[0]["line_start"], definitions[0]["col_start"])
        definite = all(
            (use["line_start"], use["col_start"]) >= first_def
            and any(
                (item["line_start"], item["col_start"])
                <= (use["line_start"], use["col_start"])
                for item in definitions
            )
            for use in uses
        )
    qualified_subject = f"{code_model.package + '.' if code_model.package else ''}{method.qualified_name}.{symbol_name}"
    refs = [
        f"{repository}@{revision}:{source_path}:def({item['line_start']}:{item['col_start']}-"
        f"{item['col_end']})->use({use['line_start']}:{use['col_start']}-{use['col_end']})"
        for use in uses
        for item in definitions
        if (item["line_start"], item["col_start"]) <= (use["line_start"], use["col_start"])
    ]
    fact = {
        "kind": "local_data_flow",
        "symbol": symbol_name,
        "method": method.name,
        "definitions": definitions,
        "uses": uses,
        "early_returns": sorted(early_returns, key=lambda item: item["line_start"]),
        "definite_assignment": "established" if definite else "unestablished",
    }
    return CodeFactResolutionV2(
        status=CodeFactStatus.RESOLVED if definite else CodeFactStatus.PARTIAL,
        repository=repository,
        revision=revision,
        fact_category=JavaFactCategory.DATA_FLOW_FACT,
        semantic_stage=SemanticStage.DECLARATION,
        declaring_type=method.name,
        subject_identity=qualified_subject,
        value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
        language_version=level_text,
        analysis_method="tree-sitter-range-v1",
        structured_fact=fact,
        data_flow_refs=tuple(refs),
        source_evidence_ids=evidence_ids,
        declaration_location=location.model_copy(
            update={
                "line_start": first_line + binding.span.line_start - 1,
                "line_end": first_line + binding.span.line_end - 1,
            }
        ),
        limitation_codes=() if definite else ("use_before_definition",),
        parser_version=code_model.parser_version,
    )
