# tests/unit/test_jcv_return_literals.py
"""JCV: every literal the statement asserts as a return value is reconciled (D27).

The field-phrasing check (``_claimed_literal_from_statement``) does not read
method-return prose, so a statement contradicting the proven guarded return
verified. Bare comparison both sides (``_bare_literal``), as today.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from test_java_method_returns import _MAPPER, _source_evidence

from investigation_agent_platform.application.analysis.java.method_return_facts import (
    resolve_java_method_return_fact,
)
from investigation_agent_platform.application.investigation.verification import (
    VerificationEngine,
)
from investigation_agent_platform.domain.code.models import (
    CODE_FACT_RESOLUTION_V2_KEY,
    CodeValueSemantics,
    JavaFactCategory,
)


def _banked():  # type: ignore[no-untyped-def]
    ev = _source_evidence(_MAPPER)
    resolution = resolve_java_method_return_fact(
        ev,
        "normalize",
        guard_condition="orderType == null",
        declaring_type="LegacyCodeMapper",
    )
    assert resolution.status.value == "RESOLVED"
    assert resolution.value_semantics == CodeValueSemantics.GUARDED_RETURN_LITERAL
    assert resolution.resolved_value == "STANDARD"
    return ev.model_copy(
        update={
            "attributes": {
                **ev.attributes,
                CODE_FACT_RESOLUTION_V2_KEY: resolution.model_dump(mode="json"),
            }
        }
    )


def _hyp(statement, claimed_value="STANDARD"):  # type: ignore[no-untyped-def]
    banked = _banked()
    return (
        SimpleNamespace(
            id=uuid4(),
            statement=statement,
            claimed_value=claimed_value,
            claim_target=SimpleNamespace(
                symbol="com.sample.orders.legacy.LegacyCodeMapper.normalize",
                declaring_type="com.sample.orders.legacy.LegacyCodeMapper",
                claimed_value=claimed_value,
            ),
            fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
            supporting_evidence_ids=[banked.evidence_id],
        ),
        [banked],
    )


def test_jcv_t01_contradictory_returned_literal() -> None:
    hyp, banked = _hyp('The one method returns "OTHER" when s is null.')
    ok, reason = VerificationEngine.java_fact_proof(hyp, banked)
    assert not ok
    assert "conflicts with the resolved value" in reason


def test_jcv_t02_consistent_statement_verifies() -> None:
    hyp, banked = _hyp(
        "If orderType is null, normalize returns 'STANDARD', so the default applies."
    )
    ok, _ = VerificationEngine.java_fact_proof(hyp, banked)
    assert ok


def test_jcv_t03_two_literal_statement_fails_closed() -> None:
    hyp, banked = _hyp('Returns "STANDARD" for null and "X" otherwise.')
    ok, _ = VerificationEngine.java_fact_proof(hyp, banked)
    assert not ok


def test_jcv_t04_omitted_claimed_value_still_fails() -> None:
    hyp, banked = _hyp("normalize returns 'STANDARD' on null.", claimed_value=None)
    ok, reason = VerificationEngine.java_fact_proof(hyp, banked)
    assert not ok
    assert "no structured claimed_value" in reason


def test_jcv_t05_field_phrasing_unchanged() -> None:
    hyp, banked = _hyp("The default value of x is 999.")
    ok, reason = VerificationEngine.java_fact_proof(hyp, banked)
    assert not ok
    assert "conflicts with resolved source value" in reason


def test_jcv_t06_no_return_statement_unaffected() -> None:
    hyp, banked = _hyp("normalize handles null input gracefully.")
    ok, _ = VerificationEngine.java_fact_proof(hyp, banked)
    assert ok
