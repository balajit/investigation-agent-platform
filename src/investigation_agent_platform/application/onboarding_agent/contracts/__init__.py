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
from investigation_agent_platform.application.onboarding_agent.contracts.linkage import (
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
    HygieneFinding,
    HygieneOutput,
    LanguageTotals,
    SizerOutput,
    SnifferEnvelope,
    validate_sniffer_payload,
)

__all__ = [
    "BridgeUse",
    "BuildDependency",
    "BuildMapOutput",
    "BuildModule",
    "BuildSystem",
    "CapabilityDisposition",
    "CensusFile",
    "CensusOutput",
    "CiMapOutput",
    "Claim",
    "Confidence",
    "CoverageReport",
    "DepScanOutput",
    "DirectoryEntry",
    "EnhancementGap",
    "EvidencePointer",
    "ExtractedCapability",
    "HygieneFinding",
    "HygieneOutput",
    "LanguageTotals",
    "RegistryEntry",
    "Remediation",
    "ReportLinkage",
    "ServiceVerdict",
    "SizerOutput",
    "SnifferEnvelope",
    "SupportStrategy",
    "SupportTier",
    "TrustState",
    "analysis_idempotency_key",
    "canonical_json_bytes",
    "input_manifest_hash",
    "report_content_hash",
    "validate_sniffer_payload",
]
