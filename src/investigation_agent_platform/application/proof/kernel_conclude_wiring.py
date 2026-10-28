# src/investigation_agent_platform/application/proof/kernel_conclude_wiring.py
"""B2 kernel wiring: conclude-path tap (shadow-first, flag-gated).

Modes via ``IAP_KERNEL_WIRING`` (process env, read per call so tests can
flip it): ``off`` (default) does nothing; ``shadow`` evaluates the
guarded promotion entry per mappable claim and logs the verdict without
affecting the run; ``enforcing`` additionally denies the conclusion when
any evaluated claim denies. Unknown values fail closed to ``off`` with a
warning. No B2 data migration, no writer routing, no persistence here.

Claim coverage v1 (explicit boundary): definition claims built from
banked ``SOURCE_CODE`` rows carrying a ``RESOLVED``
``code_fact_resolution_v2`` with symbol + file + revision and citable
window text. Everything else yields an explicit skip record (never
silent, never promoting). Single-repo assumption: all rows bind to the
tenant primary repository from operator config; anything else skips
with a logged reason.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from investigation_agent_platform.domain.proof.obligations import (
    DecisionPolicy,
    decide_guarded_promotion,
    encode_evidence_bytes,
    encode_trusted_context_bytes,
)

logger = logging.getLogger(__name__)

FLAG_ENV_VAR = "IAP_KERNEL_WIRING"
MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ENFORCING = "enforcing"

POLICY_REPOS_ENV_VAR = "IAP_KERNEL_POLICY_REPOSITORIES"
POLICY_AUTHORITIES_ENV_VAR = "IAP_KERNEL_POLICY_AUTHORITIES"


def kernel_wiring_mode() -> str:
    """Flag accessor: off unless exactly shadow/enforcing (unknown -> off)."""
    raw = str(os.environ.get(FLAG_ENV_VAR, "") or "").strip().lower()
    if raw in (MODE_SHADOW, MODE_ENFORCING):
        return raw
    if raw and raw != MODE_OFF:
        logger.warning("unknown %s=%r; failing closed to off", FLAG_ENV_VAR, raw)
    return MODE_OFF


def tenant_primary_repository() -> str | None:
    """First operator-configured repository (single-repo v1 assumption)."""
    raw = str(os.environ.get(POLICY_REPOS_ENV_VAR, "") or "")
    repos = [part.strip() for part in raw.split(",") if part.strip()]
    return repos[0] if repos else None


def select_policy(tenant_id: str) -> DecisionPolicy | None:
    """Operator-supplied trusted policy (B2 owns tenant->policy selection).

    Returns None when the operator has configured no repositories, in
    which case the guarded entry denies closed (no-policy).
    """
    raw = str(os.environ.get(POLICY_REPOS_ENV_VAR, "") or "")
    repos = tuple(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))
    if not repos:
        return None
    authorities = tuple(
        dict.fromkeys(
            part.strip() for part in str(os.environ.get(POLICY_AUTHORITIES_ENV_VAR, "") or "").split(",") if part.strip()
        )
    )
    return DecisionPolicy(
        policy_id=f"policy:{tenant_id}:conclude:v1",
        repositories=repos,
        approved_mapping_provenance_refs=authorities,
    )


def _resolution_v2(row: Any) -> dict[str, Any] | None:
    try:
        attributes = getattr(row, "attributes", None) or {}
        fact = attributes.get("code_fact_resolution_v2")
        return dict(fact) if isinstance(fact, dict) else None
    except Exception:
        return None


def _row_window(row: Any) -> str:
    try:
        attributes = getattr(row, "attributes", None) or {}
        window = attributes.get("window_text")
        if isinstance(window, str) and window.strip():
            return window
        snippet = getattr(row, "content_snippet", "") or ""
        return str(snippet)
    except Exception:
        return ""


def mappable_definition_rows(rows: list[Any]) -> tuple[list[Any], list[dict[str, Any]]]:
    """Split banked rows into mappable definition rows + skip records."""
    mappable: list[Any] = []
    skips: list[dict[str, Any]] = []
    for row in rows:
        try:
            evidence_id = str(getattr(row, "evidence_id", "") or "")
            if str(getattr(getattr(row, "evidence_type", ""), "value", getattr(row, "evidence_type", "")) or "") != "SOURCE_CODE":
                skips.append({"evidence_id": evidence_id, "reason": "unmapped-kind"})
                continue
            fact = _resolution_v2(row)
            if fact is None or str(fact.get("status") or "") != "RESOLVED":
                skips.append({"evidence_id": evidence_id, "reason": "unresolved-code-fact"})
                continue
            symbol = str(fact.get("subject_identity") or fact.get("declaring_type") or "").strip()
            location = fact.get("declaration_location") or {}
            file_path = str(location.get("file_path") or "").strip() if isinstance(location, dict) else ""
            revision = str(fact.get("revision") or "").strip()
            window = _row_window(row)
            if not symbol or not file_path:
                skips.append({"evidence_id": evidence_id, "reason": "missing-symbol-or-file"})
                continue
            if not revision:
                skips.append({"evidence_id": evidence_id, "reason": "missing-revision"})
                continue
            if symbol not in window:
                skips.append({"evidence_id": evidence_id, "reason": "missing-window-text"})
                continue
            line_start = location.get("line_start") if isinstance(location, dict) else None
            line_end = location.get("line_end") if isinstance(location, dict) else None
            mappable.append(
                {
                    "row": row,
                    "evidence_id": evidence_id,
                    "symbol": symbol,
                    "file_path": file_path,
                    "revision": revision,
                    "window_text": window,
                    "line_start": int(line_start or 1),
                    "line_end": int(line_end or max(int(line_start or 1), 1)),
                }
            )
        except Exception as exc:
            try:
                evidence_id = str(getattr(row, "evidence_id", "") or "?")
            except Exception:
                evidence_id = "?"
            skips.append({"evidence_id": evidence_id, "reason": f"adapter-error:{type(exc).__name__}"})
    return mappable, skips


def _definition_bundle(item: dict[str, Any], repository: str) -> dict[str, Any]:
    """Assemble + certify one definition claim bundle (pure assembly).

    Returns the record, proposition, manifest, certificate, and content
    digest. Raises on any defect (the caller records it, never propagates).
    """
    import hashlib as _hashlib
    import json as _json

    from investigation_agent_platform.domain.proof.certificate import certify
    from investigation_agent_platform.domain.proof.evaluation import EvaluationInputs
    from investigation_agent_platform.domain.proof.manifest import build_manifest
    from investigation_agent_platform.domain.proof.rules import RULE_BUNDLE_VERSION, get_rule

    record = {
        "evidence_id": f"ev-kw-{item['evidence_id']}",
        "premise_type": "RANGE_READ_FACT",
        "repository": repository,
        "file_path": item["file_path"],
        "revision": item["revision"],
        "line_start": item["line_start"],
        "line_end": item["line_end"],
        "target": item["symbol"],
        "window_text": item["window_text"],
    }
    proposition = {
        "canon_version": "canon/v1",
        "form": "EXISTENCE",
        "quantifier": "EXISTENTIAL",
        "predicate": f"defined({item['symbol']})",
        "target": {"symbol": item["symbol"], "file_path": item["file_path"]},
        "scope": {"revision": item["revision"]},
    }
    art = {k: v for k, v in record.items() if k not in ("evidence_id", "content_hash", "premise_type")}
    digest = "sha256:" + _hashlib.sha256(
        _json.dumps(art, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    rule = get_rule("definition/v1")
    manifest = build_manifest(
        [record["evidence_id"]], {record["evidence_id"]: digest}, premise_types={record["evidence_id"]: "RANGE_READ_FACT"}
    )
    inputs = EvaluationInputs(
        proposition=dict(proposition),
        manifest=manifest,
        rule_bundle_version=RULE_BUNDLE_VERSION,
        rule_id=rule.rule_id,
        rule_version=rule.rule_version,
        implementation_digest=rule.implementation_digest,
    )
    cert = certify(inputs, [dict(record, content_hash=digest)], {record["evidence_id"]: art})
    return {"record": record, "proposition": proposition, "manifest": manifest, "cert": cert, "digest": digest, "artifact": art}


def evaluate_conclusion_kernel(
    tenant_id: str,
    investigation_id: Any,
    evidence_rows: list[Any],
    policy: DecisionPolicy | None,
    context_id: str = "ctx-conclude",
) -> dict[str, Any]:
    """Run the guarded promotion entry per mappable claim (never raises).

    Returns a report: evaluated claims with (decision, reason, derivation)
    plus explicit skip records. All exceptions become recorded entries;
    the caller decides whether a DENY blocks (enforcing) or logs
    (shadow).
    """
    from investigation_agent_platform.domain.proof.publication import PublicationDecision

    report: dict[str, Any] = {"claims": [], "skips": [], "mode": kernel_wiring_mode()}
    if policy is None:
        report["skips"].append({"evidence_id": "*", "reason": "no-policy"})
        report["policy_present"] = False
        return report
    report["policy_present"] = True
    repository = (policy.repositories or ("",))[0]
    mappable, skips = mappable_definition_rows(evidence_rows)
    report["skips"].extend(skips)
    seen_symbols: set[str] = set()
    for item in mappable:
        key = (item["symbol"], item["file_path"])
        if key in seen_symbols:
            report["skips"].append({"evidence_id": item["evidence_id"], "reason": "duplicate-symbol"})
            continue
        seen_symbols.add(key)
        try:
            import json as _json

            from investigation_agent_platform.domain.proof.publication import PublicationDecision
            from investigation_agent_platform.domain.proof.rules import RULE_BUNDLE_VERSION

            bundle = _definition_bundle(item, repository)
            record = bundle["record"]
            proposition = bundle["proposition"]
            cert = bundle["cert"]
            digest = bundle["digest"]
            art = bundle["artifact"]
            evidence_bytes = encode_evidence_bytes(
                {
                    "rule_id": "definition/v1",
                    "proposition": proposition,
                    "certificate": cert,
                    "manifest": _json.loads(bundle["manifest"].model_dump_json()),
                    "artifacts": {record["evidence_id"]: art},
                }
            )
            trusted_bytes = encode_trusted_context_bytes(
                {
                    "rule_bundle_version": RULE_BUNDLE_VERSION,
                    "model_encoding": {},
                    "reconciliation_state": "unreconciled",
                    "known_normalization_ops": [],
                    "derivation_deps": {},
                    "repository_bindings": [
                        {
                            "evidence_id": record["evidence_id"],
                            "content_digest": digest,
                            "repository_id": repository,
                        }
                    ],
                    "expected_context_id": str(cert.get("context_id") or context_id),
                    "reconciliation_adjudicated": True,
                    "policy_eligible": True,
                    "policy_reason": "",
                }
            )
            decision, reason = decide_guarded_promotion(policy, evidence_bytes, trusted_bytes)
            entry: dict[str, Any] = {
                "evidence_id": item["evidence_id"],
                "symbol": item["symbol"],
                "decision": decision.value,
                "reason": reason,
            }
            if decision == PublicationDecision.COMMIT:
                entry["derivation_id"] = str(cert.get("derivation_id") or "")
            report["claims"].append(entry)
        except Exception as exc:
            report["claims"].append(
                {
                    "evidence_id": item["evidence_id"],
                    "symbol": item.get("symbol", "?"),
                    "decision": "DENY",
                    "reason": f"CHECKER_FAILURE:kernel-wiring:{type(exc).__name__}",
                }
            )
    return report


async def kernel_gate_for_conclusion(
    tenant_id: str,
    investigation_id: Any,
    ctx: Any,
) -> tuple[bool, list[str]]:
    """Enforcing/shadow gate for the conclude path (never raises).

    Returns (allowed, blockers). ``off`` and ``shadow`` always allow
    (shadow logs the report); ``enforcing`` denies when any evaluated
    claim denies. Skips never deny — only evaluated DENYs block.
    """
    try:
        mode = kernel_wiring_mode()
        if mode == MODE_OFF:
            return True, []
        evidence_repo: Any = getattr(ctx, "evidence_repo", None)
        rows: list[Any] = []
        if evidence_repo is not None:
            try:
                rows = await evidence_repo.find_by_investigation_id(tenant_id, investigation_id)
            except Exception as exc:
                logger.warning("kernel wiring: evidence load failed: %r", exc)
                if mode == MODE_ENFORCING:
                    return False, [f"kernel-evidence-unavailable:{type(exc).__name__}"]
                return True, []
        policy = select_policy(tenant_id)
        report = evaluate_conclusion_kernel(tenant_id, investigation_id, rows or [], policy)
        denied = [c for c in report.get("claims", []) if c.get("decision") != "COMMIT"]
        try:
            logger.info(
                "kernel wiring %s: %d claims (%d commit, %d deny), %d skips",
                mode,
                len(report.get("claims", [])),
                len(report.get("claims", [])) - len(denied),
                len(denied),
                len(report.get("skips", [])),
            )
        except Exception:
            pass
        if mode != MODE_ENFORCING:
            return True, []
        if not report.get("policy_present", False):
            # Fail closed: enforcing without a configured policy cannot
            # verify anything — misconfiguration must deny, never pass.
            return False, ["kernel-no-policy"]
        if denied:
            blockers = [f"kernel-denied:{c.get('symbol', '?')}:{c.get('reason', '')}" for c in denied]
            return False, blockers
        return True, []
    except Exception as exc:
        logger.warning("kernel wiring: gate failed closed: %r", exc)
        return False, [f"kernel-gate-failure:{type(exc).__name__}"]
