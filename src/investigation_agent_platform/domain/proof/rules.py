# src/investigation_agent_platform/domain/proof/rules.py
"""WP-B3 typed rules + semantic validators (canon/v1): closed, frozen, pure.

The closed rule registry: each :class:`ProofRule` names its rule ID and
version, typed premise contract, preconditions, explicit assumptions,
soundness label, unsupported cases, and an implementation digest binding
the declared contract to the named validator executable. Initial kernel:
``definition/v1`` (one direct fact) + ``call_edge/v1`` (multi-premise
normalized call-site fact) only — every other claim kind abstains through
:func:`rule_for_kind` returning ``None`` (unsupported abstention).

Soundness (contracts §3.2): SOUND rules may discharge when premises and
assumptions validate; CONSERVATIVE rules discharge only under their
documented inference (abstention permitted); HEURISTIC rules never
produce a publishable discharge — they prioritize and annotate only,
and :func:`apply_rule` refuses to discharge them by construction.

Semantic validation (K1): the validator checks the inference instance
against the declared premise contract — types, bindings, and content —
never a producer-supplied label. A comment row relabeled as a call site
is rejected even with valid hashes.
"""

from __future__ import annotations

import hashlib
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.domain.hypothesis.models import ClaimKind
from investigation_agent_platform.domain.proof.canonical import canonical_dict_bytes


class Soundness(str, Enum):
    """Proof-rule soundness labels (frozen)."""

    SOUND = "SOUND"
    CONSERVATIVE = "CONSERVATIVE"
    HEURISTIC = "HEURISTIC"


class RuleOutcome(str, Enum):
    """Rule-application outcomes (frozen subset of the §3.4 vocabulary)."""

    DISCHARGED = "DISCHARGED"
    MISSING_PREMISE = "MISSING_PREMISE"
    INADMISSIBLE_PREMISE = "INADMISSIBLE_PREMISE"
    INVALID_INFERENCE = "INVALID_INFERENCE"
    UNSUPPORTED_CONSTRUCT = "UNSUPPORTED_CONSTRUCT"


class ProofRule(BaseModel):
    """One normative, versioned, closed-registry inference rule."""

    model_config = ConfigDict(frozen=True)

    rule_id: str = Field(..., max_length=128)
    rule_version: str = Field(..., max_length=64)
    input_types: list[str] = Field(default_factory=list, max_length=20)
    preconditions: list[str] = Field(default_factory=list, max_length=20)
    assumptions: list[str] = Field(default_factory=list, max_length=20)
    soundness: Soundness = Soundness.SOUND
    unsupported_cases: list[str] = Field(default_factory=list, max_length=20)
    implementation_digest: str = Field(default="", max_length=128)
    validator: str = Field(default="", max_length=256)


def _contract_digest(
    rule_id: str,
    rule_version: str,
    input_types: list[str],
    preconditions: list[str],
    assumptions: list[str],
    soundness: Soundness,
    validator: str,
) -> str:
    """Stable contract digest binding the declared rule to its executable.

    Hashes the canonical contract bytes including the validator's
    qualified name, so swapping the executable without a version bump
    changes the digest (K9). Executable-byte digests (build artifacts)
    belong to release control (B4 packaging), not to this frozen record.
    """
    return "sha256:" + hashlib.sha256(
        canonical_dict_bytes(
            {
                "assumptions": sorted(assumptions),
                "canon_version": "canon/v1",
                "input_types": sorted(input_types),
                "preconditions": sorted(preconditions),
                "rule_id": rule_id,
                "rule_version": rule_version,
                "soundness": str(getattr(soundness, "value", soundness)),
                "validator": validator,
            }
        )
    ).hexdigest()


def _rule(
    rule_id: str,
    rule_version: str,
    input_types: list[str],
    preconditions: list[str],
    assumptions: list[str],
    soundness: Soundness,
    unsupported_cases: list[str],
    validator: str,
) -> ProofRule:
    return ProofRule(
        rule_id=rule_id,
        rule_version=rule_version,
        input_types=input_types,
        preconditions=preconditions,
        assumptions=assumptions,
        soundness=soundness,
        unsupported_cases=unsupported_cases,
        implementation_digest=_contract_digest(
            rule_id, rule_version, input_types, preconditions, assumptions, soundness, validator
        ),
        validator=validator,
    )


RULE_REGISTRY: dict[str, ProofRule] = {
    "definition/v1": _rule(
        rule_id="definition/v1",
        rule_version="v1",
        input_types=["RANGE_READ_FACT"],
        preconditions=[
            "exactly one RANGE_READ premise",
            "premise window contains the asserted symbol",
            "premise cites a file path and revision",
        ],
        assumptions=["static-model-only"],
        soundness=Soundness.SOUND,
        unsupported_cases=[
            "SEARCH_HIT premise (presence is not definition)",
            "premise without a file path",
            "window not containing the asserted symbol",
        ],
        validator="domain.proof.rules:validate_definition_premises",
    ),
    "call_edge/v1": _rule(
        rule_id="call_edge/v1",
        rule_version="v1",
        input_types=["NORMALIZED_CALLSITE_FACT"],
        preconditions=[
            "at least one NORMALIZED_CALLSITE premise",
            "caller and callee bound to the asserted edge",
            "call expression shown in cited text, not a search hit or comment",
        ],
        assumptions=["static-model-only"],
        soundness=Soundness.SOUND,
        unsupported_cases=[
            "SEARCH_HIT or COMMENT premise relabeled as a call site (K1)",
            "caller/callee names absent from cited text",
            "unbound caller or callee",
        ],
        validator="domain.proof.rules:validate_call_edge_premises",
    ),
}

RULE_BUNDLE_VERSION = "rules/v1"


class UnknownRule(KeyError):
    """Registry lookup failure: closed registry, fail closed."""


def get_rule(rule_id: str) -> ProofRule:
    """Fetch a registered rule by ID; unknown IDs raise (never None)."""
    try:
        return RULE_REGISTRY[str(rule_id or "")]
    except KeyError:
        raise UnknownRule(f"unregistered rule: {rule_id!r}") from None


def rule_for_kind(kind: ClaimKind | str | None) -> ProofRule | None:
    """Route a claim kind to its rule; unregistered kinds abstain (None).

    Only DEFINITION and CALL_EDGE have kernel rules. Every other kind —
    BEHAVIOR, ABSENCE, HISTORY, CAUSAL — returns None: the caller must
    abstain, never verify through the registry.
    """
    value = str(getattr(kind, "value", kind) or "")
    if value == ClaimKind.DEFINITION.value:
        return RULE_REGISTRY["definition/v1"]
    if value == ClaimKind.CALL_EDGE.value:
        return RULE_REGISTRY["call_edge/v1"]
    return None


def _premise_type(premise: Any) -> str:
    if isinstance(premise, dict):
        return str(premise.get("premise_type") or "")
    return str(getattr(premise, "premise_type", "") or "")


def validate_definition_premises(
    claim: dict[str, Any], premises: list[dict[str, Any]]
) -> tuple[RuleOutcome, str]:
    """Semantic validation for ``definition/v1`` (one direct fact).

    Discharges only when exactly one RANGE_READ premise cites a file and
    its window text contains the asserted symbol. SEARCH_HIT premises,
    missing symbols, and symbol-free windows are inadmissible or invalid
    — presence is never definition.
    """
    symbol = str((claim or {}).get("symbol") or "").strip()
    if not symbol:
        return RuleOutcome.MISSING_PREMISE, "definition claim names no symbol"
    rows = list(premises or [])
    if len(rows) != 1:
        return RuleOutcome.MISSING_PREMISE, f"definition needs exactly one premise, got {len(rows)}"
    premise = rows[0] if isinstance(rows[0], dict) else {}
    if _premise_type(premise) != "RANGE_READ_FACT":
        return (
            RuleOutcome.INADMISSIBLE_PREMISE,
            f"definition needs RANGE_READ, got {_premise_type(premise) or 'untyped'}",
        )
    if not str(premise.get("file_path") or "").strip():
        return RuleOutcome.INADMISSIBLE_PREMISE, "definition premise cites no file"
    window = premise.get("window_text")
    if not isinstance(window, str) or not window.strip():
        return RuleOutcome.MISSING_PREMISE, "definition premise carries no window text"
    if symbol not in window:
        return RuleOutcome.INVALID_INFERENCE, "window does not contain the asserted symbol"
    return RuleOutcome.DISCHARGED, "range read shows the defined symbol"


def validate_call_edge_premises(
    claim: dict[str, Any], premises: list[dict[str, Any]]
) -> tuple[RuleOutcome, str]:
    """Semantic validation for ``call_edge/v1`` (normalized call site).

    Discharges only when at least one NORMALIZED_CALLSITE premise binds
    the asserted caller and callee and the call expression appears in the
    cited text. A SEARCH_HIT or COMMENT row relabeled as a call site is
    rejected even with valid hashes (K1).
    """
    claim = claim or {}
    caller = str(claim.get("caller") or "").strip()
    callee = str(claim.get("callee") or "").strip()
    if not caller or not callee:
        return RuleOutcome.MISSING_PREMISE, "call-edge claim binds no caller/callee"
    rows = [p for p in (premises or []) if isinstance(p, dict)]
    if not rows:
        return RuleOutcome.MISSING_PREMISE, "call edge needs at least one premise"
    callsites = [p for p in rows if _premise_type(p) == "NORMALIZED_CALLSITE_FACT"]
    if not callsites:
        return (
            RuleOutcome.INADMISSIBLE_PREMISE,
            f"call edge needs NORMALIZED_CALLSITE, got {_premise_type(rows[0]) or 'untyped'}",
        )
    for premise in callsites:
        shown_caller = str(premise.get("caller") or "").strip()
        shown_callee = str(premise.get("callee") or "").strip()
        text = premise.get("call_text")
        if shown_caller != caller or shown_callee != callee:
            continue
        if not isinstance(text, str) or not text.strip():
            continue
        if caller in text and callee in text:
            return RuleOutcome.DISCHARGED, "normalized call site shows the asserted edge"
    return (
        RuleOutcome.INVALID_INFERENCE,
        "no premise binds the asserted caller/callee in cited text",
    )


_VALIDATORS = {
    "definition/v1": validate_definition_premises,
    "call_edge/v1": validate_call_edge_premises,
}


def apply_rule(
    rule: ProofRule, claim: dict[str, Any], premises: list[dict[str, Any]]
) -> tuple[RuleOutcome, str]:
    """Apply a registered rule to a claim over premises (pure).

    HEURISTIC rules never discharge by construction. Unknown validator
    bindings fail closed as INADMISSIBLE_PREMISE (a swapped executable
    without a registry update cannot prove).
    """
    if rule.soundness == Soundness.HEURISTIC:
        return RuleOutcome.UNSUPPORTED_CONSTRUCT, "heuristic rules never discharge"
    validator = _VALIDATORS.get(rule.rule_id)
    if validator is None or validator.__name__ not in rule.validator:
        return RuleOutcome.INADMISSIBLE_PREMISE, "no bound validator for rule"
    try:
        return validator(claim or {}, [p for p in premises or []])
    except Exception as exc:
        return RuleOutcome.INVALID_INFERENCE, f"validator error: {type(exc).__name__}"
