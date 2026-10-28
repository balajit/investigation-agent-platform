"""Syntax-level Java generics model and query coverage."""

from pathlib import Path

from investigation_agent_platform.application.analysis.java import model, query


def _model() -> model.JavaModel:
    path = Path(__file__).parents[1] / "fixtures" / "java_query" / "Generics.java"
    return model.build_java_model(
        path.read_bytes(),
        file_path="tests/fixtures/java_query/Generics.java",
        repository="synthetic/java-query",
        revision="synthetic-revision-generics",
    )


def test_type_parameter_bounds_are_source_preserving():
    code = _model()
    parameter = next(item for item in query.all_type_parameters(code) if item.name == "T")
    assert parameter.bounds
    assert "Number" in parameter.bounds[0].text
    assert "Comparable<T>" in parameter.bounds[0].text
    assert parameter.owner_identity == "Box"
    assert parameter.identity.endswith("T@0")


def test_nested_wildcard_generic_use_preserves_variance_and_array():
    code = _model()
    uses = list(query.type_uses_named(code, "Map"))
    assert uses
    use = uses[0]
    assert use.resolution == "UNRESOLVED"
    assert use.syntax.segments[0].name == "Map"
    assert use.syntax.arguments[1].type is not None
    nested = use.syntax.arguments[1].type
    assert nested.segments[0].name == "List"
    wildcard = nested.arguments[0]
    assert wildcard.kind == "wildcard_super"
    assert wildcard.type is not None
    assert wildcard.type.kind == "array"
    assert wildcard.type.element is not None
    assert wildcard.type.element.name == "T"


def test_diamond_is_inference_syntax_not_resolved_arguments():
    code = _model()
    diamonds = list(query.type_uses_diamond(code))
    assert len(diamonds) == 1
    assert diamonds[0].inferred_arguments is True
    assert diamonds[0].resolution == "UNRESOLVED"


def test_generic_method_parameter_is_distinguished_from_class_parameter():
    code = _model()
    parameters = list(query.all_type_parameters(code))
    assert {parameter.name for parameter in parameters} >= {"T", "U"}
    method_parameter = next(parameter for parameter in parameters if parameter.name == "U")
    assert method_parameter.owner_kind == "method"
    assert method_parameter.owner_identity == "first"
