# src/investigation_agent_platform/application/onboarding_agent/agent/analysis.py
"""Coverage analysis invocation contract (agent design §2, contract 1).

Pinned snapshot + validated sniffer outputs + registry entries in, coverage
report out. Pure functions: no I/O, no wall-clock reads, no network. The
caller (workflow analysis activity) supplies ``report_id`` so identical
inputs always assemble identically — identity is assigned, never generated
here.

Two responsibilities, deliberately separated:

- :func:`draft_dispositions` performs the mechanical join of extracted
  capabilities to registry entries. It defines no disposition vocabulary
  (platform-owned, D2.1/F-11): a MAPPED draft cites the registry strategy,
  limits, trust state, and evidence requirements as reasons, and judgment
  (human or model) turns drafts into verdicts.
- :func:`assemble_coverage_report` validates the judgment content against
  the design's binding rules — pins present, every evidence pointer
  resolves, every unmapped capability named in a gap — then computes both
  content hashes and returns a validated report.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.application.onboarding_agent.contracts.coverage import (
    BridgeUse,
    CoverageReport,
    EnhancementGap,
    Remediation,
    ServiceVerdict,
)
from investigation_agent_platform.application.onboarding_agent.contracts.linkage import (
    input_manifest_hash,
    report_content_hash,
)
from investigation_agent_platform.application.onboarding_agent.contracts.registry import (
    RegistryEntry,
)


class AssemblyErrorCode(StrEnum):
    """Named failure codes for report assembly (design §5.3)."""

    MISSING_PIN = "missing-pin"
    INVALID_SNIFFER_INPUT = "invalid-sniffer-input"
    UNRESOLVED_EVIDENCE = "unresolved-evidence"
    UNMAPPED_WITHOUT_GAP = "unmapped-without-gap"
    HASH_MISMATCH = "hash-mismatch"


class ReportAssemblyError(ValueError):
    """Report validation failure with a named code. The caller may retry
    generation with corrected inputs; it must never relax validation."""

    def __init__(self, code: AssemblyErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code


class DraftStatus(StrEnum):
    MAPPED = "mapped"
    UNMAPPED = "unmapped"
    AMBIGUOUS = "ambiguous"


class DispositionDraft(BaseModel):
    """Mechanical registry join for one extracted capability.

    Not a verdict: ``strategy``/``reasons`` record what the registry says so
    judgment has an explicit, cited basis. No disposition vocabulary is
    defined here — that stays platform-owned.
    """

    model_config = ConfigDict(extra="forbid")

    capability: str = Field(min_length=1, max_length=128)
    status: DraftStatus
    strategy: str | None = Field(default=None, max_length=32)
    registry_version: str | None = Field(default=None, max_length=32)
    reasons: list[str] = Field(default_factory=list)


def draft_dispositions(
    capabilities: list[str],
    registry: list[RegistryEntry],
    registry_version: str,
) -> list[DispositionDraft]:
    """Join extracted capability names to registry entries.

    One draft per distinct capability, input order preserved. Zero matches
    yield UNMAPPED (an explicit finding, never a silent gap); multiple
    matches yield AMBIGUOUS and name the competing versions so judgment can
    disambiguate. Empty input yields no drafts.
    """
    by_key: dict[str, list[RegistryEntry]] = {}
    for entry in registry:
        by_key.setdefault(entry.capability_key, []).append(entry)

    drafts: list[DispositionDraft] = []
    seen: set[str] = set()
    for capability in capabilities:
        if capability in seen:
            continue
        seen.add(capability)
        matches = by_key.get(capability, [])
        if not matches:
            drafts.append(
                DispositionDraft(
                    capability=capability,
                    status=DraftStatus.UNMAPPED,
                    reasons=[
                        ("no support-registry entry for this capability; "
                        "requires an explicit registry-gap finding")
                    ],
                )
            )
        elif len(matches) == 1:
            entry = matches[0]
            reasons = [f"registry strategy {entry.strategy.value}"]
            if entry.limits:
                reasons.append(f"registry limits: {entry.limits}")
            reasons.append(f"trust state {entry.trust_state.value}")
            if entry.evidence_requirements:
                reasons.append(
                    f"evidence requirements: {entry.evidence_requirements}"
                )
            drafts.append(
                DispositionDraft(
                    capability=capability,
                    status=DraftStatus.MAPPED,
                    strategy=entry.strategy.value,
                    registry_version=entry.version,
                    reasons=reasons,
                )
            )
        else:
            versions = sorted({match.version for match in matches})
            drafts.append(
                DispositionDraft(
                    capability=capability,
                    status=DraftStatus.AMBIGUOUS,
                    reasons=[
                        "multiple registry entries match: versions "
                        + ", ".join(versions)
                        + "; judgment must disambiguate"
                    ],
                )
            )
    if registry_version.strip():
        for draft in drafts:
            if draft.status == DraftStatus.MAPPED and draft.registry_version is None:
                draft.registry_version = registry_version
    return drafts


def needs_gap_names(drafts: list[DispositionDraft]) -> list[str]:
    """Capability names that must be named in an explicit gap finding.

    UNMAPPED and AMBIGUOUS drafts both need judgment coverage; returning
    them here (instead of dropping them) is what makes gaps explicit.
    """
    return [
        draft.capability
        for draft in drafts
        if draft.status in (DraftStatus.UNMAPPED, DraftStatus.AMBIGUOUS)
    ]


def _require_pin(name: str, value: str) -> None:
    if not value.strip():
        raise ReportAssemblyError(
            AssemblyErrorCode.MISSING_PIN, f"pin {name!r} is empty"
        )


def _sniffer_hashes(
    sniffer_outputs: Sequence[Mapping[str, Any]],
) -> set[str]:
    hashes: set[str] = set()
    for index, output in enumerate(sniffer_outputs):
        content_hash = output.get("content_hash")
        if not isinstance(content_hash, str) or not content_hash:
            raise ReportAssemblyError(
                AssemblyErrorCode.INVALID_SNIFFER_INPUT,
                f"sniffer output #{index} lacks a content_hash string",
            )
        hashes.add(content_hash)
    return hashes


def _check_evidence_resolves(
    verdicts: list[ServiceVerdict], known_hashes: set[str]
) -> None:
    unresolved: list[str] = []
    for verdict in verdicts:
        for claim in verdict.claims:
            for pointer in claim.evidence:
                if pointer.output_hash not in known_hashes:
                    unresolved.append(
                        f"{verdict.service}:{pointer.output_hash}:"
                        f"{pointer.field_path}"
                    )
    if unresolved:
        raise ReportAssemblyError(
            AssemblyErrorCode.UNRESOLVED_EVIDENCE,
            "evidence pointers resolve to no supplied sniffer output: "
            + "; ".join(sorted(unresolved)),
        )


def _check_unmapped_named_in_gaps(
    unmapped_capabilities: list[str], gaps: list[EnhancementGap]
) -> None:
    if not unmapped_capabilities:
        return
    haystacks = [
        f"{gap.title}\n{gap.scoped_ask}".casefold() for gap in gaps
    ]
    unnamed = [
        name
        for name in unmapped_capabilities
        if not any(name.casefold() in haystack for haystack in haystacks)
    ]
    if unnamed:
        raise ReportAssemblyError(
            AssemblyErrorCode.UNMAPPED_WITHOUT_GAP,
            "capabilities without an explicit gap finding: "
            + ", ".join(sorted(unnamed)),
        )


def assemble_coverage_report(
    *,
    report_id: UUID,
    request_id: UUID,
    revision: int,
    generation: int,
    report_version: str,
    registry_version: str,
    catalog_version: str,
    estate_sha: str,
    analyzer_version: str,
    model_version: str | None,
    sniffer_outputs: Sequence[Mapping[str, Any]],
    verdicts: list[ServiceVerdict],
    limitations: list[str],
    bridges: list[BridgeUse] | None = None,
    gaps: list[EnhancementGap] | None = None,
    remediation: list[Remediation] | None = None,
    unmapped_capabilities: list[str] | None = None,
) -> CoverageReport:
    """Assemble and validate a coverage report, computing both content hashes.

    ``report_id`` is caller-assigned (the row identity the workflow will
    file); nothing here generates identity, reads clocks, or performs I/O.
    Raises :class:`ReportAssemblyError` with a named code on any validation
    failure; pydantic enforces the remaining shape rules (limitations
    present, probes on non-VERIFIED claims, structured-argv remediation).
    """
    if revision < 1 or generation < 1:
        raise ReportAssemblyError(
            AssemblyErrorCode.MISSING_PIN,
            f"revision and generation must be >= 1, got {revision}/{generation}",
        )
    for name, value in (
        ("report_version", report_version),
        ("registry_version", registry_version),
        ("catalog_version", catalog_version),
        ("estate_sha", estate_sha),
        ("analyzer_version", analyzer_version),
    ):
        _require_pin(name, value)

    known_hashes = _sniffer_hashes(sniffer_outputs)
    _check_evidence_resolves(verdicts, known_hashes)
    resolved_gaps = gaps or []
    _check_unmapped_named_in_gaps(unmapped_capabilities or [], resolved_gaps)

    manifest_hash = input_manifest_hash(
        sniffer_outputs=sniffer_outputs,
        estate_sha=estate_sha,
        registry_version=registry_version,
        catalog_version=catalog_version,
        generation=generation,
    )
    body: dict[str, Any] = {
        "request_id": str(request_id),
        "revision": revision,
        "generation": generation,
        "report_version": report_version,
        "registry_version": registry_version,
        "catalog_version": catalog_version,
        "estate_sha": estate_sha,
        "input_manifest_hash": manifest_hash,
        "analyzer_version": analyzer_version,
        "model_version": model_version,
        "verdicts": [verdict.model_dump(mode="json") for verdict in verdicts],
        "bridges": [bridge.model_dump(mode="json") for bridge in (bridges or [])],
        "gaps": [gap.model_dump(mode="json") for gap in resolved_gaps],
        "remediation": [item.model_dump(mode="json") for item in (remediation or [])],
        "limitations": limitations,
    }
    content_hash = report_content_hash(body)
    report = CoverageReport(
        report_id=report_id,
        report_content_hash=content_hash,
        **body,
    )
    if report_content_hash(report.model_dump(mode="json")) != content_hash:
        raise ReportAssemblyError(
            AssemblyErrorCode.HASH_MISMATCH,
            "stored report_content_hash does not match report body",
        )
    return report
