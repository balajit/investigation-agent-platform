# src/investigation_agent_platform/application/analysis/python/class_facts.py
"""Python class/instance attribute resolution (declaration vs lookup vs mutation)."""

from __future__ import annotations

from typing import Any

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


def _class_nodes(root: Any, source: bytes, class_name: str):
    for node in _p.walk(root):
        if node.type == "class_definition":
            # Decorated classes are yielded via the decorated_definition
            # branch below; skip the inner node to avoid double counting.
            if node.parent is not None and node.parent.type == "decorated_definition":
                continue
            name = node.child_by_field_name("name")
            if name is not None and _p.node_text(name, source).strip() == class_name:
                yield node
        elif node.type == "decorated_definition":
            for child in node.children:
                if child.type == "class_definition":
                    name = child.child_by_field_name("name")
                    if name is not None and _p.node_text(name, source).strip() == class_name:
                        yield child


def resolve_python_class_attr_fact(
    evidence: Any,
    symbol: str,
    *,
    class_scope: str,
    language_version: object = None,
) -> CodeFactResolutionV2:
    """Resolve a class constant, constructor write, or later-write boundary."""
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
            value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
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

    # Dynamic-lookup boundaries: property/descriptor/dunder hooks/metaclass/slots.
    dynamic: str | None = None
    dynamic_node: Any = None
    for node in _p.walk(cls):
        if node.type == "decorated_definition":
            for dec in [c for c in node.children if c.type == "decorator"]:
                text = _p.node_text(dec, source_bytes)
                if "property" in text and symbol in _p.node_text(node, source_bytes)[:500]:
                    dynamic = "property_lookup_requires_runtime"
                    dynamic_node = dynamic_node or node
        if node.type in {"function_definition", "async_function_definition"}:
            name = node.child_by_field_name("name")
            label = _p.node_text(name, source_bytes).strip() if name is not None else ""
            if (
                label == symbol
                and node.parent is not None
                and node.parent.type == "decorated_definition"
            ):
                pass
            if label in {"__getattribute__", "__getattr__", "__setattr__", "__set_name__"}:
                dynamic = "descriptor_hook_requires_runtime"
                dynamic_node = dynamic_node or node
            if label in {"__get__", "__set__", "__delete__"}:
                dynamic = "descriptor_protocol_requires_runtime"
                dynamic_node = dynamic_node or node
    # __slots__ / metaclass are class-body level markers.
    cls_text = _p.node_text(cls, source_bytes)
    if "__slots__" in cls_text and symbol in cls_text:
        dynamic = dynamic or "slots_layout_requires_static_model"
    superclass = cls.child_by_field_name("superclass")
    if superclass is not None:
        sup_text = _p.node_text(superclass, source_bytes)
        if "metaclass" in sup_text or "type(" in sup_text:
            dynamic = dynamic or "metaclass_requires_runtime"

    # Collect class-body plain assignments and self.<symbol> writes per method.
    class_writes: list[tuple[Any, Any]] = []
    init_writes: list[tuple[Any, Any, str]] = []
    later_writes: list[tuple[Any, Any, str]] = []
    for node in _p.walk(cls):
        if node.type != "assignment":
            continue
        named = [c for c in node.children if c.is_named]
        if not named:
            continue
        left = named[0]
        value = named[-1] if len(named) > 1 else None
        # Determine enclosing method, if any.
        method = node
        method_name = ""
        cur = node.parent
        while cur is not None and cur != cls:
            if cur.type in {"function_definition", "async_function_definition"}:
                nm = cur.child_by_field_name("name")
                method_name = _p.node_text(nm, source_bytes).strip() if nm is not None else ""
                method = cur
                break
            cur = cur.parent
        enclosing_is_class_body = method == node or (cur == cls)
        if (
            left.type == "identifier"
            and _p.node_text(left, source_bytes).strip() == symbol
            and enclosing_is_class_body
            and value is not None
        ):
            # `InitVar` members are constructor-only: never class attributes.
            # (`ClassVar` constants resolve normally below — they are real
            # class attributes.)
            annotation = ""
            for child in node.children:
                if child.type == "type":
                    annotation = _p.node_text(child, source_bytes)
                    break
            if "InitVar" in annotation:
                continue
            class_writes.append((node, value))
        if left.type == "attribute":
            obj = left.child_by_field_name("object")
            attr = left.child_by_field_name("attribute")
            if (
                obj is not None
                and attr is not None
                and _p.node_text(obj, source_bytes).strip() == "self"
                and _p.node_text(attr, source_bytes).strip() == symbol
                and value is not None
            ):
                if method_name == "__init__":
                    init_writes.append((node, value, method_name))
                elif method_name:
                    later_writes.append((node, value, method_name))

    def loc_of(node: Any) -> Any:
        return location.model_copy(
            update={
                "line_start": lines[0].number + node.start_point[0],
                "line_end": lines[0].number + node.end_point[0],
            }
        )

    if dynamic and (class_writes or init_writes):
        node = (class_writes + [(n, v) for n, v, _ in init_writes])[0][0]
        return CodeFactResolutionV2(
            status=CodeFactStatus.PARTIAL,
            repository=repository,
            revision=revision,
            fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
            semantic_stage=SemanticStage.DECLARATION,
            declaring_type=class_scope,
            field_name=symbol,
            subject_identity=f"{class_scope}.{symbol}",
            value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
            language_version=version_label,
            analysis_method="tree-sitter-range-v1",
            source_evidence_ids=evidence_ids,
            declaration_location=loc_of(node),
            limitation_codes=(dynamic,),
            parser_version=_p.parser_version(),
        )
    if later_writes and (class_writes or init_writes):
        first = (class_writes + [(n, v) for n, v, _ in init_writes])[0]
        node, value_node = first[0], first[1]
        raw = _p.node_text(value_node, source_bytes).strip()
        evaluated, limitation = _expr.evaluate(value_node, source_bytes)
        return CodeFactResolutionV2(
            status=CodeFactStatus.PARTIAL,
            repository=repository,
            revision=revision,
            fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
            semantic_stage=SemanticStage.CONSTRUCTOR_BODY
            if init_writes
            else SemanticStage.DECLARATION,
            declaring_type=class_scope,
            field_name=symbol,
            subject_identity=f"{class_scope}.{symbol}",
            value_semantics=CodeValueSemantics.LATER_MUTABLE,
            language_version=version_label,
            analysis_method="tree-sitter-range-v1",
            raw_initializer=raw,
            resolved_value=_expr.format_value(evaluated) if evaluated else None,
            source_evidence_ids=evidence_ids,
            declaration_location=loc_of(node),
            limitation_codes=("potential_later_write",),
            parser_version=_p.parser_version(),
        )
    if len(class_writes) == 1 and not init_writes:
        node, value_node = class_writes[0]
        raw = _p.node_text(value_node, source_bytes).strip()
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
                value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
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
            value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
            language_version=version_label,
            analysis_method="tree-sitter-range-v1",
            raw_initializer=raw,
            resolved_value=_expr.format_value(evaluated),
            source_evidence_ids=evidence_ids,
            declaration_location=loc_of(node),
            assumptions=version_assumptions,
            parser_version=_p.parser_version(),
        )
    if len(init_writes) == 1 and not class_writes:
        node, value_node, _ = init_writes[0]
        raw = _p.node_text(value_node, source_bytes).strip()
        evaluated, limitation = _expr.evaluate(value_node, source_bytes)
        if limitation is not None or evaluated is None:
            return CodeFactResolutionV2(
                status=CodeFactStatus.PARTIAL,
                repository=repository,
                revision=revision,
                fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
                semantic_stage=SemanticStage.CONSTRUCTOR_BODY,
                declaring_type=class_scope,
                field_name=symbol,
                subject_identity=f"{class_scope}.{symbol}",
                value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
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
            semantic_stage=SemanticStage.CONSTRUCTOR_BODY,
            declaring_type=class_scope,
            field_name=symbol,
            subject_identity=f"{class_scope}.{symbol}",
            value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
            language_version=version_label,
            analysis_method="tree-sitter-range-v1",
            raw_initializer=raw,
            resolved_value=_expr.format_value(evaluated),
            source_evidence_ids=evidence_ids,
            declaration_location=loc_of(node),
            assumptions=version_assumptions,
            parser_version=_p.parser_version(),
        )
    if len(class_writes) + len(init_writes) > 1:
        node = (class_writes + [(n, v) for n, v, _ in init_writes])[0][0]
        return CodeFactResolutionV2(
            status=CodeFactStatus.PARTIAL,
            repository=repository,
            revision=revision,
            fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
            semantic_stage=SemanticStage.DECLARATION,
            declaring_type=class_scope,
            field_name=symbol,
            subject_identity=f"{class_scope}.{symbol}",
            value_semantics=CodeValueSemantics.LATER_MUTABLE,
            language_version=version_label,
            analysis_method="tree-sitter-range-v1",
            source_evidence_ids=evidence_ids,
            declaration_location=loc_of(node),
            limitation_codes=("multiple_write_sites",),
            parser_version=_p.parser_version(),
        )
    if dynamic is not None:
        anchor = dynamic_node if dynamic_node is not None else cls
        return CodeFactResolutionV2(
            status=CodeFactStatus.PARTIAL,
            repository=repository,
            revision=revision,
            fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
            semantic_stage=SemanticStage.DECLARATION,
            declaring_type=class_scope,
            field_name=symbol,
            subject_identity=f"{class_scope}.{symbol}",
            value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
            language_version=version_label,
            analysis_method="tree-sitter-range-v1",
            source_evidence_ids=evidence_ids,
            declaration_location=loc_of(anchor),
            limitation_codes=(dynamic,),
            parser_version=_p.parser_version(),
        )
    return unresolved("symbol_not_in_window")
