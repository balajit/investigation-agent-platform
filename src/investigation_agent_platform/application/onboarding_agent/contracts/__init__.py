"""Dependency-free contract shapes for the onboarding agent.

Import-safe for onboarding intake schemas and workflow activities: this
package depends only on pydantic and the standard library — never on the
database, Temporal, or the LLM layer.
"""

from investigation_agent_platform.application.onboarding_agent.contracts.coverage import (
    BridgeUse,
    CapabilityDisposition,
    Claim,
    Confidence,
    CoverageReport,
    EnhancementGap,
    EvidencePointer,
    Remediation,
    ServiceVerdict,
    SupportTier,
)
from investigation_agent_platform.application.onboarding_agent.contracts.guidance import (
    Audience,
    CitedAnswer,
    CoverageQuestion,
    QuestionKind,
)
from investigation_agent_platform.application.onboarding_agent.contracts.linkage import (
    CANONICALIZATION_VERSION,
    ReportLinkage,
    analysis_idempotency_key,
    canonical_json_bytes,
    input_manifest_hash,
    report_content_hash,
)
from investigation_agent_platform.application.onboarding_agent.contracts.registry import (
    RegistryEntry,
    SupportStrategy,
    TrustState,
)
from investigation_agent_platform.application.onboarding_agent.contracts.sniffer import (
    BuildDependency,
    BuildMapOutput,
    BuildModule,
    BuildSystem,
    CensusFile,
    CensusOutput,
    CiMapOutput,
    DepScanOutput,
    DirectoryEntry,
    ExtractedCapability,
    FileKind,
    HygieneFinding,
    HygieneOutput,
    LanguageTotals,
    ParseStatus,
    SizerOutput,
    SnifferEnvelope,
    validate_sniffer_payload,
)
from investigation_agent_platform.application.onboarding_agent.contracts.watchdog import (
    Diagnosis,
    RankedCause,
    StageObservation,
    WatchdogSnapshot,
)

__all__ = [
    "CANONICALIZATION_VERSION",
    "Audience",
    "BridgeUse",
    "BuildDependency",
    "BuildMapOutput",
    "BuildModule",
    "BuildSystem",
    "CapabilityDisposition",
    "CensusFile",
    "CensusOutput",
    "CiMapOutput",
    "CitedAnswer",
    "Claim",
    "Confidence",
    "CoverageQuestion",
    "CoverageReport",
    "DepScanOutput",
    "Diagnosis",
    "DirectoryEntry",
    "EnhancementGap",
    "EvidencePointer",
    "ExtractedCapability",
    "FileKind",
    "HygieneFinding",
    "HygieneOutput",
    "LanguageTotals",
    "ParseStatus",
    "QuestionKind",
    "RankedCause",
    "RegistryEntry",
    "Remediation",
    "ReportLinkage",
    "ServiceVerdict",
    "SizerOutput",
    "SnifferEnvelope",
    "StageObservation",
    "SupportStrategy",
    "SupportTier",
    "TrustState",
    "WatchdogSnapshot",
    "analysis_idempotency_key",
    "canonical_json_bytes",
    "input_manifest_hash",
    "report_content_hash",
    "validate_sniffer_payload",
]
