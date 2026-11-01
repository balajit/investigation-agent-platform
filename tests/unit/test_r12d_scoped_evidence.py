"""R12D: evidence GET bound to the investigation; approve-action gated.

get_for_investigation returns only rows the investigation owns or holds
bank interest in. Approve-action requires a pending approval for the
action (workflow query, fail-closed) and an approver role outside dev.
Written first, red before the fix.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest


def _evidence(tenant, inv_id, fingerprint, evidence_type=None):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.domain.evidence.models import (
        Evidence,
        EvidenceType,
    )

    if evidence_type is None:
        evidence_type = EvidenceType.LOG
    from investigation_agent_platform.domain.provenance.models import (
        EvidenceFreshness,
        EvidenceProvenance,
        QueryFingerprint,
        SourceLocation,
    )

    now = datetime.now(UTC)
    return Evidence(
        tenant_id=tenant,
        investigation_id=inv_id,
        evidence_type=evidence_type,
        provider="test",
        source="test://log/1",
        title="t",
        summary="startup ok",
        observed_at=now,
        retrieved_at=now,
        provenance=EvidenceProvenance(
            tenant_id=tenant,
            investigation_id=inv_id,
            provider_type="TEST",
            requested_provider_id="test",
            actual_provider_id="test",
            source_system="test",
            retrieval_timestamp=now,
            query_fingerprint=QueryFingerprint(
                provider_type="TEST", operation="test", normalized_query_hash="abc"
            ),
            source_location=SourceLocation(system="test", identifier="test://log/1"),
        ),
        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
        fingerprint=fingerprint,
    )


def _client():  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient

    from investigation_agent_platform.api.app import create_app
    from investigation_agent_platform.api.dependencies import AppContext, set_app_context

    ctx = AppContext()
    set_app_context(ctx)
    return TestClient(create_app()), ctx


@pytest.mark.asyncio
async def test_r12d_t03_cross_tenant_evidence_id() -> None:
    import asyncio as _asyncio

    from investigation_agent_platform.domain.investigation.models import InvestigationRequest

    client, ctx = _client()
    inv_a = uuid.uuid4()
    ev = _evidence("tenant-a", inv_a, "fp-cross-1")
    await ctx.evidence_repo.save("tenant-a", ev, inv_a)

    from investigation_agent_platform.api.dependencies import _DEFAULT_PROFILE

    await ctx.profile_repo.save(
        "tenant-b", _DEFAULT_PROFILE.model_copy(update={"tenant_id": "tenant-b"})
    )
    inv_b = await ctx.create_investigation_service().execute(
        InvestigationRequest(
            application_id="example-app",
            problem_description="cross-tenant",
            session_id="sess-r12d-cross",
            requested_by="t",
        ),
        tenant_id="tenant-b",
    )
    resp = client.get(
        f"/api/v1/investigations/{inv_b.id}/evidence/{ev.evidence_id}",
        headers={"X-Tenant-ID": "tenant-b"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "EVIDENCE_NOT_FOUND"
    assert "fp-cross-1" not in resp.text
    _asyncio.get_event_loop()


def _seed_two_investigations():  # type: ignore[no-untyped-def]
    import asyncio as _asyncio

    from investigation_agent_platform.domain.investigation.models import InvestigationRequest

    client, ctx = _client()

    async def _go():  # type: ignore[no-untyped-def]
        inv_a = await ctx.create_investigation_service().execute(
            InvestigationRequest(
                application_id="example-app",
                problem_description="a",
                session_id="sess-r12d-a",
                requested_by="t",
            ),
            tenant_id="tenant-a",
        )
        inv_b = await ctx.create_investigation_service().execute(
            InvestigationRequest(
                application_id="example-app",
                problem_description="b",
                session_id="sess-r12d-b",
                requested_by="t",
            ),
            tenant_id="tenant-a",
        )
        return inv_a, inv_b

    return client, ctx, _asyncio.run(_go())


def test_r12d_t01_evidence_of_another_investigation() -> None:
    import asyncio as _asyncio

    client, ctx, (inv_a, inv_b) = _seed_two_investigations()

    async def _save():  # type: ignore[no-untyped-def]
        await ctx.evidence_repo.save("tenant-a", _evidence("tenant-a", inv_b.id, "fp-b-1"), inv_b.id)

    ev_id_holder: list = []
    orig_save = ctx.evidence_repo.save

    async def _save_and_capture(tenant, ev, inv_id):  # type: ignore[no-untyped-def]
        ev_id_holder.append(ev.evidence_id)
        return await orig_save(tenant, ev, inv_id)

    ctx.evidence_repo.save = _save_and_capture  # type: ignore[method-assign]
    _asyncio.run(_save())
    resp = client.get(
        f"/api/v1/investigations/{inv_a.id}/evidence/{ev_id_holder[0]}",
        headers={"X-Tenant-ID": "tenant-a"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "EVIDENCE_NOT_FOUND"


def test_r12d_t02_shared_bank_interest_row_is_readable() -> None:
    import asyncio as _asyncio

    client, ctx, (inv_a, inv_b) = _seed_two_investigations()

    async def _save():  # type: ignore[no-untyped-def]
        # Same fingerprint banked under B first (canonical owner),
        # then referenced from A: records A's bank interest.
        from investigation_agent_platform.domain.evidence.models import (
            EvidenceType as _ET,
        )

        first = _evidence("tenant-a", inv_b.id, "fp-shared-7", _ET.SOURCE_CODE)
        await ctx.evidence_repo.save("tenant-a", first, inv_b.id)
        second = _evidence("tenant-a", inv_a.id, "fp-shared-7", _ET.SOURCE_CODE)
        await ctx.evidence_repo.save("tenant-a", second, inv_a.id)
        return first.evidence_id

    canonical_id = _asyncio.run(_save())
    resp = client.get(
        f"/api/v1/investigations/{inv_a.id}/evidence/{canonical_id}",
        headers={"X-Tenant-ID": "tenant-a"},
    )
    assert resp.status_code == 200, resp.text


def _started_inv_with_temporal(ctx, approve_ids):  # type: ignore[no-untyped-def]
    import asyncio as _asyncio
    from unittest.mock import AsyncMock, MagicMock

    from investigation_agent_platform.domain.investigation.models import (
        InvestigationRequest,
        InvestigationStatus,
    )

    async def _go():  # type: ignore[no-untyped-def]
        req = InvestigationRequest(
            application_id="example-app",
            problem_description="approval gate",
            session_id="sess-r12d-approve",
            requested_by="tester",
        )
        inv = await ctx.create_investigation_service().execute(req, tenant_id="tenant-a")
        started = inv.model_copy(update={"status": InvestigationStatus.INVESTIGATING})
        await ctx.investigation_repo.save("tenant-a", started, inv.version)
        return started

    inv = _asyncio.run(_go())
    handle = MagicMock()
    handle.signal = AsyncMock(return_value=None)
    handle.query = AsyncMock(return_value=list(approve_ids))
    client_handle = MagicMock()
    client_handle.get_workflow_handle = MagicMock(return_value=handle)
    ctx.temporal_client = client_handle
    return inv, handle


def test_r12d_t04_unknown_action_id_and_role_gate() -> None:

    from fastapi.testclient import TestClient

    from investigation_agent_platform.api.app import create_app
    from investigation_agent_platform.api.dependencies import AppContext, set_app_context

    ctx = AppContext()
    set_app_context(ctx)
    client = TestClient(create_app())
    inv, handle = _started_inv_with_temporal(ctx, [])
    # Unknown action id: no pending approval for it.
    resp = client.post(
        f"/api/v1/investigations/{inv.id}/approve-action?action_id=nope&approved=true",
        headers={"X-Tenant-ID": "tenant-a"},
    )
    assert resp.status_code == 404
    handle.query.assert_awaited()
    handle.signal.assert_not_awaited()


def test_r12d_t04_dev_header_identity_still_allowed() -> None:

    from fastapi.testclient import TestClient

    from investigation_agent_platform.api.app import create_app
    from investigation_agent_platform.api.dependencies import AppContext, set_app_context

    ctx = AppContext()
    set_app_context(ctx)
    client = TestClient(create_app())
    inv, handle = _started_inv_with_temporal(ctx, ["act-1"])
    resp = client.post(
        f"/api/v1/investigations/{inv.id}/approve-action?action_id=act-1&approved=true",
        headers={"X-Tenant-ID": "tenant-a"},
    )
    assert resp.status_code == 200, resp.text
    handle.signal.assert_awaited_once()


def test_r12d_t04_missing_role_outside_dev_is_forbidden(monkeypatch) -> None:
    import os

    from investigation_agent_platform.api import tenant as tenant_mod

    monkeypatch.setenv("IAP_ENVIRONMENT", "production")
    monkeypatch.delenv("IAP_AUTH_APPROVER_ROLE", raising=False)

    async def _go():  # type: ignore[no-untyped-def]
        from types import SimpleNamespace

        return await tenant_mod.require_approver_role(
            SimpleNamespace(tenant_id="t", principal_id="p", roles=[])
        )

    import asyncio as _asyncio

    with pytest.raises(Exception) as exc:
        _asyncio.run(_go())
    assert getattr(exc.value, "http_status", None) == 403
    assert os.environ["IAP_ENVIRONMENT"] == "production"


def test_r12d_t04_matching_role_outside_dev_is_allowed(monkeypatch) -> None:
    monkeypatch.setenv("IAP_ENVIRONMENT", "production")
    monkeypatch.setenv("IAP_AUTH_APPROVER_ROLE", "iap-approver")

    async def _go():  # type: ignore[no-untyped-def]
        from types import SimpleNamespace

        from investigation_agent_platform.api import tenant as tenant_mod

        return await tenant_mod.require_approver_role(
            SimpleNamespace(tenant_id="t", principal_id="p", roles=["viewer", "iap-approver"])
        )

    import asyncio as _asyncio

    assert _asyncio.run(_go()) == "iap-approver"
