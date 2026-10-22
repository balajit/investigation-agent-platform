"""Onboarding agent contract tests: schemas, validation, canonical hashing."""

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investigation_agent_platform.application.onboarding_agent.contracts import (
    CANONICALIZATION_VERSION,
    BuildMapOutput,
    BuildModule,
    BuildSystem,
    CensusFile,
    CensusOutput,
    Claim,
    Confidence,
    CoverageReport,
    DirectoryEntry,
    EvidencePointer,
    ExtractedCapability,
    HygieneFinding,
    HygieneOutput,
    LanguageTotals,
    RegistryEntry,
    Remediation,
    ReportLinkage,
    SnifferEnvelope,
    SupportStrategy,
    analysis_idempotency_key,
    canonical_json_bytes,
    input_manifest_hash,
    report_content_hash,
    validate_sniffer_payload,
)


def _envelope(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "tool": "census",
        "tool_version": "1.0",
        "input_sha": "abc123",
        "schema_version": "sniffer/v1",
        "produced_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def test_validate_sniffer_payload_accepts_and_rejects() -> None:
    assert validate_sniffer_payload(_envelope())["tool"] == "census"
    with pytest.raises(ValidationError):
        SnifferEnvelope(**_envelope(schema_version="sniffer/v9"))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="envelope field"):
        validate_sniffer_payload({"tool": "census"})


def test_census_totals_must_reconcile_and_include_unknown() -> None:
    files = [
        CensusFile(
            path="a.py",
            kind="text",
            size_bytes=10,
            language="python",
            parse_status="parsed",
            capabilities=[
                ExtractedCapability(
                    name="symbols", extractor="tree-sitter", extractor_version="1"
                )
            ],
            loc=10,
        ),
        CensusFile(
            path="b.bin", kind="binary", size_bytes=99, parse_status="skipped"
        ),
    ]
    with pytest.raises(ValidationError, match="unknown"):
        CensusOutput(
            envelope=SnifferEnvelope(**_envelope()),
            files=files,
            totals_by_language={"python": LanguageTotals(files=1, loc=10)},
        )
    with pytest.raises(ValidationError, match="mismatch"):
        CensusOutput(
            envelope=SnifferEnvelope(**_envelope()),
            files=files,
            totals_by_language={
                "python": LanguageTotals(files=1, loc=10),
                "unknown": LanguageTotals(files=0, loc=999),
            },
        )
    ok = CensusOutput(
        envelope=SnifferEnvelope(**_envelope()),
        files=files,
        skipped=["/dev/null"],
        totals_by_language={
            "python": LanguageTotals(files=1, loc=10),
            "unknown": LanguageTotals(files=1, loc=0),
        },
    )
    assert ok.totals_by_language["unknown"].files == 1


def test_parse_status_is_distinct_from_extracted_capabilities() -> None:
    file = CensusFile(
        path="broken.py",
        kind="text",
        size_bytes=50,
        language="python",
        parse_status="syntax-errors",
        parse_error="unexpected indent at line 3",
        capabilities=[
            ExtractedCapability(
                name="symbols", extractor="tree-sitter", extractor_version="1"
            )
        ],
        loc=50,
    )
    assert file.parse_status == "syntax-errors"
    assert file.capabilities[0].name == "symbols"


def test_buildmap_source_roots_must_resolve_or_be_explicitly_absent() -> None:
    systems = [
        BuildSystem(
            tool="maven",
            modules=[BuildModule(name="core", source_roots=["src/main/java"])],
        )
    ]
    with pytest.raises(ValidationError, match="source root"):
        BuildMapOutput(
            envelope=SnifferEnvelope(**_envelope(tool="buildmap")),
            systems=systems,
            directories=[DirectoryEntry(path="other", role="source-root")],
        )
    ok = BuildMapOutput(
        envelope=SnifferEnvelope(**_envelope(tool="buildmap")),
        systems=systems,
        directories=[DirectoryEntry(path="src/main/java", role="source-root")],
    )
    assert ok.systems[0].tool == "maven"


def test_hygiene_finding_rejects_unredacted_secrets() -> None:
    with pytest.raises(ValidationError, match="unredacted"):
        HygieneFinding(
            kind="token",
            location="deploy/prod.env",
            redacted_pattern="sk-ABCDEFGHIJKLMNOP0123456789abcdef",
        )
    ok = HygieneFinding(
        kind="token",
        location="deploy/prod.env",
        redacted_pattern="sk-...<24 hex chars redacted>",
    )
    assert ok.redaction_confident is True
    out = HygieneOutput(
        envelope=SnifferEnvelope(**_envelope(tool="hygscan")),
        findings=[ok],
        excluded_inputs=["deploy/prod.env"],
    )
    assert out.excluded_inputs == ["deploy/prod.env"]


def test_claim_requires_probe_unless_verified() -> None:
    evidence = [
        EvidencePointer(output_hash="a" * 64, field_path="census.files[0]")
    ]
    with pytest.raises(ValidationError, match="confirming probe"):
        Claim(statement="uses django", confidence=Confidence.PROJECTED, evidence=evidence)
    with pytest.raises(ValidationError, match="confirming probe"):
        Claim(statement="maybe scala", confidence=Confidence.SPECULATIVE)
    ok = Claim(
        statement="uses django",
        confidence=Confidence.PROJECTED,
        confirming_probe="check requirements.txt for Django pin",
        evidence=evidence,
    )
    assert ok.confirming_probe is not None


def test_remediation_argv_must_be_data_not_shell() -> None:
    # Glob characters (*, ?) are safe data under argv-exec (no shell
    # interprets them); shell composition operators are not.
    ok_glob = Remediation(action="list", command_argv=["rg", "--files", "vendor/*"])
    assert ok_glob.command_argv[-1] == "vendor/*"
    with pytest.raises(ValidationError, match="metacharacter"):
        Remediation(action="pin", command_argv=["echo", "a;b"])
    with pytest.raises(ValidationError, match="metacharacter"):
        Remediation(action="pin", command_argv=["git", "checkout", "$(cat ref)"])
    ok = Remediation(
        action="exclude vendor",
        command_argv=["rg", "--files", "--glob", "!vendor/**"],
        expected_outcome="vendor files excluded",
        verification="rerun census, vendor count is zero",
    )
    assert ok.command_argv[0] == "rg"


def test_coverage_report_requires_limitations() -> None:
    base = {
        "report_id": uuid4(),
        "request_id": uuid4(),
        "revision": 1,
        "generation": 1,
        "report_version": "r1",
        "registry_version": "regv1",
        "catalog_version": "catv1",
        "estate_sha": "abc123",
        "input_manifest_hash": "b" * 64,
        "report_content_hash": "c" * 64,
        "analyzer_version": "sniffer-v1",
    }
    with pytest.raises(ValidationError):
        CoverageReport(**base, limitations=[])
    with pytest.raises(ValidationError):
        CoverageReport(**base, limitations=["   "])
    ok = CoverageReport(**base, limitations=["tier-3 build system not examined"])
    assert ok.generation == 1


def test_canonical_json_is_deterministic_and_rejects_floats() -> None:
    left = canonical_json_bytes({"b": [1, 2], "a": "x"})
    right = canonical_json_bytes({"a": "x", "b": [1, 2]})
    assert left == right == b'{"a":"x","b":[1,2]}'
    assert "é".encode() in canonical_json_bytes({"k": "é"})
    with pytest.raises(TypeError, match="pre-normalize"):
        canonical_json_bytes({"n": 1.5})


def test_input_manifest_hash_ignores_order_and_volatile_fields() -> None:
    out_a = {
        "content_hash": "a" * 64,
        "tool": "census",
        "tool_version": "1",
        "schema_version": "sniffer/v1",
        "produced_at": "2026-01-01",
        "hostname": "noisy",
    }
    out_b = {
        "content_hash": "b" * 64,
        "tool": "buildmap",
        "tool_version": "1",
        "schema_version": "sniffer/v1",
    }
    kwargs = {
        "estate_sha": "estate",
        "registry_version": "regv1",
        "catalog_version": "catv1",
        "generation": 2,
    }
    first = input_manifest_hash(sniffer_outputs=[out_a, out_b], **kwargs)
    second = input_manifest_hash(sniffer_outputs=[out_b, out_a], **kwargs)
    assert first == second
    assert first != input_manifest_hash(
        sniffer_outputs=[out_a], **{**kwargs, "generation": 3}
    )
    with pytest.raises(ValueError, match="content_hash"):
        input_manifest_hash(sniffer_outputs=[{"tool": "x"}], **kwargs)


def test_report_content_hash_excludes_identity_and_envelope() -> None:
    body = {
        "report_id": "11111111-1111-1111-1111-111111111111",
        "verdicts": [{"service": "a"}],
        "produced_at": "whenever",
        "filing": {"by": "workflow"},
    }
    changed_id = dict(body, report_id="22222222-2222-2222-2222-222222222222")
    changed_when = dict(body, produced_at="later")
    assert report_content_hash(body) == report_content_hash(changed_id)
    assert report_content_hash(body) == report_content_hash(changed_when)
    changed_verdict = dict(body, verdicts=[{"service": "b"}])
    assert report_content_hash(body) != report_content_hash(changed_verdict)


def test_canonicalization_version_is_pinned_in_hash_preimages() -> None:
    assert CANONICALIZATION_VERSION == "1"
    out = {
        "content_hash": "a" * 64,
        "tool": "census",
        "tool_version": "1",
        "schema_version": "sniffer/v1",
    }
    expected_manifest = {
        "canonicalization_version": "1",
        "estate_sha": "estate",
        "registry_version": "regv1",
        "catalog_version": "catv1",
        "generation": 1,
        "inputs": [
            {
                "content_hash": "a" * 64,
                "tool": "census",
                "tool_version": "1",
                "schema_version": "sniffer/v1",
            }
        ],
    }
    expected = hashlib.sha256(
        canonical_json_bytes(expected_manifest)
    ).hexdigest()
    assert (
        input_manifest_hash(
            sniffer_outputs=[out],
            estate_sha="estate",
            registry_version="regv1",
            catalog_version="catv1",
            generation=1,
        )
        == expected
    )


def test_report_content_hash_rejects_unknown_fields() -> None:
    body = {"verdicts": [{"service": "a"}], "future_field": "v2-surprise"}
    with pytest.raises(ValueError, match="unknown report body fields"):
        report_content_hash(body)


def test_analysis_idempotency_key_is_stable_and_validated() -> None:
    request_id = uuid4()
    assert analysis_idempotency_key(request_id, 2, 3) == f"{request_id}:2:3"
    with pytest.raises(ValueError):
        analysis_idempotency_key(request_id, 0, 1)


def test_registry_strategy_vocabulary_is_closed() -> None:
    with pytest.raises(ValidationError):
        RegistryEntry(
            capability_key="code",
            version="v1",
            strategy="MAGIC",  # type: ignore[arg-type]
            owner="platform",
        )
    entry = RegistryEntry(
        capability_key="code-python",
        version="v1",
        strategy=SupportStrategy.NATIVE,
        owner="platform",
    )
    assert entry.trust_state == "UNVERIFIED"


def test_report_linkage_round_trip() -> None:
    linkage = ReportLinkage(
        request_id=uuid4(),
        revision=1,
        generation=2,
        report_id=uuid4(),
        report_content_hash="d" * 64,
    )
    assert linkage.generation == 2
