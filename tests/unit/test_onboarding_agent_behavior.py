"""Onboarding agent behavior tests: the three stateless invocation contracts."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investigation_agent_platform.application.onboarding_agent.agent import (
    AssemblyErrorCode,
    ReportAssemblyError,
    answer_coverage_question,
    assemble_coverage_report,
    diagnose,
    draft_dispositions,
    needs_gap_names,
)
from investigation_agent_platform.application.onboarding_agent.contracts import (
    Audience,
    BridgeUse,
    CapabilityDisposition,
    Claim,
    Confidence,
    CoverageQuestion,
    CoverageReport,
    EnhancementGap,
    EvidencePointer,
    QuestionKind,
    RegistryEntry,
    Remediation,
    ServiceVerdict,
    StageObservation,
    SupportStrategy,
    SupportTier,
    TrustState,
    WatchdogSnapshot,
    input_manifest_hash,
    report_content_hash,
)

HASH_A = "a" * 64
HASH_B = "b" * 64


def _sniffer_outputs() -> list[dict[str, Any]]:
    return [
        {
            "content_hash": HASH_A,
            "tool": "census",
            "tool_version": "1.0",
            "schema_version": "sniffer/v1",
        },
        {
            "content_hash": HASH_B,
            "tool": "buildmap",
            "tool_version": "2.1",
            "schema_version": "sniffer/v1",
        },
    ]


def _verdict() -> ServiceVerdict:
    return ServiceVerdict(
        service="billing",
        languages=["python"],
        tier=SupportTier.DEEP,
        dispositions=[
            CapabilityDisposition(
                capability="postgres",
                disposition="SUPPORTED",
                reasons=["native registry entry, limits observed"],
            )
        ],
        claims=[
            Claim(
                statement="billing persists to postgres",
                confidence=Confidence.VERIFIED,
                evidence=[EvidencePointer(output_hash=HASH_A, field_path="census.files[3]")],
            ),
            Claim(
                statement="read replicas cover reporting load",
                confidence=Confidence.PROJECTED,
                confirming_probe="run load probe against replica lag",
                evidence=[
                    EvidencePointer(
                        output_hash=HASH_B,
                        field_path="buildmap.systems[0].modules[1]",
                    )
                ],
            ),
        ],
    )


def _assemble(**overrides: Any) -> CoverageReport:
    params: dict[str, Any] = {
        "report_id": uuid4(),
        "request_id": uuid4(),
        "revision": 2,
        "generation": 1,
        "report_version": "coverage/v1",
        "registry_version": "registry-42",
        "catalog_version": "catalog-7",
        "estate_sha": "estate-deadbeef",
        "analyzer_version": "agent-0.1",
        "model_version": "model-9",
        "sniffer_outputs": _sniffer_outputs(),
        "verdicts": [_verdict()],
        "limitations": ["vendor/ directory excluded by policy"],
        "bridges": [
            BridgeUse(
                tool="orm-bridge",
                version_pin="3.2.1",
                trust_state="PROVISIONAL",
                verification_evidence=["deterministic re-run matched"],
            )
        ],
        "gaps": [
            EnhancementGap(
                title="Unmapped capability: sso-saml",
                scoped_ask="scope transfer analysis for sso-saml",
            )
        ],
        "remediation": [
            Remediation(
                action="pin orm-bridge",
                command_argv=["uv", "pip", "install", "orm-bridge==3.2.1"],
                expected_outcome="reproducible bridge version",
                verification="hash the installed wheel",
            )
        ],
        "unmapped_capabilities": ["sso-saml"],
    }
    params.update(overrides)
    return assemble_coverage_report(**params)


def test_assemble_happy_path_hashes_verify() -> None:
    report = _assemble()
    assert report.input_manifest_hash == input_manifest_hash(
        sniffer_outputs=_sniffer_outputs(),
        estate_sha="estate-deadbeef",
        registry_version="registry-42",
        catalog_version="catalog-7",
        generation=1,
    )
    assert report.report_content_hash == report_content_hash(report.model_dump(mode="json"))
    assert report.verdicts[0].claims[0].confidence == Confidence.VERIFIED


def test_assemble_deterministic_for_same_inputs() -> None:
    report_id = uuid4()
    request_id = uuid4()
    first = _assemble(report_id=report_id, request_id=request_id)
    second = _assemble(report_id=report_id, request_id=request_id)
    assert first.input_manifest_hash == second.input_manifest_hash
    assert first.report_content_hash == second.report_content_hash


def test_assemble_missing_pin_rejected() -> None:
    with pytest.raises(ReportAssemblyError) as exc:
        _assemble(estate_sha="  ")
    assert exc.value.code == AssemblyErrorCode.MISSING_PIN


def test_assemble_unresolved_evidence_rejected() -> None:
    bad = _verdict()
    bad.claims[0].evidence.append(
        EvidencePointer(output_hash="c" * 64, field_path="census.files[9]")
    )
    with pytest.raises(ReportAssemblyError) as exc:
        _assemble(verdicts=[bad])
    assert exc.value.code == AssemblyErrorCode.UNRESOLVED_EVIDENCE


def test_assemble_unmapped_capability_requires_named_gap() -> None:
    with pytest.raises(ReportAssemblyError) as exc:
        _assemble(unmapped_capabilities=["sso-saml", "legacy-ftp"])
    assert exc.value.code == AssemblyErrorCode.UNMAPPED_WITHOUT_GAP
    assert "legacy-ftp" in str(exc.value)


def test_assemble_probeless_speculative_claim_rejected() -> None:
    bad = _verdict()
    with pytest.raises(ValidationError):
        bad.claims.append(Claim(statement="maybe scales", confidence=Confidence.SPECULATIVE))


def test_draft_dispositions_mapped_unmapped_ambiguous() -> None:
    registry = [
        RegistryEntry(
            capability_key="postgres",
            version="v3",
            strategy=SupportStrategy.NATIVE,
            limits="single-region only",
            trust_state=TrustState.TRUSTED,
            owner="platform",
        ),
        RegistryEntry(
            capability_key="kafka",
            version="v1",
            strategy=SupportStrategy.GENERIC,
            owner="platform",
        ),
        RegistryEntry(
            capability_key="kafka",
            version="v2",
            strategy=SupportStrategy.BRIDGE,
            owner="platform",
        ),
    ]
    drafts = draft_dispositions(["postgres", "sso-saml", "kafka"], registry, "r1")
    by_cap = {draft.capability: draft for draft in drafts}
    assert by_cap["postgres"].status.value == "mapped"
    assert by_cap["postgres"].strategy == "NATIVE"
    assert any("single-region" in reason for reason in by_cap["postgres"].reasons)
    assert by_cap["sso-saml"].status.value == "unmapped"
    assert by_cap["kafka"].status.value == "ambiguous"
    assert needs_gap_names(drafts) == ["sso-saml", "kafka"]


def test_draft_dispositions_empty_registry_all_unmapped() -> None:
    drafts = draft_dispositions(["a", "a", "b"], [], "r1")
    assert [draft.capability for draft in drafts] == ["a", "b"]
    assert all(draft.status.value == "unmapped" for draft in drafts)


def _snapshot(**overrides: Any) -> WatchdogSnapshot:
    now = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
    params: dict[str, Any] = {
        "request_id": uuid4(),
        "revision": 1,
        "generation": 1,
        "current_stage": "PROVISIONING",
        "stages": [
            StageObservation(
                stage="QUEUED",
                entered_at=now - timedelta(hours=2),
                lease_fencing_token=4,
            ),
            StageObservation(
                stage="PROVISIONING",
                entered_at=now - timedelta(minutes=1),
                lease_fencing_token=5,
            ),
        ],
        "stuck_bounds_seconds": {"QUEUED": 600, "PROVISIONING": 600},
        "now": now,
    }
    params.update(overrides)
    return WatchdogSnapshot(**params)


def test_diagnose_stuck_stage_ranks_observation_first() -> None:
    diagnosis = diagnose(_snapshot())
    assert [cause.rank for cause in diagnosis.causes] == [1, 2, 3]
    first = diagnosis.causes[0]
    assert first.confidence == Confidence.VERIFIED
    assert "QUEUED" in first.cause and "exceeded its stuck bound" in first.cause
    hypotheses = diagnosis.causes[1:]
    assert all(h.confidence == Confidence.SPECULATIVE for h in hypotheses)
    assert all(h.distinguishing_tests for h in hypotheses)


def test_diagnose_healthy_snapshot_single_no_anomaly() -> None:
    now = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
    snapshot = _snapshot(
        stages=[
            StageObservation(
                stage="PROVISIONING",
                entered_at=now - timedelta(seconds=60),
                lease_fencing_token=5,
            )
        ],
        current_stage="PROVISIONING",
    )
    diagnosis = diagnose(snapshot)
    assert len(diagnosis.causes) == 1
    assert diagnosis.causes[0].confidence == Confidence.VERIFIED
    assert "no anomaly" in diagnosis.causes[0].cause


def test_diagnose_empty_history_is_insufficient_evidence() -> None:
    diagnosis = diagnose(_snapshot(stages=[]))
    assert len(diagnosis.causes) == 1
    assert "insufficient evidence" in diagnosis.causes[0].cause
    assert diagnosis.causes[0].distinguishing_tests


def test_diagnose_clock_skew_blocks_staleness_judgment() -> None:
    now = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
    skewed = _snapshot(
        stages=[StageObservation(stage="QUEUED", entered_at=now + timedelta(hours=1))],
        current_stage="QUEUED",
    )
    diagnosis = diagnose(skewed)
    assert len(diagnosis.causes) == 1
    assert "skew" in diagnosis.causes[0].cause


def test_diagnose_current_stage_missing_from_history() -> None:
    diagnosis = diagnose(_snapshot(current_stage="ACTIVE"))
    assert len(diagnosis.causes) == 1
    assert "inconsistent" in diagnosis.causes[0].cause


def test_diagnose_repeated_failures_projected() -> None:
    diagnosis = diagnose(_snapshot(consecutive_failures=4))
    kinds = [cause.cause for cause in diagnosis.causes]
    assert any("consecutive failures" in text for text in kinds)
    repeated = next(cause for cause in diagnosis.causes if "consecutive failures" in cause.cause)
    assert repeated.confidence == Confidence.PROJECTED
    assert repeated.distinguishing_tests


def test_diagnose_unbounded_stage_is_explicit_meta_finding() -> None:
    diagnosis = diagnose(_snapshot(stuck_bounds_seconds={"QUEUED": 3600 * 5}))
    assert any("no stuck bound" in cause.cause for cause in diagnosis.causes)


def _question(kind: QuestionKind, subject: str, **overrides: Any) -> CoverageQuestion:
    params: dict[str, Any] = {"kind": kind, "subject": subject}
    params.update(overrides)
    return CoverageQuestion(**params)


def test_why_unsupported_answered_with_citations() -> None:
    report = _assemble()
    answer = answer_coverage_question(report, _question(QuestionKind.WHY, subject="postgres"))
    assert answer.status == "answered"
    assert answer.matched_service == "billing"
    assert answer.matched_disposition is not None
    assert answer.matched_disposition.capability == "postgres"
    assert "native registry entry" in answer.summary
    assert len(answer.citations) == 2
    assert all(pointer.output_hash in (HASH_A, HASH_B) for pointer in answer.citations)


def test_why_unknown_subject_names_probe() -> None:
    report = _assemble()
    answer = answer_coverage_question(report, _question(QuestionKind.WHY, subject="mainframe"))
    assert answer.status == "unknown"
    assert answer.citations == []
    assert answer.resolving_probe is not None
    assert "mainframe" in answer.resolving_probe


def test_service_level_match_lists_tier_and_dispositions() -> None:
    report = _assemble()
    answer = answer_coverage_question(report, _question(QuestionKind.WHY, subject="BILLING"))
    assert answer.status == "answered"
    assert "deep" in answer.summary
    assert "postgres=SUPPORTED" in answer.summary
    assert answer.matched_disposition is None


def test_what_would_change_lists_outstanding_probes() -> None:
    report = _assemble()
    answer = answer_coverage_question(
        report, _question(QuestionKind.WHAT_WOULD_CHANGE, subject="postgres")
    )
    assert answer.status == "answered"
    assert "load probe against replica lag" in answer.summary
    assert answer.resolving_probe == "run load probe against replica lag"


def test_limitations_kind_echoes_report_limitations() -> None:
    report = _assemble()
    answer = answer_coverage_question(
        report, _question(QuestionKind.LIMITATIONS, subject="anything at all")
    )
    assert answer.status == "answered"
    assert "vendor/ directory excluded" in answer.summary


def test_customer_audience_omits_tooling_versions() -> None:
    report = _assemble()
    operator = answer_coverage_question(report, _question(QuestionKind.WHY, subject="postgres"))
    customer = answer_coverage_question(
        report,
        _question(QuestionKind.WHY, subject="postgres", audience=Audience.CUSTOMER),
    )
    assert "agent-0.1" in operator.summary
    assert "agent-0.1" not in customer.summary
    assert "model-9" not in customer.summary
    assert customer.matched_disposition == operator.matched_disposition
