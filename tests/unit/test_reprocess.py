# tests/unit/test_reprocess.py
"""Reprocess versioning + budget multiplier (no links, no disposition matching)."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from investigation_agent_platform.application.investigation.services import (
    MAX_RUN_ITERATIONS,
    MAX_RUN_LLM_TOKENS,
    ReprocessInvestigationService,
    apply_budget_multiplier,
    require_budget_approver,
)
from investigation_agent_platform.domain.common.exceptions import DomainException
from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
from investigation_agent_platform.domain.hypothesis.models import Hypothesis, HypothesisStatus
from investigation_agent_platform.domain.investigation.models import (
    Investigation,
    InvestigationContext,
    InvestigationRequest,
    InvestigationStatus,
)
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)


def run(coro):
    return asyncio.run(coro)


def _request(problem="p", app="app", session="sess-1"):
    return InvestigationRequest(
        problem_description=problem,
        application_id=app,
        session_id=session,
        requested_by="tester",
    )


def _context():
    now = datetime.now(UTC)
    return InvestigationContext(environment="test", time_window=(now, now))


def _inv(status=InvestigationStatus.COMPLETED, session="sess-1"):
    now = datetime.now(UTC)
    return Investigation(
        session_id=session,
        application_id="app",
        tenant_id="n-a",
        request=_request(session=session),
        context=_context(),
        status=status,
        created_at=now,
        updated_at=now,
        completed_at=now if status.value in ("COMPLETED", "FAILED", "CANCELLED") else None,
    )


class _InvRepo:
    def __init__(self):
        self.store = {}

    async def get_by_id(self, tenant_id, inv_id):
        inv = self.store.get((tenant_id, inv_id))
        return inv if inv is not None and inv.tenant_id == tenant_id else None

    async def create(self, tenant_id, inv):
        self.store[(tenant_id, inv.id)] = inv

    async def save(self, tenant_id, inv, expected_version):
        current = self.store.get((tenant_id, inv.id))
        assert current is not None and current.version == expected_version
        self.store[(tenant_id, inv.id)] = inv


class _ProfileRepo:
    def __init__(self, budget=None):
        from types import SimpleNamespace

        self._profile = SimpleNamespace(
            version=3,
            environment="test",
            status=SimpleNamespace(value="ACTIVE"),
            investigation=SimpleNamespace(default_time_window=3600, evidence_budget=budget or {}),
        )

    async def get_by_application_id(self, tenant_id, app_id):
        return self._profile

    async def list_revisions(self, tenant_id, app_id):
        return [self._profile]


class _EvRepo:
    def __init__(self):
        self.rows = []

    async def find_by_investigation_id(self, tenant_id, inv_id):
        return [e for e in self.rows if e.tenant_id == tenant_id and e.investigation_id == inv_id]

    async def save_batch(self, tenant_id, items, inv_id):
        self.rows.extend(items)


class _HypRepo:
    def __init__(self):
        self.rows = []

    async def find_by_investigation_id(self, tenant_id, inv_id):
        return [h for h in self.rows if h.tenant_id == tenant_id and h.investigation_id == inv_id]

    async def save(self, tenant_id, hyp, inv_id=None, expected_version=0):
        self.rows.append(hyp)


class _CheckpointRepo:
    def __init__(self, tokens=0):
        self.tokens = tokens

    async def get_latest_checkpoint(self, tenant_id, inv_id):
        return {"llm_tokens_used": self.tokens}


def _evidence(iid, summary="log line", etype=EvidenceType.LOG):
    now = datetime.now(UTC)
    return Evidence(
        tenant_id="n-a",
        investigation_id=iid,
        evidence_type=etype,
        provider="test",
        source="test://src",
        title="t",
        summary=summary,
        content_snippet="snippet",
        observed_at=now,
        retrieved_at=now,
        provenance=EvidenceProvenance(
            tenant_id="n-a",
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
    )


def _hypothesis(iid, status=HypothesisStatus.PROPOSED, supporting=()):
    return Hypothesis(
        tenant_id="n-a",
        investigation_id=iid,
        title="t",
        description="d",
        statement="candidate cause",
        status=status,
        supporting_evidence_ids=list(supporting),
    )


def _service(repos, multiplier_repos=None):
    inv_repo, profile_repo, ev_repo, hyp_repo, cp_repo = repos
    return ReprocessInvestigationService(
        investigation_repo=inv_repo,
        profile_repo=profile_repo,
        evidence_repo=ev_repo,
        hypothesis_repo=hyp_repo,
        checkpoint_repo=cp_repo,
    )


def _seed_completed(repos, status=InvestigationStatus.COMPLETED):
    inv_repo, _, ev_repo, hyp_repo, _ = repos
    inv = _inv(status)
    inv_repo.store[("n-a", inv.id)] = inv
    return inv


# --- multiplier math ---


def test_multiplier_math_and_clamps():
    out = apply_budget_multiplier(
        {
            "max_iterations": 20,
            "max_tool_calls": 120,
            "max_evidence_items": 60,
            "max_llm_tokens": 100000,
            "junk": "x",
        },
        5,
    )
    assert out == {
        "max_iterations": 100,
        "max_tool_calls": 600,
        "max_evidence_items": 300,
        "max_llm_tokens": 500000,
    }
    assert "junk" not in out
    out = apply_budget_multiplier({"max_iterations": 20, "max_llm_tokens": 900000}, 10)
    assert out["max_iterations"] == MAX_RUN_ITERATIONS == 100
    assert out["max_llm_tokens"] == MAX_RUN_LLM_TOKENS == 1_000_000
    out = apply_budget_multiplier({}, 3)
    assert out["max_llm_tokens"] == MAX_RUN_LLM_TOKENS


def test_multiplier_rejections():
    from investigation_agent_platform.domain.common.exceptions import DomainValidationException

    for bad in (0, 11, -2, 1.5, "5", True, None):
        with pytest.raises(DomainValidationException):
            apply_budget_multiplier({}, bad)


def test_approval_seam_permissive_and_recorded():
    approval = require_budget_approver("op-1", 5)
    assert approval["approved_by"] == "op-1" and approval["multiplier"] == 5
    assert approval["approved_at"]


# --- reprocess service ---


def test_fresh_creates_new_version_no_artifacts():
    repos = (_InvRepo(), _ProfileRepo(), _EvRepo(), _HypRepo(), _CheckpointRepo(tokens=4321))
    orig = _seed_completed(repos)
    svc = _service(repos)
    new = run(
        svc.execute("n-a", orig.id, "RESTART_FRESH", budget_multiplier=1, principal_id="op-1")
    )
    assert new.id != orig.id
    assert new.status == InvestigationStatus.CREATED
    assert new.session_id == f"{orig.session_id}-r2"
    md = new.metadata
    assert md["reprocessed_from"] == str(orig.id)
    assert md["reprocess_mode"] == "RESTART_FRESH"
    assert md["prior_runs"][0]["tokens_spent"] == 4321
    assert md["budget_approval"]["approved_by"] == "op-1"
    assert md["effective_limits"]["max_llm_tokens"] == MAX_RUN_LLM_TOKENS
    # Disposition bypassed: fresh, startable, never cancelled.
    assert md["disposition"]["action"] == "fresh"
    assert "duplicate" not in md["disposition"]["reason"]
    # Original untouched.
    assert repos[0].store[("n-a", orig.id)].status == InvestigationStatus.COMPLETED


def test_fresh_on_active_allowed():
    repos = (_InvRepo(), _ProfileRepo(), _EvRepo(), _HypRepo(), _CheckpointRepo())
    orig = _inv(InvestigationStatus.INVESTIGATING)
    repos[0].store[("n-a", orig.id)] = orig
    new = run(_service(repos).execute("n-a", orig.id, "RESTART_FRESH"))
    assert new.id != orig.id and new.status == InvestigationStatus.CREATED


def test_continue_copies_artifacts_with_remap_and_demotion():
    repos = (_InvRepo(), _ProfileRepo(), _EvRepo(), _HypRepo(), _CheckpointRepo())
    inv_repo, _, ev_repo, hyp_repo, _ = repos
    orig = _seed_completed(repos)
    ev1 = _evidence(orig.id, summary="a")
    ev2 = _evidence(orig.id, summary="b")
    ev_repo.rows.extend([ev1, ev2])
    hyp = _hypothesis(orig.id, status=HypothesisStatus.VERIFIED, supporting=[ev1.evidence_id])
    hyp_repo.rows.append(hyp)
    new = run(_service(repos).execute("n-a", orig.id, "RESTART_CONTINUE"))
    copied_ev = [e for e in ev_repo.rows if e.investigation_id == new.id]
    assert len(copied_ev) == 2
    assert {str(e.evidence_id) for e in copied_ev}.isdisjoint(
        {str(ev1.evidence_id), str(ev2.evidence_id)}
    )
    assert len({e.fingerprint for e in copied_ev}) == 2  # re-salted, unique
    assert all(e.provenance.investigation_id == new.id for e in copied_ev)
    copied_hyp = [h for h in hyp_repo.rows if h.investigation_id == new.id]
    assert len(copied_hyp) == 1
    assert copied_hyp[0].status == HypothesisStatus.UNDER_INVESTIGATION
    assert copied_hyp[0].assessments == []
    # Supporting refs remapped to the NEW evidence IDs.
    new_ids = {str(e.evidence_id) for e in copied_ev}
    assert set(map(str, copied_hyp[0].supporting_evidence_ids)) <= new_ids
    assert str(ev1.evidence_id) not in map(str, copied_hyp[0].supporting_evidence_ids)


def test_continue_on_active_rejected():
    repos = (_InvRepo(), _ProfileRepo(), _EvRepo(), _HypRepo(), _CheckpointRepo())
    orig = _inv(InvestigationStatus.RUNNING)
    repos[0].store[("n-a", orig.id)] = orig
    from investigation_agent_platform.domain.common.exceptions import (
        InvalidInvestigationStateException,
    )

    with pytest.raises(InvalidInvestigationStateException):
        run(_service(repos).execute("n-a", orig.id, "RESTART_CONTINUE"))


def test_missing_and_bad_mode():
    repos = (_InvRepo(), _ProfileRepo(), _EvRepo(), _HypRepo(), _CheckpointRepo())
    with pytest.raises(DomainException) as exc:
        run(_service(repos).execute("n-a", uuid4(), "RESTART_FRESH"))
    assert exc.value.http_status_code == 404
    orig = _seed_completed(repos)
    from investigation_agent_platform.domain.common.exceptions import (
        InvalidLifecycleTransitionException,
    )

    with pytest.raises(InvalidLifecycleTransitionException):
        run(_service(repos).execute("n-a", orig.id, "BOGUS_MODE"))


def test_multiplier_recorded_in_metadata():
    repos = (
        _InvRepo(),
        _ProfileRepo(budget={"max_iterations": 20, "max_tool_calls": 40}),
        _EvRepo(),
        _HypRepo(),
        _CheckpointRepo(),
    )
    orig = _seed_completed(repos)
    new = run(_service(repos).execute("n-a", orig.id, "RESTART_FRESH", budget_multiplier=5))
    assert new.metadata["effective_limits"]["max_iterations"] == 100
    assert new.metadata["effective_limits"]["max_tool_calls"] == 200
    assert new.metadata["budget_approval"]["multiplier"] == 5


# --- wire tests ---


class TestReprocessWire:
    def _client(self, monkeypatch):
        monkeypatch.setenv("IAP_ENVIRONMENT", "development")

        from fastapi.testclient import TestClient

        from investigation_agent_platform.api.app import create_app
        from investigation_agent_platform.api.dependencies import AppContext, set_app_context

        ctx = AppContext()
        set_app_context(ctx)
        return TestClient(create_app()), ctx

    def _headers(self):
        return {"X-Tenant-ID": "tenant-a"}

    def _create(self, client, problem="wire problem"):
        return client.post(
            "/api/v1/investigations",
            json={
                "application_id": "example-app",
                "problem_description": problem,
                "session_id": "sess-repro-1",
            },
            headers=self._headers(),
        )

    def test_invalid_id_400(self, monkeypatch):
        client, _ = self._client(monkeypatch)
        resp = client.post(
            "/api/v1/investigations/not-a-uuid/reprocess",
            json={"mode": "RESTART_FRESH"},
            headers=self._headers(),
        )
        assert resp.status_code == 400

    def test_unknown_id_404(self, monkeypatch):
        client, _ = self._client(monkeypatch)
        resp = client.post(
            f"/api/v1/investigations/{uuid4()}/reprocess",
            json={"mode": "RESTART_FRESH"},
            headers=self._headers(),
        )
        assert resp.status_code == 404

    def test_bad_mode_422(self, monkeypatch):
        client, _ = self._client(monkeypatch)
        inv_id = self._create(client).json()["id"]
        resp = client.post(
            f"/api/v1/investigations/{inv_id}/reprocess",
            json={"mode": "BOGUS_MODE"},
            headers=self._headers(),
        )
        assert resp.status_code == 422

    def test_continue_on_active_409(self, monkeypatch):
        client, _ = self._client(monkeypatch)
        inv_id = self._create(client).json()["id"]
        resp = client.post(
            f"/api/v1/investigations/{inv_id}/reprocess",
            json={"mode": "RESTART_CONTINUE"},
            headers=self._headers(),
        )
        assert resp.status_code == 409

    def test_fresh_versions_and_dispatches(self, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        client, ctx = self._client(monkeypatch)
        ctx.temporal_client = MagicMock()
        ctx.temporal_client.start_workflow = AsyncMock()
        import sys as _sys

        router_mod = _sys.modules["investigation_agent_platform.api.v1.routers.investigations"]
        monkeypatch.setattr(router_mod, "isWorkerVisibleStore", lambda repo: True)
        orig_id = self._create(client, problem="unique wire problem fresh").json()["id"]
        resp = client.post(
            f"/api/v1/investigations/{orig_id}/reprocess",
            json={"mode": "RESTART_FRESH", "budget_multiplier": 5, "reason": "wire"},
            headers=self._headers(),
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["investigation_id"] != orig_id
        detail_resp = client.get(
            f"/api/v1/investigations/{body['investigation_id']}", headers=self._headers()
        ).json()
        detail = detail_resp["investigation"]
        assert detail["metadata"]["reprocessed_from"] == orig_id
        assert detail["metadata"]["reprocess_mode"] == "RESTART_FRESH"
        assert detail["metadata"]["effective_limits"]["max_tool_calls"] == 250
        assert detail["metadata"]["budget_approval"]["multiplier"] == 5
        # Disposition bypassed: no duplicate linking/closure.
        assert detail["metadata"]["disposition"]["action"] == "fresh"
        assert detail["status"] == "RUNNING"  # dispatched; never CANCELLED by triage
        assert detail_resp["links"] == []


def test_continue_prunes_dangling_supporting_refs():
    from uuid import uuid4

    repos = (_InvRepo(), _ProfileRepo(), _EvRepo(), _HypRepo(), _CheckpointRepo())
    inv_repo, _, ev_repo, hyp_repo, _ = repos
    orig = _seed_completed(repos)
    ev1 = _evidence(orig.id, summary="a")
    ev_repo.rows.append(ev1)
    purged = uuid4()
    hyp = _hypothesis(
        orig.id,
        status=HypothesisStatus.UNDER_INVESTIGATION,
        supporting=[ev1.evidence_id, purged],
    )
    hyp_repo.rows.append(hyp)
    new = run(_service(repos).execute("n-a", orig.id, "RESTART_CONTINUE"))
    copied_hyp = [h for h in hyp_repo.rows if h.investigation_id == new.id]
    assert len(copied_hyp) == 1
    kept = set(map(str, copied_hyp[0].supporting_evidence_ids))
    assert len(kept) == 1
    assert str(purged) not in kept
    assert str(ev1.evidence_id) not in kept
