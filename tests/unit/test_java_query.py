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
