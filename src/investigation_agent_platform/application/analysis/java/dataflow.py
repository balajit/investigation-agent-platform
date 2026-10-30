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

    bindings = [
        binding for binding in code_model.dataflow_bindings if binding.symbol == symbol_name
    ]
    # A window can contain separate same-name bindings. Match the legacy
    # resolver's first in-window declaration when no method scope is supplied;
    # callers needing disambiguation must pass method_scope.
    if not method_scope and bindings:
        bindings = [min(bindings, key=lambda item: item.span.line_start)]
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
    method = _model.method_for_identity(code_model.methods, binding.method_identity, binding.span)
    if method is None:
        return _unresolved(
            repository, revision, subject, "local_not_found", evidence_ids, level_text
        )
    method_identity = binding.method_identity
    method_events = [
        event
        for event in code_model.dataflow_events
        if event.method_identity == method_identity
        and (event.symbol == symbol_name or event.kind == "early_return")
    ]
    method_text = "\n".join(
        line.text
        for line in lines
        if method.span.line_start <= line.number - lines[0].number + 1 <= method.span.line_end
    )
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
            repository, revision, subject, "stream_laziness", evidence_ids, level_text, partial=True
        )

    first_line = lines[0].number
    definitions: list[dict[str, Any]] = []
    uses: list[dict[str, Any]] = []
    early_returns: list[dict[str, int]] = []
    if binding.kind == "local" and binding.has_initializer:
        definitions.append(
            {
                "kind": "initializer",
                "line_start": first_line + binding.span.line_start - 1,
                "col_start": binding.span.col_start,
                "line_end": first_line + binding.span.line_end - 1,
                "col_end": binding.span.col_end,
            }
        )
    elif binding.kind == "parameter":
        definitions.append(
            {
                "kind": "parameter_binding",
                "line_start": first_line + binding.span.line_start - 1,
                "col_start": binding.span.col_start,
                "line_end": first_line + binding.span.line_end - 1,
                "col_end": binding.span.col_end,
            }
        )
    for event in method_events:
        span = {
            "line_start": first_line + event.span.line_start - 1,
            "col_start": event.span.col_start,
            "line_end": first_line + event.span.line_end - 1,
            "col_end": event.span.col_end,
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
            repository,
            revision,
            subject,
            "interprocedural_aliasing",
            evidence_ids,
            level_text,
            partial=True,
        )
    other_bindings = [
        item
        for item in code_model.dataflow_bindings
        if item.symbol == symbol_name and item.method_identity != method_identity
    ]
    other_events = [
        event
        for event in code_model.dataflow_events
        if event.symbol == symbol_name and event.method_identity != method_identity
    ]
    if other_events and not other_bindings:
        return _unresolved(
            repository,
            revision,
            subject,
            "out_of_window_reference",
            evidence_ids,
            level_text,
            partial=True,
        )
    # J3: this resolver has no control-flow graph. A definition nested in a
    # branch, loop, try or switch that the declaration is NOT also inside runs
    # only on some paths, so "definitely assigned" and "the last textual
    # definition reaches the use" are both unsound. Abstain with a typed code
    # instead of reporting an established fact.
    declaration_context = set(binding.control_context)
    if any(
        event.kind in {"assignment", "compound_assignment", "update"}
        and set(event.control_context) - declaration_context
        for event in method_events
    ):
        return _unresolved(
            repository,
            revision,
            subject,
            "control_flow_not_modeled",
            evidence_ids,
            level_text,
            partial=True,
        )
    definite = bool(definitions)
    if definitions:
        first_def = (definitions[0]["line_start"], definitions[0]["col_start"])
        definite = all(
            (use["line_start"], use["col_start"]) >= first_def
            and any(
                (item["line_start"], item["col_start"]) <= (use["line_start"], use["col_start"])
                for item in definitions
            )
            for use in uses
        )
    qualified_subject = f"{method.name}.{symbol_name}"
    refs: list[str] = []
    for use in uses:
        reaching = [
            item
            for item in definitions
            if (item["line_start"], item["col_start"]) <= (use["line_start"], use["col_start"])
        ]
        if reaching:
            item = reaching[-1]
            refs.append(
                f"{repository}@{revision}:{source_path}:def({item['line_start']}:{item['col_start']}-"
                f"{item['col_end']})->use({use['line_start']}:{use['col_start']}-{use['col_end']})"
            )
    if not uses and definitions:
        item = definitions[-1]
        refs.append(
            f"{repository}@{revision}:{source_path}:def({item['line_start']}:{item['col_start']}-"
            f"{item['col_end']})->use(none)"
        )
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
