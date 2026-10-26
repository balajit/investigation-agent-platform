# src/investigation_agent_platform/domain/proof/checker_semantics.py
"""Independent checker semantics (checker/v1): separate from producers.

Each ``check_*_semantics`` function re-derives a semantic verdict from
the certificate, canonical proposition, evidence manifest, and available
artifact bytes alone. This module never imports ``rules.py`` or
``certificate.py`` and never calls ``apply_rule``, ``_VALIDATORS``, or
any producer validator: a producer defect (including one that always
discharges) cannot change a checker verdict.

Deliberate, documented duplication lives here (independence over reuse):

- ``_artifact_hash`` duplicates the ``sha256:``-over-canonical-bytes
  wrapper: the shared primitive is ``canonical_dict_bytes`` (explicitly
  allowed), but the checker recomputes the binding with its own code.
- ``_rebuild_input`` duplicates the validator-input reconstruction shape
  (certified type + artifact record with ``window_text``/``call_text``
  aliases). The shape is mechanical; all semantic judgment is local.
- Pydantic parsing schemas (``premises.py``), closed enums, and
  ``canonical_dict_bytes`` are shared trusted primitives per the design.

Outcome precedence follows the frozen D4a contract: missing proof
inputs before malformed ones, malformed before unsupported headers,
unsupported headers before invalid bindings, invalid bindings before
unsupported external authorities. Checker exceptions and malformed
checker inputs map to ``CHECKER_FAILURE``; missing or hash-mismatched
artifacts map to ``PREMISE_UNAVAILABLE`` (never a negative proof).

The mapping-authority contract (checker-owned): ``mapping_authority``
is a mapping with an ``approved_provenance_refs`` list of strings. A
mapping premise discharges only when its ``provenance_ref`` is a
member. ``None``, a missing key, or an empty list abstains every
BEHAVIOR candidate. The producer side enforces the same rule through
its closed allowlist; differential tests pin their agreement on the
test-only synthetic authority.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from investigation_agent_platform.domain.proof.canonical import (
    canonical_dict_bytes,
    v2_unknown_scope_keys,
    v2_unknown_target_keys,
)
from investigation_agent_platform.domain.proof.manifest import EvidenceManifest
from investigation_agent_platform.domain.proof.outcomes import CheckerOutcome
from investigation_agent_platform.domain.proof.premises import (
    AbsenceCoverageFact,
    BuildSourceMapping,
    CommitHistory,
    NormalizedCallsiteFact,
    RangeReadFact,
    RuntimeObservationFact,
)


def _artifact_hash(artifact: Mapping[str, Any]) -> str:
    """Content hash of a premise artifact (independent recomputation)."""
    return "sha256:" + hashlib.sha256(canonical_dict_bytes(dict(artifact or {}))).hexdigest()


def _rebuild_input(entry: Mapping[str, Any], artifact: Mapping[str, Any]) -> dict[str, Any]:
    """Rebuild the semantic input: certified envelope + artifact record.

    The certified evidence_id, premise_type, and content_hash ride
    alongside the artifact bytes (the hash was verified against those
    bytes first, so binding the certified hash into the parsed record
    is sound). Strict models require all three envelope fields.
    """
    merged = dict(artifact or {})
    merged["premise_type"] = entry.get("premise_type")
    merged["evidence_id"] = entry.get("evidence_id")
    merged["content_hash"] = entry.get("content_hash")
    if "window_text" not in merged and isinstance(merged.get("text"), str):
        merged["window_text"] = merged["text"]
    if "call_text" not in merged and isinstance(merged.get("text"), str):
        merged["call_text"] = merged["text"]
    return merged


def _reject_unknown_v2_keys(
    proposition: Mapping[str, Any], rule_id: str
) -> tuple[CheckerOutcome, str] | None:
    """Reject proposition fields outside the rule's closed contract.

    Independent re-derivation of the rule allowlist check: target and
    scope keys outside the rule contract are inadmissible, never
    silently ignored.
    """
    if rule_id not in ("behavior/v1", "absence/v1", "history/v1"):
        return None
    proposition = proposition if isinstance(proposition, dict) else {}
    target = proposition.get("target")
    scope = proposition.get("scope")
    unknown = v2_unknown_target_keys(rule_id, target) + v2_unknown_scope_keys(rule_id, scope)
    if unknown:
        return CheckerOutcome.INADMISSIBLE_PREMISE, f"fields outside the {rule_id} contract: {unknown}"
    return None


def _as_dict(value: Any) -> dict[str, Any]:
    """Coerce a mapping to a plain dict; anything else becomes {}."""
    return dict(value) if isinstance(value, dict) else {}


def _entry_list(certificate: Mapping[str, Any]) -> list[Mapping[str, Any]] | None:
    premises = certificate.get("premises")
    if not isinstance(premises, list) or not premises:
        return None
    if any(not isinstance(p, Mapping) for p in premises):
        return None
    return list(premises)


def _parse(model: Any, record: Mapping[str, Any]) -> Any | None:
    try:
        return model.model_validate(dict(record))
    except ValidationError:
        return None


def _check_artifact_binding(
    entry: Mapping[str, Any], artifacts: Mapping[str, Mapping[str, Any]]
) -> tuple[dict[str, Any] | None, tuple[CheckerOutcome, str] | None]:
    """Verify artifact availability and hash binding for one premise."""
    pid = str(entry.get("evidence_id") or "")
    artifact = artifacts.get(pid)
    if not isinstance(artifact, dict):
        return None, (CheckerOutcome.PREMISE_UNAVAILABLE, f"artifact unavailable: {pid}")
    if _artifact_hash(artifact) != str(entry.get("content_hash") or ""):
        return None, (CheckerOutcome.PREMISE_UNAVAILABLE, f"content-hash mismatch: {pid}")
    return _rebuild_input(entry, artifact), None


def _duplicate_evidence_id(entries: list[Mapping[str, Any]]) -> str | None:
    seen: set[str] = set()
    for entry in entries:
        pid = str(entry.get("evidence_id") or "")
        if pid and pid in seen:
            return pid
        seen.add(pid)
    return None


def _cert_scope(certificate: Mapping[str, Any]) -> dict[str, Any]:
    scope = certificate.get("scope")
    return dict(scope) if isinstance(scope, dict) else {}


def _revision_scope_check(
    certificate: Mapping[str, Any], entries: list[Mapping[str, Any]]
) -> tuple[CheckerOutcome, str] | None:
    """Independent K11: every premise scope revision equals the certificate one."""
    scope = _cert_scope(certificate)
    if "revision" not in scope:
        return None
    for entry in entries:
        pscope = entry.get("scope")
        if not isinstance(pscope, dict) or "revision" not in pscope:
            return CheckerOutcome.SCOPE_MISMATCH, "premise scope missing revision (K11)"
        if str(pscope["revision"]) != str(scope["revision"]):
            return CheckerOutcome.SCOPE_MISMATCH, "premise/proposition scope revision differs (K11)"
    return None


def _scalar_equal(first: Any, second: Any) -> bool:
    if isinstance(first, bool) or isinstance(second, bool):
        return isinstance(first, bool) and isinstance(second, bool) and first == second
    return type(first) is type(second) and first == second


def check_definition_semantics(
    certificate: Mapping[str, Any],
    proposition: Mapping[str, Any],
    manifest: EvidenceManifest,
    artifacts: Mapping[str, Mapping[str, Any]],
    mapping_authority: Mapping[str, Any] | None = None,
) -> tuple[CheckerOutcome, str]:
    """Independent DEFINITION semantics (definition/v1)."""
    _ = manifest
    _ = mapping_authority
    proposition = proposition if isinstance(proposition, dict) else {}
    form = proposition.get("form")
    if form not in (None, "EXISTENCE"):
        return CheckerOutcome.UNSUPPORTED_CONSTRUCT, "definition needs EXISTENCE header"
    if proposition.get("quantifier") != "EXISTENTIAL":
        return CheckerOutcome.UNSUPPORTED_CONSTRUCT, "definition needs EXISTENTIAL quantifier"
    entries = _entry_list(certificate)
    if entries is None:
        return CheckerOutcome.MISSING_PREMISE, "certificate carries no premises"
    if len(entries) != 1:
        return CheckerOutcome.MISSING_PREMISE, "definition needs exactly one premise"
    entry = entries[0]
    if entry.get("premise_type") != "RANGE_READ_FACT":
        return CheckerOutcome.INADMISSIBLE_PREMISE, "definition needs RANGE_READ premise"
    record, failure = _check_artifact_binding(entry, artifacts)
    if failure is not None:
        return failure
    assert record is not None
    premise = _parse(RangeReadFact, record)
    if premise is None:
        return CheckerOutcome.INADMISSIBLE_PREMISE, "definition premise fails strict contract"
    target = _as_dict(proposition.get("target"))
    symbol = str(target.get("symbol") or "")
    if not symbol:
        return CheckerOutcome.MISSING_PREMISE, "definition target names no symbol"
    if not premise.file_path:
        return CheckerOutcome.INADMISSIBLE_PREMISE, "definition premise cites no file"
    if symbol not in (premise.window_text or ""):
        return CheckerOutcome.INVALID_INFERENCE, "window does not contain the asserted symbol"
    scope_failure = _revision_scope_check(certificate, entries)
    if scope_failure is not None:
        return scope_failure
    return CheckerOutcome.VALID, "range read shows the defined symbol"


def check_call_edge_semantics(
    certificate: Mapping[str, Any],
    proposition: Mapping[str, Any],
    manifest: EvidenceManifest,
    artifacts: Mapping[str, Mapping[str, Any]],
    mapping_authority: Mapping[str, Any] | None = None,
) -> tuple[CheckerOutcome, str]:
    """Independent CALL_EDGE semantics (call_edge/v1)."""
    _ = manifest
    _ = mapping_authority
    proposition = proposition if isinstance(proposition, dict) else {}
    form = proposition.get("form")
    if form not in (None, "EXISTENCE"):
        return CheckerOutcome.UNSUPPORTED_CONSTRUCT, "call edge needs EXISTENCE header"
    if proposition.get("quantifier") != "EXISTENTIAL":
        return CheckerOutcome.UNSUPPORTED_CONSTRUCT, "call edge needs EXISTENTIAL quantifier"
    entries = _entry_list(certificate)
    if entries is None:
        return CheckerOutcome.MISSING_PREMISE, "certificate carries no premises"
    for entry in entries:
        if entry.get("premise_type") != "NORMALIZED_CALLSITE_FACT":
            return CheckerOutcome.INADMISSIBLE_PREMISE, "call edge needs NORMALIZED_CALLSITE premises"
    dup = _duplicate_evidence_id(entries)
    if dup is not None:
        return CheckerOutcome.INADMISSIBLE_PREMISE, f"duplicate evidence id: {dup}"
    target = _as_dict(proposition.get("target"))
    caller = str(target.get("caller") or target.get("counterpart") or "")
    callee = str(target.get("callee") or target.get("symbol") or "")
    if not caller or not callee:
        return CheckerOutcome.MISSING_PREMISE, "call-edge target binds no caller/callee"
    for entry in entries:
        record, failure = _check_artifact_binding(entry, artifacts)
        if failure is not None:
            return failure
        assert record is not None
        premise = _parse(NormalizedCallsiteFact, record)
        if premise is None:
            return CheckerOutcome.INADMISSIBLE_PREMISE, "call-site premise fails strict contract"
        text = premise.call_text or ""
        if premise.caller == caller and premise.callee == callee and caller in text and callee in text:
            scope_failure = _revision_scope_check(certificate, entries)
            if scope_failure is not None:
                return scope_failure
            return CheckerOutcome.VALID, "normalized call site shows the asserted edge"
    return CheckerOutcome.INVALID_INFERENCE, "no premise binds the asserted caller/callee in cited text"


def check_behavior_semantics(
    certificate: Mapping[str, Any],
    proposition: Mapping[str, Any],
    manifest: EvidenceManifest,
    artifacts: Mapping[str, Mapping[str, Any]],
    mapping_authority: Mapping[str, Any] | None = None,
) -> tuple[CheckerOutcome, str]:
    """Independent BEHAVIOR semantics (behavior/v1)."""
    _ = manifest
    proposition = proposition if isinstance(proposition, dict) else {}
    if (
        proposition.get("form") != "OBSERVATION"
        or proposition.get("quantifier") != "OBSERVATIONAL"
        or proposition.get("relation") != "RUNTIME_OBSERVATION"
        or proposition.get("modality") != "RUNTIME"
    ):
        return CheckerOutcome.UNSUPPORTED_CONSTRUCT, "behavior needs OBSERVATION header"
    if proposition.get("completeness") is not None:
        return CheckerOutcome.UNSUPPORTED_CONSTRUCT, "unexpected behavior header value"
    contract_violation = _reject_unknown_v2_keys(proposition, "behavior/v1")
    if contract_violation is not None:
        return contract_violation
    asserted = proposition.get("asserted_value")
    if asserted is None:
        return CheckerOutcome.MISSING_PREMISE, "behavior proposition states no canonical asserted value"
    if isinstance(asserted, float):
        return CheckerOutcome.INADMISSIBLE_PREMISE, "behavior asserted value forbids floats (canonical scalar only)"
    entries = _entry_list(certificate)
    if entries is None:
        return CheckerOutcome.MISSING_PREMISE, "certificate carries no premises"
    by_type: dict[str, list[Mapping[str, Any]]] = {}
    for entry in entries:
        by_type.setdefault(str(entry.get("premise_type") or ""), []).append(entry)
    codes = by_type.get("RANGE_READ_FACT", [])
    runtimes = by_type.get("RUNTIME_OBSERVATION_FACT", [])
    mappings = by_type.get("BUILD_SOURCE_MAPPING", [])
    if not codes or not runtimes or not mappings:
        return CheckerOutcome.MISSING_PREMISE, "behavior needs code, runtime, and mapping premises"
    if len(codes) > 1 or len(runtimes) > 1 or len(mappings) > 1 or len(entries) != 3:
        return CheckerOutcome.INADMISSIBLE_PREMISE, "behavior takes exactly one code, runtime, and mapping premise"
    dup = _duplicate_evidence_id(entries)
    if dup is not None:
        return CheckerOutcome.INADMISSIBLE_PREMISE, f"duplicate evidence id: {dup}"
    records: dict[str, Any] = {}
    for entry in codes + runtimes + mappings:
        record, failure = _check_artifact_binding(entry, artifacts)
        if failure is not None:
            return failure
        assert record is not None
        records[str(entry.get("premise_type") or "")] = record
    code = _parse(RangeReadFact, records["RANGE_READ_FACT"])
    runtime = _parse(RuntimeObservationFact, records["RUNTIME_OBSERVATION_FACT"])
    mapping = _parse(BuildSourceMapping, records["BUILD_SOURCE_MAPPING"])
    if code is None or runtime is None or mapping is None:
        return CheckerOutcome.INADMISSIBLE_PREMISE, "behavior premise fails strict contract"
    target = _as_dict(proposition.get("target"))
    scope = _as_dict(proposition.get("scope"))
    if code.target != str(target.get("symbol") or "") or code.file_path != str(target.get("file_path") or ""):
        return CheckerOutcome.INVALID_INFERENCE, "code premise does not show the asserted target"
    if str(target.get("symbol") or "") not in (code.window_text or ""):
        return CheckerOutcome.INVALID_INFERENCE, "code window does not show the asserted target"
    if code.repository != str(scope.get("repository") or "") or code.revision != str(scope.get("revision") or ""):
        return CheckerOutcome.INVALID_INFERENCE, "code source differs from claim scope"
    asserted = proposition.get("asserted_value")
    if (
        runtime.symbol != str(target.get("symbol") or "")
        or runtime.predicate != str(proposition.get("predicate") or "")
        or not _scalar_equal(runtime.observed_value, asserted)
    ):
        return CheckerOutcome.INVALID_INFERENCE, "runtime observation does not establish the asserted behavior"
    if (
        runtime.repository != code.repository
        or runtime.source_revision != code.revision
        or runtime.build_identity != str(scope.get("build_identity") or "")
        or runtime.execution_id != str(scope.get("execution_id") or "")
        or runtime.environment != str(scope.get("environment") or "")
    ):
        return CheckerOutcome.INVALID_INFERENCE, "runtime scope does not join the cited source"
    if (
        mapping.build_identity != runtime.build_identity
        or mapping.repository != runtime.repository
        or mapping.source_revision != runtime.source_revision
    ):
        return CheckerOutcome.INVALID_INFERENCE, "mapping does not bind the observed build to the cited source"
    approved = []
    if isinstance(mapping_authority, Mapping):
        refs = mapping_authority.get("approved_provenance_refs")
        if isinstance(refs, list):
            approved = [str(r) for r in refs]
    if mapping.provenance_ref not in approved:
        return CheckerOutcome.UNSUPPORTED_CONSTRUCT, "build-source mapping authority not approved"
    scope_failure = _revision_scope_check(certificate, entries)
    if scope_failure is not None:
        return scope_failure
    return CheckerOutcome.VALID, "code, runtime, and attested mapping join on the asserted behavior"


def check_absence_semantics(
    certificate: Mapping[str, Any],
    proposition: Mapping[str, Any],
    manifest: EvidenceManifest,
    artifacts: Mapping[str, Mapping[str, Any]],
    mapping_authority: Mapping[str, Any] | None = None,
) -> tuple[CheckerOutcome, str]:
    """Independent ABSENCE semantics (absence/v1)."""
    _ = mapping_authority
    proposition = proposition if isinstance(proposition, dict) else {}
    if (
        proposition.get("form") != "ABSENCE"
        or proposition.get("quantifier") != "UNIVERSAL"
        or proposition.get("modality") != "STATIC"
        or proposition.get("completeness") != "CLOSED_WORLD"
    ):
        return CheckerOutcome.UNSUPPORTED_CONSTRUCT, "absence needs ABSENCE/UNIVERSAL/STATIC/CLOSED_WORLD header"
    if proposition.get("relation") is not None:
        return CheckerOutcome.UNSUPPORTED_CONSTRUCT, "unexpected absence header value"
    contract_violation = _reject_unknown_v2_keys(proposition, "absence/v1")
    if contract_violation is not None:
        return contract_violation
    entries = _entry_list(certificate)
    if entries is None:
        return CheckerOutcome.MISSING_PREMISE, "certificate carries no premises"
    if len(entries) != 1 or entries[0].get("premise_type") != "ABSENCE_COVERAGE_FACT":
        if not entries:
            return CheckerOutcome.MISSING_PREMISE, "absence needs exactly one coverage premise"
        return CheckerOutcome.INADMISSIBLE_PREMISE, "absence takes no match-result premises"
    dup = _duplicate_evidence_id(entries)
    if dup is not None:
        return CheckerOutcome.INADMISSIBLE_PREMISE, f"duplicate evidence id: {dup}"
    record, failure = _check_artifact_binding(entries[0], artifacts)
    if failure is not None:
        return failure
    assert record is not None
    coverage = _parse(AbsenceCoverageFact, record)
    if coverage is None:
        return CheckerOutcome.INADMISSIBLE_PREMISE, "coverage premise fails strict contract"
    if not isinstance(manifest, EvidenceManifest):
        return CheckerOutcome.MISSING_PREMISE, "absence needs manifest binding"
    if coverage.evidence_id not in list(manifest.evidence_ids or []):
        return CheckerOutcome.MISSING_PREMISE, "coverage id absent from manifest"
    if (manifest.content_hashes or {}).get(coverage.evidence_id) != coverage.content_hash:
        return CheckerOutcome.PREMISE_UNAVAILABLE, "coverage artifact hash mismatch"
    if (manifest.premise_types or {}).get(coverage.evidence_id) != "ABSENCE_COVERAGE_FACT":
        return CheckerOutcome.INADMISSIBLE_PREMISE, "coverage id bound to another premise type"
    if coverage.search_status != "COMPLETE_EMPTY":
        return CheckerOutcome.UNSUPPORTED_CONSTRUCT, "only complete-empty searches support absence"
    if coverage.match_count != 0:
        return CheckerOutcome.INVALID_INFERENCE, "nonzero match contradicts absence"
    target = _as_dict(proposition.get("target"))
    scope = _as_dict(proposition.get("scope"))
    if coverage.universe != str(target.get("universe") or "") or coverage.predicate != str(
        proposition.get("predicate") or ""
    ):
        return CheckerOutcome.INVALID_INFERENCE, "coverage does not address the claimed predicate"
    if (
        coverage.scope.repository != str(scope.get("repository") or "")
        or coverage.scope.revision != str(scope.get("revision") or "")
        or coverage.scope.path_scope != str(scope.get("path_scope") or "")
    ):
        return CheckerOutcome.INVALID_INFERENCE, "coverage scope differs from proposition scope"
    retrieval = manifest.retrieval_config or {}
    if not isinstance(retrieval, dict):
        return CheckerOutcome.INVALID_INFERENCE, "retrieval configuration malformed"
    if retrieval.get("search_config_digest") != coverage.search_config_digest:
        return CheckerOutcome.INVALID_INFERENCE, "coverage search config differs from retrieval config"
    if retrieval.get("scope") != coverage.universe:
        return CheckerOutcome.INVALID_INFERENCE, "retrieval scope differs from coverage universe"
    if list(manifest.exclusions or []) != list(coverage.exclusions):
        return CheckerOutcome.INVALID_INFERENCE, "manifest and coverage exclusions differ"
    for exclusion in coverage.exclusions:
        exc = str(exclusion or "").rstrip("/")
        path = str(coverage.scope.path_scope or "").rstrip("/")
        if exc and (exc == path or path.startswith(exc + "/") or exc in coverage.universe):
            return CheckerOutcome.UNSUPPORTED_CONSTRUCT, "relevant scope excluded from the universe"
    return CheckerOutcome.VALID, "complete-empty coverage establishes absence in the universe"


def check_history_semantics(
    certificate: Mapping[str, Any],
    proposition: Mapping[str, Any],
    manifest: EvidenceManifest,
    artifacts: Mapping[str, Mapping[str, Any]],
    mapping_authority: Mapping[str, Any] | None = None,
) -> tuple[CheckerOutcome, str]:
    """Independent HISTORY semantics (history/v1)."""
    _ = manifest
    _ = mapping_authority
    proposition = proposition if isinstance(proposition, dict) else {}
    if (
        proposition.get("form") != "HISTORICAL"
        or proposition.get("quantifier") != "EXISTENTIAL"
        or proposition.get("modality") != "HISTORY"
    ):
        return CheckerOutcome.UNSUPPORTED_CONSTRUCT, "history needs HISTORICAL/EXISTENTIAL/HISTORY header"
    present = {k: proposition[k] for k in ("relation", "completeness") if k in proposition}
    if present:
        if any(v is None for v in present.values()):
            return CheckerOutcome.INVALID_INFERENCE, "optional history headers must be omitted, not null"
        return CheckerOutcome.UNSUPPORTED_CONSTRUCT, "unexpected history header value"
    contract_violation = _reject_unknown_v2_keys(proposition, "history/v1")
    if contract_violation is not None:
        return contract_violation
    entries = _entry_list(certificate)
    if entries is None:
        return CheckerOutcome.MISSING_PREMISE, "certificate carries no premises"
    if len(entries) != 1 or entries[0].get("premise_type") != "COMMIT_HISTORY":
        if not [e for e in entries if e.get("premise_type") == "COMMIT_HISTORY"]:
            return CheckerOutcome.MISSING_PREMISE, "history needs exactly one commit premise"
        return CheckerOutcome.INADMISSIBLE_PREMISE, "history takes exactly one commit premise"
    dup = _duplicate_evidence_id(entries)
    if dup is not None:
        return CheckerOutcome.INADMISSIBLE_PREMISE, f"duplicate evidence id: {dup}"
    record, failure = _check_artifact_binding(entries[0], artifacts)
    if failure is not None:
        return failure
    assert record is not None
    history = _parse(CommitHistory, record)
    if history is None:
        return CheckerOutcome.INADMISSIBLE_PREMISE, "history premise fails strict contract"
    target = _as_dict(proposition.get("target"))
    scope = _as_dict(proposition.get("scope"))
    if history.repository != str(target.get("repository") or ""):
        return CheckerOutcome.INVALID_INFERENCE, "history repository differs from target"
    if history.repository_revision != str(scope.get("repository_revision") or ""):
        return CheckerOutcome.INVALID_INFERENCE, "history query revision differs from proposition context"
    if history.commit_id != str(target.get("commit_id") or ""):
        return CheckerOutcome.INVALID_INFERENCE, "commit id does not match the target commit"
    if history.file_path != str(target.get("file_path") or ""):
        return CheckerOutcome.INVALID_INFERENCE, "history file differs from target"
    if history.subject != str(target.get("subject") or ""):
        return CheckerOutcome.INVALID_INFERENCE, "history subject differs from target"
    return CheckerOutcome.VALID, "commit history exactly binds the claimed change"
