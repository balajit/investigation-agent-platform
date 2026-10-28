# src/investigation_agent_platform/application/analysis/java/method_facts.py
"""Deterministic Java call-target resolution (Phase 4, D5).

Names the statically resolved declaration target of one in-window method
invocation using Java invocation applicability (strict, boxing, varargs
phases) plus most-specific selection over a complete in-window candidate
set. A ``CALL_TARGET_FACT`` names a declaration; it never states that the
invocation runs.
"""

from __future__ import annotations

from dataclasses import dataclass
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

_NUMERIC_RANK = {
    "byte": 0,
    "short": 1,
    "char": 1,
    "int": 2,
    "long": 3,
    "float": 4,
    "double": 5,
}

_BOXED = {
    "byte": "Byte",
    "short": "Short",
    "char": "Character",
    "int": "Integer",
    "long": "Long",
    "float": "Float",
    "double": "Double",
    "boolean": "Boolean",
}

_UNBOXED = {v: k for k, v in _BOXED.items()}


@dataclass(frozen=True)
class _Candidate:
    qualified_type: str
    name: str
    param_types: tuple[str, ...]
    is_varargs: bool
    is_default: bool
    is_override: bool
    node: Any  # MethodDef record from the window code model.


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
        fact_category=JavaFactCategory.CALL_TARGET_FACT,
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


def _simple(name: str) -> str:
    return name.split(".")[-1].split("<", 1)[0].strip()


def _is_reference(type_name: str) -> bool:
    base = _simple(type_name)
    return base not in _NUMERIC_RANK and base != "boolean" and base != "void"


def _strict_applicable(arg: str, param: str) -> bool:
    arg_s, param_s = _simple(arg), _simple(param)
    if arg == "unknown" or param == "unknown":
        return False
    if arg_s == param_s:
        return True
    if arg == "null":
        return _is_reference(param)
    if arg in _NUMERIC_RANK and param in _NUMERIC_RANK:
        return _NUMERIC_RANK[param] >= _NUMERIC_RANK[arg] and not (
            arg == "char" and param in {"byte", "short"}
        )
    if _is_reference(arg) and param_s in {"Object", "CharSequence", "Serializable"}:
        return True
    if arg_s == "String" and param_s == "Object":
        return True
    if arg_s in _UNBOXED and param_s in _UNBOXED:
        return False
    if not _is_reference(arg) and not _is_reference(param):
        return False
    return _is_reference(arg) and _is_reference(param) and arg_s == param_s


def _boxing_applicable(arg: str, param: str) -> bool:
    if _strict_applicable(arg, param):
        return True
    arg_s, param_s = _simple(arg), _simple(param)
    if arg in _BOXED and param_s == _BOXED[arg]:
        return True
    if arg_s in _UNBOXED and param == _UNBOXED[arg_s]:
        return True
    if arg_s in _UNBOXED and _UNBOXED[arg_s] in _NUMERIC_RANK and param in _NUMERIC_RANK:
        return _strict_applicable(_UNBOXED[arg_s], param)
    if arg in _NUMERIC_RANK and param_s in _UNBOXED:
        widened = _UNBOXED[param_s]
        return widened in _NUMERIC_RANK and _NUMERIC_RANK.get(widened, 99) >= _NUMERIC_RANK[arg]
    return False


def _fixed_applicable(args: list[str], params: tuple[str, ...], *, boxing: bool) -> bool:
    if len(args) != len(params):
        return False
    check = _boxing_applicable if boxing else _strict_applicable
    return all(check(a, p) for a, p in zip(args, params))


def _varargs_applicable(args: list[str], params: tuple[str, ...]) -> bool:
    if not params:
        return False
    fixed, var = params[:-1], params[-1]
    if len(args) < len(fixed):
        return False
    if not all(_boxing_applicable(a, p) for a, p in zip(args, fixed)):
        return False
    rest = args[len(fixed) :]
    if len(rest) == 1 and _boxing_applicable(rest[0], f"{var}[]"):
        return True
    return all(_boxing_applicable(a, var) for a in rest)


def _applicable_phase(args: list[str], params: tuple[str, ...], *, varargs: bool) -> str | None:
    if not varargs and _fixed_applicable(args, params, boxing=False):
        return "strict"
    if not varargs and _fixed_applicable(args, params, boxing=True):
        return "boxing"
    if varargs and _varargs_applicable(args, params):
        return "varargs"
    return None


def _more_specific_than(a: tuple[str, ...], b: tuple[str, ...], a_var: bool, b_var: bool) -> bool:
    if a_var and not b_var:
        return False
    if b_var and not a_var:
        return True
    if len(a) != len(b):
        return len(a) == len(b)
    result = False
    for x, y in zip(a, b):
        xs, ys = _simple(x), _simple(y)
        if xs == ys:
            continue
        if _strict_applicable(x, y) and x != y:
            result = True
            continue
        return False
    return result


def resolve_java_call_target(
    evidence: Any,
    method_name: str,
    *,
    declaring_type: str | None = None,
    language_version: object = None,
) -> CodeFactResolutionV2:
    """Name the statically declared target of one in-window invocation."""
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
    subject = method_name or "unknown"

    if reason:
        return _unresolved(repository, revision, subject, reason, evidence_ids, level_text)
    assert location is not None
    if not method_name or not method_name.strip():
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
    if any(
        marker in window_text
        for marker in (
            "java.lang.reflect",
            "getDeclaredMethod",
            "getMethod(",
            "Method.invoke",
            "Class.forName",
        )
    ):
        return _unresolved(
            repository, revision, subject, "reflection_boundary", evidence_ids, level_text
        )
    if "Proxy.newProxyInstance" in window_text or "InvocationHandler" in window_text:
        return _unresolved(
            repository, revision, subject, "reflection_boundary", evidence_ids, level_text
        )

    from investigation_agent_platform.application.analysis.java import model as _model

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

    candidates = [
        _Candidate(
            qualified_type=method.enclosing_class or "unknown",
            name=method.name,
            param_types=tuple(method.param_types),
            is_varargs=method.is_varargs,
            is_default=method.is_default,
            is_override=method.is_override,
            node=method,
        )
        for method in code_model.methods
        if method.name == method_name and not method.is_constructor
    ]
    if declaring_type:
        candidates = [
            c for c in candidates if _p.matches_declaring_type(c.qualified_type, declaring_type)
        ]
        if not candidates:
            return _unresolved(
                repository,
                revision,
                subject,
                "cross_module_scope",
                evidence_ids,
                level_text,
            )

    invocations: list[tuple[Any, list[str], str]] = []
    for call in code_model.calls:
        if call.is_constructor or call.target_name != method_name:
            continue
        arg_types = [arg.static_type for arg in call.args]
        invocations.append((call, arg_types, call.receiver))

    if not invocations:
        return _unresolved(
            repository,
            revision,
            subject,
            "incomplete_candidate_set",
            evidence_ids,
            level_text,
        )
    if not candidates:
        return _unresolved(
            repository,
            revision,
            subject,
            "incomplete_candidate_set",
            evidence_ids,
            level_text,
        )

    first_arity = len(invocations[0][1])
    if any(len(args) != first_arity for _, args, _ in invocations):
        return _unresolved(
            repository,
            revision,
            subject,
            "ambiguous_overload",
            evidence_ids,
            level_text,
            partial=True,
        )
    _, arg_types, receiver = invocations[0]

    for call, _, _ in invocations:
        if call.has_method_reference:
            return _unresolved(
                repository,
                revision,
                subject,
                "dynamic_dispatch",
                evidence_ids,
                level_text,
                partial=True,
            )
        if call.in_lambda:
            return _unresolved(
                repository,
                revision,
                subject,
                "dynamic_dispatch",
                evidence_ids,
                level_text,
                partial=True,
            )

    if any(c.is_override or c.is_default for c in candidates):
        return _unresolved(
            repository,
            revision,
            subject,
            "dynamic_dispatch",
            evidence_ids,
            level_text,
            partial=True,
        )

    if "unknown" in arg_types or "null" in arg_types and len(candidates) > 1:
        if "unknown" in arg_types:
            return _unresolved(
                repository,
                revision,
                subject,
                "incomplete_candidate_set",
                evidence_ids,
                level_text,
                partial=True,
            )

    applicable: list[tuple[_Candidate, str]] = []
    for candidate in candidates:
        phase = _applicable_phase(arg_types, candidate.param_types, varargs=candidate.is_varargs)
        if phase is not None:
            applicable.append((candidate, phase))

    if not applicable:
        return _unresolved(
            repository,
            revision,
            subject,
            "incomplete_candidate_set",
            evidence_ids,
            level_text,
            partial=True,
        )

    phase_rank = {"strict": 0, "boxing": 1, "varargs": 2}
    best_phase = min(phase_rank[p] for _, p in applicable)
    in_phase = [c for c, p in applicable if phase_rank[p] == best_phase]
    winners = [
        c
        for c in in_phase
        if all(
            d is c or _more_specific_than(c.param_types, d.param_types, c.is_varargs, d.is_varargs)
            for d in in_phase
        )
    ]
    if len(winners) != 1:
        return _unresolved(
            repository,
            revision,
            subject,
            "ambiguous_overload",
            evidence_ids,
            level_text,
            partial=True,
        )
    winner = winners[0]
    phase_name = next(p for c, p in applicable if c is winner)

    first_line = lines[0].number
    declaration_location = location.model_copy(
        update={
            "line_start": first_line + winner.node.span.line_start - 1,
            "line_end": first_line + winner.node.span.line_end - 1,
        }
    )
    target = f"{winner.qualified_type}.{winner.name}({', '.join(winner.param_types)})"
    call_record = invocations[0][0]
    call_line = first_line + call_record.span.line_start - 1
    subject_identity = f"{winner.qualified_type}.{winner.name}/{len(arg_types)}"
    _ = receiver
    return CodeFactResolutionV2(
        status=CodeFactStatus.RESOLVED,
        repository=repository,
        revision=revision,
        fact_category=JavaFactCategory.CALL_TARGET_FACT,
        semantic_stage=SemanticStage.DECLARATION,
        declaring_type=winner.qualified_type,
        subject_identity=subject_identity,
        value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
        language_version=level_text,
        analysis_method="tree-sitter-range-v1",
        structured_fact={
            "kind": "static_call_target",
            "method_name": method_name,
            "target": target,
            "argument_types": list(arg_types),
            "resolution_phase": phase_name,
            "call_site_line": call_line,
            "declaration_line_start": first_line + winner.node.span.line_start - 1,
            "note": "names the statically declared target only; "
            "does not state that the invocation runs",
        },
        target_identity=target,
        call_target=target,
        source_evidence_ids=evidence_ids,
        declaration_location=declaration_location,
        assumptions=("static target naming only; invocation running is not established",),
        parser_version=_p.parser_version(),
    )


def overload_key(name: str, arity: int, type_names: list[str]) -> str:
    """Stable overload identity helper (D5 sketch)."""
    return f"{name}/{arity}({','.join(type_names)})"
