# src/investigation_agent_platform/application/analysis/java/window_facts.py
"""Bank-time enumeration of Java source-value facts (single home).

When a range read banks Java source without selector-driven analysis,
enumerate every cleanly-declared guarded method-return literal and
every cleanly-declared class-field value in the window as RESOLVED v2
records, plus typed markers for constructs the analyzer saw but did not
model (enum constants, switch returns, over-long literals). Verify-time
selection picks by subject — no method_name / field_name parameter, no
forcing turn, and no per-case trigger code is ever needed to make
analysis happen.

Soundness, not intent: a record attaches only when the resolved
declaration's own syntax subtree is error-free. Errors elsewhere in
the window (an unclosed sibling past the provider's byte cap) never
contribute records, and the proof's revision, path, window, subject,
and value checks still bind every record. Anything unparseable yields
a typed WindowAnalysis (REFUSED/FAILED), never a silent empty list.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from investigation_agent_platform.application.analysis.failure import failure_code
from investigation_agent_platform.application.analysis.java import _parse as _p
from investigation_agent_platform.application.analysis.java import expressions as _expr
from investigation_agent_platform.application.analysis.java import model as _model
from investigation_agent_platform.application.analysis.java._provenance import (
    java_source_lines,
    repository_of,
)
from investigation_agent_platform.application.analysis.java.field_facts import (
    _language_default,
)
from investigation_agent_platform.application.analysis.java.window_result import (
    WindowAnalysis,
    WindowStatus,
)
from investigation_agent_platform.application.analysis.subjects import (
    subject_identifies,
)
from investigation_agent_platform.domain.code.models import (
    CodeFactResolutionV2,
    CodeFactStatus,
    CodeValueSemantics,
    JavaFactCategory,
)

MAX_WINDOW_RECORDS = 100


@dataclass(frozen=True)
class _Candidate:
    kind: str  # "guard" | "field" | "marker"
    subject: str
    record: CodeFactResolutionV2


def _marker(subject: str, code: str, **common: Any) -> _Candidate:
    return _Candidate(
        "marker",
        subject,
        CodeFactResolutionV2(
            status=CodeFactStatus.UNRESOLVED,
            fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
            subject_identity=subject or None,
            value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
            limitation_codes=(code,),
            parser_version=_p.parser_version(),
            **common,
        ),
    )


def _contained(build: Callable[[], _Candidate], subject: str, **common: Any) -> _Candidate:
    """Build one candidate; a bad declaration becomes a typed marker, never a lost window."""
    try:
        return build()
    except ValidationError:
        return _marker(subject, "literal_too_long", **common)


@dataclass(frozen=True)
class _Context:
    model: Any
    tree: Any
    location: Any
    first_line: int
    evidence: Any
    repository: str
    revision: str
    evidence_ids: tuple[Any, ...]


def _shift(ctx: _Context, span: Any) -> Any:
    return ctx.location.model_copy(
        update={
            "line_start": ctx.first_line + span.line_start - 1,
            "line_end": ctx.first_line + span.line_end - 1,
        }
    )


def _class_fallback(model: Any, file_path: str) -> str:
    """Basename stem when the window names no class at all (unclosed tail).

    Mirrors the selector resolver's basename fallback: multi-class windows keep
    failing closed (no guess across classes). The stem feeds _owner_of, which
    labels inferred owners (D14); the method-only window live runs depend on
    keeps resolving, now with an explicit inference stamp.
    """
    stem = (file_path or "").rsplit("/", 1)[-1]
    if "." in stem:
        stem = stem.rsplit(".", 1)[0]
    return stem if not model.classes and stem else ""


_INFERRED_OWNER_ASSUMPTION = (
    "declaring class inferred from the file name; a nested or secondary class is not excluded"
)


def _owner_of(enclosing: str | None, package: str, stem: str) -> tuple[str | None, bool]:
    """(qualified owner, inferred). ``enclosing_class`` already carries package and nesting."""
    if enclosing:
        return enclosing, False
    if stem:
        return (f"{package}.{stem}" if package else stem, True)
    return None, False


def _declaration_subtree_clean(tree: Any, node_type: str, span: Any) -> bool:
    """True when the declaration at exactly ``span`` parses clean.

    The declaration is matched by its tree-sitter start and end points
    (row and byte column), which identify one node. Matching by line range
    alone let a clean declaration that shares a line span with a broken one
    vouch for it (e.g. two methods on one line).
    """
    try:
        start = (int(span.line_start) - 1, int(span.byte_col_start))
        end = (int(span.line_end) - 1, int(span.byte_col_end))
        for node in _p.walk(tree.root_node):
            if (
                node.type == node_type
                and (int(node.start_point[0]), int(node.start_point[1])) == start
                and (int(node.end_point[0]), int(node.end_point[1])) == end
            ):
                return not bool(getattr(node, "has_error", False))
        return False
    except Exception:
        return False


def _guard_candidates(ctx: _Context) -> list[_Candidate]:
    model = ctx.model
    package = model.package or ""
    class_fallback = _class_fallback(model, getattr(ctx.location, "file_path", None) or "")
    candidates: list[_Candidate] = []
    for branch in model.guard_returns:
        host = _model.method_for_identity(model.methods, branch.method_identity, branch.span)
        if host is None or host.is_constructor:
            continue
        # J1/J2: a method of an anonymous class has no stable named
        # owner (its nearest named class is the outer one), so it is not
        # emitted rather than attributed to a method that does not exist.
        if host.declared_in == "anonymous_class_body":
            continue
        if not _declaration_subtree_clean(ctx.tree, "method_declaration", host.span):
            continue
        owner, inferred = _owner_of(host.enclosing_class, package, class_fallback)
        if owner is None:
            # No class on record and no file stem: a bare name would let any
            # qualified claim match, so the declaration becomes a marker.
            candidates.append(
                _marker(
                    host.name,
                    "declaring_type_not_in_window",
                    repository=ctx.repository or None,
                    revision=ctx.revision,
                    source_evidence_ids=ctx.evidence_ids,
                    declaration_location=_shift(ctx, host.span),
                )
            )
            continue
        assumptions: tuple[str, ...] = (_INFERRED_OWNER_ASSUMPTION,) if inferred else ()
        subject = f"{owner}.{host.name}"
        class_attribution = "file_stem_inferred" if inferred else None
        for ret in branch.returns:
            value = _model.literal_return_value(ret.value_shape, ret.value_text)
            if value is None:
                continue

            def _build(
                _ret: Any = ret,
                _subject: str = subject,
                _owner: str | None = owner,
                _branch: Any = branch,
                _value: Any = value,
                _assumptions: tuple[str, ...] = assumptions,
                _attribution: str | None = class_attribution,
            ) -> _Candidate:
                _structured: dict[str, Any] = {
                    "guard_text": _branch.guard_text,
                    "path_conditions": [pc.model_dump() for pc in _branch.path_conditions],
                }
                if _attribution is not None:
                    _structured["class_attribution"] = _attribution
                return _Candidate(
                    "guard",
                    _subject,
                    CodeFactResolutionV2(
                        status=CodeFactStatus.RESOLVED,
                        repository=ctx.repository or None,
                        revision=ctx.revision,
                        fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
                        declaring_type=_owner,
                        subject_identity=_subject or None,
                        value_semantics=CodeValueSemantics.GUARDED_RETURN_LITERAL,
                        raw_initializer=_ret.raw_text[:512],
                        resolved_value=_value,
                        structured_fact=_structured,
                        assumptions=_assumptions,
                        source_evidence_ids=ctx.evidence_ids,
                        declaration_location=_shift(ctx, _ret.span),
                        parser_version=_p.parser_version(),
                    ),
                )

            candidates.append(
                _contained(
                    _build,
                    subject,
                    repository=ctx.repository or None,
                    revision=ctx.revision,
                    source_evidence_ids=ctx.evidence_ids,
                    declaration_location=_shift(ctx, ret.span),
                    assumptions=assumptions,
                )
            )
    return candidates


def _field_candidates(ctx: _Context) -> list[_Candidate]:
    model = ctx.model
    class_fallback = _class_fallback(model, getattr(ctx.location, "file_path", None) or "")
    candidates: list[_Candidate] = []
    for field in model.fields:
        if field.declared_in not in {"class_declaration", "interface_declaration"}:
            continue
        if not field.has_declared_type:
            continue
        node_type = (
            "constant_declaration" if field.declared_in == "interface_declaration" else "field_declaration"
        )
        if not _declaration_subtree_clean(ctx.tree, node_type, field.span):
            continue
        owner, inferred = _owner_of(field.enclosing_class, model.package or "", class_fallback)
        if owner is None:
            candidates.append(
                _marker(
                    field.name,
                    "declaring_type_not_in_window",
                    repository=ctx.repository or None,
                    revision=ctx.revision,
                    source_evidence_ids=ctx.evidence_ids,
                    declaration_location=_shift(ctx, field.span),
                )
            )
            continue
        assumptions: tuple[str, ...] = (_INFERRED_OWNER_ASSUMPTION,) if inferred else ()
        subject = f"{owner}.{field.name}"
        class_attribution = "file_stem_inferred" if inferred else None
        raw_initializer = field.raw_initializer
        if raw_initializer is None:
            value, semantics = _language_default(field.field_type)
            if value is None or semantics is None:
                continue
            evaluated: tuple[str, Any] | None = ("default", value)
        else:
            evaluated = _model.coerce_evaluated(field.evaluated_kind, field.evaluated_value)
            if field.evaluation_limitation is not None or evaluated is None:
                continue
            value, _ = _expr.format_for_field_type(evaluated, field.field_type)
            if value is None:
                continue
            semantics = CodeValueSemantics.EXPLICIT_INITIALIZER

        def _build(
            _field: Any = field,
            _subject: str = subject,
            _owner: str | None = owner,
            _raw: Any = raw_initializer,
            _value: Any = value,
            _semantics: Any = semantics,
            _assumptions: tuple[str, ...] = assumptions,
            _attribution: str | None = class_attribution,
        ) -> _Candidate:
            _structured: dict[str, Any] = {}
            if _attribution is not None:
                _structured["class_attribution"] = _attribution
            return _Candidate(
                "field",
                _subject,
                CodeFactResolutionV2(
                    status=CodeFactStatus.RESOLVED,
                    repository=ctx.repository or None,
                    revision=ctx.revision,
                    fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
                    declaring_type=_owner,
                    subject_identity=_subject or None,
                    field_name=_field.name,
                    field_type=_field.field_type,
                    field_category=("STATIC_FIELD" if _field.is_static else "INSTANCE_FIELD"),
                    value_semantics=_semantics,
                    raw_initializer=_raw,
                    resolved_value=_value,
                    structured_fact=_structured or None,
                    assumptions=_assumptions,
                    source_evidence_ids=ctx.evidence_ids,
                    declaration_location=_shift(ctx, _field.span),
                    parser_version=_p.parser_version(),
                ),
            )

        candidates.append(
            _contained(
                _build,
                subject,
                repository=ctx.repository or None,
                revision=ctx.revision,
                source_evidence_ids=ctx.evidence_ids,
                declaration_location=_shift(ctx, field.span),
                assumptions=assumptions,
            )
        )
    return candidates


def _marker_candidates(ctx: _Context) -> list[_Candidate]:
    candidates: list[_Candidate] = []
    for skipped in ctx.model.skipped:
        owner = skipped.owner or ""
        subject = f"{owner}.{skipped.name}" if owner and skipped.name else (owner or skipped.name)
        candidates.append(
            _marker(
                subject,
                skipped.code,
                repository=ctx.repository or None,
                revision=ctx.revision,
                source_evidence_ids=ctx.evidence_ids,
                declaration_location=_shift(ctx, skipped.span),
            )
        )
    return candidates


def _identifies(hint: str | None, subject: str) -> bool:
    return bool(hint) and subject_identifies(hint, {subject})


def _allocate(
    candidates: list[_Candidate], hint: str | None, cap: int
) -> tuple[list[_Candidate], int]:
    """Subject-first, then fair share across kinds, then document order; reports the omitted."""
    first = [c for c in candidates if _identifies(hint, c.subject)]
    taken = {id(c) for c in first}
    queues = {
        kind: deque(c for c in candidates if c.kind == kind and id(c) not in taken)
        for kind in ("field", "guard", "marker")
    }
    chosen = list(first[:cap])
    while len(chosen) < cap and any(queues.values()):
        for kind in ("field", "guard", "marker"):
            if queues[kind] and len(chosen) < cap:
                chosen.append(queues[kind].popleft())
    return chosen, len(candidates) - len(chosen)


def enumerate_java_window_facts(
    evidence: Any, *, language_version: object = None, subject_hint: str | None = None
) -> WindowAnalysis:
    """Enumerate RESOLVED source-value facts and typed skip markers for one banked read (pure)."""
    lines, reason = java_source_lines(evidence)
    if reason:
        return WindowAnalysis(WindowStatus.REFUSED, limitations=(reason,))
    location = getattr(getattr(evidence, "provenance", None), "source_location", None)
    source_path = str(getattr(location, "file_path", "") or "")
    revision = str(getattr(location, "revision", "") or "")
    if not source_path.lower().endswith(".java"):
        return WindowAnalysis(WindowStatus.REFUSED, limitations=("not_a_java_source_file",))
    if not revision or revision in {"HEAD", "UNPINNED"}:
        return WindowAnalysis(WindowStatus.REFUSED, limitations=("unpinned_revision",))
    source_bytes = "\n".join(line.text for line in lines).encode("utf-8")
    try:
        tree = _p.parse_java(source_bytes)
        code_model = _model.build_java_model(
            source_bytes,
            file_path=source_path,
            repository=repository_of(str(getattr(location, "identifier", "") or ""), source_path)
            or "unknown",
            revision=revision,
            language_version=language_version,
        )
    except Exception as exc:  # contained and typed; never a silent []
        return WindowAnalysis(
            WindowStatus.FAILED,
            limitations=(failure_code(exc),),
            failure_type=type(exc).__name__,
        )
    evidence_id = getattr(evidence, "evidence_id", None)
    ctx = _Context(
        code_model,
        tree,
        location,
        lines[0].number,
        evidence,
        repository_of(str(getattr(location, "identifier", "") or ""), source_path),
        revision,
        (evidence_id,) if evidence_id is not None else (),
    )
    candidates = [*_guard_candidates(ctx), *_field_candidates(ctx), *_marker_candidates(ctx)]
    chosen, omitted = _allocate(candidates, subject_hint, MAX_WINDOW_RECORDS)
    limitations: list[str] = []
    if code_model.has_syntax_error:
        # Head reads run into the provider byte cap and end mid-declaration; records come
        # only from declarations whose own subtree is clean (J4), so they stay sound.
        limitations.append("java_syntax_error_or_incomplete_source_window")
    if omitted:
        limitations.append("window_records_truncated")
    return WindowAnalysis(
        WindowStatus.DEGRADED if limitations else WindowStatus.OK,
        records=tuple(c.record for c in chosen),
        limitations=tuple(limitations),
        omitted=omitted,
    )
