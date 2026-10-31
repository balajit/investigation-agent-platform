# src/investigation_agent_platform/application/analysis/limitations.py
"""Typed vocabulary for analyzer limitation codes (single home, J D1)."""

from __future__ import annotations

from enum import StrEnum


class LimitationClass(StrEnum):
    WINDOW = "window"  # a wider or different source read can fix it
    RUNTIME = "runtime"  # needs runtime evidence; no source read helps
    UNSUPPORTED = "unsupported"  # construct not modeled by this analyzer version
    FAILURE = "failure"  # analyzer or infrastructure fault
    PROVENANCE = "provenance"  # the row was not an admissible source read


_W, _R, _U, _F, _P = (
    LimitationClass.WINDOW,
    LimitationClass.RUNTIME,
    LimitationClass.UNSUPPORTED,
    LimitationClass.FAILURE,
    LimitationClass.PROVENANCE,
)

_REGISTRY: dict[str, LimitationClass] = {
    # window-bound
    "out_of_window_reference": _W,
    "java_syntax_error_or_incomplete_source_window": _W,
    "python_syntax_error_or_incomplete_source_window": _W,
    "source_range_truncated": _P,
    "symbol_not_in_window": _W,
    "branch_target_outside_window": _W,
    "lambda_capture_outside_window": _W,
    "method_reference_target_outside_window": _W,
    "incomplete_candidate_set": _W,
    "no_binding_declaration_in_window": _W,
    "interprocedural_aliasing": _W,
    "declaring_type_not_in_window": _W,
    "declaring_type_mismatch_in_window": _W,
    "window_records_truncated": _W,
    "method_not_found": _W,
    "method_not_found_in_mro": _W,
    "field_not_found": _W,
    "function_not_found": _W,
    "parameter_not_found": _W,
    "type_not_found": _W,
    "local_not_found": _W,
    "class_not_found": _W,
    "guard_return_not_found": _W,
    "missing_symbol_name": _W,
    "missing_field_name": _W,
    "missing_method_name": _W,
    "missing_function_scope": _W,
    "missing_class_scope": _W,
    "ambiguous_class_definition": _W,
    "ambiguous_constructor_delegation": _W,
    "ambiguous_field_declaration": _W,
    "ambiguous_function_definition": _W,
    "ambiguous_import_binding": _W,
    "ambiguous_local_declaration": _W,
    "ambiguous_method_declaration": _W,
    "ambiguous_overload": _W,
    "ambiguous_symbol": _W,
    "ambiguous_target_identity": _W,
    "ambiguous_type_declaration": _W,
    "ambiguous_constructor": _U,
    "annotation_without_assignment": _U,
    "conditional_import_may_not_execute": _R,
    "dynamic_import_requires_runtime": _R,
    "multiple_write_sites": _W,
    "potential_write_sites": _W,
    "potential_later_write": _W,
    "name_deleted": _U,
    "parameter_has_no_default": _U,
    "star_import_binding_unknown": _W,
    "unsupported_call_expression": _U,
    "unsupported_field_arguments": _U,
    "invalid_java_literal": _W,
    "use_before_definition": _W,
    "binding_hint_not_found": _W,
    "branch_hint_not_found": _W,
    "effect_hint_not_found": _W,
    "feature_hint_not_found": _W,
    "classvar_not_a_field": _W,
    "initvar_not_a_field": _W,
    "unparseable_base_list": _W,
    "cross_module_base_class": _W,
    "mro_linearization_failed": _W,
    "field_type_unavailable": _W,
    "compact_constructor_requires_dedicated_rule": _W,
    "not_a_config_declaration": _W,
    "not_a_framework_field": _W,
    # runtime-bound
    "concurrent_visibility": _R,
    "nondeterministic_scheduling": _R,
    "requires_runtime_observation": _R,
    "applied_behavior_requires_runtime_evidence": _R,
    "async_boundary": _R,
    "lombok_generated_code": _R,
    "spel_expression": _R,
    "undeclared_default_requires_runtime": _R,
    # unsupported construct
    "control_flow_not_modeled": _U,
    "unsupported_language_version": _U,
    "unsupported_initializer_expression": _U,
    "unsupported_default_type": _U,
    "unsupported_default_expression": _U,
    "type_erasure_changes_answer": _U,
    "stream_laziness": _U,
    "enum_constant_not_modeled": _U,
    "switch_return_not_modeled": _U,
    "ambiguous_guard_branch": _U,
    "literal_too_long": _U,
    "receiver_mutation_possible": _U,
    "alias_escape": _U,
    "lambda_capture_not_modeled": _U,
    # failure
    "analysis_failed": _F,
    "grammar_unavailable": _F,
    "java_parser_unavailable": _F,
    "python_parser_unavailable": _F,
    "negative_cache": _F,
    "unsupported_language": _F,
    # provenance
    "not_a_source_read": _P,
    "source_read_not_range_pinned": _P,
    "source_read_is_discovery_only": _P,
    "missing_immutable_source_provenance": _P,
    "missing_source_line_window": _P,
    "source_range_missing_numbered_lines": _P,
    "source_lines_outside_provenance_window": _P,
    "source_lines_noncontiguous": _P,
    "not_source_code_evidence": _P,
    "not_a_java_source_file": _P,
    "not_a_python_source_file": _P,
    "unpinned_revision": _P,
    "repository_identity_unavailable": _P,
    "missing_source_location": _P,
}


def limitation_class(code: str) -> LimitationClass:
    """Class of a code; an unregistered code reads as UNSUPPORTED, never WINDOW or RUNTIME.

    A trailing ``:detail`` suffix is ignored. The registry-completeness test
    fails the build when an analyzer emits a code that is not registered, so
    the fallback is a safety net, not a feature.
    """
    return _REGISTRY.get(str(code).split(":", 1)[0], LimitationClass.UNSUPPORTED)
