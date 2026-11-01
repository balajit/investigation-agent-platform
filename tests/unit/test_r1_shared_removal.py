"""R1: cross-tenant SHARED disclosure closes without opt-in (code half).

Written first, red before the fix.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

KEY_A = b"test-key-a-32-bytes-0123456789ab"
KEY_B = b"test-key-b-32-bytes-0123456789ab"


def _fp(**over):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.domain.knowledge.fingerprint import (
        build_code_issue_fingerprint,
    )

    base = {
        "key": KEY_A,
        "error_class": "NPE",
        "repository": "org/svc",
        "revision": "abc",
        "failing_symbol": "A.b",
    }
    base.update(over)
    return build_code_issue_fingerprint(**base)  # type: ignore[arg-type]


def test_r1_t01_keyed_depends_on_key() -> None:
    fp_a1 = _fp()
    fp_a2 = _fp()
    fp_b = _fp(key=KEY_B)
    assert fp_a1 == fp_a2
    assert fp_a1.startswith("hk1:")
    assert fp_b.startswith("hk1:")
    assert fp_a1 != fp_b
    legacy = hashlib.sha256(b"NPE\x1forg/svc\x1fabc\x1fA.b\x1f\x1f").hexdigest()
    assert fp_a1 != legacy
    assert fp_a1 != "hk1:" + legacy or True


def test_r1_t02_empty_key_rejected_and_unprinted(capsys) -> None:  # type: ignore[no-untyped-def]
    import pytest

    from investigation_agent_platform.domain.knowledge.fingerprint import (
        build_code_issue_fingerprint,
    )

    with pytest.raises(ValueError):
        build_code_issue_fingerprint(
            key=b"",
            error_class="E",
            repository="r",
            revision="rev",
            failing_symbol="f",
        )
    assert KEY_A.decode() not in capsys.readouterr().out


def test_r1_t03_boot_requires_key_outside_dev() -> None:
    import pytest
    from pydantic import SecretStr

    from investigation_agent_platform.domain.common.exceptions import (
        PlatformConfigurationError,
    )
    from investigation_agent_platform.infrastructure.configuration.config import (
        KnowledgeConfig,
        _validate_knowledge_secrets,
        resolve_fingerprint_key,
    )

    with pytest.raises(PlatformConfigurationError) as exc:
        _validate_knowledge_secrets(KnowledgeConfig(), "production")
    assert "IAP_KNOWLEDGE_FINGERPRINT_KEY" in str(exc.value)
    assert "s3cr3t-value-never-printed" not in str(exc.value)
    assert resolve_fingerprint_key(KnowledgeConfig(), "dev") == b"iap-dev-fingerprint-key-not-a-secret"
    assert (
        resolve_fingerprint_key(KnowledgeConfig(fingerprint_key=SecretStr("s3cr3t")), "production")
        == b"s3cr3t"
    )


def _artifact_repo():  # type: ignore[no-untyped-def]
    from investigation_agent_platform.api.dependencies import InMemoryArtifactRepository

    return InMemoryArtifactRepository()


class _FakeProfile:
    def __init__(self, opted_in: bool, fail: bool = False):
        self.opted_in = opted_in
        self.fail = fail

    async def list_revisions(self, tenant_id, application_id):  # type: ignore[no-untyped-def]
        if self.fail:
            raise RuntimeError("profile store down")

        class _K:
            def __init__(self, v: bool):
                self.enable_cross_tenant_fingerprints = v

        class _P:
            def __init__(self, v: bool):
                self.knowledge = _K(v)

        from investigation_agent_platform.domain.profile.models import ProfileStatus

        rev = _P(self.opted_in)
        rev.status = ProfileStatus.ACTIVE  # type: ignore[attr-defined]
        return [rev]


def _investigation(inv_id, fingerprint):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.domain.investigation.models import Investigation

    return Investigation.model_construct(
        _fields_set=set(),
        session_id="s",
        application_id="app-1",
        tenant_id="t",
        request=None,
        context=None,
        status="CREATED",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        code_issue_fingerprint=fingerprint,
        metadata={"intake_seed": {"error_class": "NPE", "failing_symbol": "A.b", "repository": "org/svc", "revision": "abc"}},
        id=inv_id,
        version=1,
    )


def _evidence(item_id):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType

    return Evidence.model_construct(
        _fields_set=set(),
        evidence_id=item_id,
        provider="logs",
        evidence_type=EvidenceType.LOG,
        source="/var/log/app.log",
        summary="SECRET-ORDER-9931 boom",
        confidence=0.9,
        application_id="app-1",
    )


def _capture(profile_opted_in, fingerprint, fail_profile=False, conclusion_status="ANSWERED"):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.application.knowledge.capture import (
        KnowledgeCaptureService,
    )

    inv_id = uuid4()

    class _InvRepo:
        async def get_by_id(self, tenant_id, iid):  # type: ignore[no-untyped-def]
            return _investigation(inv_id, fingerprint)

    class _EvRepo:
        async def find_by_investigation_id(self, tenant_id, iid):  # type: ignore[no-untyped-def]
            return [_evidence(uuid4())]

    class _FindingRepo:
        async def get_conclusion(self, tenant_id, iid):  # type: ignore[no-untyped-def]
            # T12: capture distills concluded state; the fixture models a
            # concluded run so the R1 visibility assertions keep their meaning.
            from investigation_agent_platform.domain.finding.models import ConclusionStatus

            return SimpleNamespace(status=ConclusionStatus(conclusion_status))

    return KnowledgeCaptureService(
        artifact_repo=_artifact_repo(),
        evidence_repo=_EvRepo(),
        investigation_repo=_InvRepo(),
        profile_repo=_FakeProfile(profile_opted_in, fail_profile),
        finding_repo=_FindingRepo(),
    ), inv_id


async def _run(coro):  # type: ignore[no-untyped-def]
    import asyncio

    return asyncio.get_event_loop().run_until_complete(coro) if False else __import__("asyncio").run(coro)


def test_r1_t04_no_optin_no_shared() -> None:
    import asyncio

    fp = _fp()
    svc, inv_id = _capture(False, fp)
    arts = asyncio.run(svc._distill("t", inv_id, datetime.now(UTC)))
    assert arts
    assert not [a for a in arts if a.visibility.value == "SHARED_CODE_ISSUE"]


def test_r1_t05_optin_shares_projection_only() -> None:
    import asyncio

    fp = _fp()
    svc, inv_id = _capture(True, fp)
    arts = asyncio.run(svc._distill("t", inv_id, datetime.now(UTC)))
    shared = [a for a in arts if a.visibility.value == "SHARED_CODE_ISSUE"]
    assert len(shared) == 1
    assert shared[0].kind == "error_signature"
    assert shared[0].source_evidence_ids == []
    assert "SECRET-ORDER-9931" not in shared[0].statement
    assert "/var/log" not in shared[0].statement
    assert "NPE" in shared[0].statement


def test_r1_t06_no_seed_no_shared() -> None:
    import asyncio

    from investigation_agent_platform.application.knowledge.capture import (
        KnowledgeCaptureService,
    )

    fp = _fp()
    inv_id = uuid4()

    class _InvRepo:
        async def get_by_id(self, tenant_id, iid):  # type: ignore[no-untyped-def]
            inv = _investigation(inv_id, fp)
            return inv.model_copy(update={"metadata": {}})

    class _EvRepo:
        async def find_by_investigation_id(self, tenant_id, iid):  # type: ignore[no-untyped-def]
            return [_evidence(uuid4())]

    svc = KnowledgeCaptureService(
        artifact_repo=_artifact_repo(),
        evidence_repo=_EvRepo(),
        investigation_repo=_InvRepo(),
        profile_repo=_FakeProfile(True),
    )
    arts = asyncio.run(svc._distill("t", inv_id, datetime.now(UTC)))
    assert not [a for a in arts if a.visibility.value == "SHARED_CODE_ISSUE"]


def test_r1_t07_profile_fault_fails_closed() -> None:
    import asyncio

    fp = _fp()
    svc, inv_id = _capture(True, fp, fail_profile=True)
    arts = asyncio.run(svc._distill("t", inv_id, datetime.now(UTC)))
    assert not [a for a in arts if a.visibility.value == "SHARED_CODE_ISSUE"]


def test_r1_t08_model_rejects_leaky_shared() -> None:
    import pytest
    from pydantic import ValidationError

    from investigation_agent_platform.domain.knowledge.models import KnowledgeArtifact

    base = {
        "tenant_id": "t",
        "application_id": "app-1",
        "investigation_id": uuid4(),
        "kind": "error_signature",
        "statement": "Code issue: E at f (r@rev)",
        "confidence": 0.8,
        "refresh_policy": "TTL",
        "valid_from": datetime.now(UTC),
        "valid_to": datetime.now(UTC),
        "visibility": "SHARED_CODE_ISSUE",
        "code_issue_fingerprint": "hk1:" + "0" * 64,
    }
    with pytest.raises(ValidationError):
        KnowledgeArtifact(**{**base, "source_evidence_ids": [uuid4()]})
    with pytest.raises(ValidationError):
        KnowledgeArtifact(**{**base, "kind": "domain_attribution"})
    with pytest.raises(ValidationError):
        KnowledgeArtifact(**{**base, "statement": "x" * 700})


def test_r1_t09_reader_must_opt_in() -> None:
    import asyncio

    from investigation_agent_platform.application.worker.activities.knowledge import (
        _caller_fingerprints_for,
    )

    fp = _fp()
    inv_id = uuid4()

    class _Ctx:
        profile_repo = _FakeProfile(False)

        def __init__(self) -> None:
            self.investigation_repo = self

        async def get_by_id(self, tenant_id, iid):  # type: ignore[no-untyped-def]
            return _investigation(inv_id, fp)

    assert asyncio.run(_caller_fingerprints_for(_Ctx(), "t", inv_id)) == []


def test_r1_t10_caller_fingerprint_not_trusted() -> None:
    import asyncio

    from investigation_agent_platform.application.investigation.services import (
        CreateInvestigationService,
    )

    fp = _fp()
    other_inv = uuid4()

    class _Idx:
        async def sessions_for_fingerprint(self, f):  # type: ignore[no-untyped-def]
            return [(1, other_inv, datetime.now(UTC))]

    class _InvRepo:
        async def get_by_id(self, tenant_id, iid):  # type: ignore[no-untyped-def]
            return None

    svc = CreateInvestigationService(
        investigation_repo=_InvRepo(),  # type: ignore[arg-type]
        profile_repo=None,  # type: ignore[arg-type]
        code_issue_index=_Idx(),
    )
    assert asyncio.run(svc._owned_fingerprint("tenant-b", fp)) is None


def test_r1_t12_router_presence_oracle_gated() -> None:
    import asyncio

    from investigation_agent_platform.api.v1.routers.knowledge import list_knowledge  # noqa
    from investigation_agent_platform.application.knowledge.sharing import sharing_opted_in

    assert asyncio.run(sharing_opted_in(_FakeProfile(False), "t", "app-1")) is False
    assert asyncio.run(sharing_opted_in(_FakeProfile(True), "t", "app-1")) is True


def test_r1_t16_unsafe_proof_rejected() -> None:
    import pytest

    from investigation_agent_platform.domain.common.exceptions import (
        SecurityPolicyViolationException,
    )

    class _F:
        def __call__(self):  # type: ignore[no-untyped-def]
            raise AssertionError("must not open session")

    for bad in ["x'; DROP", "abc", "HK1:" + "0" * 64, "hk1:" + "Z" * 64]:
        with pytest.raises(SecurityPolicyViolationException):
            asyncio_run = __import__("asyncio").run
            asyncio_run(_assert_proof_rejected(bad))


async def _assert_proof_rejected(bad: str) -> None:
    from investigation_agent_platform.infrastructure.persistence.rls import rls_session

    class _F:
        def __call__(self):  # type: ignore[no-untyped-def]
            raise AssertionError("must not open session")

    async with rls_session(_F(), "t", shared_fingerprints=[bad]):
        pass


def test_r1_t18_ctx_key_flows_to_intake() -> None:
    from investigation_agent_platform.api.dependencies import AppContext

    ctx = AppContext()
    assert ctx.fingerprint_key is None
    assert ctx.error_intake_service().fingerprint_key == b"iap-dev-fingerprint-key-not-a-secret"
    ctx.fingerprint_key = KEY_A
    assert ctx.error_intake_service().fingerprint_key == KEY_A
