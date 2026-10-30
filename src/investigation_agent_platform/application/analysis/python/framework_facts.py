# src/investigation_agent_platform/application/analysis/python/framework_facts.py
"""Python dataclass / Pydantic declared-default resolution (no factory execution)."""

from __future__ import annotations

from typing import Any

from investigation_agent_platform.application.analysis.python import _parse as _p
from investigation_agent_platform.application.analysis.python import expressions as _expr
from investigation_agent_platform.application.analysis.python._provenance import (
    python_source_lines,
    repository_of,
)
from investigation_agent_platform.application.analysis.python.class_facts import (
    _class_nodes,
)
from investigation_agent_platform.domain.code.models import (
    CodeFactResolutionV2,
    CodeFactStatus,
    CodeValueSemantics,
    JavaFactCategory,
    SemanticStage,
)


def _has_dataclass_decorator(cls: Any, root: Any, source: bytes) -> bool:
    for node in _p.walk(root):
        if node.type == "decorated_definition":
            text = _p.node_text(node, source)
            if "dataclass" not in text:
                continue
            for child in node.children:
                if child.type == "class_definition":
                    name = child.child_by_field_name("name")
                    target = cls.child_by_field_name("name")
                    if (
                        name is not None
                        and target is not None
                        and _p.node_text(name, source) == _p.node_text(target, source)
                    ):
                        return True
    return False


def resolve_python_framework_fact(
    evidence: Any,
    symbol: str,
    *,
    class_scope: str,
    language_version: object = None,
) -> CodeFactResolutionV2:
    """Classify a dataclass/Pydantic field default without executing factories."""
    from investigation_agent_platform.application.analysis.python import literals as _lit

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

    def unresolved(code: str) -> CodeFactResolutionV2:
        from investigation_agent_platform.domain.provenance.models import SourceLocation

        return CodeFactResolutionV2(
            status=CodeFactStatus.UNRESOLVED,
            repository=repository or "unknown",
            revision=revision or None,
            fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
            semantic_stage=SemanticStage.DECLARATION,
            declaring_type=class_scope or None,
            field_name=symbol,
            subject_identity=f"{class_scope}.{symbol}" if class_scope else symbol,
            value_semantics=CodeValueSemantics.FRAMEWORK_DEFAULT,
            language_version=version_label,
            analysis_method="tree-sitter-range-v1",
            source_evidence_ids=tuple(e for e in evidence_ids if e is not None),
            declaration_location=SourceLocation(system="Git", identifier="unknown"),
            limitation_codes=(code,),
            parser_version=_p.parser_version(),
        )

    if reason:
        return unresolved(reason)
    assert location is not None
    if not symbol or not symbol.strip():
        return unresolved("missing_symbol_name")
    if not class_scope or not class_scope.strip():
        return unresolved("missing_class_scope")
    symbol = symbol.strip()
    class_scope = class_scope.strip()
    if not source_path.lower().endswith(".py"):
        return unresolved("not_a_python_source_file")
    if not repository:
        return unresolved("repository_identity_unavailable")

    source_bytes = "\n".join(line.text for line in lines).encode("utf-8")
    try:
        tree = _p.parse_python(source_bytes)
    except Exception as exc:
        return unresolved(f"python_parser_unavailable:{type(exc).__name__}")
    if _p.has_syntax_error(tree.root_node):
        return unresolved("python_syntax_error_or_incomplete_source_window")

    matches = list(_class_nodes(tree.root_node, source_bytes, class_scope))
    if len(matches) != 1:
        return unresolved("class_not_found" if not matches else "ambiguous_class_definition")
    cls = matches[0]
    is_dataclass = _has_dataclass_decorator(cls, tree.root_node, source_bytes)

    def loc_of(node: Any) -> Any:
        return location.model_copy(
            update={
                "line_start": lines[0].number + node.start_point[0],
                "line_end": lines[0].number + node.end_point[0],
            }
        )

    # Validators transform values at runtime: declared default is not the outcome.
    cls_text = _p.node_text(cls, source_bytes)
    has_validator = any(
        marker in cls_text
        for marker in ("field_validator", "model_validator", "validator", "@valid")
    )

    for node in _p.walk(cls):
        if node.type != "assignment":
            continue
        # Only direct class-body assignments.
        cur = node.parent
        inside_method = False
        while cur is not None and cur != cls:
            if cur.type in {"function_definition", "async_function_definition"}:
                inside_method = True
                break
            cur = cur.parent
        if inside_method:
            continue
        named = [c for c in node.children if c.is_named]
        if not named or named[0].type != "identifier":
            continue
        if _p.node_text(named[0], source_bytes).strip() != symbol:
            continue
        value_node = (
            named[-1]
            if len(named) > 1
            and any(
                not c.is_named and _p.node_text(c, source_bytes).strip() == "="
                for c in node.children
            )
            else None
        )
        if value_node is None or value_node == named[0]:
            continue
        # `ClassVar` is a plain class constant, not a dataclass field;
        # `InitVar` is constructor-only. Neither is a framework default.
        annotation = ""
        for child in node.children:
            if child.type == "type":
                annotation = _p.node_text(child, source_bytes)
                break
        if "ClassVar" in annotation:
            return unresolved("classvar_not_a_field")
        if "InitVar" in annotation:
            return unresolved("initvar_not_a_field")
        raw = _p.node_text(value_node, source_bytes).strip()
        # field(default_factory=...) / Field(default_factory=...) : classify, never execute.
        if value_node.type == "call":
            call_text = raw
            if "default_factory" in call_text:
                return CodeFactResolutionV2(
                    status=CodeFactStatus.RESOLVED,
                    repository=repository,
                    revision=revision,
                    fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
                    semantic_stage=SemanticStage.DECLARATION,
                    declaring_type=class_scope,
                    field_name=symbol,
                    subject_identity=f"{class_scope}.{symbol}",
                    value_semantics=CodeValueSemantics.FRAMEWORK_DEFAULT,
                    language_version=version_label,
                    analysis_method="tree-sitter-range-v1",
                    raw_initializer=raw,
                    resolved_value="default_factory",
                    structured_fact={"framework_factory": True},
                    source_evidence_ids=evidence_ids,
                    declaration_location=loc_of(node),
                    assumptions=(
                        *version_assumptions,
                        "factory_not_executed",
                        "validator_may_transform",
                    )
                    if has_validator
                    else (*version_assumptions, "factory_not_executed"),
                    parser_version=_p.parser_version(),
                )
            if "Field(" in call_text or "field(" in call_text:
                # Pydantic Field(default=...) / dataclass field(default=...): resolve inner default.
                for arg in _p.walk(value_node):
                    if arg.type == "keyword_argument" and arg != value_node:
                        kw = arg.child_by_field_name("name")
                        val = arg.child_by_field_name("value")
                        if kw is None or val is None:
                            # tree-sitter-python leaves keyword_argument
                            # unnamed: first named child is the key.
                            _named = [c for c in arg.children if c.is_named]
                            if len(_named) >= 2:
                                kw, val = _named[0], _named[-1]
                        if (
                            kw is not None
                            and _p.node_text(kw, source_bytes).strip() == "default"
                            and val is not None
                        ):
                            evaluated, limitation = _expr.evaluate(val, source_bytes)
                            if limitation is not None or evaluated is None:
                                return CodeFactResolutionV2(
                                    status=CodeFactStatus.PARTIAL,
                                    repository=repository,
                                    revision=revision,
                                    fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
                                    semantic_stage=SemanticStage.DECLARATION,
                                    declaring_type=class_scope,
                                    field_name=symbol,
                                    subject_identity=f"{class_scope}.{symbol}",
                                    value_semantics=CodeValueSemantics.FRAMEWORK_DEFAULT,
                                    language_version=version_label,
                                    analysis_method="tree-sitter-range-v1",
                                    raw_initializer=raw,
                                    source_evidence_ids=evidence_ids,
                                    declaration_location=loc_of(node),
                                    limitation_codes=(limitation or "unsupported_expression",),
                                    parser_version=_p.parser_version(),
                                )
                            return CodeFactResolutionV2(
                                status=CodeFactStatus.RESOLVED,
                                repository=repository,
                                revision=revision,
                                fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
                                semantic_stage=SemanticStage.DECLARATION,
                                declaring_type=class_scope,
                                field_name=symbol,
                                subject_identity=f"{class_scope}.{symbol}",
                                value_semantics=CodeValueSemantics.FRAMEWORK_DEFAULT,
                                language_version=version_label,
                                analysis_method="tree-sitter-range-v1",
                                raw_initializer=raw,
                                resolved_value=_expr.format_value(evaluated),
                                source_evidence_ids=evidence_ids,
                                declaration_location=loc_of(node),
                                assumptions=(*version_assumptions, "validator_may_transform")
                                if has_validator
                                else version_assumptions,
                                parser_version=_p.parser_version(),
                            )
                return CodeFactResolutionV2(
                    status=CodeFactStatus.PARTIAL,
                    repository=repository,
                    revision=revision,
                    fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
                    semantic_stage=SemanticStage.DECLARATION,
                    declaring_type=class_scope,
                    field_name=symbol,
                    subject_identity=f"{class_scope}.{symbol}",
                    value_semantics=CodeValueSemantics.FRAMEWORK_DEFAULT,
                    language_version=version_label,
                    analysis_method="tree-sitter-range-v1",
                    raw_initializer=raw,
                    source_evidence_ids=evidence_ids,
                    declaration_location=loc_of(node),
                    limitation_codes=("unsupported_field_arguments",),
                    parser_version=_p.parser_version(),
                )
            return CodeFactResolutionV2(
                status=CodeFactStatus.PARTIAL,
                repository=repository,
                revision=revision,
                fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
                semantic_stage=SemanticStage.DECLARATION,
                declaring_type=class_scope,
                field_name=symbol,
                subject_identity=f"{class_scope}.{symbol}",
                value_semantics=CodeValueSemantics.FRAMEWORK_DEFAULT
                if (is_dataclass or "Field" in call_text)
                else CodeValueSemantics.EXPLICIT_INITIALIZER,
                language_version=version_label,
                analysis_method="tree-sitter-range-v1",
                raw_initializer=raw,
                source_evidence_ids=evidence_ids,
                declaration_location=loc_of(node),
                limitation_codes=("unsupported_call_expression",),
                parser_version=_p.parser_version(),
            )
        # Plain default in a dataclass/pydantic class: framework default semantics.
        if (
            is_dataclass
            or "BaseModel" in _p.node_text(cls, source_bytes)[:2000]
            or "Field" in cls_text
        ):
            evaluated, limitation = _expr.evaluate(value_node, source_bytes)
            if limitation is not None or evaluated is None:
                return CodeFactResolutionV2(
                    status=CodeFactStatus.PARTIAL,
                    repository=repository,
                    revision=revision,
                    fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
                    semantic_stage=SemanticStage.DECLARATION,
                    declaring_type=class_scope,
                    field_name=symbol,
                    subject_identity=f"{class_scope}.{symbol}",
                    value_semantics=CodeValueSemantics.FRAMEWORK_DEFAULT,
                    language_version=version_label,
                    analysis_method="tree-sitter-range-v1",
                    raw_initializer=raw,
                    source_evidence_ids=evidence_ids,
                    declaration_location=loc_of(node),
                    limitation_codes=(limitation or "unsupported_expression",),
                    parser_version=_p.parser_version(),
                )
            return CodeFactResolutionV2(
                status=CodeFactStatus.RESOLVED,
                repository=repository,
                revision=revision,
                fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
                semantic_stage=SemanticStage.DECLARATION,
                declaring_type=class_scope,
                field_name=symbol,
                subject_identity=f"{class_scope}.{symbol}",
                value_semantics=CodeValueSemantics.FRAMEWORK_DEFAULT,
                language_version=version_label,
                analysis_method="tree-sitter-range-v1",
                raw_initializer=raw,
                resolved_value=_expr.format_value(evaluated),
                source_evidence_ids=evidence_ids,
                declaration_location=loc_of(node),
                assumptions=(*version_assumptions, "validator_may_transform")
                if has_validator
                else version_assumptions,
                parser_version=_p.parser_version(),
            )
        return unresolved("not_a_framework_field")
    return unresolved("symbol_not_in_window")
