"""claim_shape: single value-shape detector (synthetic fixtures only)."""

from investigation_agent_platform.application.investigation.claim_shape import (
    suggested_typing,
    value_shape_of,
)


def test_method_return_shape():
    assert (
        value_shape_of(
            'The findOrder method returns "STORE_NOT_SUPPORTED" if the store is not allowed.'
        )
        == "method-return"
    )
    assert value_shape_of('normalize returns "STANDARD".') == "method-return"
    assert value_shape_of("The method returns 0 on success.") == "method-return"


def test_field_value_shape():
    assert value_shape_of('The initial value of the field MAX_RETRIES is "3".') == "field-value"
    assert value_shape_of("The default value of TIMEOUT is 30.") == "field-value"
    assert value_shape_of('The field mode is initialized to "AUTO".') == "field-value"


def test_non_value_statements_have_no_shape():
    assert value_shape_of("ErrorConstants is defined in the util package.") is None
    assert value_shape_of("Which code calls the method fetch?") is None
    assert value_shape_of("") is None
    assert value_shape_of("Latency exceeds 500ms under load.") is None


def test_suggested_typing_table():
    method = suggested_typing("method-return")
    assert method["fact_category"] == "SOURCE_VALUE_FACT"
    assert method["value_semantics"] == "GUARDED_RETURN_LITERAL"
    field = suggested_typing("field-value")
    assert field["fact_category"] == "SOURCE_VALUE_FACT"
    assert field["value_semantics"] == "EXPLICIT_INITIALIZER"


def test_unstructured_value_claims_flags_missing_structure_only():
    from types import SimpleNamespace

    from investigation_agent_platform.application.investigation.reasoning import (
        ReasoningCoordinator,
    )
    from investigation_agent_platform.ports.reasoning.reasoner import DecisionClaim

    def claim(**kw):
        base = {"statement": "x", "claim_kind": "CAUSAL"}
        base.update(kw)
        return DecisionClaim(**base)

    stmt = 'The findOrder method returns "STORE_NOT_SUPPORTED" if the store is not allowed.'
    typed = claim(
        statement=stmt,
        fact_category="SOURCE_VALUE_FACT",
        claimed_value="STORE_NOT_SUPPORTED",
        value_semantics="GUARDED_RETURN_LITERAL",
        target={"symbol": "OrderLookupServiceImpl.findOrder", "file_path": "x.java"},
    )
    untyped = claim(statement=stmt)
    partial = claim(statement=stmt, fact_category="SOURCE_VALUE_FACT")
    prose_value = claim(
        statement=stmt,
        fact_category="SOURCE_VALUE_FACT",
        claimed_value='"STORE_NOT_SUPPORTED" if the store is blocked, otherwise fetch',
        target={"symbol": "OrderLookupServiceImpl.findOrder", "file_path": "x.java"},
    )
    quoted_exact = claim(
        statement=stmt,
        fact_category="SOURCE_VALUE_FACT",
        value_semantics="GUARDED_RETURN_LITERAL",
        claimed_value='"STORE_NOT_SUPPORTED"',
        target={"symbol": "OrderLookupServiceImpl.findOrder", "file_path": "x.java"},
    )
    non_value = claim(statement="ErrorConstants is defined in the util package.")
    no_literal = claim(statement="The method returns something eventually.")
    decision = SimpleNamespace(
        claims=[typed, untyped, partial, prose_value, quoted_exact, non_value, no_literal]
    )
    assert ReasoningCoordinator._unstructured_value_claims(decision) == [1, 2, 3]
    assert ReasoningCoordinator._unstructured_value_claims(SimpleNamespace(claims=[])) == []
    note = ReasoningCoordinator._structure_repair_note(decision, [1])
    assert "SOURCE_VALUE_FACT" in note and "GUARDED_RETURN_LITERAL" in note
    assert "target.symbol" in note


def test_path_infix_field_statement_parses():
    """Models cite the path between symbol and value; the canonical
    parser tolerates exactly that infix and nothing else."""
    from investigation_agent_platform.application.investigation.reasoning import (
        _statement_literal,
    )

    stmt = (
        "The initial value of the field ROLE_ID in "
        "service/src/main/java/com/sample/orders/ServiceConstants.java "
        'is "role_id".'
    )
    assert _statement_literal(stmt) == "role_id"
    assert _statement_literal("The initial value of the field ROLE_ID is definitely large.") is None
    assert _statement_literal("The initial value of the field ROLE_ID is 30.") == "30"
