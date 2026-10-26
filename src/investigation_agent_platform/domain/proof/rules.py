# src/investigation_agent_platform/domain/proof/rules.py
"""WP-B3 typed rules + semantic validators (canon/v1): closed, frozen, pure.

The closed rule registry: each :class:`ProofRule` names its rule ID and
version, typed premise contract, preconditions, explicit assumptions,
soundness label, unsupported cases, and an implementation digest binding
the declared contract to the named validator executable. The closed
registry holds ``definition/v1``, ``call_edge/v1``, ``behavior/v1``,
``absence/v1``, and ``history/v1``; every other claim kind abstains
through :func:`rule_for_kind` returning ``None`` (unsupported
abstention).

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
import re
from collections.abc import Callable
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from investigation_agent_platform.domain.hypothesis.models import ClaimKind
from investigation_agent_platform.domain.proof.canonical import canonical_dict_bytes
from investigation_agent_platform.domain.proof.premises import (
    AbsenceCoverageFact,
    BuildSourceMapping,
    CommitHistory,
    RangeReadFact,
    RuntimeObservationFact,
)


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
    "behavior/v1": _rule(
        rule_id="behavior/v1",
        rule_version="v1",
        input_types=["RANGE_READ_FACT", "RUNTIME_OBSERVATION_FACT", "BUILD_SOURCE_MAPPING"],
        preconditions=[
            "exactly one code, runtime, and mapping premise",
            "code target/file_path/repository/revision equal the proposition",
            "runtime predicate/value/symbol/repository/build/execution/environment/source revision equal the proposition",
            "mapping binds the observed build to the cited source",
            "mapping provenance authority independently approved",
        ],
        assumptions=["static-model-only", "test-only-mapping-authority"],
        soundness=Soundness.SOUND,
        unsupported_cases=[
            "code premise alone (structure is not behavior)",
            "runtime text without provenance",
            "unapproved mapping authority",
            "mismatched builds, executions, or environments",
        ],
        validator="domain.proof.rules:validate_behavior_premises",
    ),
    "absence/v1": _rule(
        rule_id="absence/v1",
        rule_version="v1",
        input_types=["ABSENCE_COVERAGE_FACT"],
        preconditions=[
            "exactly one ABSENCE_COVERAGE_FACT premise, no match-result premises",
            "coverage id, hash, and type bound in the manifest",
            "search completed empty with zero matches in a bounded universe",
            "predicate, universe, scope, exclusions, and search config equal the proposition and manifest",
        ],
        assumptions=["static-model-only"],
        soundness=Soundness.SOUND,
        unsupported_cases=[
            "incomplete or failed search",
            "unbounded universe",
            "relevant scope excluded from the universe",
            "empty premise list without a coverage record",
        ],
        validator="domain.proof.rules:validate_absence_premises",
    ),
    "history/v1": _rule(
        rule_id="history/v1",
        rule_version="v1",
        input_types=["COMMIT_HISTORY"],
        preconditions=[
            "exactly one COMMIT_HISTORY premise",
            "full lowercase 40-hex commit id in premise and proposition",
            "repository, queried revision, commit, file, and subject exactly equal",
            "relation and completeness omitted, not null",
        ],
        assumptions=["static-model-only"],
        soundness=Soundness.SOUND,
        unsupported_cases=[
            "abbreviated or uppercase commit ids",
            "branch names or time ranges instead of a commit id",
            "causal interpretations of a commit subject",
            "unexpected relation or completeness values",
        ],
        validator="domain.proof.rules:validate_history_premises",
    ),
}

RULE_BUNDLE_VERSION = "rules/v2"

#: Independently approved build-to-source mapping authorities (frozen set;
#: append-only by reviewed contract change). Test-only synthetic authority
#: approved 2026-10-24; real-world mapping authority remains OPEN.
APPROVED_BUILD_MAPPING_AUTHORITIES = frozenset({"synthetic-test-only-authority-001"})

_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


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

    All five kernel rules have registry rows. CAUSAL and any other
    unregistered kind returns None: the caller must abstain, never
    verify through the registry.
    """
    value = str(getattr(kind, "value", kind) or "")
    if value == ClaimKind.DEFINITION.value:
        return RULE_REGISTRY["definition/v1"]
    if value == ClaimKind.CALL_EDGE.value:
        return RULE_REGISTRY["call_edge/v1"]
    if value == ClaimKind.BEHAVIOR.value:
        return RULE_REGISTRY["behavior/v1"]
    if value == ClaimKind.ABSENCE.value:
        return RULE_REGISTRY["absence/v1"]
    if value == ClaimKind.HISTORY.value:
        return RULE_REGISTRY["history/v1"]
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


def _reject_unknown_v2_keys(
    claim: dict[str, Any], rule_id: str
) -> tuple[RuleOutcome, str] | None:
    """Reject claim fields outside the rule's closed contract.

    Validator-level claims mix header fields with the rule's target and
    scope keys. Anything else is inadmissible: a meaningful new field
    must arrive through a contract change, never silently. v1 rules
    keep their legacy tolerance; the closed vocabulary is enforced for
    the new rules only, where fixtures are controlled.
    """
    from investigation_agent_platform.domain.proof.canonical import V2_SCOPE_KEYS, V2_TARGET_KEYS

    if rule_id not in ("behavior/v1", "absence/v1", "history/v1"):
        return None
    allowed = (
        {"form", "quantifier", "relation", "modality", "completeness", "predicate", "asserted_value"}
        | set(V2_TARGET_KEYS.get(rule_id, frozenset()))
        | set(V2_SCOPE_KEYS.get(rule_id, frozenset()))
    )
    unknown = sorted(key for key in (claim or {}) if key not in allowed)
    if unknown:
        return RuleOutcome.INADMISSIBLE_PREMISE, f"fields outside the {rule_id} contract: {unknown}"
    return None


def _duplicate_evidence_id(rows: list[dict[str, Any]]) -> str | None:
    """First repeated evidence_id, or None (K14 at the rule boundary)."""
    seen: set[str] = set()
    for row in rows:
        pid = str(row.get("evidence_id") or "")
        if pid and pid in seen:
            return pid
        seen.add(pid)
    return None


def _scalar_equal(first: Any, second: Any) -> bool:
    """Type-strict canonical scalar equality (bool is not int here)."""
    if isinstance(first, bool) or isinstance(second, bool):
        return (
            isinstance(first, bool) and isinstance(second, bool) and first == second
        )
    return type(first) is type(second) and first == second


def validate_behavior_premises(
    claim: dict[str, Any], premises: list[dict[str, Any]]
) -> tuple[RuleOutcome, str]:
    """Semantic validation for ``behavior/v1`` (code + runtime + mapping).

    Claim shape: ``form/quantifier/relation/modality`` header plus
    ``symbol``, ``file_path``, ``repository``, ``revision``,
    ``build_identity``, ``execution_id``, ``environment``, ``predicate``,
    and ``asserted_value`` (canonical scalar, never float). Discharges
    only when the three premises join on the same target, source, build,
    execution, and environment with matching predicate and value, under
    an approved mapping authority. An unapproved authority abstains
    (UNSUPPORTED_CONSTRUCT) instead of discharging.
    """
    claim = claim or {}
    for key in (
        "symbol",
        "file_path",
        "repository",
        "revision",
        "build_identity",
        "execution_id",
        "environment",
        "predicate",
    ):
        if not str(claim.get(key) or "").strip():
            return RuleOutcome.MISSING_PREMISE, f"behavior claim names no {key}"
    asserted = claim.get("asserted_value")
    if asserted is None:
        return RuleOutcome.MISSING_PREMISE, "behavior claim states no canonical asserted value"
    if isinstance(asserted, float):
        return RuleOutcome.INADMISSIBLE_PREMISE, "behavior asserted value forbids floats (canonical scalar only)"
    if (
        claim.get("form") != "OBSERVATION"
        or claim.get("quantifier") != "OBSERVATIONAL"
        or claim.get("relation") != "RUNTIME_OBSERVATION"
        or claim.get("modality") != "RUNTIME"
    ):
        return (
            RuleOutcome.UNSUPPORTED_CONSTRUCT,
            "behavior needs OBSERVATION/OBSERVATIONAL/RUNTIME_OBSERVATION/RUNTIME header",
        )
    contract_violation = _reject_unknown_v2_keys(claim, "behavior/v1")
    if contract_violation is not None:
        return contract_violation
    rows = [p for p in (premises or []) if isinstance(p, dict)]
    codes = [p for p in rows if _premise_type(p) == "RANGE_READ_FACT"]
    runtimes = [p for p in rows if _premise_type(p) == "RUNTIME_OBSERVATION_FACT"]
    mappings = [p for p in rows if _premise_type(p) == "BUILD_SOURCE_MAPPING"]
    if not codes or not runtimes or not mappings:
        return (
            RuleOutcome.MISSING_PREMISE,
            "behavior needs code, runtime, and mapping premises",
        )
    if len(codes) > 1 or len(runtimes) > 1 or len(mappings) > 1 or len(rows) != 3:
        return (
            RuleOutcome.INADMISSIBLE_PREMISE,
            "behavior takes exactly one code, runtime, and mapping premise",
        )
    dup = _duplicate_evidence_id(rows)
    if dup is not None:
        return RuleOutcome.INADMISSIBLE_PREMISE, f"duplicate evidence id: {dup}"
    try:
        code = RangeReadFact.model_validate(codes[0])
        runtime = RuntimeObservationFact.model_validate(runtimes[0])
        mapping = BuildSourceMapping.model_validate(mappings[0])
    except ValidationError:
        return RuleOutcome.INADMISSIBLE_PREMISE, "behavior premise fails strict contract"
    if code.target != str(claim["symbol"]) or code.file_path != str(claim["file_path"]):
        return RuleOutcome.INVALID_INFERENCE, "code premise does not show the asserted target"
    if str(claim["symbol"]) not in code.window_text:
        return RuleOutcome.INVALID_INFERENCE, "code window does not show the asserted target"
    if code.repository != str(claim["repository"]) or code.revision != str(claim["revision"]):
        return RuleOutcome.INVALID_INFERENCE, "code source differs from claim scope"
    if (
        runtime.symbol != str(claim["symbol"])
        or runtime.predicate != str(claim["predicate"])
        or not _scalar_equal(runtime.observed_value, asserted)
    ):
        return RuleOutcome.INVALID_INFERENCE, "runtime observation does not establish the asserted behavior"
    if (
        runtime.repository != code.repository
        or runtime.source_revision != code.revision
        or runtime.build_identity != str(claim["build_identity"])
        or runtime.execution_id != str(claim["execution_id"])
        or runtime.environment != str(claim["environment"])
    ):
        return RuleOutcome.INVALID_INFERENCE, "runtime scope does not join the cited source"
    if (
        mapping.build_identity != runtime.build_identity
        or mapping.repository != runtime.repository
        or mapping.source_revision != runtime.source_revision
    ):
        return RuleOutcome.INVALID_INFERENCE, "mapping does not bind the observed build to the cited source"
    if mapping.provenance_ref not in APPROVED_BUILD_MAPPING_AUTHORITIES:
        return RuleOutcome.UNSUPPORTED_CONSTRUCT, "build-source mapping authority not approved"
    return RuleOutcome.DISCHARGED, "code, runtime, and attested mapping join on the asserted behavior"


def validate_absence_premises(
    claim: dict[str, Any],
    premises: list[dict[str, Any]],
    manifest: dict[str, Any] | None = None,
) -> tuple[RuleOutcome, str]:
    """Semantic validation for ``absence/v1`` (typed coverage proof).

    Claim shape: ``ABSENCE``/``UNIVERSAL``/``STATIC``/``CLOSED_WORLD``
    header plus ``universe``, ``predicate``, ``repository``,
    ``revision``, and ``path_scope``. The manifest (plain mapping with
    ``evidence_ids``, ``content_hashes``, ``premise_types``,
    ``retrieval_config``, and ``exclusions``) binds the single coverage
    artifact; without it the application is missing a proof input. Hash
    or availability failures are PREMISE_UNAVAILABLE, never negative
    proof. A relevant exclusion abstains instead of proving absence.
    """
    claim = claim or {}
    if (
        claim.get("form") != "ABSENCE"
        or claim.get("quantifier") != "UNIVERSAL"
        or claim.get("modality") != "STATIC"
        or claim.get("completeness") != "CLOSED_WORLD"
    ):
        return (
            RuleOutcome.UNSUPPORTED_CONSTRUCT,
            "absence needs ABSENCE/UNIVERSAL/STATIC/CLOSED_WORLD header",
        )
    contract_violation = _reject_unknown_v2_keys(claim, "absence/v1")
    if contract_violation is not None:
        return contract_violation
    for key in ("universe", "predicate", "repository", "revision", "path_scope"):
        if not str(claim.get(key) or "").strip():
            return RuleOutcome.MISSING_PREMISE, f"absence claim states no {key}"
    rows = [p for p in (premises or []) if isinstance(p, dict)]
    covers = [p for p in rows if _premise_type(p) == "ABSENCE_COVERAGE_FACT"]
    if not covers:
        return RuleOutcome.MISSING_PREMISE, "absence needs exactly one coverage premise"
    if len(covers) > 1 or len(rows) != 1:
        return RuleOutcome.INADMISSIBLE_PREMISE, "absence takes no match-result premises"
    dup = _duplicate_evidence_id(rows)
    if dup is not None:
        return RuleOutcome.INADMISSIBLE_PREMISE, f"duplicate evidence id: {dup}"
    try:
        coverage = AbsenceCoverageFact.model_validate(covers[0])
    except ValidationError:
        return RuleOutcome.INADMISSIBLE_PREMISE, "coverage premise fails strict contract"
    if not isinstance(manifest, dict):
        return RuleOutcome.MISSING_PREMISE, "absence needs manifest binding"
    ids = list(manifest.get("evidence_ids") or [])
    hashes = manifest.get("content_hashes") or {}
    types = manifest.get("premise_types") or {}
    if not isinstance(hashes, dict) or not isinstance(types, dict):
        return RuleOutcome.MISSING_PREMISE, "absence manifest carries no content maps"
    pid = coverage.evidence_id
    if pid not in ids:
        return RuleOutcome.MISSING_PREMISE, "coverage id absent from manifest"
    if hashes.get(pid) != coverage.content_hash:
        # Rule layer has no PREMISE_UNAVAILABLE member (frozen vocabulary):
        # bytes that are not the manifested bytes are inadmissible as
        # inference premises. certify() and the checker, which own that
        # outcome, report the same condition as PREMISE_UNAVAILABLE.
        return RuleOutcome.INADMISSIBLE_PREMISE, "coverage artifact not the manifested bytes"
    if types.get(pid) != "ABSENCE_COVERAGE_FACT":
        return RuleOutcome.INADMISSIBLE_PREMISE, "coverage id bound to another premise type"
    if coverage.search_status != "COMPLETE_EMPTY":
        return RuleOutcome.UNSUPPORTED_CONSTRUCT, "only complete-empty searches support absence"
    if coverage.match_count != 0:
        return RuleOutcome.INVALID_INFERENCE, "nonzero match contradicts absence"
    if coverage.universe != str(claim["universe"]) or coverage.predicate != str(claim["predicate"]):
        return RuleOutcome.INVALID_INFERENCE, "coverage does not address the claimed predicate"
    scope = coverage.scope
    if (
        scope.repository != str(claim["repository"])
        or scope.revision != str(claim["revision"])
        or scope.path_scope != str(claim["path_scope"])
    ):
        return RuleOutcome.INVALID_INFERENCE, "coverage scope differs from proposition scope"
    retrieval = manifest.get("retrieval_config") or {}
    if not isinstance(retrieval, dict):
        return RuleOutcome.INVALID_INFERENCE, "retrieval configuration malformed"
    if retrieval.get("search_config_digest") != coverage.search_config_digest:
        return RuleOutcome.INVALID_INFERENCE, "coverage search config differs from retrieval config"
    if retrieval.get("scope") != coverage.universe:
        return RuleOutcome.INVALID_INFERENCE, "retrieval scope differs from coverage universe"
    if list(manifest.get("exclusions") or []) != list(coverage.exclusions):
        return RuleOutcome.INVALID_INFERENCE, "manifest and coverage exclusions differ"
    for exclusion in coverage.exclusions:
        if _exclusion_covers_scope(exclusion, coverage.universe, scope.path_scope):
            return RuleOutcome.UNSUPPORTED_CONSTRUCT, "relevant scope excluded from the universe"
    return RuleOutcome.DISCHARGED, "complete-empty coverage establishes absence in the universe"


def _exclusion_covers_scope(exclusion: str, universe: str, path_scope: str) -> bool:
    """True when an exclusion removes the claimed scope (conservative).

    Equality or prefix overlap on the normalized path, or substring
    overlap on the universe text, counts as relevant: an empty result
    after carving out the target cannot prove its absence.
    """
    exc = str(exclusion or "").rstrip("/")
    path = str(path_scope or "").rstrip("/")
    uni = str(universe or "")
    if not exc:
        return False
    return exc == path or path.startswith(exc + "/") or exc in uni


def validate_history_premises(
    claim: dict[str, Any], premises: list[dict[str, Any]]
) -> tuple[RuleOutcome, str]:
    """Semantic validation for ``history/v1`` (exact history identity).

    Claim shape: ``HISTORICAL``/``EXISTENTIAL``/``HISTORY`` header (no
    ``relation`` or ``completeness`` keys) plus ``repository``,
    ``commit_id`` (full lowercase 40-hex), ``file_path``, ``subject``,
    and ``repository_revision``. A ``CAUSAL`` form never routes here;
    causal readings of a commit subject are unsupported by construction.
    """
    claim = claim or {}
    if (
        claim.get("form") != "HISTORICAL"
        or claim.get("quantifier") != "EXISTENTIAL"
        or claim.get("modality") != "HISTORY"
    ):
        return (
            RuleOutcome.UNSUPPORTED_CONSTRUCT,
            "history needs HISTORICAL/EXISTENTIAL/HISTORY header",
        )
    present = {key: claim[key] for key in ("relation", "completeness") if key in claim}
    if present:
        if any(value is None for value in present.values()):
            return RuleOutcome.INVALID_INFERENCE, "optional history headers must be omitted, not null"
        return RuleOutcome.UNSUPPORTED_CONSTRUCT, "unexpected history header value"
    contract_violation = _reject_unknown_v2_keys(claim, "history/v1")
    if contract_violation is not None:
        return contract_violation
    for key in ("repository", "commit_id", "file_path", "subject", "repository_revision"):
        if not str(claim.get(key) or "").strip():
            return RuleOutcome.MISSING_PREMISE, f"history claim states no {key}"
    if not _FULL_SHA_RE.fullmatch(str(claim["commit_id"])) or not _FULL_SHA_RE.fullmatch(
        str(claim["repository_revision"])
    ):
        return RuleOutcome.INADMISSIBLE_PREMISE, "history claim states no resolvable full commit"
    rows = [p for p in (premises or []) if isinstance(p, dict)]
    histories = [p for p in rows if _premise_type(p) == "COMMIT_HISTORY"]
    if not histories:
        return RuleOutcome.MISSING_PREMISE, "history needs exactly one commit premise"
    if len(histories) > 1 or len(rows) != 1:
        return RuleOutcome.INADMISSIBLE_PREMISE, "history takes exactly one commit premise"
    dup = _duplicate_evidence_id(rows)
    if dup is not None:
        return RuleOutcome.INADMISSIBLE_PREMISE, f"duplicate evidence id: {dup}"
    try:
        record = CommitHistory.model_validate(histories[0])
    except ValidationError:
        return RuleOutcome.INADMISSIBLE_PREMISE, "history premise fails strict contract"
    if record.repository != str(claim["repository"]):
        return RuleOutcome.INVALID_INFERENCE, "history repository differs from target"
    if record.repository_revision != str(claim["repository_revision"]):
        return RuleOutcome.INVALID_INFERENCE, "history query revision differs from proposition context"
    if record.commit_id != str(claim["commit_id"]):
        return RuleOutcome.INVALID_INFERENCE, "commit id does not match the target commit"
    if record.file_path != str(claim["file_path"]):
        return RuleOutcome.INVALID_INFERENCE, "history file differs from target"
    if record.subject != str(claim["subject"]):
        return RuleOutcome.INVALID_INFERENCE, "history subject differs from target"
    return RuleOutcome.DISCHARGED, "commit history exactly binds the claimed change"


_VALIDATORS: dict[str, Callable[..., tuple[RuleOutcome, str]]] = {
    "definition/v1": validate_definition_premises,
    "call_edge/v1": validate_call_edge_premises,
    "behavior/v1": validate_behavior_premises,
    "absence/v1": validate_absence_premises,
    "history/v1": validate_history_premises,
}


def apply_rule(
    rule: ProofRule,
    claim: dict[str, Any],
    premises: list[dict[str, Any]],
    manifest: dict[str, Any] | None = None,
) -> tuple[RuleOutcome, str]:
    """Apply a registered rule to a claim over premises (pure).

    HEURISTIC rules never discharge by construction. Unknown validator
    bindings fail closed as INADMISSIBLE_PREMISE (a swapped executable
    without a registry update cannot prove). The manifest is passed
    through only to rules whose contract requires manifest binding
    (currently ``absence/v1``); all other rules ignore it.
    """
    if rule.soundness == Soundness.HEURISTIC:
        return RuleOutcome.UNSUPPORTED_CONSTRUCT, "heuristic rules never discharge"
    validator = _VALIDATORS.get(rule.rule_id)
    if validator is None or validator.__name__ not in rule.validator:
        return RuleOutcome.INADMISSIBLE_PREMISE, "no bound validator for rule"
    try:
        if rule.rule_id == "absence/v1":
            return validator(claim or {}, [p for p in premises or []], manifest)
        return validator(claim or {}, [p for p in premises or []])
    except Exception as exc:
        return RuleOutcome.INVALID_INFERENCE, f"validator error: {type(exc).__name__}"
