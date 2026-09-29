# src/investigation_agent_platform/domain/evidence/completeness.py
"""Completeness contract for bounded evidence retrieval (Part 9).

An investigation agent must distinguish "no evidence found" from "evidence
exists but the bounded page was truncated". Every bounded result carries this
model so truncation is explicit, never implied.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CompletenessReason(StrEnum):
    """Machine-readable reason for an incomplete result."""

    PAGINATION_AVAILABLE = "pagination_available"
    PROVIDER_LIMIT = "provider_limit"
    APPLICATION_LIMIT = "application_limit"
    PROVIDER_FAILURE = "provider_failure"
    METADATA_INCOMPLETE = "metadata_incomplete"
    INPUT_BOUND = "input_bound"


class EvidenceDataQuality(StrEnum):
    """Machine-readable data-quality flags for individual evidence records.

    Unknown observation time is always ``observed_at=None``; these flags say
    *why*, plus whether the projected attributes were reduced. Only flags
    that change how an investigator should interpret the record are set.
    """

    TIMESTAMP_MISSING = "timestamp_missing"
    TIMESTAMP_INVALID = "timestamp_invalid"
    TIMESTAMP_NAIVE = "timestamp_naive"
    ATTRIBUTES_TRUNCATED = "attributes_truncated"


class EvidenceCompleteness(BaseModel):
    """Whether a bounded evidence result consumed its source fully."""

    model_config = ConfigDict(frozen=True)

    complete: bool
    truncated: bool = False
    reason: CompletenessReason | None = None
    returned_count: int = Field(default=0, ge=0)
    examined_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_completeness_invariants(self) -> "EvidenceCompleteness":
        if self.complete and (self.truncated or self.reason is not None):
            raise ValueError("A complete result must have truncated=False and reason=None.")
        if self.truncated and self.reason is None:
            raise ValueError("A truncated result must carry a reason.")
        return self
