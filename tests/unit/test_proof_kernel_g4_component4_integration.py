"""G4 Component 4 integration gate: end-to-end proof-kernel routes (TEST-ONLY).

D1 (12 tests): five valid routes (definition, call_edge, behavior, absence,
history) through build_manifest -> EvaluationInputs -> get_rule/apply_rule
-> certify -> CheckRequest/check_certificate -> make_node/compose ->
_decide_linearization private seam -> build_commit, plus seven
single-fault negatives (checker-rejection, checker-stale, decision-stale,
missing-premise, policy-denial, alternative-survival, multi-premise).

D2 (6 tests): one denial per DenyReason member plus a preservation test —
policy denial and staleness recompute the decision, never mutate the proof.

D3 (1 test): static consumer-scan over ``src/`` for the three
forbidden-pattern definitions (P1 decision-API refs, P2 harness-module refs,
P3 harness literals); only the allowlist (domain/proof internal, tests/,
guarded-only ``publication_commit_service.py`` + ``kernel_conclude_wiring.py``) may match.

D4 (1 test): ``test_c4_report_table_complete`` parses the mapping table below
and fails unless every row matches its ``C4_CASES`` entry exactly.

Single source: ``C4_CASES`` (19 entries, keyed by test name = 18 e2e + scan)
is the ONLY place per-case values are pinned. Every registry-bound test reads
its pins from ``C4_CASES``; the table below transcribes the same entries
(fixture cells serialize as ``fid=digest`` pairs joined by ``; ``, ``(none)``
for empty; expected cells read ``<outcome> / <reason>``; actual cells equal
their expected cells on green); the detector compares all three. Green table,
green gate, pinned registry — all three agree, or nothing passes.

G4-ONLY SCOPE (no shared helpers): every builder, record, and proposition in
this module is defined inline below. Nothing is imported from any other test
module, and nothing here is exported for other suites (enforced by D3 P2).

BEH-001 TEST-ONLY: the behavior routes use the frozen TEST-ONLY mapping
authority ``synthetic-test-only-authority-001``; synthetic fixtures only.

ACTUAL-CELL RULE: no test asserts a value the implementation did not produce.
``C4_CASES`` digests are filled from observed ``sha256:`` artifact hashes;
assertion messages embed observed-vs-pinned on mismatch.

Mapping table (transcription of ``C4_CASES``):

| Test | Fixture IDs | Expected | Actual |
| test_e2e_definition_valid_commit | ev-c4-definition-0=sha256:26eacd8379495e50e98069b18f62ccbfb775163ca4e55fe5debf79f52ec5ddd2 | VALID / range read shows the defined symbol | VALID / range read shows the defined symbol |
| test_e2e_call_edge_valid_commit | ev-c4-call-edge-0=sha256:b533ed7596e24bd8a44cc7ae99e960a8587acc1fdb612707dc45dc9ccdfbaf83 | VALID / normalized call site shows the asserted edge | VALID / normalized call site shows the asserted edge |
| test_e2e_behavior_valid_commit | ev-c4-behavior-0=sha256:0c77fd5241f3c442aab2cae299583037ce8f30969c5c15b1c4d2af6df22e04fa; ev-c4-behavior-1=sha256:72936f03a5ba3ea88f9cb87f5c6f4b7e43e141fc7948a933b915813eb1e948bb; ev-c4-behavior-2=sha256:ca5cc8b95f3828107152332f419f9774fb09432f335bbe568471ad4df7c7370e | VALID / code, runtime, and attested mapping join on the asserted behavior | VALID / code, runtime, and attested mapping join on the asserted behavior |
| test_e2e_absence_valid_commit | ev-c4-absence-0=sha256:a098a8abb1b01b3f4ecb6a47f91f575da79ba4ad356b2235ea7791a0b9aba856 | VALID / complete-empty coverage establishes absence in the universe | VALID / complete-empty coverage establishes absence in the universe |
| test_e2e_history_valid_commit | ev-c4-history-0=sha256:65e2075ae8c4f996a59ee10124eec11fcd3bc29154745935a3722e91f0fed421 | VALID / commit history exactly binds the claimed change | VALID / commit history exactly binds the claimed change |
| test_e2e_checker_rejection_denies | ev-c4-definition-0=sha256:26eacd8379495e50e98069b18f62ccbfb775163ca4e55fe5debf79f52ec5ddd2 | DENY / NOT_CHECKER_ACCEPTED:PREMISE_UNAVAILABLE | DENY / NOT_CHECKER_ACCEPTED:PREMISE_UNAVAILABLE |
| test_e2e_checker_stale_rejection_denies | ev-c4-definition-0=sha256:26eacd8379495e50e98069b18f62ccbfb775163ca4e55fe5debf79f52ec5ddd2 | DENY / NOT_CHECKER_ACCEPTED:STALE_CERTIFICATE | DENY / NOT_CHECKER_ACCEPTED:STALE_CERTIFICATE |
| test_e2e_decision_stale_context_denies | ev-c4-definition-0=sha256:26eacd8379495e50e98069b18f62ccbfb775163ca4e55fe5debf79f52ec5ddd2 | DENY / STALE_CONTEXT | DENY / STALE_CONTEXT |
| test_e2e_missing_premise_denies | ev-c4-call-edge-0=sha256:b533ed7596e24bd8a44cc7ae99e960a8587acc1fdb612707dc45dc9ccdfbaf83 | DENY / NOT_CHECKER_ACCEPTED:PREMISE_UNAVAILABLE | DENY / NOT_CHECKER_ACCEPTED:PREMISE_UNAVAILABLE |
| test_e2e_policy_denial_recomputes_without_mutating_proof | ev-c4-behavior-0=sha256:0c77fd5241f3c442aab2cae299583037ce8f30969c5c15b1c4d2af6df22e04fa; ev-c4-behavior-1=sha256:72936f03a5ba3ea88f9cb87f5c6f4b7e43e141fc7948a933b915813eb1e948bb; ev-c4-behavior-2=sha256:ca5cc8b95f3828107152332f419f9774fb09432f335bbe568471ad4df7c7370e | DENY / POLICY_DENIED:c4-policy-freeze | DENY / POLICY_DENIED:c4-policy-freeze |
| test_e2e_alternative_survival_commits | ev-c4-call-edge-0=sha256:b533ed7596e24bd8a44cc7ae99e960a8587acc1fdb612707dc45dc9ccdfbaf83; ev-c4-definition-0=sha256:26eacd8379495e50e98069b18f62ccbfb775163ca4e55fe5debf79f52ec5ddd2 | COMMIT / linearization checklist satisfied | COMMIT / linearization checklist satisfied |
| test_e2e_multi_premise_behavior_commits | ev-c4-behavior-0=sha256:0c77fd5241f3c442aab2cae299583037ce8f30969c5c15b1c4d2af6df22e04fa; ev-c4-behavior-1=sha256:72936f03a5ba3ea88f9cb87f5c6f4b7e43e141fc7948a933b915813eb1e948bb; ev-c4-behavior-2=sha256:ca5cc8b95f3828107152332f419f9774fb09432f335bbe568471ad4df7c7370e | COMMIT / linearization checklist satisfied | COMMIT / linearization checklist satisfied |
| test_e2e_deny_no_pinned_context | ev-c4-definition-0=sha256:26eacd8379495e50e98069b18f62ccbfb775163ca4e55fe5debf79f52ec5ddd2 | DENY / NO_PINNED_CONTEXT | DENY / NO_PINNED_CONTEXT |
| test_e2e_deny_not_checker_accepted | ev-c4-definition-0=sha256:26eacd8379495e50e98069b18f62ccbfb775163ca4e55fe5debf79f52ec5ddd2 | DENY / NOT_CHECKER_ACCEPTED:INVALID_INFERENCE | DENY / NOT_CHECKER_ACCEPTED:INVALID_INFERENCE |
| test_e2e_deny_conflict_unresolved | ev-c4-definition-0=sha256:26eacd8379495e50e98069b18f62ccbfb775163ca4e55fe5debf79f52ec5ddd2 | DENY / CONFLICT_UNRESOLVED | DENY / CONFLICT_UNRESOLVED |
| test_e2e_deny_stale_context | ev-c4-definition-0=sha256:26eacd8379495e50e98069b18f62ccbfb775163ca4e55fe5debf79f52ec5ddd2 | DENY / STALE_CONTEXT | DENY / STALE_CONTEXT |
| test_e2e_deny_policy_denied | ev-c4-definition-0=sha256:26eacd8379495e50e98069b18f62ccbfb775163ca4e55fe5debf79f52ec5ddd2 | DENY / POLICY_DENIED:c4-deny-pin | DENY / POLICY_DENIED:c4-deny-pin |
| test_e2e_denied_decision_preserves_proof_artifact | ev-c4-definition-0=sha256:26eacd8379495e50e98069b18f62ccbfb775163ca4e55fe5debf79f52ec5ddd2 | PRESERVED / H0 stable; IDs bound; no commit constructed | PRESERVED / H0 stable; IDs bound; no commit constructed |
| test_no_production_consumer_of_g4_decision | P1=decide_publication/build_commit/decide_guarded_promotion import-or-attribute refs; P2=test_proof_kernel_g4_component4_integration refs; P3=component4_integration/g4_component4 literals under src/; allowlist=domain/proof internal, tests/, service + conclude-tap guarded-only; searched-roots=src/ | no violations / zero out-of-allowlist matches | no violations / zero out-of-allowlist matches |
"""

from __future__ import annotations

import copy
import hashlib
import re
from pathlib import Path
from typing import Any

from investigation_agent_platform.domain.proof.canonical import (
    canonical_dict_bytes,
    proposition_id,
)
from investigation_agent_platform.domain.proof.certificate import (
    CERT_SCHEMA_VERSION,
    CertificationError,
    CheckRequest,
    certify,
    check_certificate,
)
from investigation_agent_platform.domain.proof.checker_semantics import (
    check_absence_semantics,
    check_behavior_semantics,
    check_call_edge_semantics,
    check_definition_semantics,
    check_history_semantics,
)
from investigation_agent_platform.domain.proof.composition import (
    CheckerOutcome,
    compose_alternatives,
    compose_conjunction,
    make_node,
)
from investigation_agent_platform.domain.proof.evaluation import EvaluationInputs
from investigation_agent_platform.domain.proof.manifest import (
    ManifestCoverage,
    build_manifest,
    manifest_digest,
)
from investigation_agent_platform.domain.proof.publication import (
    PublicationDecision,
    _decide_linearization,
    build_commit,
)
from investigation_agent_platform.domain.proof.rules import (
    RULE_BUNDLE_VERSION,
    RULE_REGISTRY,
    apply_rule,
    get_rule,
)

REPO = "synthetic/c4"
REV_A = "c4a0" * 10
REV_B = "c4b1" * 10
REV_C = "c4c2" * 10
REV_D = "c4d3" * 10
BEHAVIOR_BUILD = "c4-build-001"
BEHAVIOR_EXEC = "c4-exec-001"
BEHAVIOR_ENV = "test"
TEST_ONLY_AUTHORITY = "synthetic-test-only-authority-001"
ABSENCE_UNIVERSE = "repository revision path prefix"
ABSENCE_SEARCH_CONFIG = "sha256:" + "c4" * 32
WRONG_CONTEXT = "ctx-moved-c4"

D1_NAMES = [
    "test_e2e_definition_valid_commit",
    "test_e2e_call_edge_valid_commit",
    "test_e2e_behavior_valid_commit",
    "test_e2e_absence_valid_commit",
    "test_e2e_history_valid_commit",
    "test_e2e_checker_rejection_denies",
    "test_e2e_checker_stale_rejection_denies",
    "test_e2e_decision_stale_context_denies",
    "test_e2e_missing_premise_denies",
    "test_e2e_policy_denial_recomputes_without_mutating_proof",
    "test_e2e_alternative_survival_commits",
    "test_e2e_multi_premise_behavior_commits",
]
D2_NAMES = [
    "test_e2e_deny_no_pinned_context",
    "test_e2e_deny_not_checker_accepted",
    "test_e2e_deny_conflict_unresolved",
    "test_e2e_deny_stale_context",
    "test_e2e_deny_policy_denied",
    "test_e2e_denied_decision_preserves_proof_artifact",
]
SCAN_NAME = "test_no_production_consumer_of_g4_decision"

C4_CASES: dict[str, dict[str, Any]] = {
    "test_e2e_definition_valid_commit": {
        "fixtures": {
            "ev-c4-definition-0": "sha256:26eacd8379495e50e98069b18f62ccbfb775163ca4e55fe5debf79f52ec5ddd2"
        },
        "expected": "VALID",
        "reason": "range read shows the defined symbol",
    },
    "test_e2e_call_edge_valid_commit": {
        "fixtures": {
            "ev-c4-call-edge-0": "sha256:b533ed7596e24bd8a44cc7ae99e960a8587acc1fdb612707dc45dc9ccdfbaf83"
        },
        "expected": "VALID",
        "reason": "normalized call site shows the asserted edge",
    },
    "test_e2e_behavior_valid_commit": {
        "fixtures": {
            "ev-c4-behavior-0": "sha256:0c77fd5241f3c442aab2cae299583037ce8f30969c5c15b1c4d2af6df22e04fa",
            "ev-c4-behavior-1": "sha256:72936f03a5ba3ea88f9cb87f5c6f4b7e43e141fc7948a933b915813eb1e948bb",
            "ev-c4-behavior-2": "sha256:ca5cc8b95f3828107152332f419f9774fb09432f335bbe568471ad4df7c7370e",
        },
        "expected": "VALID",
        "reason": "code, runtime, and attested mapping join on the asserted behavior",
    },
    "test_e2e_absence_valid_commit": {
        "fixtures": {
            "ev-c4-absence-0": "sha256:a098a8abb1b01b3f4ecb6a47f91f575da79ba4ad356b2235ea7791a0b9aba856"
        },
        "expected": "VALID",
        "reason": "complete-empty coverage establishes absence in the universe",
    },
    "test_e2e_history_valid_commit": {
        "fixtures": {
            "ev-c4-history-0": "sha256:65e2075ae8c4f996a59ee10124eec11fcd3bc29154745935a3722e91f0fed421"
        },
        "expected": "VALID",
        "reason": "commit history exactly binds the claimed change",
    },
    "test_e2e_checker_rejection_denies": {
        "fixtures": {
            "ev-c4-definition-0": "sha256:26eacd8379495e50e98069b18f62ccbfb775163ca4e55fe5debf79f52ec5ddd2"
        },
        "expected": "DENY",
        "reason": "NOT_CHECKER_ACCEPTED:PREMISE_UNAVAILABLE",
    },
    "test_e2e_checker_stale_rejection_denies": {
        "fixtures": {
            "ev-c4-definition-0": "sha256:26eacd8379495e50e98069b18f62ccbfb775163ca4e55fe5debf79f52ec5ddd2"
        },
        "expected": "DENY",
        "reason": "NOT_CHECKER_ACCEPTED:STALE_CERTIFICATE",
    },
    "test_e2e_decision_stale_context_denies": {
        "fixtures": {
            "ev-c4-definition-0": "sha256:26eacd8379495e50e98069b18f62ccbfb775163ca4e55fe5debf79f52ec5ddd2"
        },
        "expected": "DENY",
        "reason": "STALE_CONTEXT",
    },
    "test_e2e_missing_premise_denies": {
        "fixtures": {
            "ev-c4-call-edge-0": "sha256:b533ed7596e24bd8a44cc7ae99e960a8587acc1fdb612707dc45dc9ccdfbaf83"
        },
        "expected": "DENY",
        "reason": "NOT_CHECKER_ACCEPTED:PREMISE_UNAVAILABLE",
    },
    "test_e2e_policy_denial_recomputes_without_mutating_proof": {
        "fixtures": {
            "ev-c4-behavior-0": "sha256:0c77fd5241f3c442aab2cae299583037ce8f30969c5c15b1c4d2af6df22e04fa",
            "ev-c4-behavior-1": "sha256:72936f03a5ba3ea88f9cb87f5c6f4b7e43e141fc7948a933b915813eb1e948bb",
            "ev-c4-behavior-2": "sha256:ca5cc8b95f3828107152332f419f9774fb09432f335bbe568471ad4df7c7370e",
        },
        "expected": "DENY",
        "reason": "POLICY_DENIED:c4-policy-freeze",
    },
    "test_e2e_alternative_survival_commits": {
        "fixtures": {
            "ev-c4-call-edge-0": "sha256:b533ed7596e24bd8a44cc7ae99e960a8587acc1fdb612707dc45dc9ccdfbaf83",
            "ev-c4-definition-0": "sha256:26eacd8379495e50e98069b18f62ccbfb775163ca4e55fe5debf79f52ec5ddd2",
        },
        "expected": "COMMIT",
        "reason": "linearization checklist satisfied",
    },
    "test_e2e_multi_premise_behavior_commits": {
        "fixtures": {
            "ev-c4-behavior-0": "sha256:0c77fd5241f3c442aab2cae299583037ce8f30969c5c15b1c4d2af6df22e04fa",
            "ev-c4-behavior-1": "sha256:72936f03a5ba3ea88f9cb87f5c6f4b7e43e141fc7948a933b915813eb1e948bb",
            "ev-c4-behavior-2": "sha256:ca5cc8b95f3828107152332f419f9774fb09432f335bbe568471ad4df7c7370e",
        },
        "expected": "COMMIT",
        "reason": "linearization checklist satisfied",
    },
    "test_e2e_deny_no_pinned_context": {
        "fixtures": {
            "ev-c4-definition-0": "sha256:26eacd8379495e50e98069b18f62ccbfb775163ca4e55fe5debf79f52ec5ddd2"
        },
        "expected": "DENY",
        "reason": "NO_PINNED_CONTEXT",
    },
    "test_e2e_deny_not_checker_accepted": {
        "fixtures": {
            "ev-c4-definition-0": "sha256:26eacd8379495e50e98069b18f62ccbfb775163ca4e55fe5debf79f52ec5ddd2"
        },
        "expected": "DENY",
        "reason": "NOT_CHECKER_ACCEPTED:INVALID_INFERENCE",
    },
    "test_e2e_deny_conflict_unresolved": {
        "fixtures": {
            "ev-c4-definition-0": "sha256:26eacd8379495e50e98069b18f62ccbfb775163ca4e55fe5debf79f52ec5ddd2"
        },
        "expected": "DENY",
        "reason": "CONFLICT_UNRESOLVED",
    },
    "test_e2e_deny_stale_context": {
        "fixtures": {
            "ev-c4-definition-0": "sha256:26eacd8379495e50e98069b18f62ccbfb775163ca4e55fe5debf79f52ec5ddd2"
        },
        "expected": "DENY",
        "reason": "STALE_CONTEXT",
    },
    "test_e2e_deny_policy_denied": {
        "fixtures": {
            "ev-c4-definition-0": "sha256:26eacd8379495e50e98069b18f62ccbfb775163ca4e55fe5debf79f52ec5ddd2"
        },
        "expected": "DENY",
        "reason": "POLICY_DENIED:c4-deny-pin",
    },
    "test_e2e_denied_decision_preserves_proof_artifact": {
        "fixtures": {
            "ev-c4-definition-0": "sha256:26eacd8379495e50e98069b18f62ccbfb775163ca4e55fe5debf79f52ec5ddd2"
        },
        "expected": "PRESERVED",
        "reason": "H0 stable; IDs bound; no commit constructed",
    },
    "test_no_production_consumer_of_g4_decision": {
        "fixtures": {
            "P1": "decide_publication/build_commit/decide_guarded_promotion import-or-attribute refs",
            "P2": "test_proof_kernel_g4_component4_integration refs",
            "P3": "component4_integration/g4_component4 literals under src/",
            "allowlist": "domain/proof internal, tests/, service + conclude-tap guarded-only",
            "searched-roots": "src/",
        },
        "expected": "no violations",
        "reason": "zero out-of-allowlist matches",
    },
}


def _real_hash(artifact: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_dict_bytes(dict(artifact))).hexdigest()


def _strip_envelope(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.items()
        if key not in ("evidence_id", "content_hash", "premise_type")
    }


def _definition_record() -> dict[str, Any]:
    return {
        "evidence_id": "ev-c4-definition-0",
        "premise_type": "RANGE_READ_FACT",
        "repository": REPO,
        "file_path": "src/handler.py",
        "revision": REV_A,
        "line_start": 10,
        "line_end": 14,
        "target": "handle",
        "window_text": "def handle(event):\n    return route(event)",
    }


def _definition_proposition() -> dict[str, Any]:
    return {
        "canon_version": "canon/v1",
        "form": "EXISTENCE",
        "quantifier": "EXISTENTIAL",
        "predicate": "defined(handle)",
        "target": {"symbol": "handle", "file_path": "src/handler.py"},
        "scope": {"revision": REV_A},
    }


def _call_edge_record() -> dict[str, Any]:
    return {
        "evidence_id": "ev-c4-call-edge-0",
        "premise_type": "NORMALIZED_CALLSITE_FACT",
        "repository": REPO,
        "file_path": "src/handler.py",
        "revision": REV_A,
        "line_start": 12,
        "line_end": 12,
        "caller": "handle",
        "callee": "route",
        "call_text": "return route(event)  # handle delegates",
    }


def _call_edge_proposition() -> dict[str, Any]:
    return {
        "canon_version": "canon/v1",
        "form": "EXISTENCE",
        "quantifier": "EXISTENTIAL",
        "predicate": "calls(handle,route)",
        "target": {"caller": "handle", "callee": "route"},
        "scope": {"revision": REV_A},
    }


def _behavior_records() -> list[dict[str, Any]]:
    return [
        {
            "evidence_id": "ev-c4-behavior-0",
            "premise_type": "RANGE_READ_FACT",
            "repository": REPO,
            "file_path": "src/serve.py",
            "revision": REV_A,
            "line_start": 10,
            "line_end": 14,
            "target": "serve",
            "window_text": "def serve(request):\n    return respond(request, 200)",
        },
        {
            "evidence_id": "ev-c4-behavior-1",
            "premise_type": "RUNTIME_OBSERVATION_FACT",
            "repository": REPO,
            "symbol": "serve",
            "predicate": "returns_status(serve)",
            "observed_value": 200,
            "execution_id": BEHAVIOR_EXEC,
            "build_identity": BEHAVIOR_BUILD,
            "environment": BEHAVIOR_ENV,
            "source_revision": REV_A,
        },
        {
            "evidence_id": "ev-c4-behavior-2",
            "premise_type": "BUILD_SOURCE_MAPPING",
            "build_identity": BEHAVIOR_BUILD,
            "repository": REPO,
            "source_revision": REV_A,
            "provenance_ref": TEST_ONLY_AUTHORITY,
            "mapping_digest": "sha256:" + "c4" * 32,
        },
    ]


def _behavior_proposition() -> dict[str, Any]:
    return {
        "canon_version": "canon/v1",
        "predicate": "returns_status(serve)",
        "quantifier": "OBSERVATIONAL",
        "form": "OBSERVATION",
        "relation": "RUNTIME_OBSERVATION",
        "modality": "RUNTIME",
        "asserted_value": 200,
        "target": {"symbol": "serve", "file_path": "src/serve.py"},
        "scope": {
            "repository": REPO,
            "revision": REV_A,
            "build_identity": BEHAVIOR_BUILD,
            "execution_id": BEHAVIOR_EXEC,
            "environment": BEHAVIOR_ENV,
        },
    }


def _absence_record() -> dict[str, Any]:
    return {
        "evidence_id": "ev-c4-absence-0",
        "premise_type": "ABSENCE_COVERAGE_FACT",
        "universe": ABSENCE_UNIVERSE,
        "predicate": "contains_symbol(legacy_hook)",
        "search_status": "COMPLETE_EMPTY",
        "search_config_digest": ABSENCE_SEARCH_CONFIG,
        "scope": {"repository": REPO, "revision": REV_B, "path_scope": "src/"},
        "exclusions": [],
        "coverage_basis": {"kind": "synthetic_complete_inventory", "value": "all enumerated"},
        "match_count": 0,
    }


def _absence_proposition() -> dict[str, Any]:
    return {
        "canon_version": "canon/v1",
        "form": "ABSENCE",
        "quantifier": "UNIVERSAL",
        "modality": "STATIC",
        "completeness": "CLOSED_WORLD",
        "predicate": "contains_symbol(legacy_hook)",
        "target": {"universe": ABSENCE_UNIVERSE},
        "scope": {"repository": REPO, "revision": REV_B, "path_scope": "src/"},
    }


def _history_record() -> dict[str, Any]:
    return {
        "evidence_id": "ev-c4-history-0",
        "premise_type": "COMMIT_HISTORY",
        "repository": REPO,
        "repository_revision": REV_D,
        "commit_id": REV_C,
        "file_path": "src/handler.py",
        "subject": "Route lookup fix",
    }


def _history_proposition() -> dict[str, Any]:
    return {
        "canon_version": "canon/v1",
        "form": "HISTORICAL",
        "quantifier": "EXISTENTIAL",
        "modality": "HISTORY",
        "predicate": "changed_at_commit(src/handler.py, commit)",
        "target": {
            "repository": REPO,
            "commit_id": REV_C,
            "file_path": "src/handler.py",
            "subject": "Route lookup fix",
        },
        "scope": {"repository_revision": REV_D},
    }


def _assemble(
    rule_id: str,
    records: list[dict[str, Any]],
    proposition: dict[str, Any],
    manifest_kwargs: dict[str, Any],
    universe: str | None = None,
) -> dict[str, Any]:
    artifacts: dict[str, dict[str, Any]] = {}
    hashes: dict[str, str] = {}
    types: dict[str, str] = {}
    premises: list[dict[str, Any]] = []
    for record in records:
        art = _strip_envelope(record)
        digest = _real_hash(art)
        pid = str(record["evidence_id"])
        artifacts[pid] = art
        hashes[pid] = digest
        types[pid] = str(record["premise_type"])
        premises.append(dict(record, content_hash=digest))
    manifest = build_manifest(
        [str(record["evidence_id"]) for record in records],
        hashes,
        premise_types=types,
        **manifest_kwargs,
    )
    rule = get_rule(rule_id)
    inputs = EvaluationInputs(
        proposition=dict(proposition),
        manifest=manifest,
        rule_bundle_version=RULE_BUNDLE_VERSION,
        rule_id=rule.rule_id,
        rule_version=rule.rule_version,
        implementation_digest=rule.implementation_digest,
    )
    return {
        "rule_id": rule_id,
        "records": records,
        "proposition": proposition,
        "artifacts": artifacts,
        "hashes": hashes,
        "premises": premises,
        "manifest": manifest,
        "inputs": inputs,
        "universe": universe,
    }


def _certified_bundle(
    rule_id: str,
    records: list[dict[str, Any]],
    proposition: dict[str, Any],
    manifest_kwargs: dict[str, Any],
    universe: str | None = None,
    mapping_authority: dict[str, Any] | None = None,
) -> dict[str, Any]:
    asm = _assemble(rule_id, records, proposition, manifest_kwargs, universe)
    cert = certify(asm["inputs"], asm["premises"], asm["artifacts"], universe)
    request = CheckRequest(
        certificate=cert,
        proposition=dict(proposition),
        manifest=asm["manifest"],
        rule_bundle_version=RULE_BUNDLE_VERSION,
        artifacts=dict(asm["artifacts"]),
        expected_context_id=str(cert.get("context_id") or ""),
        mapping_authority=mapping_authority,
    )
    outcome, reasons = check_certificate(request)
    return {**asm, "cert": cert, "request": request, "outcome": outcome, "reasons": reasons}


def _check_premise(rule_id: str, bundle: dict[str, Any]) -> tuple[Any, str]:
    from investigation_agent_platform.domain.proof.certificate import _claim_for_rule

    rule = get_rule(rule_id)
    claim = _claim_for_rule(bundle["proposition"], rule_id)
    validator_inputs = []
    for premise in bundle["premises"]:
        merged = dict(bundle["artifacts"][str(premise["evidence_id"])])
        merged.update(
            {
                "evidence_id": str(premise["evidence_id"]),
                "content_hash": str(premise["content_hash"]),
                "premise_type": str(premise["premise_type"]),
            }
        )
        validator_inputs.append(merged)
    return apply_rule(rule, claim, validator_inputs, bundle["manifest"].model_dump(mode="json"))


def _assert_identity(rule_id: str, bundle: dict[str, Any]) -> None:
    cert = bundle["cert"]
    assert cert["schema_version"] == CERT_SCHEMA_VERSION == "cert/v2", cert["schema_version"]
    assert cert["rule_id"] == rule_id, cert["rule_id"]
    assert cert["rule_version"] == get_rule(rule_id).rule_version, cert["rule_version"]
    assert cert["implementation_digest"] == RULE_REGISTRY[rule_id].implementation_digest
    assert cert["proposition_id"] == proposition_id(bundle["proposition"]), (
        f"proposition_id {cert['proposition_id']} != recomputed"
    )
    assert cert["input_manifest_digest"] == manifest_digest(bundle["manifest"]), (
        f"input_manifest_digest {cert['input_manifest_digest']} != recomputed"
    )
    assert str(cert.get("context_id") or ""), "empty context_id"
    assert str(cert.get("derivation_id") or ""), "empty derivation_id"


def _compose_and_commit(bundle: dict[str, Any], outcomes: list[Any]) -> tuple[Any, str, Any]:
    cert = bundle["cert"]
    node = make_node(
        str(cert.get("derivation_id") or ""),
        str(cert.get("rule_id") or ""),
        [str(p["evidence_id"]) for p in cert.get("premises", [])],
        [],
    )
    assert node.derivation_id == cert.get("derivation_id")
    assert node.rule_id == cert.get("rule_id")
    composed, composition_reason = compose_conjunction(outcomes)
    decision, reason = _decide(cert, composed)
    commit = None
    if decision == PublicationDecision.COMMIT:
        commit = build_commit(
            str(cert.get("derivation_id") or ""),
            str(cert.get("context_id") or ""),
            str(cert.get("context_id") or ""),
        )
    return decision, reason, commit


def _assert_pins(case_key: str, actual: dict[str, str]) -> None:
    pinned = C4_CASES[case_key]["fixtures"]
    assert set(actual) == set(pinned), (
        f"{case_key}: fixture set {sorted(actual)} != pinned {sorted(pinned)}"
    )
    for fid, digest in actual.items():
        assert digest == pinned[fid], f"{case_key}/{fid}: observed {digest} != pinned {pinned[fid]}"


_SEMANTICS: dict[str, Any] = {
    "definition/v1": check_definition_semantics,
    "call_edge/v1": check_call_edge_semantics,
    "behavior/v1": check_behavior_semantics,
    "absence/v1": check_absence_semantics,
    "history/v1": check_history_semantics,
}


def _assert_checker_row(
    case_key: str, bundle: dict[str, Any], authority: dict[str, Any] | None = None
) -> None:
    entry = C4_CASES[case_key]
    outcome = bundle["outcome"]
    reasons = bundle["reasons"]
    observed = outcome.value if isinstance(outcome, CheckerOutcome) else str(outcome)
    assert observed == entry["expected"], (
        f"{case_key}: checker={observed} reasons={reasons} != pinned {entry['expected']}"
    )
    if observed == "VALID":
        # check_certificate returns no reason text on success, so the pinned
        # reason is asserted against the rule semantic function's message —
        # the same pure function the checker dispatches internally.
        semantic = _SEMANTICS[str(bundle["rule_id"])]
        semantic_outcome, message = semantic(
            bundle["cert"],
            bundle["proposition"],
            bundle["manifest"],
            bundle["artifacts"],
            authority,
        )
        assert semantic_outcome == CheckerOutcome.VALID, (
            f"{case_key}: semantic={semantic_outcome} message={message!r}"
        )
        assert message == entry["reason"], (
            f"{case_key}: semantic message {message!r} != pinned {entry['reason']!r}"
        )
    else:
        assert any(entry["reason"] in reason for reason in reasons), (
            f"{case_key}: reason {entry['reason']!r} not in {reasons}"
        )


def _assert_decision_row(case_key: str, decision: Any, reason: str) -> None:
    entry = C4_CASES[case_key]
    observed = decision.value if isinstance(decision, PublicationDecision) else str(decision)
    assert observed == entry["expected"], (
        f"{case_key}: decision={observed} reason={reason!r} != pinned {entry['expected']}"
    )
    assert entry["reason"] in reason, f"{case_key}: reason {entry['reason']!r} not in {reason!r}"


def _definition_bundle() -> dict[str, Any]:
    return _certified_bundle("definition/v1", [_definition_record()], _definition_proposition(), {})


_UNSET: Any = object()


def _decide(
    cert: dict[str, Any],
    outcome: Any,
    **over: Any,
) -> tuple[Any, str]:
    """Linearization sub-step over gate-derived values (private seam)."""
    if "checker_outcome" in over:
        outcome = over.pop("checker_outcome")
    certificate_context_id = over.pop("certificate_context_id", _UNSET)
    expected_context_id = over.pop("expected_context_id", _UNSET)
    reconciliation_adjudicated = over.pop("reconciliation_adjudicated", True)
    policy_eligible = over.pop("policy_eligible", True)
    policy_reason = over.pop("policy_reason", "")
    assert not over, f"unexpected _decide kwargs: {sorted(over)}"
    return _decide_linearization(
        checker_outcome=outcome,
        certificate_context_id=(
            str(cert.get("context_id") or "")
            if certificate_context_id is _UNSET
            else certificate_context_id
        ),
        expected_context_id=(
            str(cert.get("context_id") or "")
            if expected_context_id is _UNSET
            else expected_context_id
        ),
        reconciliation_adjudicated=reconciliation_adjudicated,
        policy_eligible=policy_eligible,
        policy_reason=policy_reason,
    )


def _cert_hash(cert: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_dict_bytes(dict(cert))).hexdigest()


def _assert_preserved(
    case_key: str,
    before: dict[str, Any],
    snapshot: dict[str, Any],
    before_hash: str,
) -> None:
    assert _cert_hash(before) == before_hash, f"{case_key}: certificate mutated by denial"
    assert before == snapshot, f"{case_key}: certificate dict differs after denial"


def test_e2e_definition_valid_commit() -> None:
    case = "test_e2e_definition_valid_commit"
    bundle = _definition_bundle()
    _assert_pins(case, bundle["hashes"])
    _assert_identity("definition/v1", bundle)
    validator_outcome, _ = _check_premise("definition/v1", bundle)
    assert validator_outcome.value == "DISCHARGED"
    assert bundle["cert"]["coverage"] is None
    _assert_checker_row(case, bundle)
    decision, reason, commit = _compose_and_commit(bundle, [bundle["outcome"]])
    assert decision == PublicationDecision.COMMIT, reason
    assert commit is not None
    assert commit.derivation_id == bundle["cert"].get("derivation_id")
    assert commit.context_id == bundle["cert"].get("context_id")


def test_e2e_call_edge_valid_commit() -> None:
    case = "test_e2e_call_edge_valid_commit"
    bundle = _certified_bundle("call_edge/v1", [_call_edge_record()], _call_edge_proposition(), {})
    _assert_pins(case, bundle["hashes"])
    _assert_identity("call_edge/v1", bundle)
    validator_outcome, _ = _check_premise("call_edge/v1", bundle)
    assert validator_outcome.value == "DISCHARGED"
    _assert_checker_row(case, bundle)
    decision, reason, commit = _compose_and_commit(bundle, [bundle["outcome"]])
    assert decision == PublicationDecision.COMMIT, reason
    assert commit is not None
    assert commit.derivation_id == bundle["cert"].get("derivation_id")
    assert commit.context_id == bundle["cert"].get("context_id")


def test_e2e_behavior_valid_commit() -> None:
    case = "test_e2e_behavior_valid_commit"
    bundle = _certified_bundle(
        "behavior/v1",
        _behavior_records(),
        _behavior_proposition(),
        {"retrieval_config": {"scope": REPO}},
        None,
        {"approved_provenance_refs": [TEST_ONLY_AUTHORITY]},
    )
    _assert_pins(case, bundle["hashes"])
    _assert_identity("behavior/v1", bundle)
    validator_outcome, _ = _check_premise("behavior/v1", bundle)
    assert validator_outcome.value == "DISCHARGED"
    _assert_checker_row(case, bundle, {"approved_provenance_refs": [TEST_ONLY_AUTHORITY]})
    decision, reason, commit = _compose_and_commit(bundle, [bundle["outcome"]])
    assert decision == PublicationDecision.COMMIT, reason
    assert commit is not None
    assert commit.derivation_id == bundle["cert"].get("derivation_id")
    assert commit.context_id == bundle["cert"].get("context_id")


def test_e2e_absence_valid_commit() -> None:
    case = "test_e2e_absence_valid_commit"
    bundle = _certified_bundle(
        "absence/v1",
        [_absence_record()],
        _absence_proposition(),
        {
            "retrieval_config": {
                "scope": ABSENCE_UNIVERSE,
                "search_config_digest": ABSENCE_SEARCH_CONFIG,
            },
            "coverage": ManifestCoverage.COMPLETE_EMPTY,
            "coverage_evidence_id": "ev-c4-absence-0",
            "coverage_evidence": {
                "premise_type": "ABSENCE_COVERAGE_FACT",
                "universe": ABSENCE_UNIVERSE,
                "predicate": "contains_symbol(legacy_hook)",
                "search_status": "COMPLETE_EMPTY",
                "search_config_digest": ABSENCE_SEARCH_CONFIG,
                "scope": {"repository": REPO, "revision": REV_B, "path_scope": "src/"},
                "exclusions": [],
                "coverage_basis": {
                    "kind": "synthetic_complete_inventory",
                    "value": "all enumerated",
                },
                "match_count": 0,
            },
        },
        ABSENCE_UNIVERSE,
    )
    _assert_pins(case, bundle["hashes"])
    _assert_identity("absence/v1", bundle)
    validator_outcome, _ = _check_premise("absence/v1", bundle)
    assert validator_outcome.value == "DISCHARGED"
    assert bundle["cert"]["coverage"]["result"] == "COMPLETE_PATH"
    _assert_checker_row(case, bundle)
    decision, reason, commit = _compose_and_commit(bundle, [bundle["outcome"]])
    assert decision == PublicationDecision.COMMIT, reason
    assert commit is not None
    assert commit.context_id == bundle["cert"].get("context_id")


def test_e2e_history_valid_commit() -> None:
    case = "test_e2e_history_valid_commit"
    bundle = _certified_bundle("history/v1", [_history_record()], _history_proposition(), {})
    _assert_pins(case, bundle["hashes"])
    _assert_identity("history/v1", bundle)
    validator_outcome, _ = _check_premise("history/v1", bundle)
    assert validator_outcome.value == "DISCHARGED"
    _assert_checker_row(case, bundle)
    decision, reason, commit = _compose_and_commit(bundle, [bundle["outcome"]])
    assert decision == PublicationDecision.COMMIT, reason
    assert commit is not None
    assert commit.context_id == bundle["cert"].get("context_id")


def test_e2e_checker_rejection_denies() -> None:
    case = "test_e2e_checker_rejection_denies"
    bundle = _definition_bundle()
    _assert_pins(case, bundle["hashes"])
    tampered = copy.deepcopy(bundle["artifacts"])
    victim = str(_definition_record()["evidence_id"])
    tampered[victim] = dict(tampered[victim], window_text="class Other:\n    pass")
    request = CheckRequest(
        certificate=bundle["cert"],
        proposition=dict(bundle["proposition"]),
        manifest=bundle["manifest"],
        rule_bundle_version=RULE_BUNDLE_VERSION,
        artifacts=tampered,
        expected_context_id=str(bundle["cert"].get("context_id") or ""),
    )
    outcome, reasons = check_certificate(request)
    assert outcome == CheckerOutcome.PREMISE_UNAVAILABLE, (
        f"{case}: tampered window surfaced {outcome}; reasons={reasons}"
    )
    decision, reason = _decide(bundle["cert"], outcome)
    _assert_decision_row(case, decision, reason)
    assert decision == PublicationDecision.DENY


def test_e2e_checker_stale_rejection_denies() -> None:
    case = "test_e2e_checker_stale_rejection_denies"
    bundle = _definition_bundle()
    _assert_pins(case, bundle["hashes"])
    request = CheckRequest(
        certificate=bundle["cert"],
        proposition=dict(bundle["proposition"]),
        manifest=bundle["manifest"],
        rule_bundle_version=RULE_BUNDLE_VERSION,
        artifacts=dict(bundle["artifacts"]),
        expected_context_id=WRONG_CONTEXT,
    )
    outcome, reasons = check_certificate(request)
    assert outcome == CheckerOutcome.STALE_CERTIFICATE, (
        f"{case}: moved context surfaced {outcome}; reasons={reasons}"
    )
    assert any("K8/K15" in reason for reason in reasons), reasons
    decision, reason = _decide(bundle["cert"], outcome)
    _assert_decision_row(case, decision, reason)
    assert "STALE_CONTEXT" not in reason, f"{case}: outcome gate must fire first: {reason!r}"


def test_e2e_decision_stale_context_denies() -> None:
    case = "test_e2e_decision_stale_context_denies"
    bundle = _definition_bundle()
    _assert_pins(case, bundle["hashes"])
    _assert_checker_row("test_e2e_definition_valid_commit", bundle)
    decision, reason = _decide(bundle["cert"], bundle["outcome"], expected_context_id=WRONG_CONTEXT)
    _assert_decision_row(case, decision, reason)
    with __import__("pytest").raises(ValueError, match="STALE_CONTEXT"):
        build_commit(
            str(bundle["cert"].get("derivation_id") or ""),
            str(bundle["cert"].get("context_id") or ""),
            WRONG_CONTEXT,
        )


def test_e2e_missing_premise_denies() -> None:
    case = "test_e2e_missing_premise_denies"
    bundle = _certified_bundle("call_edge/v1", [_call_edge_record()], _call_edge_proposition(), {})
    _assert_pins(case, bundle["hashes"])
    request = CheckRequest(
        certificate=bundle["cert"],
        proposition=dict(bundle["proposition"]),
        manifest=bundle["manifest"],
        rule_bundle_version=RULE_BUNDLE_VERSION,
        artifacts={},
    )
    outcome, reasons = check_certificate(request)
    assert outcome == CheckerOutcome.PREMISE_UNAVAILABLE, (
        f"{case}: withheld bytes surfaced {outcome}; reasons={reasons}"
    )
    decision, reason = _decide(bundle["cert"], outcome)
    _assert_decision_row(case, decision, reason)


def test_e2e_policy_denial_recomputes_without_mutating_proof() -> None:
    case = "test_e2e_policy_denial_recomputes_without_mutating_proof"
    bundle = _certified_bundle(
        "behavior/v1",
        _behavior_records(),
        _behavior_proposition(),
        {"retrieval_config": {"scope": REPO}},
        None,
        {"approved_provenance_refs": [TEST_ONLY_AUTHORITY]},
    )
    _assert_pins(case, bundle["hashes"])
    _assert_checker_row(
        "test_e2e_behavior_valid_commit",
        bundle,
        {"approved_provenance_refs": [TEST_ONLY_AUTHORITY]},
    )
    cert = bundle["cert"]
    snapshot = copy.deepcopy(cert)
    digest_before = _cert_hash(cert)
    decision, reason = _decide(
        cert, bundle["outcome"], policy_eligible=False, policy_reason="c4-policy-freeze"
    )
    _assert_decision_row(case, decision, reason)
    _assert_preserved(case, cert, snapshot, digest_before)
    assert str(cert.get("context_id") or "")
    assert str(cert.get("derivation_id") or "")
    decision, reason = _decide(cert, bundle["outcome"])
    assert decision == PublicationDecision.COMMIT, reason
    commit = build_commit(
        str(cert.get("derivation_id") or ""),
        str(cert.get("context_id") or ""),
        str(cert.get("context_id") or ""),
    )
    assert commit.derivation_id == cert.get("derivation_id")
    assert commit.context_id == cert.get("context_id")


def test_e2e_alternative_survival_commits() -> None:
    case = "test_e2e_alternative_survival_commits"
    survivor = _definition_bundle()
    failed = _certified_bundle("call_edge/v1", [_call_edge_record()], _call_edge_proposition(), {})
    _assert_pins("test_e2e_definition_valid_commit", survivor["hashes"])
    _assert_pins("test_e2e_call_edge_valid_commit", failed["hashes"])
    _assert_checker_row("test_e2e_definition_valid_commit", survivor)
    wrong_proposition = dict(failed["proposition"])
    wrong_proposition["predicate"] = "calls(handle,other)"
    request = CheckRequest(
        certificate=failed["cert"],
        proposition=wrong_proposition,
        manifest=failed["manifest"],
        rule_bundle_version=RULE_BUNDLE_VERSION,
        artifacts=dict(failed["artifacts"]),
        expected_context_id=str(failed["cert"].get("context_id") or ""),
    )
    failed_outcome, failed_reasons = check_certificate(request)
    assert failed_outcome == CheckerOutcome.INVALID_INFERENCE, (
        f"{case}: diverged proposition surfaced {failed_outcome}; reasons={failed_reasons}"
    )
    composed, _ = compose_alternatives([failed_outcome, survivor["outcome"]])
    assert composed == CheckerOutcome.VALID
    decision, reason = _decide(survivor["cert"], composed)
    entry = C4_CASES[case]
    observed = decision.value
    assert observed == entry["expected"], f"{case}: decision={observed} reason={reason!r}"
    assert entry["reason"] in reason
    commit = build_commit(
        str(survivor["cert"].get("derivation_id") or ""),
        str(survivor["cert"].get("context_id") or ""),
        str(survivor["cert"].get("context_id") or ""),
    )
    failed_ids = {
        str(failed["cert"].get("derivation_id") or ""),
        str(failed["cert"].get("context_id") or ""),
    }
    assert commit.derivation_id not in failed_ids
    assert commit.context_id == survivor["cert"].get("context_id")
    assert commit.derivation_id == survivor["cert"].get("derivation_id")


def test_e2e_multi_premise_behavior_commits() -> None:
    case = "test_e2e_multi_premise_behavior_commits"
    records = _behavior_records()
    bundle = _certified_bundle(
        "behavior/v1",
        records,
        _behavior_proposition(),
        {"retrieval_config": {"scope": REPO}},
        None,
        {"approved_provenance_refs": [TEST_ONLY_AUTHORITY]},
    )
    _assert_pins(case, bundle["hashes"])
    _assert_checker_row(
        "test_e2e_behavior_valid_commit",
        bundle,
        {"approved_provenance_refs": [TEST_ONLY_AUTHORITY]},
    )
    committed_ids = {str(p["evidence_id"]) for p in bundle["cert"].get("premises", [])}
    assert committed_ids == {"ev-c4-behavior-0", "ev-c4-behavior-1", "ev-c4-behavior-2"}, (
        f"{case}: committed premise ids {sorted(committed_ids)}"
    )
    for skipped in sorted(committed_ids):
        partial = [p for p in bundle["premises"] if str(p["evidence_id"]) != skipped]
        with __import__("pytest").raises(CertificationError, match="RULE_NOT_DISCHARGED"):
            certify(bundle["inputs"], partial, bundle["artifacts"], None)
    decision, reason, commit = _compose_and_commit(bundle, [bundle["outcome"]])
    entry = C4_CASES[case]
    assert decision.value == entry["expected"], f"{case}: decision={decision} reason={reason!r}"
    assert entry["reason"] in reason
    assert commit is not None
    assert commit.derivation_id == bundle["cert"].get("derivation_id")


def _deny_definition_case(case_key: str, **delta: Any) -> tuple[Any, str, dict[str, Any]]:
    bundle = _definition_bundle()
    _assert_pins(case_key, bundle["hashes"])
    cert = bundle["cert"]
    snapshot = copy.deepcopy(cert)
    digest_before = _cert_hash(cert)
    decision, reason = _decide(cert, CheckerOutcome.VALID, **delta)
    _assert_decision_row(case_key, decision, reason)
    _assert_preserved(case_key, cert, snapshot, digest_before)
    assert str(cert.get("context_id") or "")
    assert str(cert.get("derivation_id") or "")
    return decision, reason, bundle


def test_e2e_deny_no_pinned_context() -> None:
    _deny_definition_case("test_e2e_deny_no_pinned_context", expected_context_id=None)


def test_e2e_deny_not_checker_accepted() -> None:
    _deny_definition_case(
        "test_e2e_deny_not_checker_accepted",
        checker_outcome=CheckerOutcome.INVALID_INFERENCE,
    )


def test_e2e_deny_conflict_unresolved() -> None:
    _deny_definition_case("test_e2e_deny_conflict_unresolved", reconciliation_adjudicated=False)


def test_e2e_deny_stale_context() -> None:
    decision, reason, bundle = _deny_definition_case(
        "test_e2e_deny_stale_context", expected_context_id=WRONG_CONTEXT
    )
    assert decision == PublicationDecision.DENY
    with __import__("pytest").raises(ValueError, match="STALE_CONTEXT"):
        build_commit(
            str(bundle["cert"].get("derivation_id") or ""),
            str(bundle["cert"].get("context_id") or ""),
            WRONG_CONTEXT,
        )


def test_e2e_deny_policy_denied() -> None:
    _deny_definition_case(
        "test_e2e_deny_policy_denied",
        policy_eligible=False,
        policy_reason="c4-deny-pin",
    )


def test_e2e_denied_decision_preserves_proof_artifact() -> None:
    case = "test_e2e_denied_decision_preserves_proof_artifact"
    bundle = _definition_bundle()
    _assert_pins(case, bundle["hashes"])
    cert = bundle["cert"]
    snapshot = copy.deepcopy(cert)
    digest_before = _cert_hash(cert)
    deltas: list[dict[str, Any]] = [
        {"expected_context_id": None},
        {"checker_outcome": CheckerOutcome.INVALID_INFERENCE},
        {"reconciliation_adjudicated": False},
        {"expected_context_id": WRONG_CONTEXT},
        {"policy_eligible": False, "policy_reason": "c4-deny-pin"},
    ]
    for delta in deltas:
        decision, reason = _decide(cert, CheckerOutcome.VALID, **delta)
        assert decision == PublicationDecision.DENY, f"{case}/{delta}: {reason!r}"
        _assert_preserved(case, cert, snapshot, digest_before)
        assert str(cert.get("context_id") or "")
        assert str(cert.get("derivation_id") or "")
    entry = C4_CASES[case]
    assert entry["expected"] == "PRESERVED"


def test_no_production_consumer_of_g4_decision() -> None:
    case = "test_no_production_consumer_of_g4_decision"
    root = Path(__file__).resolve().parents[2]
    src_root = root / "src"
    assert src_root.is_dir(), f"searched-root missing: {src_root}"
    decision_api = re.compile(
        r"\bdecide_publication\b|\bbuild_commit\b|\bdecide_guarded_promotion\b"
    )
    harness_ref = re.compile(r"test_proof_kernel_g4_component4_integration")
    harness_literal = re.compile(r"component4_integration|g4_component4")
    allowlisted = (
        src_root
        / "investigation_agent_platform"
        / "application"
        / "proof"
        / "publication_commit_service.py",
        src_root
        / "investigation_agent_platform"
        / "application"
        / "proof"
        / "kernel_conclude_wiring.py",
    )
    for target in allowlisted:
        assert target.is_file(), f"allowlist target missing: {target}"
    grandfathered = allowlisted[0]
    text = grandfathered.read_text(encoding="utf-8")
    assert "decide_guarded_promotion" in text, "grandfathered consumer lost its guarded ref"
    assert "decide_publication" not in text, "grandfathered consumer kept the demoted scalar ref"
    assert "build_commit" not in text, "grandfathered consumer gained a build ref"
    assert "component4_integration" not in text, "grandfathered consumer refs the harness"
    violations: list[str] = []
    pattern1_files: set[str] = set()
    package_root = src_root / "investigation_agent_platform"
    for path in sorted(src_root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        try:
            in_domain_proof = path.relative_to(package_root / "domain" / "proof")
        except ValueError:
            in_domain_proof = None
        if in_domain_proof is not None:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            violations.append(f"P0 unreadable {rel}: {exc}")
            continue
        for lineno, line in enumerate(lines, 1):
            if decision_api.search(line):
                pattern1_files.add(rel)
                if path != grandfathered and path not in allowlisted:
                    violations.append(f"P1 decision-API ref {rel}:{lineno}: {line.strip()}")
                elif "decide_publication" in line or "build_commit" in line:
                    violations.append(
                        f"P1 demoted-API ref in allowlist {rel}:{lineno}: {line.strip()}"
                    )
            if harness_ref.search(line):
                violations.append(f"P2 harness-module ref {rel}:{lineno}: {line.strip()}")
            if harness_literal.search(line):
                violations.append(f"P3 harness literal {rel}:{lineno}: {line.strip()}")
    assert pattern1_files == {t.relative_to(root).as_posix() for t in allowlisted}, (
        f"{case}: P1 files {sorted(pattern1_files)} != allowlist-only"
    )
    assert violations == [], f"{case}: {len(violations)} out-of-allowlist: {violations[:8]}"
    entry = C4_CASES[case]
    assert entry["expected"] == "no violations"


def _serialize_fixtures(fixtures: dict[str, Any]) -> str:
    if not fixtures:
        return "(none)"
    return "; ".join(f"{key}={fixtures[key]}" for key in sorted(fixtures))


def test_c4_report_table_complete() -> None:
    assert len(C4_CASES) == 19, f"C4_CASES has {len(C4_CASES)} entries, registry pins 19"
    assert set(D1_NAMES) | set(D2_NAMES) | {SCAN_NAME} == set(C4_CASES), (
        f"C4_CASES keys {sorted(C4_CASES)} != normative inventory"
    )
    assert len(D1_NAMES) == 12 and len(D2_NAMES) == 6
    rows: dict[str, tuple[str, str, str]] = {}
    for line in str(__doc__ or "").splitlines():
        match = re.match(r"^\|\s*(test_\S+)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|?\s*$", line)
        if match:
            name, fixtures_cell, expected_cell, actual_cell = match.groups()
            assert name not in rows, f"duplicate table row: {name}"
            rows[name] = (fixtures_cell, expected_cell, actual_cell)
    assert set(rows) == set(C4_CASES), f"table rows {sorted(rows)} != C4_CASES {sorted(C4_CASES)}"
    for key, entry in C4_CASES.items():
        fixtures_cell, expected_cell, actual_cell = rows[key]
        assert fixtures_cell == _serialize_fixtures(entry["fixtures"]), (
            f"{key}: table fixtures {fixtures_cell!r} != pinned"
        )
        pinned_expected = f"{entry['expected']} / {entry['reason']}"
        assert expected_cell == pinned_expected, (
            f"{key}: table expected {expected_cell!r} != pinned {pinned_expected!r}"
        )
        assert actual_cell == expected_cell, (
            f"{key}: actual {actual_cell!r} != expected {expected_cell!r}"
        )


def _k11_mismatch_cert(cert: dict[str, Any]) -> dict[str, Any]:
    """Copy a certified bundle's cert with a non-matching scope revision (R-C3-01 detector)."""
    bad = dict(cert)
    bad["scope"] = {**dict(cert.get("scope") or {}), "revision": "rev-9"}
    return bad


def test_k11_direct_definition_revision_mismatch() -> None:
    bundle = _definition_bundle()
    semantic = _SEMANTICS["definition/v1"]
    ok_outcome, _ = semantic(
        bundle["cert"], bundle["proposition"], bundle["manifest"], bundle["artifacts"]
    )
    assert ok_outcome == CheckerOutcome.VALID
    outcome, reason = semantic(
        _k11_mismatch_cert(bundle["cert"]),
        bundle["proposition"],
        bundle["manifest"],
        bundle["artifacts"],
    )
    assert outcome == CheckerOutcome.SCOPE_MISMATCH
    assert "K11" in reason


def test_k11_direct_call_edge_revision_mismatch() -> None:
    bundle = _certified_bundle("call_edge/v1", [_call_edge_record()], _call_edge_proposition(), {})
    semantic = _SEMANTICS["call_edge/v1"]
    ok_outcome, _ = semantic(
        bundle["cert"], bundle["proposition"], bundle["manifest"], bundle["artifacts"]
    )
    assert ok_outcome == CheckerOutcome.VALID
    outcome, reason = semantic(
        _k11_mismatch_cert(bundle["cert"]),
        bundle["proposition"],
        bundle["manifest"],
        bundle["artifacts"],
    )
    assert outcome == CheckerOutcome.SCOPE_MISMATCH
    assert "K11" in reason


def test_k11_direct_behavior_revision_mismatch() -> None:
    authority = {"approved_provenance_refs": [TEST_ONLY_AUTHORITY]}
    bundle = _certified_bundle(
        "behavior/v1",
        _behavior_records(),
        _behavior_proposition(),
        {"retrieval_config": {"scope": REPO}},
        None,
        authority,
    )
    semantic = _SEMANTICS["behavior/v1"]
    ok_outcome, _ = semantic(
        bundle["cert"], bundle["proposition"], bundle["manifest"], bundle["artifacts"], authority
    )
    assert ok_outcome == CheckerOutcome.VALID
    outcome, reason = semantic(
        _k11_mismatch_cert(bundle["cert"]),
        bundle["proposition"],
        bundle["manifest"],
        bundle["artifacts"],
        authority,
    )
    assert outcome == CheckerOutcome.SCOPE_MISMATCH
    assert "K11" in reason


def test_certify_definition_revision_mismatch_refuses() -> None:
    record = _definition_record()
    record = dict(record, revision=REV_B)
    asm = _assemble("definition/v1", [record], _definition_proposition(), {})
    with __import__("pytest").raises(CertificationError, match="RULE_NOT_DISCHARGED"):
        certify(asm["inputs"], asm["premises"], asm["artifacts"])


def test_certify_call_edge_revision_mismatch_refuses() -> None:
    record = _call_edge_record()
    record = dict(record, revision=REV_B)
    asm = _assemble("call_edge/v1", [record], _call_edge_proposition(), {})
    with __import__("pytest").raises(CertificationError, match="RULE_NOT_DISCHARGED"):
        certify(asm["inputs"], asm["premises"], asm["artifacts"])
