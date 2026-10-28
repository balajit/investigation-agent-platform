"""Q1 acceptance: the six QL-style questions over a synthetic Java corpus.

Corpus: Store.java (com.example.db, defines execute/fetch) and Search.java
(com.example.app, calls into Store plus a local eval). Models are built per
file; callee-side library attribution joins definitions across both models.
"""

from pathlib import Path

from investigation_agent_platform.application.analysis.java import model as _model
from investigation_agent_platform.application.analysis.java import query as _q

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "java_query"


def _build(name: str) -> _model.JavaModel:
    path = _FIXTURES / name
    return _model.build_java_model(
        path.read_bytes(),
        file_path=f"tests/fixtures/java_query/{name}",
        repository="synthetic/java-query",
        revision="synthetic-revision-java-query",
    )


def _corpus() -> list[_model.JavaModel]:
    return [_build("Store.java"), _build("Search.java")]


def test_question_1_all_calls():
    calls = list(_q.all_calls(_corpus()))
    assert len(calls) == 8


def test_question_2_calls_to_named_target():
    calls = list(_q.named(_q.all_calls(_corpus()), "execute"))
    assert len(calls) == 3


def test_question_3_all_definitions():
    methods = list(_q.all_methods(_corpus()))
    assert len(methods) == 6


def test_question_4_definitions_named():
    methods = list(_q.definitions_named(_corpus(), "execute"))
    assert len(methods) == 1
    assert methods[0].qualified_name == "com.example.db.Store.execute"


def test_question_5_vulnerability_shaped_calls():
    calls = list(_q.named(_q.all_calls(_corpus()), "execute"))
    assert len(calls) == 3
    assert all(call.arity == 1 for call in calls)


def test_question_6_library_scoped_non_literal_calls():
    models = _corpus()
    scoped = list(
        _q.in_library(_q.named(_q.all_calls(models), "execute"), models, "com.example.db")
    )
    assert len(scoped) == 3
    non_literal = [call for call in scoped if _q.arg_not_literal(call, 0)]
    assert len(non_literal) == 2
    literal = [call for call in scoped if _q.takes_string_literal(call)]
    assert len(literal) == 1


def test_overload_keys_distinguish_arities():
    keys = {method.key for method in _q.all_methods(_corpus()) if method.name == "find"}
    assert keys == {"find/1(String)", "find/2(String,int)"}


def test_constructor_call_is_marked():
    constructors = [call for call in _q.all_calls(_corpus()) if call.is_constructor]
    assert len(constructors) == 1
    assert constructors[0].target_name == "Store"


def _advanced() -> _model.JavaModel:
    path = _FIXTURES / "Advanced.java"
    return _model.build_java_model(
        path.read_bytes(),
        file_path="tests/fixtures/java_query/Advanced.java",
        repository="synthetic/java-query",
        revision="synthetic-revision-java-query",
    )


def test_q2_definitions_in_package():
    model = _advanced()
    app_methods = list(_q.definitions_in_package(_q.all_methods(model), "com.example.app"))
    assert {m.name for m in app_methods} == {
        "toString",
        "sum",
        "take",
        "execute",
        "check",
        "fmt",
        "run",
    }
    assert list(_q.definitions_in_package(_q.all_methods(model), "com.example.db")) == []


def test_q2_callee_side_library_attribution():
    models = [_advanced(), *_corpus()]
    scoped = list(_q.calls_to_in_library(_q.all_calls(models), models, "com.example.db", "execute"))
    assert len(scoped) == 3
    local_zero_arg = [call for call in _q.named(_q.all_calls(models), "execute") if call.arity == 0]
    assert len(local_zero_arg) == 1
    assert all(call not in scoped for call in local_zero_arg)


def test_q3_operator_and_shape_predicates():
    model = _advanced()
    take_call = next(call for call in _q.named(_q.all_calls(model), "take"))
    assert _q.arg_shape(take_call, 0) == "binary_expression"
    assert _q.arg_operator(take_call, 0) == "&&"
    assert _q.arg_shape(take_call, 1) == "unary_expression"
    assert _q.arg_operator(take_call, 1) == "-"
    assert _q.arg_shape(take_call, 2) == "ternary_expression"
    assert _q.arg_operator(take_call, 2) == "ternary"


def test_q3_static_types_and_literal_kinds():
    model = _advanced()
    sum_call = next(call for call in _q.named(_q.all_calls(model), "sum") if not call.in_lambda)
    assert [_q.arg_static_type(sum_call, i) for i in range(3)] == ["int", "int", "int"]
    assert all(_q.is_int_arg(sum_call, i) for i in range(3))
    of_call = next(call for call in _q.named(_q.all_calls(model), "of"))
    assert _q.arg_static_type(of_call, 0) == "String"
    assert _q.is_string_arg(of_call, 0)
    search = _corpus()[1]
    fetch_call = next(call for call in _q.named(_q.all_calls(search), "fetch"))
    assert _q.arg_is_name(fetch_call, 0)
    assert _q.arg_static_type(fetch_call, 0) == "unknown"


def test_q3_computed_and_limitation_arguments():
    model = _advanced()
    take_call = next(call for call in _q.named(_q.all_calls(model), "take"))
    assert _q.arg_is_complex(take_call, 0)
    assert _q.arg_is_complex(take_call, 2)
    assert not _q.arg_is_literal(take_call, 0)
    assert not _q.arg_has_limitation(take_call, 0)


def test_method_flags_varargs_override():
    model = _advanced()
    by_name = {m.name: m for m in _q.all_methods(model)}
    assert by_name["sum"].is_varargs is True
    assert by_name["toString"].is_override is True
    assert by_name["take"].is_varargs is False
    assert by_name["take"].is_override is False


def test_receiver_lambda_and_method_reference():
    model = _advanced()
    of_call = next(call for call in _q.named(_q.all_calls(model), "of"))
    assert of_call.receiver == "List"
    foreach_call = next(call for call in _q.named(_q.all_calls(model), "forEach"))
    assert foreach_call.receiver == "items"
    assert _q.arg_shape(foreach_call, 0) == "method_reference"
    # A method reference passed as an argument is tracked on the argument's
    # shape. It must not mark the enclosing invocation as dynamically
    # dispatched: the dispatch check is scoped to the call target itself.
    assert foreach_call.has_method_reference is False
    lambda_sums = [call for call in _q.named(_q.all_calls(model), "sum") if call.in_lambda]
    assert len(lambda_sums) == 1
    assert _q.arg_evaluates_to(lambda_sums[0], 0, "int", "7")
    assert len(list(_q.calls_in_lambda(_q.all_calls(model)))) == 1


def test_literal_vs_computed_constant():
    model = _advanced()
    take_call = next(call for call in _q.named(_q.all_calls(model), "take"))
    # Unary minus over an int literal evaluates to a constant but is not
    # written as a literal: the two predicates must disagree here.
    assert _q.arg_is_constant(take_call, 1) is True
    assert _q.arg_is_literal(take_call, 1) is False
    assert _q.is_int_arg(take_call, 1) is False
    sum_call = next(call for call in _q.named(_q.all_calls(model), "sum") if not call.in_lambda)
    assert _q.arg_is_constant(sum_call, 0) is True
    assert _q.arg_is_literal(sum_call, 0) is True


def test_non_decimal_integer_literals():
    model = _advanced()
    check_call = next(call for call in _q.named(_q.all_calls(model), "check"))
    assert [_q.arg_shape(check_call, i) for i in range(3)] == [
        "hex_integer_literal",
        "octal_integer_literal",
        "binary_integer_literal",
    ]
    assert all(_q.arg_is_literal(check_call, i) for i in range(3))
    assert [_q.arg_static_type(check_call, i) for i in range(3)] == ["int", "int", "int"]
    assert [
        _q.arg_evaluates_to(check_call, i, "int", v) for i, v in enumerate(["16", "8", "5"])
    ] == [True, True, True]


def test_float_suffix_keeps_static_type():
    model = _advanced()
    fmt_calls = [call for call in _q.named(_q.all_calls(model), "fmt")]
    assert len(fmt_calls) == 2
    suffixed = next(call for call in fmt_calls if call.args[0].text == "1.0f")
    plain = next(call for call in fmt_calls if call.args[0].text == "1.0")
    assert _q.arg_static_type(suffixed, 0) == "float"
    assert _q.arg_static_type(plain, 0) == "double"
    assert _q.arg_is_literal(suffixed, 0) is True
    assert _q.arg_is_constant(suffixed, 0) is True


def test_field_initializer_evaluated_kind():
    model = _advanced()
    base = next(f for f in model.fields if f.name == "base")
    assert base.initializer_kind == "value:int"
