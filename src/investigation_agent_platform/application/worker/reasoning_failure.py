# src/investigation_agent_platform/application/worker/reasoning_failure.py
"""Typed reasoning-failure vocabulary shared by the workflow and the activities (H7/T7).

Pure and stdlib-only so the workflow sandbox can import it. Classification of
a raw exception into a kind lives next to ``classify_provider_failure``
(``activities/common.classify_reasoning_failure``): there is exactly one
classifier, and this module only names the outcomes and the terminal reasons.

The line the review drew:

- ``transient``   timeout / 5xx / rate-limit: the activity raises a retryable
  failure so Temporal retries. If retries exhaust, the workflow degrades the
  run (it does not fail it) when evidence is banked.
- ``blocked``     auth / not-configured: a ``provider_blocked:*`` handoff.
- ``model_output`` malformed model output after the bounded resample: an
  INSUFFICIENT_EVIDENCE conclusion with a model-quality reason.
- ``other``       anything unclassified: INSUFFICIENT_EVIDENCE, named as such.

``FAILED`` is kept only for a transient outage with nothing banked to judge.
"""

from __future__ import annotations

KIND_TRANSIENT = "transient"
KIND_BLOCKED = "blocked"
KIND_MODEL_OUTPUT = "model_output"
KIND_OTHER = "other"

#: Terminal-reason prefixes. ``provider_blocked`` is shared with the execution
#: side so the existing handoff routing applies unchanged.
REASON_PROVIDER_BLOCKED = "provider_blocked"
REASON_MODEL_OUTPUT_INVALID = "reasoning_model_output_invalid"
REASON_PROVIDER_UNAVAILABLE = "reasoning_provider_unavailable"
REASON_UNAVAILABLE = "reasoning_unavailable"
REASON_DEFER_INVALID = "defer_gathering_invalid"

#: Name of the retryable activity failure the workflow recognises when retries
#: exhaust (the SDK wraps it in ActivityError).
TRANSIENT_FAILURE_TYPE = "ReasoningProviderTransient"

_PREFIX_FOR_KIND = {
    KIND_BLOCKED: REASON_PROVIDER_BLOCKED,
    KIND_MODEL_OUTPUT: REASON_MODEL_OUTPUT_INVALID,
    KIND_TRANSIENT: REASON_PROVIDER_UNAVAILABLE,
    KIND_OTHER: REASON_UNAVAILABLE,
}

_MAX_DETAIL = 200


def last_error_for(kind: str, detail: str) -> str:
    """The workflow's terminal reason for a typed reasoning failure. Pure."""
    prefix = _PREFIX_FOR_KIND.get(kind, REASON_UNAVAILABLE)
    text = str(detail or "").strip()[:_MAX_DETAIL]
    return f"{prefix}:{text}" if text else prefix
