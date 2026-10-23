"""Unit tests for probe/readiness repository validation (no database)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from investigation_agent_platform.application.onboarding.adapters import (
    AdapterReference,
    AdapterScope,
    SampleGitHostAdapter,
)
from investigation_agent_platform.application.onboarding.catalog import V01_CATALOG
from investigation_agent_platform.application.onboarding.readiness import (
    evaluate_readiness,
)
from investigation_agent_platform.infrastructure.persistence.onboarding_probe_repository import (
    SqlAlchemyOnboardingProbeRepository,
)


def _factory_raises_on_use() -> Any:
    def _factory() -> Any:
        raise AssertionError("session factory must not be used on validation failure")

    return _factory


def _scope(request_id: Any = None) -> AdapterScope:
    return AdapterScope(
        tenant_id="tenant-a",
        application_id="app-1",
        environment="prod",
        request_id=request_id or uuid4(),
        revision=1,
        policy_version="p1",
        catalog_version="v0.1",
    )


async def test_record_evidence_rejects_scope_mismatch_without_io() -> None:
    scope = _scope()
    evidence = SampleGitHostAdapter({"org/repo": {}}).probe(
        _scope(request_id=scope.request_id),
        AdapterReference(kind="repository", provider_key="sample-git-host", resource_id="org/repo"),
        now=datetime.now(UTC),
    )
    other = scope.model_copy(update={"tenant_id": "tenant-b"})
    repository = SqlAlchemyOnboardingProbeRepository(_factory_raises_on_use())
    with pytest.raises(ValueError, match="not pinned to the given scope"):
        await repository.record_evidence(other, evidence)


async def test_record_report_rejects_inconsistent_decision_without_io() -> None:
    scope = _scope()
    report = evaluate_readiness(V01_CATALOG, scope, [], tier="standard", now=datetime.now(UTC))
    assert report.decision.value == "BLOCKED"
    tampered = report.model_copy(update={"blocking_codes": ()})
    repository = SqlAlchemyOnboardingProbeRepository(_factory_raises_on_use())
    with pytest.raises(ValueError, match="must match its blocking codes"):
        await repository.record_report(tampered)
