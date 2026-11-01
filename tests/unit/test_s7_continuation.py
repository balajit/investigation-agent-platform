"""WP-04 S7 continuation model and deterministic fetch parser tests."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investigation_agent_platform.application.investigation.continuation import (
    continuation_idempotency_key,
    derive_continuation_params,
)
from investigation_agent_platform.domain.continuation.models import (
    ContinuationStatus,
    S7Continuation,
)
from investigation_agent_platform.domain.finding.models import HandoffState
from investigation_agent_platform.domain.investigation.models import ActionType


@pytest.mark.parametrize(
    ("text", "action", "parameters"),
    [
        ("GET_CODE svc/Foo.java lines 10-40", ActionType.GET_CODE, {"file_path": "svc/Foo.java", "start_line": 10, "end_line": 40}),
        ("GET_CODE_HISTORY svc/Foo.java", ActionType.GET_CODE_HISTORY, {"file_path": "svc/Foo.java"}),
        ("SEARCH_CODE Foo within svc/", ActionType.SEARCH_CODE, {"query": "Foo", "scope": "svc/"}),
        ("GET_CODE svc/Foo.java (call site of Caller.run)", ActionType.GET_CODE, {"file_path": "svc/Foo.java"}),
        ("FIND_SYMBOL Foo", ActionType.FIND_SYMBOL, {"symbol_name": "Foo"}),
        ("FIND_CALL_GRAPH Foo", ActionType.FIND_CALL_GRAPH, {"symbol": "Foo"}),
        ("SEARCH_LOGS timeout database", ActionType.SEARCH_LOGS, {"keywords": ["timeout database"]}),
        ("SEARCH_REF_DOCS retry policy", ActionType.SEARCH_REF_DOCS, {"query": "retry policy"}),
    ],
)
def test_fetch_parser_derives_handler_parameters(text: str, action: ActionType, parameters: dict[str, object]) -> None:
    assert derive_continuation_params(text) == (action, parameters)


def test_fetch_parser_uses_path_only_as_get_code() -> None:
    assert derive_continuation_params("Read svc/src/Foo.java") == (
        ActionType.GET_CODE,
        {"file_path": "svc/src/Foo.java"},
    )


@pytest.mark.parametrize(
    "text",
    [
        None,
        "resume the investigation",
        "QUERY_STATE template-1",
        "GET_CODE",
        "SEARCH_CODE within svc/",
        "WALK_STACK frame details",
        "GET_CODE svc/Foo.java lines 40-10",
    ],
)
def test_fetch_parser_refuses_non_derivable_fetches(text: str | None) -> None:
    assert derive_continuation_params(text) is None


def test_fetch_parser_prefers_longest_action_token() -> None:
    assert derive_continuation_params("GET_CODE_HISTORY svc/Foo.java")[0] == ActionType.GET_CODE_HISTORY  # type: ignore[index]


def test_idempotency_key_is_canonical_and_order_independent() -> None:
    investigation_id = uuid4()
    left = continuation_idempotency_key("tenant", investigation_id, "handoff-v1", "SEARCH_CODE", {"query": "Foo", "scope": "svc"})
    right = continuation_idempotency_key("tenant", investigation_id, "handoff-v1", "SEARCH_CODE", {"scope": "svc", "query": "Foo"})
    assert left == right
    assert len(left) == 64


def _continuation(**overrides: object) -> S7Continuation:
    now = datetime.now(UTC)
    values: dict[str, object] = {
        "continuation_id": uuid4(),
        "tenant_id": "tenant",
        "investigation_id": uuid4(),
        "handoff_version": "handoff-v1",
        "source_handoff_state": HandoffState.BUDGET_DEFERRED,
        "action": "GET_CODE",
        "parameters": {"file_path": "svc/Foo.java"},
        "target": "GET_CODE svc/Foo.java",
        "price": 1.0,
        "idempotency_key": "0123456789abcdef0123456789abcdef",
        "created_at": now,
        "expires_at": now + timedelta(days=7),
    }
    values.update(overrides)
    return S7Continuation(**values)


def test_continuation_model_accepts_resumable_payload() -> None:
    continuation = _continuation(status=ContinuationStatus.PENDING)
    assert continuation.model_config["frozen"] is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"source_handoff_state": HandoffState.PROVED},
        {"action": "FORMULATE_HYPOTHESIS"},
        {"target": "   "},
        {"price": 0},
    ],
)
def test_continuation_model_rejects_invalid_payload(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _continuation(**overrides)
