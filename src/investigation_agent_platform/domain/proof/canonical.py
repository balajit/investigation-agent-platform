# src/investigation_agent_platform/domain/proof/canonical.py
"""WP-B1 canonical identity (canon/v1): pure, deterministic, fail-closed.

Implements the contract in ``tests/fixtures/proof_kernel/canon_spec_v1.json``
(normative per ADR-KERNEL-002): canonical JSON bytes (sorted keys, compact
separators, UTF-8, NFC-normalized strings, floats rejected) and the four
identity domains — proposition, context, derivation, decision.

Validation mirrors ``canon_invalid_vectors.json`` (X1–X9): bad inputs are
rejected with a stable error code, never coerced — except repository paths,
which are normalized per owner decision (strip leading ``./``, collapse
duplicate slashes, strip trailing slash, case preserved; backslash rejected
per X5). Assumption arrays sort before hashing (set semantics per X6).

No I/O, no environment, no clock. The only evolution lever is
``CANON_VERSION``: a bump invalidates prior hashes by design.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from enum import Enum
from typing import Any

CANON_VERSION = "canon/v1"


class CanonError(str, Enum):
    """Stable canonicalization failure codes (X1–X9 plus INVALID_FLOAT).

    Values are frozen API: never rename or remove a code, only append.
    """

    MISSING_FIELD = "MISSING_FIELD"
    UNKNOWN_ENUM = "UNKNOWN_ENUM"
    NULL_FIELD = "NULL_FIELD"
    NOT_NORMALIZED = "NOT_NORMALIZED"
    BAD_PATH = "BAD_PATH"
    DUPLICATE_ASSUMPTIONS = "DUPLICATE_ASSUMPTIONS"
    UNKNOWN_NORMALIZATION_OP = "UNKNOWN_NORMALIZATION_OP"
    INVALID_RANGE = "INVALID_RANGE"
    CANON_VERSION_MISMATCH = "CANON_VERSION_MISMATCH"
    INVALID_FLOAT = "INVALID_FLOAT"
    UNKNOWN_FIELD = "UNKNOWN_FIELD"


class CanonicalizationError(ValueError):
    """Fail-closed canonicalization failure carrying a stable code."""

    def __init__(self, code: CanonError | str, detail: str = "") -> None:
        self.code = str(getattr(code, "value", code))
        super().__init__(f"{self.code}: {detail}" if detail else self.code)


_QUANTIFIERS = frozenset(
    {"EXISTENTIAL", "UNIVERSAL", "OBSERVATIONAL", "CONDITIONAL", "STRUCTURAL"}
)
_FORMS = frozenset(
    {"EXISTENCE", "UNIVERSAL", "ABSENCE", "HISTORICAL", "CAUSAL", "OBSERVATION"}
)
_RELATIONS = frozenset(
    {"CALL_EDGE", "DATA_FLOW", "CONTROL_DEPENDENCE", "RUNTIME_OBSERVATION", "CAUSAL_SUPPORT"}
)
_MODALITIES = frozenset({"STATIC", "RUNTIME", "HISTORY", "SOLVER_DERIVED", "DOCUMENTATION"})
_COMPLETENESS = frozenset({"PARTIAL_OK", "COMPLETE_PATH", "CLOSED_WORLD"})


def normalize_path(path: str) -> str:
    """Normalize a repository-relative path (owner-decided strip rules).

    Rejects backslashes (X5); strips a leading ``./``, collapses duplicate
    slashes, strips a single trailing slash. Case is preserved — case
    changes address different files on case-sensitive filesystems.
    """
    text = str(path or "")
    if "\\" in text:
        raise CanonicalizationError(CanonError.BAD_PATH, f"backslash path: {text[:160]}")
    parts = [p for p in text.split("/") if p not in ("", ".")]
    return "/".join(parts)


def _check_string(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise CanonicalizationError(CanonError.MISSING_FIELD, f"non-string at {where}")
    if value != unicodedata.normalize("NFC", value):
        raise CanonicalizationError(CanonError.NOT_NORMALIZED, where)
    return value


def _check_no_floats(node: Any, where: str) -> None:
    if isinstance(node, float):
        raise CanonicalizationError(CanonError.INVALID_FLOAT, where)
    if isinstance(node, dict):
        for key, item in node.items():
            _check_no_floats(item, f"{where}.{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _check_no_floats(item, f"{where}[{index}]")


def _check_nfc(node: Any, where: str) -> None:
    if isinstance(node, str):
        _check_string(node, where)
    elif isinstance(node, dict):
        for key, item in node.items():
            _check_nfc(item, f"{where}.{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _check_nfc(item, f"{where}[{index}]")


def _check_lines(node: Any, where: str) -> None:
    """Line fields: omitted or positive ints, never null (X3); ordered (X8)."""
    if not isinstance(node, dict):
        return
    for key in ("line_start", "line_end"):
        if key in node and node[key] is None:
            raise CanonicalizationError(CanonError.NULL_FIELD, f"{where}.{key}")
    start = node.get("line_start")
    end = node.get("line_end")
    for key, value in (("line_start", start), ("line_end", end)):
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise CanonicalizationError(CanonError.INVALID_RANGE, f"{where}.{key}")
    if start is not None and end is not None and start > end:
        raise CanonicalizationError(CanonError.INVALID_RANGE, where)


def _normalize_paths(node: Any) -> Any:
    """Apply strip-rule path normalization to file_path fields (recursive)."""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, item in node.items():
            if key == "file_path" and isinstance(item, str):
                out[key] = normalize_path(item)
            else:
                out[key] = _normalize_paths(item)
        return out
    if isinstance(node, list):
        return [_normalize_paths(item) for item in node]
    return node


def _check_normalization(
    normalization: Any, known_ops: frozenset[str], where: str
) -> None:
    """Normalization blocks authorize equivalence; never bare trust (X7).

    With no populated registry every op is unknown. Registry-gated
    comparison (V4) passes its known-op set explicitly; the validator,
    artifact, and revision binding must all be present — a bare op string
    never authorizes equivalence.
    """
    op_id = (
        str(normalization.get("op_id") or "")
        if isinstance(normalization, dict)
        else ""
    )
    if not op_id or op_id not in known_ops:
        raise CanonicalizationError(
            CanonError.UNKNOWN_NORMALIZATION_OP, f"{where}.op_id={op_id!r}"
        )
    if isinstance(normalization, dict):
        for key in ("validator", "artifact", "revision_binding"):
            if not str(normalization.get(key) or "").strip():
                raise CanonicalizationError(
                    CanonError.UNKNOWN_NORMALIZATION_OP, f"{where}.{key} missing"
                )


#: Proposition identity fields (canon_spec_v1 field_boundary). Everything
#: else in the input (scope, line numbers, commit, normalization block) is
#: context or authorization metadata: validated, never hashed. V2 (line
#: move), V6 (configuration broadening), V7 (revision change) keep
#: proposition identity with a new context.
_PROPOSITION_KEYS = (
    "predicate",
    "quantifier",
    "form",
    "relation",
    "modality",
    "completeness",
    "assumptions",
    "canon_version",
    "target",
    "asserted_value",
)

#: Target identity fields. Line numbers and commit IDs are context.
#: caller/callee ride alongside symbol/counterpart per the ClaimTarget
#: contract (they are the binding keys _claim_bindings reads).
#: v2 additions (cert/v2): ``universe`` (ABSENCE identity), ``repository``,
#: ``commit_id``, and ``subject`` (HISTORY identity). Additive only: no
#: existing vector carries these keys, so no existing digest changes.
_TARGET_KEYS = (
    "symbol",
    "caller",
    "callee",
    "counterpart",
    "declaring_type",
    "file_path",
    "value_semantics",
    "claimed_value",
    "fact_category",
    "universe",
    "repository",
    "commit_id",
    "subject",
)

#: Context fields: validated where schema-constrained (paths, lines) but
#: never hashed. Union of the spec field_boundary context list
#: (canon_spec_v1.json) and the keys the package observes in practice
#: (scope, normalization carriers, top-level file_path, commit). Anything
#: outside identity + context is rejected (UNKNOWN_FIELD) so a meaningful
#: new proposition field can never be silently projected away.
_CONTEXT_KEYS = (
    "scope",
    "normalization",
    "file_path",
    "line_start",
    "line_end",
    "revision",
    "commit",
    "configuration",
    "environment",
    "execution",
    "evidence_manifest_digest",
    "rule_bundle_version",
    "model_config",
    "reconciliation_state",
)

#: Target-level context fields (observed in vectors; never hashed).
_TARGET_CONTEXT_KEYS = (
    "line_start",
    "line_end",
    "revision",
    "commit",
)

#: v2 rule-specific target keys (cert/v2, rules/v2). Validators require
#: the listed keys per rule; the canonical projection includes any listed
#: key present and never silently drops an unlisted one (unknown keys fail
#: closed as UNKNOWN_FIELD above).
V2_TARGET_KEYS: dict[str, frozenset[str]] = {
    "definition/v1": frozenset({"symbol", "file_path"}),
    "call_edge/v1": frozenset({"caller", "callee", "counterpart", "symbol"}),
    "behavior/v1": frozenset({"symbol", "file_path"}),
    "absence/v1": frozenset({"universe"}),
    "history/v1": frozenset({"repository", "commit_id", "file_path", "subject"}),
}

#: v2 rule-specific scope keys (cert/v2, rules/v2). Scope rides in the
#: context preimage, never in the proposition identity. Validators require
#: the listed keys per rule; anything else fails closed there.
V2_SCOPE_KEYS: dict[str, frozenset[str]] = {
    "definition/v1": frozenset({"revision"}),
    "call_edge/v1": frozenset({"revision"}),
    "behavior/v1": frozenset(
        {"repository", "revision", "build_identity", "execution_id", "environment"}
    ),
    "absence/v1": frozenset({"repository", "revision", "path_scope"}),
    "history/v1": frozenset({"repository_revision"}),
}


def v2_unknown_target_keys(rule_id: str, target: Any) -> list[str]:
    """Keys in target outside the rule's v2 target contract (sorted)."""
    allowed = V2_TARGET_KEYS.get(str(rule_id or ""), frozenset())
    if not isinstance(target, dict):
        return []
    return sorted(key for key in target if key not in allowed)


def v2_unknown_scope_keys(rule_id: str, scope: Any) -> list[str]:
    """Keys in scope outside the rule's v2 scope contract (sorted)."""
    allowed = V2_SCOPE_KEYS.get(str(rule_id or ""), frozenset())
    if not isinstance(scope, dict):
        return []
    return sorted(key for key in scope if key not in allowed)


def canonical_dict_bytes(node: dict[str, Any]) -> bytes:
    """Canonical bytes for an arbitrary JSON dict (no proposition schema).

    Same byte layout as propositions (sorted keys, compact separators,
    UTF-8) with the same fail-closed guards (NFC strings, no floats).
    Used for manifest and envelope digests.
    """
    if not isinstance(node, dict):
        raise CanonicalizationError(CanonError.MISSING_FIELD, "mapping")
    _check_no_floats(node, "$")
    _check_nfc(node, "$")
    return json.dumps(
        node, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def canonical_bytes(
    proposition: dict[str, Any],
    known_normalization_ops: frozenset[str] = frozenset(),
) -> bytes:
    """Validate a canonical proposition and return its canonical bytes.

    Fail-closed: any X-code violation raises CanonicalizationError. The
    byte layout reproduces ``canon_spec_v1.json`` digest vectors exactly:
    ``json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)``
    encoded as UTF-8. A ``normalization`` block must be registry-authorized
    (else X7) and never participates in identity — it authorizes
    comparison, not bytes.
    """
    if not isinstance(proposition, dict):
        raise CanonicalizationError(CanonError.MISSING_FIELD, "proposition")
    version = proposition.get("canon_version")
    if version != CANON_VERSION:
        raise CanonicalizationError(
            CanonError.CANON_VERSION_MISMATCH, f"canon_version={version!r}"
        )
    predicate = proposition.get("predicate")
    if not isinstance(predicate, str) or not predicate.strip():
        raise CanonicalizationError(CanonError.MISSING_FIELD, "predicate")
    if "quantifier" not in proposition:
        raise CanonicalizationError(CanonError.MISSING_FIELD, "quantifier")
    quantifier = proposition.get("quantifier")
    if quantifier not in _QUANTIFIERS:
        raise CanonicalizationError(CanonError.UNKNOWN_ENUM, f"quantifier={quantifier!r}")
    for key, allowed in (
        ("form", _FORMS),
        ("relation", _RELATIONS),
        ("modality", _MODALITIES),
        ("completeness", _COMPLETENESS),
    ):
        if key in proposition and proposition[key] not in allowed:
            raise CanonicalizationError(CanonError.UNKNOWN_ENUM, f"{key}={proposition[key]!r}")
    for key in proposition:
        if key not in _PROPOSITION_KEYS and key not in _CONTEXT_KEYS:
            raise CanonicalizationError(CanonError.UNKNOWN_FIELD, f"proposition.{key}")
    if "scope" in proposition and not isinstance(proposition["scope"], dict):
        raise CanonicalizationError(CanonError.MISSING_FIELD, "scope must be an object")
    target_keys = proposition.get("target")
    if isinstance(target_keys, dict):
        for key in target_keys:
            if key not in _TARGET_KEYS and key not in _TARGET_CONTEXT_KEYS:
                raise CanonicalizationError(CanonError.UNKNOWN_FIELD, f"target.{key}")
    _check_no_floats(proposition, "$")
    _check_nfc(proposition, "$")
    _check_lines(proposition, "$")
    target = proposition.get("target")
    if target is not None:
        if not isinstance(target, dict):
            raise CanonicalizationError(CanonError.MISSING_FIELD, "target")
        _check_lines(target, "target")
    assumptions = proposition.get("assumptions", [])
    if assumptions is None:
        assumptions = []
    if not isinstance(assumptions, list) or any(not isinstance(a, str) for a in assumptions):
        raise CanonicalizationError(CanonError.MISSING_FIELD, "assumptions")
    if len(set(assumptions)) != len(assumptions):
        raise CanonicalizationError(CanonError.DUPLICATE_ASSUMPTIONS, "assumptions")
    normalization = proposition.get("normalization")
    if normalization is not None:
        _check_normalization(normalization, known_normalization_ops, "normalization")
    top_path = proposition.get("file_path")
    if top_path is not None:
        # A top-level file_path is not a proposition field (target carries
        # it), but when present it must still be a valid path (X5).
        if not isinstance(top_path, str):
            raise CanonicalizationError(CanonError.BAD_PATH, "file_path")
        normalize_path(top_path)
    projected: dict[str, Any] = {
        key: proposition[key] for key in _PROPOSITION_KEYS if key in proposition
    }
    target = projected.get("target")
    if isinstance(target, dict):
        projected["target"] = {
            key: target[key] for key in _TARGET_KEYS if key in target
        }
    normalized = _normalize_paths(projected)
    if isinstance(normalized, dict):
        normalized = {
            key: value for key, value in normalized.items() if key != "normalization"
        }
        if isinstance(normalized.get("assumptions"), list):
            normalized["assumptions"] = sorted(normalized["assumptions"])
    return json.dumps(
        normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _digest_sha256_prefixed(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def proposition_id(proposition: dict[str, Any]) -> str:
    """Canonical proposition identity: ``sha256:`` + hex of canonical bytes."""
    return _digest_sha256_prefixed(canonical_bytes(proposition))


def _check_normalization_subjects(
    normalization: Any,
    first_bytes: bytes,
    second_bytes: bytes,
    where: str,
) -> None:
    """Pair binding: a normalization block authorizes exactly one pair.

    The block must name both compared propositions by their canonical IDs
    (``subjects``, order-insensitive). A block authorizing a different pair
    — however plausible its validator/artifact strings — never merges this
    pair. Validator *execution* binding (running the named validator over
    the pair) is staged: no normalization execution model exists in this
    package yet, so execution remains out of scope and is recorded as open
    in the remediation plan rather than claimed here.
    """
    subjects = normalization.get("subjects") if isinstance(normalization, dict) else None
    if (
        not isinstance(subjects, list)
        or len(subjects) != 2
        or not all(isinstance(s, str) and s for s in subjects)
    ):
        raise CanonicalizationError(
            CanonError.UNKNOWN_NORMALIZATION_OP, f"{where} must name the pair"
        )
    want = {
        "sha256:" + hashlib.sha256(first_bytes).hexdigest(),
        "sha256:" + hashlib.sha256(second_bytes).hexdigest(),
    }
    if set(subjects) != want:
        raise CanonicalizationError(
            CanonError.UNKNOWN_NORMALIZATION_OP, f"{where} authorizes another pair"
        )


def same_proposition(
    first: dict[str, Any],
    second: dict[str, Any],
    known_normalization_ops: frozenset[str] = frozenset(),
    normalization: dict[str, Any] | None = None,
) -> bool:
    """Proposition equivalence with registry-gated normalization (V4 rule).

    Byte-identical canonical forms are the same proposition. Otherwise the
    pair is equivalent only when a shared normalization block (vector-level,
    as in V4) authorizes it: the block must be registry-listed, carry a
    validator, artifact, and revision binding, AND name exactly this pair
    in ``subjects`` — a bare op string never authorizes equivalence (V3/X7),
    and a block naming any other pair never merges this one.
    """
    first_bytes = canonical_bytes(first, known_normalization_ops)
    second_bytes = canonical_bytes(second, known_normalization_ops)
    if first_bytes == second_bytes:
        return True
    if normalization is None:
        return False
    try:
        _check_normalization(normalization, known_normalization_ops, "normalization")
        _check_normalization_subjects(
            normalization, first_bytes, second_bytes, "normalization.subjects"
        )
    except CanonicalizationError:
        return False
    return True


def context_id(proposition_id_value: str, context: dict[str, Any]) -> str:
    """Context identity: proposition + frozen evaluation-context fields."""
    envelope = {
        "canon_version": CANON_VERSION,
        "context": context,
        "proposition_id": proposition_id_value,
    }
    raw = json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    _check_no_floats(envelope, "context")
    _check_nfc(envelope, "context")
    return _digest_sha256_prefixed(raw.encode("utf-8"))


def derivation_id(
    context_id_value: str,
    rule_id: str,
    rule_version: str,
    implementation_digest: str,
    premise_ids: list[str],
    inference: dict[str, Any],
) -> str:
    """Derivation identity: context + rule + ordered premises + inference."""
    envelope = {
        "canon_version": CANON_VERSION,
        "context_id": context_id_value,
        "implementation_digest": implementation_digest,
        "inference": inference,
        "premise_ids": list(premise_ids or []),
        "rule_id": rule_id,
        "rule_version": rule_version,
    }
    raw = json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    _check_no_floats(envelope, "derivation")
    _check_nfc(envelope, "derivation")
    return _digest_sha256_prefixed(raw.encode("utf-8"))


def decision_id(
    derivation_id_value: str,
    certificate_id: str,
    gate_policy_version: str,
    freshness: dict[str, Any],
    reconciliation_state: str,
) -> str:
    """Publication-decision identity: derivation + certificate + gate inputs."""
    envelope = {
        "canon_version": CANON_VERSION,
        "certificate_id": certificate_id,
        "derivation_id": derivation_id_value,
        "freshness": freshness,
        "gate_policy_version": gate_policy_version,
        "reconciliation_state": reconciliation_state,
    }
    raw = json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    _check_no_floats(envelope, "decision")
    _check_nfc(envelope, "decision")
    return _digest_sha256_prefixed(raw.encode("utf-8"))
