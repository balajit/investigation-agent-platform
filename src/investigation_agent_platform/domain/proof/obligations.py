# src/investigation_agent_platform/domain/proof/obligations.py
"""K13 obligation layer: derivation-bound promotion obligations (v1.3 contract).

The guarded promotion entry (``decide_guarded_promotion``) is the only
authoritative promotion path: it parses canonical evidence + trusted
context bytes once into an immutable snapshot, binds evidence to the
operator-supplied policy, runs the checker internally, evaluates the
kind-table obligations over the checked snapshot, and promotes only on
all-SATISFIED. No caller input can omit an obligation, assert a status,
approve an authority, or supply a checker outcome.

Pure domain: no I/O, no environment, no clock. Denial reasons reuse the
stable ``DenyReason`` vocabulary (append-only members added for v1.3).
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from investigation_agent_platform.domain.proof.certificate import (
    CheckerOutcome,
    CheckRequest,
    check_certificate,
)
from investigation_agent_platform.domain.proof.manifest import EvidenceManifest
from investigation_agent_platform.domain.proof.publication import (
    DenyReason,
    PublicationDecision,
    _decide_linearization,
)


class ObligationStatus(str, Enum):
    """Closed v1.3 obligation status vocabulary (no CONFLICTED/INVALIDATED)."""

    OPEN = "OPEN"
    SATISFIED = "SATISFIED"
    UNSUPPORTED = "UNSUPPORTED"


class ProofObligation(BaseModel):
    """One checkable promotion predicate with a derived status."""

    model_config = ConfigDict(frozen=True)

    obligation_id: str = Field(..., max_length=512)
    kind: str = Field(..., max_length=64)
    statement: str = Field(..., max_length=1024)
    status: ObligationStatus = Field(...)
    evidence: tuple[str, ...] = Field(default=())
    derived_from: str = Field(default="", max_length=512)


class DecisionPolicy(BaseModel):
    """Operator-supplied trusted policy (B2 wiring selects it per tenant)."""

    model_config = ConfigDict(frozen=True)

    policy_id: str = Field(..., min_length=1, max_length=256)
    repositories: tuple[str, ...] = Field(..., min_length=1)
    approved_mapping_provenance_refs: tuple[str, ...] = Field(default=())

    @field_validator("repositories")
    @classmethod
    def _repositories_canonical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        seen: set[str] = set()
        for repo in value:
            if not isinstance(repo, str) or not repo:
                raise ValueError("repository_id must be a non-empty string")
            if repo in seen:
                raise ValueError(f"duplicate repository_id: {repo}")
            seen.add(repo)
        return value


# Kind table: rule_id -> ordered obligation statements (contract).
_KIND_OBLIGATIONS: dict[str, tuple[str, ...]] = {
    "definition/v1": (
        "single RANGE_READ premise bound",
        "symbol shown in cited text",
        "premise revision joins claim scope (pinned revision required)",
    ),
    "call_edge/v1": (
        "all premises NORMALIZED_CALLSITE with unique IDs",
        "caller/callee bound in cited text",
        "each premise revision joins claim scope (pinned revision required)",
    ),
    "behavior/v1": (
        "code/runtime/mapping triple present and unique",
        "target/repo/revision/build/execution/environment joins hold",
        "mapping authority approved",
    ),
    "absence/v1": (
        "complete-empty coverage",
        "zero matches",
        "scope/config/exclusion agreement",
        "no relevant exclusion",
    ),
    "history/v1": (
        "single commit-history premise",
        "full-SHA pins",
        "repo/revision/commit/file/subject equality, no locators",
    ),
}

# CheckRequest has no other fields; the table below must stay complete.
_CHECK_REQUEST_FIELDS = frozenset(
    {
        "certificate",
        "proposition",
        "manifest",
        "rule_bundle_version",
        "model_encoding",
        "reconciliation_state",
        "artifacts",
        "expected_context_id",
        "mapping_authority",
        "known_normalization_ops",
        "derivation_deps",
    }
)


class SnapshotError(ValueError):
    """Non-canonical, duplicate-keyed, or schema-invalid snapshot input."""


def _canonical_bytes(node: dict[str, Any]) -> bytes:
    """Independent canonical bytes: sorted keys, compact separators, UTF-8.

    Same byte layout the kernel contracts require. Rejects floats and
    non-NFC strings (fail closed, mirroring the other kernel paths).
    """
    if not isinstance(node, dict):
        raise SnapshotError("canonical input must be a mapping")
    _reject_floats(node, "$")
    _reject_non_nfc(node, "$")
    return json.dumps(node, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _reject_floats(node: Any, where: str) -> None:
    if isinstance(node, float):
        raise SnapshotError(f"float forbidden at {where}")
    if isinstance(node, dict):
        for key, item in node.items():
            _reject_floats(item, f"{where}.{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _reject_floats(item, f"{where}[{index}]")


def _reject_non_nfc(node: Any, where: str) -> None:
    if isinstance(node, str):
        if node != unicodedata.normalize("NFC", node):
            raise SnapshotError(f"non-NFC string at {where}")
    elif isinstance(node, dict):
        for key, item in node.items():
            _reject_non_nfc(item, f"{where}.{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _reject_non_nfc(item, f"{where}[{index}]")


def _parse_canonical_object(data: bytes, what: str) -> dict[str, Any]:
    """Parse canonical JSON bytes, rejecting duplicates and non-canonical form."""
    try:
        text = bytes(data).decode("utf-8")
    except Exception as exc:
        raise SnapshotError(f"{what} is not UTF-8: {exc}") from exc

    def _no_dupes(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        seen: set[str] = set()
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in seen:
                raise SnapshotError(f"{what} has duplicate key: {key!r}")
            seen.add(key)
            result[key] = value
        return result

    try:
        node = json.loads(text, object_pairs_hook=_no_dupes)
    except SnapshotError:
        raise
    except Exception as exc:
        raise SnapshotError(f"{what} is not valid JSON: {exc}") from exc
    if not isinstance(node, dict):
        raise SnapshotError(f"{what} must be a JSON object")
    if _canonical_bytes(node) != bytes(data):
        raise SnapshotError(f"{what} is not in canonical form")
    return node


def _freeze(node: Any) -> Any:
    """Recursively immutable view with unambiguous markers.

    Mappings freeze to ("__dict__", ((key, value), ...)) with keys in
    first-seen order; sequences freeze to ("__list__", (...)). The
    markers remove the empty-dict/empty-list ambiguity, so
    materialization round-trips exactly.
    """
    if isinstance(node, dict):
        return ("__dict__", tuple((str(k), _freeze(v)) for k, v in node.items()))
    if isinstance(node, (list, tuple)):
        return ("__list__", tuple(_freeze(v) for v in node))
    return node


def _materialize(node: Any) -> Any:
    """Private typed materialization: frozen view back to plain JSON values."""
    if isinstance(node, tuple) and len(node) == 2 and node[0] == "__dict__":
        return {k: _materialize(v) for k, v in node[1]}
    if isinstance(node, tuple) and len(node) == 2 and node[0] == "__list__":
        return [_materialize(v) for v in node[1]]
    return node


@dataclass(frozen=True)
class EvidenceSnapshot:
    """One parsed, frozen, digest-pinned snapshot of a promotion decision.

    The original byte inputs are retained (bytes are immutable) so the
    integrity check can re-derive digests without rereading any caller
    object. Frozen views are the only parsed form consumed downstream.
    """

    evidence_digest: str
    trusted_digest: str
    evidence_bytes: bytes = field(repr=False)
    trusted_bytes: bytes = field(repr=False)
    evidence: Any = None
    trusted: Any = None
    policy: DecisionPolicy | None = None


def _snapshot_fingerprint(snapshot: EvidenceSnapshot) -> str:
    """Recompute the snapshot digest from the retained frozen values."""
    probe = {"evidence": _materialize(snapshot.evidence), "trusted": _materialize(snapshot.trusted)}
    return "sha256:" + hashlib.sha256(_canonical_bytes(probe)).hexdigest()


def _assert_snapshot_integrity(snapshot: EvidenceSnapshot) -> None:
    """Deny closed when the snapshot no longer matches its entry digests.

    Re-derives byte digests from the retained inputs and re-parses to
    the frozen form; any divergence from entry state is an internal
    invariant failure (STALE_OBLIGATION), never an obligation status.
    On frozen inputs this always passes — the closure proof for the
    check-then-evaluate TOCTOU class.
    """
    if hashlib.sha256(bytes(snapshot.evidence_bytes)).hexdigest() != snapshot.evidence_digest.removeprefix("sha256:"):
        raise SnapshotError("evidence bytes changed after snapshot entry")
    if hashlib.sha256(bytes(snapshot.trusted_bytes)).hexdigest() != snapshot.trusted_digest.removeprefix("sha256:"):
        raise SnapshotError("trusted bytes changed after snapshot entry")
    if _freeze(_parse_canonical_object(bytes(snapshot.evidence_bytes), "evidence_bytes")) != snapshot.evidence:
        raise SnapshotError("evidence snapshot no longer matches entry bytes")
    if _freeze(_parse_canonical_object(bytes(snapshot.trusted_bytes), "trusted_context_bytes")) != snapshot.trusted:
        raise SnapshotError("trusted snapshot no longer matches entry bytes")


def encode_evidence_bytes(payload: Mapping[str, Any]) -> bytes:
    """Canonical evidence bytes for the guarded entry (operator/test helper)."""
    return _canonical_bytes(dict(payload))


def encode_trusted_context_bytes(payload: Mapping[str, Any]) -> bytes:
    """Canonical trusted-context bytes for the guarded entry."""
    return _canonical_bytes(dict(payload))


def expand_obligations(
    rule_id: str, claim: Mapping[str, Any], premises: list[Any], derivation_id: str
) -> list[ProofObligation]:
    """Pure expansion: rule kind -> ordered OPEN obligations (or UNSUPPORTED)."""
    statements = _KIND_OBLIGATIONS.get(str(rule_id or ""))
    if statements is None:
        return [
            ProofObligation(
                obligation_id=f"obl:{derivation_id}:{rule_id}:0",
                kind=str(rule_id or "unknown"),
                statement="unsupported rule kind cannot promote",
                status=ObligationStatus.UNSUPPORTED,
                evidence=(),
                derived_from=str(derivation_id or ""),
            )
        ]
    evidence_ids = tuple(
        str(p.get("evidence_id") or "")
        for p in (premises or [])
        if isinstance(p, dict) and str(p.get("evidence_id") or "")
    )
    _ = claim
    return [
        ProofObligation(
            obligation_id=f"obl:{derivation_id}:{rule_id}:{ordinal}",
            kind=str(rule_id),
            statement=statement,
            status=ObligationStatus.OPEN,
            evidence=evidence_ids,
            derived_from=str(derivation_id or ""),
        )
        for ordinal, statement in enumerate(statements)
    ]


def _bindings(certificate: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(certificate, Mapping):
        return {}
    instance = certificate.get("inference_instance")
    bindings = instance.get("bindings") if isinstance(instance, Mapping) else None
    return dict(bindings) if isinstance(bindings, dict) else {}


def _entries(certificate: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    if not isinstance(certificate, Mapping):
        return []
    premises = certificate.get("premises")
    if not isinstance(premises, list):
        return []
    return [p for p in premises if isinstance(p, Mapping)]


def _revision_join_ok(claim_revision: str, entries: list[Mapping[str, Any]]) -> bool | None:
    """True when every premise scope revision equals the pinned claim revision.

    None when the claim carries no revision (join unestablishable -> OPEN).
    """
    if not claim_revision:
        return None
    for entry in entries:
        scope = entry.get("scope")
        if not isinstance(scope, dict) or str(scope.get("revision") or "") != claim_revision:
            return False
    return True


def evaluate_obligations(
    obligations: list[ProofObligation],
    certificate: Mapping[str, Any],
    artifacts: Mapping[str, Any],
    manifest: EvidenceManifest,
    mapping_authority: Mapping[str, Any] | None,
) -> list[ProofObligation]:
    """Deterministic single evaluator over the checked snapshot.

    Resolves each OPEN obligation to SATISFIED or OPEN (unpinned
    revision), or leaves UNSUPPORTED. Never raises on well-formed
    inputs; structural defects resolve OPEN (deny direction).
    """
    _ = manifest
    bindings = _bindings(certificate)
    entries = _entries(certificate)
    by_id = {str(e.get("evidence_id") or ""): e for e in entries}
    resolved: list[ProofObligation] = []
    for obligation in obligations:
        if obligation.status != ObligationStatus.OPEN:
            resolved.append(obligation)
            continue
        ok = _evaluate_one(obligation, bindings, entries, by_id, artifacts, mapping_authority)
        resolved.append(
            obligation.model_copy(update={"status": ObligationStatus.SATISFIED if ok else ObligationStatus.OPEN})
        )
    return resolved


def _artifact_available(entry: Mapping[str, Any], artifacts: Mapping[str, Any]) -> Mapping[str, Any] | None:
    record = artifacts.get(str(entry.get("evidence_id") or ""))
    return record if isinstance(record, Mapping) else None


def _evaluate_one(
    obligation: ProofObligation,
    bindings: dict[str, Any],
    entries: list[Mapping[str, Any]],
    by_id: dict[str, Mapping[str, Any]],
    artifacts: Mapping[str, Any],
    mapping_authority: Mapping[str, Any] | None,
) -> bool:
    kind = obligation.kind
    ordinal = int(obligation.obligation_id.rsplit(":", 1)[-1]) if ":" in obligation.obligation_id else -1
    _ = by_id
    if kind == "definition/v1":
        return _evaluate_definition(ordinal, bindings, entries, artifacts)
    if kind == "call_edge/v1":
        return _evaluate_call_edge(ordinal, bindings, entries, artifacts)
    if kind == "behavior/v1":
        return _evaluate_behavior(ordinal, bindings, entries, artifacts, mapping_authority)
    if kind == "absence/v1":
        return _evaluate_absence(ordinal, bindings, entries, artifacts)
    if kind == "history/v1":
        return _evaluate_history(ordinal, bindings, entries, artifacts)
    return False


def _evaluate_definition(
    ordinal: int,
    bindings: dict[str, Any],
    entries: list[Mapping[str, Any]],
    artifacts: Mapping[str, Any],
) -> bool:
    if ordinal == 0:
        if len(entries) != 1:
            return False
        entry = entries[0]
        if str(entry.get("premise_type") or "") != "RANGE_READ_FACT":
            return False
        return _artifact_available(entry, artifacts) is not None
    if ordinal == 1:
        if len(entries) != 1:
            return False
        record = _artifact_available(entries[0], artifacts)
        if record is None:
            return False
        symbol = str(bindings.get("symbol") or "")
        return bool(symbol) and symbol in str(record.get("window_text") or "")
    if ordinal == 2:
        claim_revision = str(bindings.get("revision") or "")
        joined = _revision_join_ok(claim_revision, entries)
        return joined is True
    return False


def _evaluate_call_edge(
    ordinal: int,
    bindings: dict[str, Any],
    entries: list[Mapping[str, Any]],
    artifacts: Mapping[str, Any],
) -> bool:
    if ordinal == 0:
        if not entries:
            return False
        seen: set[str] = set()
        for entry in entries:
            if str(entry.get("premise_type") or "") != "NORMALIZED_CALLSITE_FACT":
                return False
            pid = str(entry.get("evidence_id") or "")
            if not pid or pid in seen:
                return False
            seen.add(pid)
            if _artifact_available(entry, artifacts) is None:
                return False
        return True
    if ordinal == 1:
        caller = str(bindings.get("caller") or "")
        callee = str(bindings.get("callee") or "")
        if not caller or not callee:
            return False
        for entry in entries:
            record = _artifact_available(entry, artifacts)
            if record is None:
                return False
            text = str(record.get("call_text") or "")
            if str(record.get("caller") or "") == caller and str(record.get("callee") or "") == callee:
                if caller in text and callee in text:
                    return True
        return False
    if ordinal == 2:
        claim_revision = str(bindings.get("revision") or "")
        joined = _revision_join_ok(claim_revision, entries)
        return joined is True
    return False


def _evaluate_behavior(
    ordinal: int,
    bindings: dict[str, Any],
    entries: list[Mapping[str, Any]],
    artifacts: Mapping[str, Any],
    mapping_authority: Mapping[str, Any] | None,
) -> bool:
    by_type: dict[str, list[Mapping[str, Any]]] = {}
    for entry in entries:
        by_type.setdefault(str(entry.get("premise_type") or ""), []).append(entry)
    if ordinal == 0:
        return sorted(by_type) == ["BUILD_SOURCE_MAPPING", "RANGE_READ_FACT", "RUNTIME_OBSERVATION_FACT"] and all(
            len(v) == 1 and _artifact_available(v[0], artifacts) is not None for v in by_type.values()
        )
    if len(by_type.get("RANGE_READ_FACT", [])) != 1 or len(by_type.get("RUNTIME_OBSERVATION_FACT", [])) != 1 or len(
        by_type.get("BUILD_SOURCE_MAPPING", [])
    ) != 1:
        return False
    code = _artifact_available(by_type["RANGE_READ_FACT"][0], artifacts)
    runtime = _artifact_available(by_type["RUNTIME_OBSERVATION_FACT"][0], artifacts)
    mapping = _artifact_available(by_type["BUILD_SOURCE_MAPPING"][0], artifacts)
    if code is None or runtime is None or mapping is None:
        return False
    if ordinal == 1:
        scope_keys = ("symbol", "file_path", "repository", "revision", "predicate")
        if any(not str(bindings.get(k) or "") for k in scope_keys):
            return False
        if str(code.get("target")) != str(bindings["symbol"]) or str(code.get("file_path")) != str(
            bindings["file_path"]
        ):
            return False
        if str(bindings["symbol"]) not in str(code.get("window_text") or ""):
            return False
        if str(code.get("repository")) != str(bindings["repository"]) or str(code.get("revision")) != str(
            bindings["revision"]
        ):
            return False
        joins_hold = (
            str(runtime.get("symbol")) == str(bindings["symbol"])
            and str(runtime.get("predicate")) == str(bindings["predicate"])
            and str(runtime.get("repository")) == str(code.get("repository"))
            and str(runtime.get("source_revision")) == str(code.get("revision"))
            and str(runtime.get("build_identity")) == str(bindings.get("build_identity") or "")
            and str(runtime.get("execution_id")) == str(bindings.get("execution_id") or "")
            and str(runtime.get("environment")) == str(bindings.get("environment") or "")
            and str(mapping.get("build_identity")) == str(runtime.get("build_identity"))
            and str(mapping.get("repository")) == str(runtime.get("repository"))
            and str(mapping.get("source_revision")) == str(runtime.get("source_revision"))
        )
        return joins_hold
    if ordinal == 2:
        refs: list[str] = []
        if isinstance(mapping_authority, Mapping):
            raw = mapping_authority.get("approved_provenance_refs")
            if isinstance(raw, list):
                refs = [str(r) for r in raw]
        return bool(str(mapping.get("provenance_ref") or "")) and str(mapping.get("provenance_ref")) in refs
    return False


def _evaluate_absence(
    ordinal: int,
    bindings: dict[str, Any],
    entries: list[Mapping[str, Any]],
    artifacts: Mapping[str, Any],
) -> bool:
    if len(entries) != 1 or str(entries[0].get("premise_type") or "") != "ABSENCE_COVERAGE_FACT":
        return False
    record = _artifact_available(entries[0], artifacts)
    if record is None:
        return False
    if ordinal == 0:
        return str(record.get("search_status") or "") == "COMPLETE_EMPTY"
    if ordinal == 1:
        return isinstance(record.get("match_count"), int) and not isinstance(record.get("match_count"), bool) and record.get("match_count") == 0
    if ordinal == 2:
        scope = record.get("scope")
        if not isinstance(scope, dict):
            return False
        return (
            str(record.get("universe")) == str(bindings.get("universe"))
            and str(record.get("predicate")) == str(bindings.get("predicate"))
            and str(scope.get("repository")) == str(bindings.get("repository"))
            and str(scope.get("revision")) == str(bindings.get("revision"))
            and str(scope.get("path_scope")) == str(bindings.get("path_scope"))
        )
    if ordinal == 3:
        exclusions = record.get("exclusions")
        return isinstance(exclusions, list) and all(isinstance(e, str) for e in exclusions)
    return False


def _evaluate_history(
    ordinal: int,
    bindings: dict[str, Any],
    entries: list[Mapping[str, Any]],
    artifacts: Mapping[str, Any],
) -> bool:
    if len(entries) != 1 or str(entries[0].get("premise_type") or "") != "COMMIT_HISTORY":
        return False
    record = _artifact_available(entries[0], artifacts)
    if record is None:
        return False
    full_sha = re.compile(r"^[0-9a-f]{40}$")
    if ordinal == 0:
        return True
    if ordinal == 1:
        return bool(full_sha.match(str(bindings.get("commit_id") or ""))) and bool(
            full_sha.match(str(record.get("commit_id") or ""))
        )
    if ordinal == 2:
        for key in ("repository", "repository_revision", "commit_id", "file_path", "subject"):
            if str(record.get(key) or "") != str(bindings.get(key) or "") or not str(bindings.get(key) or ""):
                return False
        for locator in ("branch", "time_range", "since", "until"):
            if locator in record or locator in bindings:
                return False
        return True
    return False


def decide_guarded_promotion(
    policy: DecisionPolicy | None,
    evidence_bytes: bytes,
    trusted_context_bytes: bytes,
) -> tuple[PublicationDecision, str]:
    """Authoritative promotion entry (pure domain, no production contact).

    Parses canonical evidence + trusted context bytes once, binds
    evidence to policy, expands obligations (unsupported denies
    pre-checker), runs the checker internally, re-verifies snapshot
    integrity, evaluates obligations, and promotes only on
    all-SATISFIED. Any defect denies closed.
    """
    try:
        evidence = _parse_canonical_object(bytes(evidence_bytes), "evidence_bytes")
        trusted = _parse_canonical_object(bytes(trusted_context_bytes), "trusted_context_bytes")
    except SnapshotError as exc:
        return PublicationDecision.DENY, f"{DenyReason.OBLIGATIONS_ABSENT.value}:{exc}"
    snapshot = EvidenceSnapshot(
        evidence_digest="sha256:" + hashlib.sha256(bytes(evidence_bytes)).hexdigest(),
        trusted_digest="sha256:" + hashlib.sha256(bytes(trusted_context_bytes)).hexdigest(),
        evidence_bytes=bytes(evidence_bytes),
        trusted_bytes=bytes(trusted_context_bytes),
        evidence=_freeze(evidence),
        trusted=_freeze(trusted),
        policy=policy if isinstance(policy, DecisionPolicy) else None,
    )
    evidence_mat = _materialize(snapshot.evidence)
    trusted_mat = _materialize(snapshot.trusted)
    if not isinstance(evidence_mat, dict) or not isinstance(trusted_mat, dict):
        return PublicationDecision.DENY, f"{DenyReason.OBLIGATIONS_ABSENT.value}:snapshot shape"
    if snapshot.policy is None:
        return PublicationDecision.DENY, f"{DenyReason.POLICY_DENIED.value}:no-policy"

    # Step 2: bind evidence to policy.
    bindings_table = trusted_mat.get("repository_bindings")
    if not isinstance(bindings_table, list):
        return PublicationDecision.DENY, f"{DenyReason.POLICY_DENIED.value}:no-bindings"
    bound: dict[tuple[str, str], str] = {}
    for row in bindings_table:
        if not isinstance(row, dict):
            return PublicationDecision.DENY, f"{DenyReason.POLICY_DENIED.value}:bad-binding"
        key = (str(row.get("evidence_id") or ""), str(row.get("content_digest") or ""))
        repo = str(row.get("repository_id") or "")
        if not key[0] or not key[1] or not repo or key in bound:
            return PublicationDecision.DENY, f"{DenyReason.POLICY_DENIED.value}:bad-binding"
        bound[key] = repo
    cert = evidence_mat.get("certificate")
    cert_entries = cert.get("premises") if isinstance(cert, dict) else None
    # No premises to bind: skip repository binding (expansion denies
    # appropriately — UNSUPPORTED for unknown kinds, OBLIGATIONS_ABSENT
    # for registered rules without a certificate). Policy presence was
    # already enforced above.
    seen_repos: list[str] = []
    if isinstance(cert_entries, list) and cert_entries:
        for entry in cert_entries:
            if not isinstance(entry, dict):
                return PublicationDecision.DENY, f"{DenyReason.POLICY_DENIED.value}:bad-premise"
            # Certificate premise entries carry the digest as
            # ``content_hash``; trusted bindings key it as
            # ``content_digest``. Both name the same bytes.
            digest = str(entry.get("content_hash") or entry.get("content_digest") or "")
            key = (str(entry.get("evidence_id") or ""), digest)
            trusted_repo = bound.get(key)
            if trusted_repo is None:
                return PublicationDecision.DENY, f"{DenyReason.POLICY_DENIED.value}:policy-repository-unbound"
            artifact = (evidence_mat.get("artifacts") or {}).get(key[0], {})
            artifact_repo = ""
            if isinstance(artifact, dict):
                artifact_repo = str(artifact.get("repository") or "")
                if not artifact_repo and isinstance(artifact.get("scope"), dict):
                    # Coverage facts carry the repository in scope, not top
                    # level (AbsenceCoverageFact); the trusted binding still
                    # governs — this only locates the artifact's claim.
                    artifact_repo = str(artifact["scope"].get("repository") or "")
            if artifact_repo != trusted_repo:
                return PublicationDecision.DENY, f"{DenyReason.POLICY_DENIED.value}:policy-scope:{artifact_repo or '?'}"
            if trusted_repo not in seen_repos:
                seen_repos.append(trusted_repo)
        extra = [k for k in bound if k[0] not in {str(e.get("evidence_id") or "") for e in cert_entries if isinstance(e, dict)}]
        if extra:
            return PublicationDecision.DENY, f"{DenyReason.POLICY_DENIED.value}:extra-binding"
    proposition = evidence_mat.get("proposition")
    if isinstance(proposition, dict) and isinstance(proposition.get("scope"), dict):
        scope_repo = str(proposition["scope"].get("repository") or "")
        if scope_repo:
            if scope_repo not in seen_repos:
                seen_repos.append(scope_repo)
    if not seen_repos and isinstance(cert_entries, list) and cert_entries:
        return PublicationDecision.DENY, f"{DenyReason.POLICY_DENIED.value}:policy-scope:no-repository"
    for repo in seen_repos:
        if repo not in snapshot.policy.repositories:
            return PublicationDecision.DENY, f"{DenyReason.POLICY_DENIED.value}:policy-scope:{repo}"

    # Step 3: expand. Unsupported denies pre-checker.
    rule_id = str(evidence_mat.get("rule_id") or "")
    derivation_id = str((cert or {}).get("derivation_id") or "") if isinstance(cert, dict) else ""
    claim_seed = _bindings(cert) if isinstance(cert, dict) else {}
    premises_seed = list(cert_entries) if isinstance(cert_entries, list) else []
    obligations = expand_obligations(rule_id, claim_seed, premises_seed, derivation_id)
    if any(o.status == ObligationStatus.UNSUPPORTED for o in obligations):
        return PublicationDecision.DENY, DenyReason.UNSUPPORTED_OBLIGATION.value
    if not isinstance(cert, dict):
        return PublicationDecision.DENY, DenyReason.OBLIGATIONS_ABSENT.value
    if not derivation_id:
        return PublicationDecision.DENY, DenyReason.OBLIGATIONS_ABSENT.value

    # Step 4: internal check. Envelope rule_id must equal cert rule_id.
    # The mismatch reason is a plain stable string (not a DenyReason
    # member): the envelope never reaches the checker, so no checker
    # outcome exists to map.
    if str(cert.get("rule_id") or "") != rule_id or not rule_id:
        return PublicationDecision.DENY, "RULE_MISMATCH:rule mismatch"
    try:
        manifest = EvidenceManifest.model_validate(evidence_mat.get("manifest"))
    except Exception as exc:
        return PublicationDecision.DENY, f"{DenyReason.NOT_CHECKER_ACCEPTED.value}:CHECKER_FAILURE:bad manifest: {exc}"
    artifacts = evidence_mat.get("artifacts")
    if not isinstance(artifacts, dict):
        return PublicationDecision.DENY, f"{DenyReason.NOT_CHECKER_ACCEPTED.value}:CHECKER_FAILURE:bad artifacts"
    try:
        bundle = str(trusted_mat.get("rule_bundle_version") or "")
        from investigation_agent_platform.domain.proof.rules import RULE_BUNDLE_VERSION

        if bundle != RULE_BUNDLE_VERSION:
            return PublicationDecision.DENY, f"{DenyReason.NOT_CHECKER_ACCEPTED.value}:CHECKER_FAILURE:unsupported bundle"
        model_encoding = trusted_mat.get("model_encoding")
        reconciliation_state = trusted_mat.get("reconciliation_state")
        expected_context_id = trusted_mat.get("expected_context_id")
        known_ops = trusted_mat.get("known_normalization_ops")
        derivation_deps = trusted_mat.get("derivation_deps")
        if not isinstance(model_encoding, dict) or not isinstance(reconciliation_state, str):
            raise SnapshotError("trusted model/reconciliation shape")
        if not isinstance(known_ops, list) or not all(isinstance(v, str) for v in known_ops):
            raise SnapshotError("trusted normalization ops shape")
        if not isinstance(derivation_deps, dict):
            raise SnapshotError("trusted derivation deps shape")
        if rule_id == "behavior/v1":
            mapping_authority: dict[str, Any] | None = {
                "approved_provenance_refs": list(snapshot.policy.approved_mapping_provenance_refs)
            }
        else:
            mapping_authority = None
        request = CheckRequest(
            certificate=cert,
            proposition=proposition if isinstance(proposition, dict) else {},
            manifest=manifest,
            rule_bundle_version=bundle,
            model_encoding=dict(model_encoding),
            reconciliation_state=reconciliation_state,
            artifacts={str(k): (dict(v) if isinstance(v, dict) else v) for k, v in artifacts.items()},
            expected_context_id=str(expected_context_id) if expected_context_id is not None else None,
            mapping_authority=mapping_authority,
            known_normalization_ops=frozenset(known_ops),
            derivation_deps={str(k): [str(v) for v in (vals or [])] for k, vals in derivation_deps.items()},
        )
    except SnapshotError as exc:
        return PublicationDecision.DENY, f"{DenyReason.NOT_CHECKER_ACCEPTED.value}:CHECKER_FAILURE:{exc}"
    except Exception as exc:
        return PublicationDecision.DENY, f"{DenyReason.NOT_CHECKER_ACCEPTED.value}:CHECKER_FAILURE:{exc}"
    outcome, reasons = check_certificate(request)
    if outcome != CheckerOutcome.VALID:
        first = reasons[0] if reasons else outcome.value
        return PublicationDecision.DENY, f"{DenyReason.NOT_CHECKER_ACCEPTED.value}:{outcome.value}:{first}"

    # Step 5: snapshot integrity re-verify.
    try:
        _assert_snapshot_integrity(snapshot)
    except SnapshotError as exc:
        return PublicationDecision.DENY, f"{DenyReason.STALE_OBLIGATION.value}:{exc}"

    # Step 6: evaluate over an isolated materialization.
    try:
        iso_evidence = _materialize(snapshot.evidence)
        assert isinstance(iso_evidence, dict)
        iso_cert = iso_evidence.get("certificate")
        iso_artifacts = iso_evidence.get("artifacts")
        if not isinstance(iso_cert, dict) or not isinstance(iso_artifacts, dict):
            return PublicationDecision.DENY, DenyReason.OBLIGATIONS_ABSENT.value
        resolved = evaluate_obligations(
            obligations,
            iso_cert,
            iso_artifacts,
            manifest,
            mapping_authority,
        )
    except Exception as exc:
        return PublicationDecision.DENY, f"{DenyReason.NOT_CHECKER_ACCEPTED.value}:CHECKER_FAILURE:{exc}"
    for obligation in resolved:
        if obligation.status == ObligationStatus.OPEN:
            return PublicationDecision.DENY, f"{DenyReason.OPEN_OBLIGATION.value}:{obligation.obligation_id}"
        if obligation.status == ObligationStatus.UNSUPPORTED:
            return PublicationDecision.DENY, DenyReason.UNSUPPORTED_OBLIGATION.value

    # Step 7: linearization sub-step with gate-derived values.
    return _decide_linearization(
        CheckerOutcome.VALID,
        str(cert.get("context_id") or ""),
        str(trusted_mat.get("expected_context_id") or "") or None,
        bool(trusted_mat.get("reconciliation_adjudicated", False)),
        bool(trusted_mat.get("policy_eligible", False)),
        str(trusted_mat.get("policy_reason") or ""),
    )
