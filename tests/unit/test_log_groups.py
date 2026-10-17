# tests/unit/test_log_groups.py
"""Log-group persistence + compile grouping + per-run override (no DB)."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from investigation_agent_platform.application.investigation.context_compiler import (
    compile_evidence_excerpts,
    compile_reasoning_context,
    log_grouping_enabled,
)
from investigation_agent_platform.application.worker.activities import (
    _persist_log_exemplars,
    _split_log_bypass,
)
from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType, LogExemplar
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)


def run(coro):
    return asyncio.run(coro)


def _evidence(
    summary="log line",
    snippet="",
    etype=EvidenceType.LOG,
    relevance=1.0,
    provider="elastic",
    source="es://logs",
    age_seconds=0,
):
    from datetime import timedelta

    iid = uuid4()
    now = datetime.now(UTC) - timedelta(seconds=age_seconds)
    return Evidence(
        tenant_id="tenant-a",
        investigation_id=iid,
        evidence_type=etype,
        provider=provider,
        source=source,
        title="t",
        summary=summary,
        content_snippet=snippet,
        observed_at=now,
        retrieved_at=now,
        provenance=EvidenceProvenance(
            tenant_id="tenant-a",
            investigation_id=iid,
            provider_type="test",
            requested_provider_id="test",
            actual_provider_id="test",
            source_system="test",
            retrieval_timestamp=now,
            query_fingerprint=QueryFingerprint(
                provider_type="test", operation="test", normalized_query_hash="abc"
            ),
            source_location=SourceLocation(system="test", identifier="x"),
        ),
        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
        fingerprint=f"fp-{uuid4()}",
        relevance=relevance,
    )


class _Repo:
    def __init__(self, fail=False):
        self.saved: list = []
        self.fail = fail

    async def save_batch(self, tenant_id, exemplars):
        if self.fail:
            raise RuntimeError("store down")
        self.saved.extend(exemplars)
        return len(exemplars)


# --- banking bypass ---


def test_split_log_bypass_partitions():
    logs = [_evidence(etype=EvidenceType.RUNTIME_LOG), _evidence(etype=EvidenceType.LOG)]
    other = [_evidence(etype=EvidenceType.DATABASE_STATE)]
    kept_logs, rest = _split_log_bypass(logs + other)
    assert len(kept_logs) == 2 and len(rest) == 1


# --- exemplar persistence ---


def test_persist_groups_duplicates_with_verbatim_ids():
    repo = _Repo()
    ctx = SimpleNamespace(log_exemplar_repo=repo)
    inv = uuid4()
    rows = [
        _evidence(summary="GET /x 200", snippet="body", age_seconds=30),
        _evidence(summary="GET /x 200", snippet="body", age_seconds=10),
        _evidence(summary="GET /y 500", snippet="err", age_seconds=0),
    ]
    n = run(_persist_log_exemplars(ctx, "n-a", inv, rows))
    assert n == 2
    groups = sorted(repo.saved, key=lambda g: g.count, reverse=True)
    assert groups[0].count == 2
    assert [str(m) for m in groups[0].member_evidence_ids] == [
        str(rows[0].evidence_id),
        str(rows[1].evidence_id),
    ]
    assert groups[0].investigation_id == inv
    assert groups[0].tenant_id == "n-a"


def test_persist_skips_non_log_rows():
    repo = _Repo()
    ctx = SimpleNamespace(log_exemplar_repo=repo)
    n = run(_persist_log_exemplars(ctx, "n-a", uuid4(), [_evidence(etype=EvidenceType.DATABASE_STATE)]))
    assert n == 0 and repo.saved == []


def test_persist_fail_open_without_repo_and_on_error():
    assert run(_persist_log_exemplars(SimpleNamespace(), "n-a", uuid4(), [_evidence()])) == 0
    assert (
        run(_persist_log_exemplars(SimpleNamespace(log_exemplar_repo=_Repo(fail=True)), "n-a", uuid4(), [_evidence()]))
        == 0
    )


def test_log_exemplar_domain_bounds():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        LogExemplar(tenant_id="t", investigation_id=uuid4(), signature="s", count=0)


# --- compiler grouping + override ---


def test_compile_groups_duplicate_logs_with_member_ids():
    rows = [
        _evidence(summary="GET /x 200", snippet="b", relevance=0.2, age_seconds=100),
        _evidence(summary="GET /x 200", snippet="b", relevance=0.2, age_seconds=90),
        _evidence(summary="GET /y 500", snippet="e", relevance=0.9, age_seconds=0),
    ]
    excerpts = compile_evidence_excerpts(rows, set(), True)
    assert len(excerpts) == 2
    grouped = next(e for e in excerpts if e["log_group"] is not None)
    assert grouped["log_group"]["count"] == 2
    assert sorted(grouped["log_group"]["member_ids"]) == sorted(
        str(r.evidence_id) for r in rows[:2]
    )


def test_compile_grouping_off_restores_rows():
    rows = [
        _evidence(summary="GET /x 200", snippet="b", relevance=0.2, age_seconds=100),
        _evidence(summary="GET /x 200", snippet="b", relevance=0.2, age_seconds=90),
    ]
    excerpts = compile_evidence_excerpts(rows, set(), False)
    assert len(excerpts) == 2
    assert all(e["log_group"] is None for e in excerpts)


def test_bound_member_keeps_group_tier1():
    low = _evidence(summary="GET /x 200", snippet="b", relevance=0.05, age_seconds=500)
    twin = _evidence(summary="GET /x 200", snippet="b", relevance=0.05, age_seconds=400)
    excerpts = compile_evidence_excerpts([low, twin], {str(low.evidence_id)}, True)
    assert len(excerpts) == 1
    assert excerpts[0]["tier"] == 1


def test_non_log_rows_never_grouped():
    rows = [
        _evidence(summary="same", etype=EvidenceType.DATABASE_STATE),
        _evidence(summary="same", etype=EvidenceType.DATABASE_STATE),
    ]
    excerpts = compile_evidence_excerpts(rows, set(), True)
    assert len(excerpts) == 2


def _state_with_params(evidence, parameters):
    from datetime import timedelta

    from investigation_agent_platform.domain.investigation.models import (
        Investigation,
        InvestigationContext,
        InvestigationRequest,
        InvestigationState,
    )

    now = datetime.now(UTC)
    inv = Investigation(
        session_id="s",
        application_id="app",
        tenant_id="n-a",
        request=InvestigationRequest(
            application_id="app",
            problem_description="p",
            session_id="s",
            requested_by="t",
            parameters=parameters,
        ),
        context=InvestigationContext(
            environment="test", time_window=(now - timedelta(hours=1), now)
        ),
    )
    return InvestigationState(investigation=inv, evidence=evidence)


def test_per_run_override_off():
    rows = [
        _evidence(summary="GET /x 200", snippet="b", relevance=0.2, age_seconds=100),
        _evidence(summary="GET /x 200", snippet="b", relevance=0.2, age_seconds=90),
    ]
    assert log_grouping_enabled(_state_with_params(rows, {})) is True
    assert log_grouping_enabled(_state_with_params(rows, {"log_optimization": "off"})) is False
    ctx = compile_reasoning_context(_state_with_params(rows, {"log_optimization": "off"}))
    assert len(ctx["evidence_excerpts"]) == 2
    ctx = compile_reasoning_context(_state_with_params(rows, {}))
    assert len(ctx["evidence_excerpts"]) == 1


def test_env_kill_switch(monkeypatch):

    rows = [_evidence(summary="GET /x 200", snippet="b", relevance=0.2)]
    state = _state_with_params(rows, {})
    assert log_grouping_enabled(state) is True
    monkeypatch.setenv("IAP_COMPILER_LOG_GROUP", "0")
    assert log_grouping_enabled(state) is False
    monkeypatch.setenv("IAP_COMPILER_LOG_GROUP", "false")
    assert log_grouping_enabled(state) is False


def test_in_memory_exemplar_repo_roundtrip():
    from investigation_agent_platform.api.dependencies import InMemoryLogExemplarRepository

    repo = InMemoryLogExemplarRepository()
    inv = uuid4()
    exemplar = LogExemplar(
        tenant_id="n-a",
        investigation_id=inv,
        signature="sig",
        member_evidence_ids=[uuid4(), uuid4()],
        count=2,
    )
    assert run(repo.save_batch("n-a", [exemplar])) == 1
    assert run(repo.save_batch("n-a", [exemplar])) == 0  # idempotent
    rows = run(repo.list_by_investigation("n-a", inv))
    assert len(rows) == 1 and rows[0].count == 2
    assert run(repo.list_by_investigation("other", inv)) == []


def test_coerce_root_ids_drops_unparseable():
    from uuid import uuid4

    from investigation_agent_platform.application.worker.activities import _coerce_root_ids

    good = str(uuid4())
    valid, dropped = _coerce_root_ids(
        {"root_evidence_ids": [good, "t0dxR6EB", None, 12345, "not-a-uuid"]}
    )
    assert len(valid) == 1 and str(valid[0]) == good
    assert dropped == 4
    assert _coerce_root_ids({}) == ([], 0)
    assert _coerce_root_ids(None) == ([], 0)

