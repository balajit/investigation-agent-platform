# tests/unit/test_part10_phase12.py
"""Part 10 Phase 12: reserved query_string contract, vocabulary mechanism."""

import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from investigation_agent_platform.domain.evidence.requests import RuntimeEvidenceRequest
from investigation_agent_platform.domain.observability.mapping import (
    FieldMapping,
    FieldValueType,
    generic_ecs_mapping,
)
from investigation_agent_platform.infrastructure.configuration.mapping_registry import (
    load_mapping_profiles,
)
from investigation_agent_platform.infrastructure.evidence.runtime.query import (
    build_query_body,
    normalize_request,
)
from investigation_agent_platform.infrastructure.evidence.runtime.validation import (
    ProviderCeilings,
)

SHIPPED_DIR = Path(__file__).resolve().parents[2] / "config" / "observability-mappings"


def test_query_string_accepted_sanitized_never_executed():
    """Reserved legacy field: validated at the boundary, invisible to the body."""
    request = RuntimeEvidenceRequest(environment="production", query_string="timeout checkout")
    mapping = generic_ecs_mapping()
    normalized = normalize_request("tenant-a", uuid.uuid4(), request, ProviderCeilings(), mapping)
    body = build_query_body(normalized, mapping, search_after=None, query_timeout_seconds=25.0)
    serialized = str(body)
    assert "timeout checkout" not in serialized
    # The sanitizer still rejects hostile content at construction.
    with pytest.raises(ValidationError):
        RuntimeEvidenceRequest(environment="production", query_string="x; DROP TABLE y")


def test_identifier_vocabulary_mechanism_not_frozen_set():
    """New logical keys are addable per-mapping without touching shared code."""
    import pytest

    from investigation_agent_platform.domain.common.exceptions import DomainValidationException

    registry = load_mapping_profiles(SHIPPED_DIR)
    union: set[str] = set()
    for source_id in sorted(registry.source_ids):
        union |= set(registry.resolve(source_id).identifiers)
    # Generic-ECS keys remain available everywhere they were.
    assert {"trace_id", "session_id"} <= union
    # Shipped mappings extend the vocabulary (not a frozen six).
    assert "transaction_key" in union or "request_id" in union

    extended = generic_ecs_mapping().model_copy(
        update={
            "source_id": "synthetic",
            "identifiers": {
                **generic_ecs_mapping().identifiers,
                "correlation_token": FieldMapping(
                    candidates=["correlation.token"], value_type=FieldValueType.KEYWORD
                ),
            },
        }
    )
    from investigation_agent_platform.infrastructure.evidence.runtime.validation import (
        validate_identifier_items,
    )

    assert validate_identifier_items({"correlation_token": "abc"}, extended) == [
        ("correlation_token", "abc")
    ]
    with pytest.raises(DomainValidationException):
        validate_identifier_items({"correlation_token": "abc"}, generic_ecs_mapping())
