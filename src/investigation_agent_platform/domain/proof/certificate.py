# src/investigation_agent_platform/domain/proof/certificate.py
"""WP-B4 certificate serializer + independent checker (cert/v2).

``certify()`` serializes a manifest-bound derivation into a
schema-valid certificate (``tests/fixtures/proof_kernel/cert_schema_v2.json``).
``check_certificate()`` accepts, rejects, or replays from the schema +
registry alone: it recomputes every ID it can derive and re-derives
semantic verdicts through the independent checker semantics, enforcing
coverage, scope, cycle, and freshness gates. Producer-supplied ``validation``
and ``proof_classification`` fields are ignored — the checker's own
verdict is the only acceptance evidence.

Independence note: the checker shares the frozen registry, canonical
primitives, and Pydantic parsing schemas (the contracts, not engine
state) but runs a separate decision path with no evaluator internals,
no shared mutable state, no workflow access, and — since Component 2 —
no producer validators: semantic verdicts come from
``checker_semantics.py`` alone (``_CHECKER_SEMANTICS`` dispatch).
``certify()`` still runs the producer validator first and refuses to
serialize a non-discharging application.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.domain.proof.canonical import (
    context_id,
    derivation_id,
    proposition_id,
)
from investigation_agent_platform.domain.proof.checker_semantics import (
    check_absence_semantics,
    check_behavior_semantics,
    check_call_edge_semantics,
    check_definition_semantics,
    check_history_semantics,
)
from investigation_agent_platform.domain.proof.evaluation import EvaluationInputs
from investigation_agent_platform.domain.proof.manifest import (
    EvidenceManifest,
    ManifestCoverage,
    manifest_digest,
)
from investigation_agent_platform.domain.proof.outcomes import CheckerOutcome
from investigation_agent_platform.domain.proof.rules import (
    RULE_BUNDLE_VERSION,
    RuleOutcome,
    Soundness,
    apply_rule,
    get_rule,
)

CERT_SCHEMA_VERSION = "cert/v2"

__all__ = [
    "CERT_SCHEMA_VERSION",
    "CertificationError",
    "CheckRequest",
    "CheckerOutcome",
    "certify",
    "check_certificate",
]
"""Public checker surface (the re-exported CheckerOutcome keeps current importers working)."""

_CHECKER_SEMANTICS = {
    "definition/v1": check_definition_semantics,
    "call_edge/v1": check_call_edge_semantics,
    "behavior/v1": check_behavior_semantics,
    "absence/v1": check_absence_semantics,
    "history/v1": check_history_semantics,
}
"""Independent semantic entry points by rule (never producer validators)."""


class CertificationError(ValueError):
    """Serializer refusal carrying a stable code."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code)
        super().__init__(f"{self.code}: {detail}" if detail else self.code)


_MANIFEST_COVERAGE_TO_CERT = {
    ManifestCoverage.COMPLETE_EMPTY: "COMPLETE_PATH",
    ManifestCoverage.MATCHES_FOUND: "COMPLETE_PATH",
    ManifestCoverage.INCOMPLETE: "INCOMPLETE",
    ManifestCoverage.FAILED: "FAILED",
}


def _certificate_coverage(
    proposition: dict[str, Any], manifest: EvidenceManifest, universe: str | None
) -> dict[str, Any] | None:
    """Coverage block: present only for absence/universal claims (schema).

    Absence/universal claims MUST carry coverage; without a stated
    universe they cannot be certified at all (fail closed, K3).
    """
    needs_coverage = proposition.get("form") == "ABSENCE" or (
        proposition.get("quantifier") == "UNIVERSAL"
    )
    if not needs_coverage:
        return None
    resolved_universe = (
        universe or str((manifest.retrieval_config or {}).get("scope") or "").strip() or None
    )
    if not resolved_universe:
        raise CertificationError(
            "UNBOUNDED_ABSENCE", "absence/universal claims need a stated universe"
        )
    return {
        "exclusions": list(manifest.exclusions or []),
        "result": _MANIFEST_COVERAGE_TO_CERT[manifest.coverage],
        "universe": resolved_universe,
    }


def _claim_for_rule(proposition: dict[str, Any], rule_id: str) -> dict[str, Any]:
    """Validator claim from canonical header + rule-specific projection.

    Copies present header fields (form/quantifier/relation/modality/
    completeness/predicate/asserted_value, including explicit nulls so
    null-vs-omitted distinctions reach the validators), then projects
    the target and scope through the rule's closed key sets. For
    ``call_edge/v1`` the legacy ClaimTarget aliases apply (counterpart
    is the caller, symbol is the callee). Validators ignore keys they
    do not need; unknown keys never enter the claim because the
    projection only copies allowlisted fields.
    """
    from investigation_agent_platform.domain.proof.canonical import V2_SCOPE_KEYS, V2_TARGET_KEYS

    claim: dict[str, Any] = {}
    proposition = proposition if isinstance(proposition, dict) else {}
    for key in (
        "form",
        "quantifier",
        "relation",
        "modality",
        "completeness",
        "predicate",
        "asserted_value",
    ):
        if key in proposition:
            claim[key] = proposition[key]
    target = proposition.get("target")
    target = target if isinstance(target, dict) else {}
    for key in V2_TARGET_KEYS.get(str(rule_id or ""), frozenset()):
        if key in target:
            claim[key] = target[key]
    scope = proposition.get("scope")
    scope = scope if isinstance(scope, dict) else {}
    for key in V2_SCOPE_KEYS.get(str(rule_id or ""), frozenset()):
        if key in scope:
            claim[key] = scope[key]
    if str(rule_id or "") == "call_edge/v1":
        if "caller" not in claim and isinstance(target.get("counterpart"), str):
            if target["counterpart"].strip():
                claim["caller"] = target["counterpart"].strip()
        if "callee" not in claim and isinstance(target.get("symbol"), str):
            if target["symbol"].strip():
                claim["callee"] = target["symbol"].strip()
    return claim


def _artifact_hash(artifact: dict[str, Any]) -> str:
    """Content hash of a premise artifact: ``sha256:`` over canonical bytes.

    The artifact is the normalized premise record (file path, cited text,
    extracted names). The hash binds all of it: swapping a field without
    updating the manifest hash fails K10.
    """
    from investigation_agent_platform.domain.proof.canonical import canonical_dict_bytes

    return "sha256:" + hashlib.sha256(canonical_dict_bytes(dict(artifact or {}))).hexdigest()


def _entry_scope(rule_id: str, premise: dict[str, Any], artifact: dict[str, Any]) -> dict[str, Any]:
    """Premise-entry scope: explicit scope wins, else rule-specific synthesis.

    Component-1-style premise records carry no ``scope`` key; the entry
    scope is then synthesized from the artifact's revision-bearing
    fields so the K11 scope-revision gate can verify every premise
    against the certificate scope. v1 callers that already attach an
    explicit scope are unaffected.
    """
    scope = premise.get("scope")
    if isinstance(scope, dict) and scope:
        return dict(scope)
    artifact = artifact if isinstance(artifact, dict) else {}
    if rule_id in ("definition/v1", "call_edge/v1"):
        rev = artifact.get("revision")
    elif rule_id == "history/v1":
        rev = artifact.get("repository_revision")
    elif rule_id == "behavior/v1":
        rev = artifact.get("revision") or artifact.get("source_revision")
    elif rule_id == "absence/v1":
        ascope = artifact.get("scope")
        return dict(ascope) if isinstance(ascope, dict) else {}
    else:
        rev = None
    return {"revision": rev} if rev else {}


def _validator_premise(entry: dict[str, Any], artifact: dict[str, Any]) -> dict[str, Any]:
    """Rebuild the validator input: certified envelope + artifact record.

    The certified evidence_id, premise_type, and content_hash ride
    alongside the artifact bytes (certify() verifies the hash binding
    before rule application, so the envelope is sound here). Strict
    premise models require all three envelope fields.
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


def _premise_entry(
    premise: dict[str, Any], rule_id: str, artifact: dict[str, Any] | None
) -> dict[str, Any]:
    """Schema-shaped premise: evidence_id, content_hash, type, scope only.

    Semantic fields live in the artifact (bound by content_hash), never
    in the certificate — Layer 1 forbids them here, Layer 3 re-derives
    them from the hashed bytes.
    """
    return {
        "content_hash": premise.get("content_hash"),
        "evidence_id": str(premise.get("evidence_id") or ""),
        "premise_type": premise.get("premise_type"),
        "scope": _entry_scope(rule_id, premise, artifact or {}),
    }


def certify(
    inputs: EvaluationInputs,
    premises: list[dict[str, Any]],
    artifacts: Mapping[str, dict[str, Any]],
    universe: str | None = None,
) -> dict[str, Any]:
    """Serialize a manifest-bound derivation into a schema-valid certificate.

    Premises carry ``evidence_id`` + ``content_hash`` + ``premise_type`` +
    ``scope``; semantic fields live in ``artifacts`` (bound by content
    hash). certify runs the registry validator first and refuses to
    certify a non-discharging application; it also refuses artifact bytes
    that do not match the manifest hashes. Producer ``validation`` is
    always REJECT (never self-accept) and ``proof_classification`` mirrors
    the registry — both ignored by the checker.
    """
    from investigation_agent_platform.domain.proof.evaluation import evaluate

    proposition = inputs.proposition
    manifest = inputs.manifest
    rule = get_rule(inputs.rule_id)
    if inputs.rule_bundle_version != RULE_BUNDLE_VERSION:
        raise CertificationError(
            "UNSUPPORTED_BUNDLE",
            f"rule bundle must be {RULE_BUNDLE_VERSION}: {inputs.rule_bundle_version!r}",
        )
    claim = _claim_for_rule(proposition, rule.rule_id)
    artifacts = dict(artifacts or {})
    validator_inputs = []
    for premise in premises or []:
        if not isinstance(premise, dict):
            raise CertificationError("BAD_PREMISE", "premise must be an object")
        pid = str(premise.get("evidence_id") or "")
        artifact = artifacts.get(pid)
        if not isinstance(artifact, dict):
            raise CertificationError("PREMISE_UNAVAILABLE", f"no artifact bytes: {pid}")
        if _artifact_hash(artifact) != str(premise.get("content_hash") or ""):
            raise CertificationError("PREMISE_UNAVAILABLE", f"artifact hash mismatch: {pid}")
        if _artifact_hash(artifact) != str(manifest.content_hashes.get(pid) or ""):
            raise CertificationError(
                "PREMISE_UNAVAILABLE", f"artifact not the manifested bytes: {pid}"
            )
        validator_inputs.append(_validator_premise(premise, artifact))
    outcome, reason = apply_rule(rule, claim, validator_inputs, manifest.model_dump(mode="json"))
    if outcome != RuleOutcome.DISCHARGED:
        raise CertificationError("RULE_NOT_DISCHARGED", reason)
    # Single construction, used twice: the derivation binds exactly the
    # inference the certificate carries, so recomputation always matches.
    # (Building these separately once caused a binding mismatch.)
    inference_instance = {"bindings": claim, "rule": rule.rule_id}
    # The derivation binds exactly the inference the certificate carries:
    # evaluate with the typed instance, so recomputation matches.
    derivation = evaluate(inputs.model_copy(update={"inference": inference_instance}))
    header_keys = (
        "predicate",
        "quantifier",
        "canon_version",
        "form",
        "relation",
        "modality",
        "completeness",
        "asserted_value",
    )
    header = {k: proposition[k] for k in header_keys if k in proposition}
    scope = proposition.get("scope")
    certificate = {
        "assumptions": sorted(proposition.get("assumptions") or []),
        "canonical_proposition": header,
        "context_id": derivation.context_id,
        "coverage": _certificate_coverage(proposition, manifest, universe),
        "derivation_id": derivation.derivation_id,
        "implementation_digest": rule.implementation_digest,
        "inference_instance": inference_instance,
        "input_manifest_digest": derivation.manifest_digest,
        "premises": [
            _premise_entry(p, rule.rule_id, artifacts.get(str(p.get("evidence_id") or "")))
            for p in premises
        ],
        "proof_classification": rule.soundness.value,
        "proposition_id": derivation.proposition_id,
        "rule_id": rule.rule_id,
        "rule_version": rule.rule_version,
        "schema_version": CERT_SCHEMA_VERSION,
        "scope": scope if isinstance(scope, dict) else {},
        "validation": {
            "reasons": ["producer must not self-accept; checker decides"],
            "result": "REJECT",
        },
    }
    return certificate


class CheckRequest(BaseModel):
    """Frozen checker inputs: the certificate plus everything it verifies.

    ``artifacts`` maps premise evidence ID to the normalized premise
    record actually on hand (file path, cited text, extracted names);
    each record is hashed and compared to the certified content hash.
    ``context`` carries the frozen evaluation-context fields (rule bundle
    version, model encoding, reconciliation state).
    ``expected_context_id`` pins freshness at publication time.
    ``mapping_authority`` carries the independently addressable
    build-to-source attestation contract for BEHAVIOR checks
    (``{"approved_provenance_refs": [...]}``); ``None`` abstains every
    behavior candidate. ``derivation_deps`` maps derivation IDs to the
    derivation IDs they depend on, for cycle detection (K2).
    """

    model_config = ConfigDict(frozen=True)

    certificate: dict[str, Any] = Field(...)
    proposition: dict[str, Any] = Field(...)
    manifest: EvidenceManifest = Field(...)
    rule_bundle_version: str = Field(..., max_length=64)
    model_encoding: dict[str, Any] = Field(default_factory=dict)
    reconciliation_state: str = Field(default="unreconciled", max_length=64)
    artifacts: dict[str, dict[str, Any]] = Field(default_factory=dict)
    expected_context_id: str | None = Field(default=None)
    mapping_authority: dict[str, Any] | None = Field(default=None)
    known_normalization_ops: frozenset[str] = Field(default_factory=frozenset)
    derivation_deps: dict[str, list[str]] = Field(default_factory=dict)


def _fail(outcome: CheckerOutcome, reason: str) -> tuple[CheckerOutcome, list[str]]:
    return outcome, [reason]


def _reachable(target: str, deps: Mapping[str, list[str]]) -> bool:
    """True when target transitively depends on itself (K2)."""
    seen = {target}
    stack = [target]
    while stack:
        current = stack.pop()
        for dep in deps.get(current, []) or []:
            if dep == target:
                return True
            if dep not in seen:
                seen.add(dep)
                stack.append(dep)
    return False


def check_certificate(request: CheckRequest) -> tuple[CheckerOutcome, list[str]]:
    """Accept, reject, or replay from schema + registry alone (pure).

    Deterministic: identical inputs always yield the identical verdict.
    Check order is fixed — schema, registry, identity, binding,
    availability, coverage-presence (absence/universal only, so a
    missing coverage block reports MISSING before any header verdict),
    semantics, rule-aware header admission, scope, cycle, freshness —
    and the first failure wins, so verdicts are attributable to exactly
    one cause.
    """
    cert = request.certificate if isinstance(request.certificate, dict) else {}
    if cert.get("schema_version") != CERT_SCHEMA_VERSION:
        return _fail(CheckerOutcome.CHECKER_FAILURE, "unknown schema_version")
    if request.rule_bundle_version != RULE_BUNDLE_VERSION:
        return _fail(
            CheckerOutcome.CHECKER_FAILURE,
            f"unsupported rule bundle version: {request.rule_bundle_version!r}",
        )
    required = (
        "proposition_id",
        "canonical_proposition",
        "rule_id",
        "rule_version",
        "implementation_digest",
        "premises",
        "inference_instance",
        "assumptions",
        "scope",
        "context_id",
        "input_manifest_digest",
        "derivation_id",
        "proof_classification",
        "validation",
    )
    missing = [k for k in required if k not in cert]
    if missing:
        return _fail(CheckerOutcome.CHECKER_FAILURE, f"missing fields: {missing}")
    try:
        rule = get_rule(str(cert.get("rule_id") or ""))
    except Exception:
        return _fail(
            CheckerOutcome.UNSUPPORTED_CONSTRUCT,
            f"unregistered rule: {cert.get('rule_id')!r}",
        )
    if rule.rule_version != str(cert.get("rule_version") or ""):
        return _fail(CheckerOutcome.INVALID_INFERENCE, "rule version mismatch")
    if rule.implementation_digest != str(cert.get("implementation_digest") or ""):
        return _fail(CheckerOutcome.INVALID_INFERENCE, "implementation digest mismatch (K9)")
    if rule.soundness == Soundness.HEURISTIC:
        return _fail(CheckerOutcome.UNSUPPORTED_CONSTRUCT, "heuristic rules never certify")
    if str(cert.get("proof_classification") or "") != rule.soundness.value:
        return _fail(CheckerOutcome.INVALID_INFERENCE, "proof_classification forgery")
    header = cert.get("canonical_proposition")
    if not isinstance(header, dict):
        return _fail(CheckerOutcome.CHECKER_FAILURE, "canonical_proposition malformed")
    proposition = request.proposition if isinstance(request.proposition, dict) else {}
    for key in (
        "predicate",
        "quantifier",
        "canon_version",
        "form",
        "relation",
        "modality",
        "completeness",
        "asserted_value",
    ):
        in_header = key in header
        in_proposition = key in proposition
        if in_header != in_proposition or (in_header and header.get(key) != proposition.get(key)):
            return _fail(CheckerOutcome.INVALID_INFERENCE, f"header/proposition mismatch: {key}")
    cert_assumptions = cert.get("assumptions")
    prop_assumptions = proposition.get("assumptions") or []
    if not isinstance(cert_assumptions, list) or sorted(cert_assumptions) != sorted(
        prop_assumptions
    ):
        return _fail(CheckerOutcome.INVALID_INFERENCE, "assumptions mismatch")
    try:
        recomputed_prop_id = proposition_id(proposition)
    except Exception as exc:
        return _fail(CheckerOutcome.INVALID_INFERENCE, f"proposition not canonical: {exc}")
    if recomputed_prop_id != str(cert.get("proposition_id") or ""):
        return _fail(CheckerOutcome.INVALID_INFERENCE, "proposition_id mismatch")
    if manifest_digest(request.manifest) != str(cert.get("input_manifest_digest") or ""):
        return _fail(CheckerOutcome.INVALID_INFERENCE, "input_manifest_digest mismatch")
    recomputed_ctx = context_id(
        recomputed_prop_id,
        {
            "evidence_manifest_digest": manifest_digest(request.manifest),
            "model_encoding": dict(request.model_encoding or {}),
            "proposition_scope": (
                dict(proposition["scope"]) if isinstance(proposition.get("scope"), dict) else {}
            ),
            "reconciliation_state": request.reconciliation_state,
            "rule_bundle_version": request.rule_bundle_version,
        },
    )
    if recomputed_ctx != str(cert.get("context_id") or ""):
        return _fail(CheckerOutcome.INVALID_INFERENCE, "context_id mismatch")
    available: dict[str, dict[str, Any]] = {}
    for pid, artifact in (request.artifacts or {}).items():
        if not isinstance(artifact, dict):
            return _fail(CheckerOutcome.PREMISE_UNAVAILABLE, f"artifact malformed: {pid}")
        available[str(pid)] = artifact
    premises = cert.get("premises")
    if not isinstance(premises, list) or not premises:
        return _fail(CheckerOutcome.MISSING_PREMISE, "certificate carries no premises")
    for premise in premises:
        if not isinstance(premise, dict):
            return _fail(CheckerOutcome.INADMISSIBLE_PREMISE, "premise malformed")
        pid = str(premise.get("evidence_id") or "")
        want = str(premise.get("content_hash") or "")
        artifact = available.get(pid)
        if artifact is None:
            return _fail(CheckerOutcome.PREMISE_UNAVAILABLE, f"artifact unavailable: {pid}")
        if _artifact_hash(artifact) != want:
            return _fail(CheckerOutcome.PREMISE_UNAVAILABLE, f"content-hash mismatch: {pid}")
    allowed = set(rule.input_types or [])
    for premise in premises:
        if not isinstance(premise, dict):
            continue
        if str(premise.get("premise_type") or "") not in allowed:
            return _fail(
                CheckerOutcome.INADMISSIBLE_PREMISE,
                f"premise type not in rule contract: {premise.get('premise_type')!r}",
            )
    bindings = cert.get("inference_instance", {}).get("bindings", {})
    bindings = bindings if isinstance(bindings, dict) else {}
    if bindings != _claim_for_rule(proposition, rule.rule_id):
        return _fail(
            CheckerOutcome.INVALID_INFERENCE,
            "inference bindings diverge from proposition target",
        )
    cert_scope = cert.get("scope")
    prop_scope = proposition.get("scope") or {}
    if not isinstance(cert_scope, dict) or cert_scope != (
        prop_scope if isinstance(prop_scope, dict) else {}
    ):
        return _fail(CheckerOutcome.SCOPE_MISMATCH, "scope mismatch")
    semantic = _CHECKER_SEMANTICS.get(rule.rule_id)
    if semantic is None:
        return _fail(
            CheckerOutcome.UNSUPPORTED_CONSTRUCT,
            f"no independent semantic check for rule: {rule.rule_id!r}",
        )
    quantifier = str(proposition.get("quantifier") or "")
    form = str(proposition.get("form") or "")
    if form == "ABSENCE" or quantifier == "UNIVERSAL":
        coverage = cert.get("coverage")
        if not isinstance(coverage, dict) or coverage.get("result") != "COMPLETE_PATH":
            return _fail(
                CheckerOutcome.MISSING_PREMISE,
                "absence/universal needs COMPLETE_PATH coverage (K3)",
            )
        universe = coverage.get("universe")
        if not isinstance(universe, str) or not universe.strip():
            return _fail(
                CheckerOutcome.MISSING_PREMISE, "absence/universal needs a stated universe (K3)"
            )
        if not isinstance(coverage.get("exclusions"), list):
            return _fail(
                CheckerOutcome.MISSING_PREMISE, "absence/universal needs an exclusions list (K3)"
            )
        manifest_scope = ""
        try:
            manifest_scope = str(
                (request.manifest.retrieval_config or {}).get("scope") or ""
            ).strip()
        except Exception:
            manifest_scope = ""
        if manifest_scope and universe.strip() != manifest_scope:
            return _fail(
                CheckerOutcome.INVALID_INFERENCE,
                "coverage universe differs from manifest scope (K3)",
            )
    outcome, reason = semantic(
        cert, proposition, request.manifest, available, request.mapping_authority
    )
    if outcome != CheckerOutcome.VALID:
        return _fail(outcome, reason)
    # B1 diverse validator (Option 1): the certificate is VALID only if the
    # independent second path agrees. Any disagreement, or any second-path
    # failure, denies closed — a bug in one path can never by itself
    # produce a false accept. Local import keeps the module boundary
    # explicit (second_path shares no modules with this file).
    try:
        from investigation_agent_platform.domain.proof.second_path import (
            check_rule_second_path,
        )

        second_outcome, second_reason = check_rule_second_path(
            rule.rule_id,
            cert,
            proposition,
            request.manifest,
            available,
            request.mapping_authority,
        )
    except Exception as exc:
        return _fail(
            CheckerOutcome.CHECKER_FAILURE,
            f"second-path failure fails closed: {exc}",
        )
    if second_outcome != CheckerOutcome.VALID:
        return _fail(
            CheckerOutcome.CHECKER_FAILURE,
            f"second-path disagreement ({second_outcome.value}): {second_reason}",
        )
    admitted_forms: dict[str, set[str | None]] = {
        "definition/v1": {"EXISTENCE", None},
        "call_edge/v1": {"EXISTENCE", None},
        "behavior/v1": {"OBSERVATION"},
        "absence/v1": {"ABSENCE"},
        "history/v1": {"HISTORICAL"},
    }
    admitted_quantifiers: dict[str, set[str]] = {
        "definition/v1": {"EXISTENTIAL"},
        "call_edge/v1": {"EXISTENTIAL"},
        "behavior/v1": {"OBSERVATIONAL"},
        "absence/v1": {"UNIVERSAL"},
        "history/v1": {"EXISTENTIAL"},
    }
    if (form or None) not in admitted_forms.get(
        rule.rule_id, set()
    ) or quantifier not in admitted_quantifiers.get(rule.rule_id, set()):
        return _fail(
            CheckerOutcome.UNSUPPORTED_CONSTRUCT,
            f"rule {rule.rule_id} does not establish {form or '?'} claims",
        )
    cert_scope = cert.get("scope")
    if isinstance(cert_scope, dict) and "revision" in cert_scope:
        for premise in premises:
            if not isinstance(premise, dict):
                continue
            pscope = premise.get("scope")
            if not isinstance(pscope, dict) or "revision" not in pscope:
                return _fail(CheckerOutcome.SCOPE_MISMATCH, "premise scope missing revision (K11)")
            if str(pscope["revision"]) != str(cert_scope["revision"]):
                return _fail(
                    CheckerOutcome.SCOPE_MISMATCH,
                    "premise/proposition scope revision differs (K11)",
                )
    deps = request.derivation_deps or {}
    if deps and _reachable(str(cert.get("derivation_id") or ""), deps):
        return _fail(CheckerOutcome.INVALID_INFERENCE, "derivation cycle detected (K2)")
    if request.expected_context_id is not None and str(cert.get("context_id") or "") != str(
        request.expected_context_id
    ):
        return _fail(CheckerOutcome.STALE_CERTIFICATE, "context moved since evaluation (K8/K15)")
    recomputed_der = derivation_id(
        str(cert.get("context_id") or ""),
        rule.rule_id,
        rule.rule_version,
        rule.implementation_digest,
        [str(p.get("evidence_id") or "") for p in premises if isinstance(p, dict)],
        cert.get("inference_instance") if isinstance(cert.get("inference_instance"), dict) else {},
    )
    if recomputed_der != str(cert.get("derivation_id") or ""):
        return _fail(CheckerOutcome.INVALID_INFERENCE, "derivation_id mismatch")
    return CheckerOutcome.VALID, []
