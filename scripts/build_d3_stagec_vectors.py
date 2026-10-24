#!/usr/bin/env python3
"""Deterministic builder for the Stage C schema vectors (v1.15).

Regenerates docs/IAP-proof-kernel-d3-stageC-schema-vectors-v1.15.json from
explicit scenario code (no hidden state, no randomness). Each report case
composes source records first (turns, attempts, receipts, controls with
shadow counterparts, capture events, cohort), then computes the expected
report from those records; each negative case mutates exactly one recomputed
field so every failure maps to a single invariant (R1–R8, CONTROL_MATCH
counterpart recompute, reservation-cap binding, terminal/open accounting,
formula identity, freeze binding, closed vocabularies). Every report case
carries the freeze its report is bound to (`report["freeze_hash"]` equals the
recomputed freeze digest); two negatives prove the binding (retargeted report
hash, corrupted freeze self-hash).

Usage:
  .venv/bin/python scripts/build_d3_stagec_vectors.py

The script asserts its own preconditions (formula set equals the registry's
requiredMetricSet, golden digests recompute from the pinned canonical
bytes) before writing. Re-running must produce byte-identical output.
"""
import copy
import hashlib
import json
import sys

REGISTRY_PATH = "docs/IAP-proof-kernel-d3-stageC-harness-schemas-v1.13.json"
OUT_PATH = "docs/IAP-proof-kernel-d3-stageC-schema-vectors-v1.15.json"
DOMAIN = b"iap:d3-stagec:metric-formula/v1"
REGISTRY_ID = "iap:d3-stagec:registry/v12"

# Formulas are read from the registry's authoritative formulaCatalog, never
# maintained as a second hardcoded list (SC-V19-02). The builder asserts the
# catalog's key set equals requiredMetricSet and that every catalog digest
# recomputes from its catalog text before building.
def load_formula_catalog():
    reg = json.load(open(REGISTRY_PATH, encoding="utf-8"))
    assert reg["$id"] == REGISTRY_ID, "builder targets a different registry"
    required = reg["$defs"]["requiredMetricSet"]["const"]
    catalog = reg["$defs"]["formulaCatalog"]["const"]
    assert sorted(catalog.keys()) == sorted(required), \
        "catalog set must equal the required metric set exactly"
    for mid, entry in catalog.items():
        assert entry["digest"] == formula_digest(entry["text"]), \
            f"catalog digest does not recompute for {mid}"
    return required, catalog

CELLS = ("accept-PASS", "accept-FAIL", "nonaccept-PASS", "nonaccept-FAIL")
TAPS = ("T1", "T2a", "T2b", "T3")
COUNT_KEY_RUN = "rs-001"


def H(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode()).hexdigest()


def payload_digest(receipt_id: str) -> str:
    return "sha256:" + hashlib.sha256(
        f"payload-bytes:{receipt_id}".encode()).hexdigest()


def formula_digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(DOMAIN + b"\x00" +
                                       text.encode()).hexdigest()


def freeze_record(**kw):
    """Schema-valid freeze bound to run set rs-001 (registry `freeze`)."""
    h = H("ab")
    base = {"schema": "freeze/v4", "run_set_id": "rs-001",
            "protocol_hash": h, "code_revision": "c" * 40,
            "tree_hash": h,
            "flags": {"IAP_SHADOW_TYPED_DEFINITIONS": "1"},
            "authority_flag_state": "MUST_BE_ABSENT",
            "cohort": [{"fixture_id": "L1", "fixture_version": h,
                        "case_id": "c1", "prompt_hash": h}],
            "prompts": [{"prompt_id": "p1", "hash": h}],
            "comparison_hash": h,
            "cell_budget": {"accept-PASS": 12, "accept-FAIL": 12,
                            "nonaccept-PASS": 12, "nonaccept-FAIL": 12},
            "budget_rule_hash": h, "control_hash": h,
            "exclusion_hash": h, "capture_hash": h,
            "dependency_hash": h, "harness_version": "harness-0.0.0",
            "decisions": {"OD-01": h, "OD-02": h, "D-01": h,
                          "BUD-01": h, "DIGEST-01": h, "ID-01": h,
                          "CAPTURE-01": h},
            "comparison_preimage": {"path": "artifacts/comparison.txt",
                                    "digest": h},
            "budget_preimage": {"path": "artifacts/budget.txt",
                                "digest": h},
            "control_preimage": {"path": "artifacts/control.txt",
                                 "digest": h},
            "exclusion_preimage": {"path": "artifacts/exclusion.txt",
                                   "digest": h},
            "capture_preimage": {"path": "artifacts/capture.txt",
                                 "digest": h},
            "dependency_preimage": {"path": "artifacts/dependency.txt",
                                    "digest": h},
            "key_list_ref": {"path": "artifacts/key_list.json",
                             "digest": h},
            "total_reservations_max": 48, "freeze_hash": h}
    base.update(kw)
    return base


def freeze_digest_of(record) -> str:
    """Recomputed freeze digest (canonical bytes excluding the self field).

    Builder bodies are ASCII-only by construction, so these bytes equal the
    package `canonical_bytes` exactly.
    """
    body = {k: v for k, v in record.items() if k != "freeze_hash"}
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def build_formulas(catalog):
    return {mid: {"text": entry["text"], "digest": entry["digest"]}
            for mid, entry in catalog.items()}


def base_scenario():
    """24 recorded attempts: 20 eligible T1 turns + 1 eligible T3 turn +
    2 excluded turns + 1 non-eligible turn. Every turn links to its
    attempt (attempt_ref) and receipt (receipt_ref) and carries the join
    identity (receipt_index, iteration, activity); every receipt carries
    complete identity (run set, workflow, iteration, order, activity)
    plus its payload digest."""
    def act(tag):
        return {"namespace": "n", "workflow_id": "w",
                "workflow_run_id": "wr", "activity_id": f"act-{tag}",
                "attempt": 1}

    def turn(cell, elig, reason, tap, ck, order, iteration, tag, rid, aid):
        return {
            "cell": cell, "eligibility": elig, "reason": reason,
            "tap": tap, "count_key": ck,
            "attempt_ref": aid, "receipt_ref": rid,
            "payload_digest": payload_digest(rid),
            "receipt_index": order, "iteration": iteration,
            "activity": act(tag),
        }

    previous_receipt_digest = None

    def receipt(rid, event, order, iteration, tag, capture_error=False):
        nonlocal previous_receipt_digest
        record = {"schema": "receipt/v2", "id": rid, "run_set_id": COUNT_KEY_RUN,
                "workflow_id": "w", "workflow_run_id": "wr",
                "iteration": iteration, "order": order,
                "activity": act(tag), "event": event,
                "capture_error": capture_error,
                "payload_digest": payload_digest(rid),
                "payload_ref": f"artifacts/{rid}.json",
                "previous_digest": previous_receipt_digest}
        previous_receipt_digest = record["payload_digest"]
        return record

    cells = (["accept-PASS"] * 6 + ["accept-FAIL"] * 5 +
             ["nonaccept-PASS"] * 5 + ["nonaccept-FAIL"] * 4)
    turns, receipts, records = [], [], []
    for i, cell in enumerate(cells):
        rid, aid = f"e-{i}", f"a-{i}"
        turns.append(turn(
            cell, "ELIGIBLE", "JOIN_OK", "T1",
            [COUNT_KEY_RUN, f"inv-{i}", f"hyp-{i}", i,
             "BOUND_FULL", H(f"env-{i}")],
            i, i, str(i), rid, aid))
        receipts.append(receipt(rid, "VERIFY_COMPLETED", i, i, str(i)))
        records.append({"id": aid, "disposition": "ELIGIBLE"})
    turns.append(turn(
        "accept-PASS", "ELIGIBLE", "JOIN_OK", "T3",
        ["rs-001", "inv-t3", "hyp-t3", 99, "CONCLUDE_GATE", H("env-t3")],
        20, 99, "t3", "e-t3", "a-t3"))
    receipts.append(receipt("e-t3", "CONCLUDE_COMPLETED", 20, 99, "t3"))
    records.append({"id": "a-t3", "disposition": "ELIGIBLE"})
    for idx, (reason, disp, key) in enumerate([
            ("TRUNCATED_AWAY", "EXCLUDED", "x1"),
            ("SUPERSEDED_PRESENT_EXCLUDED", "EXCLUDED", "x2"),
            ("NON_ELIGIBLE_NO_CANDIDATE", "NON_ELIGIBLE", "x3")]):
        rid, aid = f"e-{key}", f"a-{key}"
        turns.append(turn(
            "accept-PASS", disp, reason, "T1",
            ["rs-001", f"inv-{key}", f"hyp-{key}", 20 + idx,
             "BOUND_FULL", H(f"env-{key}")],
            21 + idx, 20 + idx, key, rid, aid))
        receipts.append(receipt(rid, "VERIFY_COMPLETED", 21 + idx,
                               20 + idx, key))
        records.append({"id": aid, "disposition": disp})
    # One stray capture-error receipt with no turn (transport loss event).
    receipts.append(receipt("e-cap-1", "VERIFY_COMPLETED", 24, 24,
                            "cap-1", capture_error=True))
    return turns, receipts, records


MATCH_TUPLE_FIELDS = ("fixture_id", "fixture_version", "prompt_hash",
                      "code_revision", "tenant", "database",
                      "capture_hash")


def control_counterpart(fx, order, back_ref):
    """Shadow counterpart record for one native control (registry `controlCounterpart`)."""
    return {
        "fixture_id": fx, "fixture_version": H(fx),
        "prompt_hash": H("prompt"), "code_revision": "c" * 40,
        "tenant": "probe-tenant", "database": "probe-db",
        "capture_hash": H(f"cap-{fx}"),
        "counterpart_ref": back_ref, "control_order": order,
    }


def control_label(record) -> str:
    """Recompute one control record's label from its shadow counterpart.

    Mirrors the package `compare_pair`: UNMATCHED when no counterpart is
    recorded, MISMATCH on any tuple-field difference or missing/disagreeing
    order evidence, MATCHED only on full agreement.
    """
    shadow = record.get("counterpart")
    if shadow is None:
        return "UNMATCHED"
    if any(record.get(f) != shadow.get(f) for f in MATCH_TUPLE_FIELDS):
        return "MISMATCH"
    if not record.get("counterpart_ref") or record.get("control_order") is None:
        return "MISMATCH"
    if not shadow.get("counterpart_ref") or shadow.get("control_order") is None:
        return "MISMATCH"
    if record.get("control_order") != shadow.get("control_order"):
        return "MISMATCH"
    return "MATCHED"


def base_controls():
    ctrls = []
    for i, fx in enumerate(["L1", "L2", "L3"]):
        ctrls.append({
            "fixture_id": fx, "fixture_version": H(fx),
            "prompt_hash": H("prompt"), "code_revision": "c" * 40,
            "sequence": i + 1, "match": "MATCHED",
            "outcome_digest": H(f"out-{fx}"), "tenant": "probe-tenant",
            "database": "probe-db", "capture_hash": H(f"cap-{fx}"),
            "counterpart_ref": f"shadow-rs-001-000{i + 7}",
            "control_order": i + 1,
            "counterpart": control_counterpart(fx, i + 1, f"native-{fx}"),
        })
    return ctrls


def recompute_report(turns, records, receipts, controls, cohort,
                     capture_events, verdict, terminal, formulas,
                     limitations=(), report_id="r1"):
    """Recompute every report field from source records."""
    elig = [t for t in turns
            if t["eligibility"] == "ELIGIBLE" and t["tap"] == "T1"]
    cells = {c: sum(1 for t in elig if t["cell"] == c) for c in CELLS}
    tally = {"ELIGIBLE": 0, "EXCLUDED": 0, "NON_ELIGIBLE": 0,
             "VOID_CONTRIBUTING": 0}
    for a in records:
        tally[a["disposition"]] = tally.get(a["disposition"], 0) + 1
    excl = {}
    for t in turns:
        if t["eligibility"] in ("EXCLUDED", "NON_ELIGIBLE", "VOID"):
            excl[t["reason"]] = excl.get(t["reason"], 0) + 1
    fixtures = sorted({c["fixture_id"] for c in controls})
    matched = []
    for fx in fixtures:
        recs = [c for c in controls if c["fixture_id"] == fx]
        if len(recs) != 1:
            continue
        if control_label(recs[0]) == "MATCHED" and recs[0].get("match") == "MATCHED":
            matched.append(recs[0])
    EVENT_TO_COUNTER = {"evaluator_failure": "evaluator_failures",
                          "capture_error": "capture_errors",
                          "telemetry_omission": "telemetry_omissions",
                          "dropped": "dropped",
                          "duplicate": "duplicates"}
    cap = {"evaluator_failures": 0, "capture_errors": 0,
           "telemetry_omissions": 0, "dropped": 0, "duplicates": 0}
    for r in receipts:
        if r.get("capture_error"):
            cap["capture_errors"] += 1
    for t in turns:
        if t["reason"] in ("TELEMETRY_OMISSION_PATH_A",
                           "TELEMETRY_OMISSION_PATH_B_UNEXPLAINED"):
            cap["telemetry_omissions"] += 1
    for e in capture_events:
        assert e["kind"] in EVENT_TO_COUNTER, \
            f"unknown capture event kind {e['kind']!r}"
        cap[EVENT_TO_COUNTER[e["kind"]]] += 1
    taps = {}
    for tap in TAPS:
        tt = [t for t in turns if t["tap"] == tap]
        taps[tap] = {
            "population": len(tt),
            "eligible": sum(1 for t in tt if t["eligibility"] == "ELIGIBLE"),
            "excluded": sum(1 for t in tt if t["eligibility"] != "ELIGIBLE"),
            "divergences": sum(1 for t in tt if t.get("divergent")),
        }
    metrics = [
        ("ELIGIBLE_T1_COUNT", len(elig), None, "COUNT",
         [{"reason": k, "count": v} for k, v in sorted(excl.items())]),
        ("CELL_ACCEPT_PASS", cells["accept-PASS"], None, "COUNT", []),
        ("CELL_ACCEPT_FAIL", cells["accept-FAIL"], None, "COUNT", []),
        ("CELL_NONACCEPT_PASS", cells["nonaccept-PASS"], None, "COUNT", []),
        ("CELL_NONACCEPT_FAIL", cells["nonaccept-FAIL"], None, "COUNT", []),
        ("CAPTURE_HEALTH", sum(cap.values()), None, "COUNT", []),
        ("PER_TAP_T1", taps["T1"]["population"], None, "COUNT", []),
        ("PER_TAP_T2a", taps["T2a"]["population"], None, "COUNT", []),
        ("PER_TAP_T2b", taps["T2b"]["population"], None, "COUNT", []),
        ("PER_TAP_T3", taps["T3"]["population"], None, "COUNT", []),
        ("CONTROL_MATCH", len(matched), len(fixtures), "RATIO", []),
    ]
    return {
        "schema": "report/v3", "run_set_id": "rs-001",
        "freeze_hash": H("freeze"), "terminal_ref": report_id,
        "manifest_digest": H("manifest"), "harness_version": "harness-0.0.0",
        "populations": {"D-01": H("d01"), "T1": "t1-frozen",
                        "T2a": "t2a-attr", "T2b": "t2b-attr",
                        "T3": "t3-separate"},
        "metrics": [{"id": mid, "numerator": num, "denominator": den,
                     "measure_kind": kind, "exclusions": exc,
                     "formula_ref": formulas[mid]["digest"]}
                    for mid, num, den, kind, exc in metrics],
        "classes": {"native_verified": 5, "native_rejection": 2,
                    "native_abstention": 1, "native_error": 1,
                    "shadow_failure": 0, "capture_failure": 1,
                    "ambiguous_join": 0},
        "capture": cap,
        "cells": cells,
        "taps": taps,
        "controls": [dict(m) for m in matched],
        "divergences": [],
        "limitations": list(limitations),
        "verdict": verdict,
        "conclusion": "MINIMUM_COVERAGE_MAY_WARRANT_PROPOSING_LARGER_SAMPLE",
        "accounting": {
            "total_reservations": len(records),
            "eligible": tally.get("ELIGIBLE", 0),
            "excluded": tally.get("EXCLUDED", 0),
            "non_eligible": tally.get("NON_ELIGIBLE", 0),
            "void_contributing": tally.get("VOID_CONTRIBUTING", 0),
            "reserved_open": 0,
        },
    }


def schema_vectors(formulas):
    h = H("ab")
    V = []

    def add(i, c, inst, valid, note=""):
        V.append({"id": i, "kind": "schema", "check": c, "instance": inst,
                  "valid": valid, "note": note})

    add("P-freeze", "freeze", freeze_record(), True)
    add("P-attempt-mismatch", "attempt",
        {"schema": "attempt/v1", "run_set_id": "rs-001", "freeze_hash": h,
         "seq": 1, "id": h, "fixture_id": "L1", "fixture_version": h,
         "case_id": "c1", "budget_token": "accept-PASS", "parent_id": None,
         "state_version": 1, "disposition": "EXCLUDED",
         "reason": "BUDGET_RESERVED_CELL_MISMATCH", "receipts": [],
         "digest": h}, True)
    add("P-receipt-genesis", "receipt",
        {"schema": "receipt/v2", "run_set_id": "rs-001", "id": "e-1",
         "event": "VERIFY_COMPLETED", "workflow_id": "w",
         "workflow_run_id": "wr", "order": 1, "iteration": 0,
         "activity": {"namespace": "n", "workflow_id": "w",
                      "workflow_run_id": "wr", "activity_id": "a",
                      "attempt": 1},
         "payload_ref": "artifacts/e1.json",
         "payload_digest": payload_digest("e-1"),
         "previous_digest": None}, True)
    add("P-perturn-6tuple", "perTurn",
        {"schema": "turn/v2", "run_set_id": "rs-001",
         "count_key": ["rs-001", "inv-1", "hyp-9", 3, "BOUND_FULL", h],
         "activity": {"namespace": "n", "workflow_id": "w",
                      "workflow_run_id": "wr", "activity_id": "a",
                      "attempt": 1},
         "iteration": 3, "receipt_index": 0, "input_set": "BOUND_FULL",
         "tap": "T1",
         "native": {"verified": True, "status": "s", "confidence": None,
                    "hypothesis_id": "hyp-9", "producing_path":
                    "general-path"},
         "shadow": {"observation_id": h,
                    "outcome": "OBSERVATION_ADMISSIBLE",
                    "stage1": "ASSOCIATION_ADMISSIBLE",
                    "reason": "STAGE1_ADMISSIBLE",
                    "scope": "source-range-only",
                    "declaration_requirement_satisfied": False},
         "bundle_digest": h, "envelope_digest": h, "cell": "accept-PASS",
         "eligibility": "ELIGIBLE", "disposition": "ELIGIBLE",
         "reasons": ["JOIN_OK"]}, True)
    add("P-ownerauthz", "ownerAuthorization",
        {"schema": "owner-authorization/v5", "approval_ref": "DEC-2026-10-24-A1",
         "run_set_id": "rs-001", "freeze_hash": h, "decision_type":
         "FREEZE_APPROVAL", "decision_digest": h, "approver": "owner",
         "approved_at": "2026-10-24T00:00:00Z", "evidence_ref":
        "amendment-A1", "key_id": "owner-key-1", "signed_digest": h,
        "algorithm": "Ed25519",
        "signature": "c2lnbmF0dXJlLXNpZ25hdHVyZS1wbGFjZWhvbGRlcg=="}, True)
    add("P-attestation", "attestation",
        {"schema": "runtime-attestation/v4", "run_set_id": "rs-001",
         "freeze_hash": h, "wrapper_id": "exec-wrapper-1",
         "code_revision": "c" * 40, "tree_hash": h,
         "flags": {"IAP_SHADOW_TYPED_DEFINITIONS": "1"},
         "tenant": "probe-tenant", "database": "probe-db",
         "dependency_hash": h, "key_id": "owner-key-1",
         "signed_digest": h, "algorithm": "Ed25519",
         "signature": "c2lnbmF0dXJlLXNpZ25hdHVyZS1wbGFjZWhvbGRlcg==",
         "attested_at": "2026-10-24T00:00:00Z"}, True)
    add("P-keylist", "keyList",
        {"schema": "key-list/v1",
         "keys": [{"key_id": "owner-key-1", "algorithm": "Ed25519",
                   "valid_from": "2026-01-01T00:00:00Z", "valid_until": None,
                   "revoked": False,
                   "key_reference": "vault:owner-signing-key-1"}]}, True)
    add("P-control", "control",
        {"fixture_id": "L1", "fixture_version": H("L1"),
         "prompt_hash": H("prompt"), "code_revision": "c" * 40,
         "sequence": 1, "match": "MATCHED",
         "outcome_digest": H("out-L1"), "tenant": "probe-tenant",
         "database": "probe-db", "capture_hash": H("cap-L1"),
         "counterpart_ref": "shadow-rs-001-0007", "control_order": 1,
         "counterpart": control_counterpart("L1", 1, "native-L1")},
        True)
    add("P-metric-count", "metric",
        {"id": "ELIGIBLE_T1_COUNT", "numerator": 20, "denominator": None,
         "measure_kind": "COUNT",
         "exclusions": [{"reason": "TRUNCATED_AWAY", "count": 1}],
         "formula_ref": formulas["ELIGIBLE_T1_COUNT"]["digest"]}, True,
        "digest checked by report vectors against pinned goldens")
    add("P-metric-ratio", "metric",
        {"id": "CONTROL_MATCH", "numerator": 3, "denominator": 3,
         "measure_kind": "RATIO", "exclusions": [],
         "formula_ref": formulas["CONTROL_MATCH"]["digest"]}, True,
        "digest checked by report vectors against pinned goldens")
    add("P-required-metric-set", "requiredMetricSet",
        ["ELIGIBLE_T1_COUNT",
         "CELL_ACCEPT_PASS", "CELL_ACCEPT_FAIL",
         "CELL_NONACCEPT_PASS", "CELL_NONACCEPT_FAIL",
         "CAPTURE_HEALTH",
         "PER_TAP_T1", "PER_TAP_T2a", "PER_TAP_T2b", "PER_TAP_T3",
         "CONTROL_MATCH"], True, "exact closed set, no more no fewer")
    bad = freeze_record()
    bad["total_reservations_max"] = 49
    add("N-freeze-total", "freeze", bad, False)
    bad = freeze_record()
    del bad["budget_preimage"]
    add("N-freeze-no-preimage", "freeze", bad, False)
    add("N-attempt-free-reason", "attempt",
        {"schema": "attempt/v1", "run_set_id": "r", "freeze_hash": h,
         "seq": 1, "id": h, "fixture_id": "L1", "fixture_version": h,
         "case_id": "c", "budget_token": "x", "parent_id": None,
         "state_version": 1, "disposition": "EXCLUDED",
         "reason": "some free text", "receipts": [], "digest": h}, False)
    add("N-perturn-5tuple", "perTurn",
        {"schema": "turn/v2", "run_set_id": "r",
         "count_key": ["r", "i", 3, "BOUND_FULL", h],
         "activity": {"namespace": "n", "workflow_id": "w",
                      "workflow_run_id": "wr", "activity_id": "a",
                      "attempt": 1},
         "iteration": 3, "receipt_index": 0, "input_set": "BOUND_FULL",
         "tap": "T1",
         "native": {"verified": True, "status": "s", "confidence": None,
                    "hypothesis_id": "h", "producing_path": "g"},
         "shadow": {"observation_id": h, "outcome": "ABSTAINED",
                    "stage1": "ABSTAINED", "reason": "STAGE1_ADMISSIBLE",
                    "scope": "source-range-only",
                    "declaration_requirement_satisfied": False},
         "bundle_digest": h, "envelope_digest": h, "cell": "accept-PASS",
         "eligibility": "ELIGIBLE", "disposition": "ELIGIBLE",
         "reasons": ["JOIN_OK"]}, False)
    add("N-receipt-weak-link", "receipt",
        {"schema": "receipt/v2", "run_set_id": "r", "id": "e2",
         "event": "VERIFY_COMPLETED", "workflow_id": "w",
         "workflow_run_id": "wr", "order": 2, "iteration": 1,
         "activity": {"namespace": "n", "workflow_id": "w",
                      "workflow_run_id": "wr", "activity_id": "a",
                      "attempt": 1},
         "payload_ref": "artifacts/e2.json",
         "payload_digest": payload_digest("e2"),
         "previous_digest": "not-a-digest"}, False)
    add("N-authz-algorithm", "ownerAuthorization",
        {"schema": "owner-authorization/v5", "approval_ref": "x",
         "run_set_id": "r", "freeze_hash": h, "decision_type":
         "FREEZE_APPROVAL", "decision_digest": h, "approver": "o",
         "approved_at": "2026-10-24T00:00:00Z", "evidence_ref": "e",
         "key_id": "k", "signed_digest": h, "algorithm": "RSA"}, False)
    add("N-authz-v4-const", "ownerAuthorization",
        dict(ownerauthz_v4_base(h),
             **{"schema": "owner-authorization/v4"}), False,
        "superseded const rejected")
    add("N-metric-count-denom", "metric",
        {"id": "ELIGIBLE_T1_COUNT", "numerator": 20, "denominator": 5,
         "measure_kind": "COUNT", "exclusions": [],
         "formula_ref": formulas["ELIGIBLE_T1_COUNT"]["digest"]}, False)
    add("N-control-thin", "control",
        {"fixture_id": "L1", "fixture_version": h, "prompt_hash": h,
         "code_revision": "c", "sequence": 1, "match": "MATCHED",
         "outcome_digest": h}, False)
    bad_counterpart = control_counterpart("L1", 1, "native-L1")
    del bad_counterpart["capture_hash"]
    add("N-control-bad-counterpart", "control",
        {"fixture_id": "L1", "fixture_version": H("L1"),
         "prompt_hash": H("prompt"), "code_revision": "c" * 40,
         "sequence": 1, "match": "MATCHED",
         "outcome_digest": H("out-L1"), "tenant": "probe-tenant",
         "database": "probe-db", "capture_hash": H("cap-L1"),
         "counterpart_ref": "shadow-rs-001-0007", "control_order": 1,
         "counterpart": bad_counterpart},
        False, "counterpart must carry the full match tuple")
    add("N-report-free-limitation", "report",
        {"schema": "report/v3", "run_set_id": "rs-001", "freeze_hash": h,
         "terminal_ref": "t1", "manifest_digest": h,
         "harness_version": "harness-0.0.0",
         "populations": {"D-01": h, "T1": "t1", "T2a": "t2a", "T2b": "t2b",
                         "T3": "t3"},
         "metrics": [], "classes": {"native_verified": 0,
                                    "native_rejection": 0,
                                    "native_abstention": 0,
                                    "native_error": 0, "shadow_failure": 0,
                                    "capture_failure": 0,
                                    "ambiguous_join": 0},
         "capture": {"evaluator_failures": 0, "capture_errors": 0,
                     "telemetry_omissions": 0, "dropped": 0,
                     "duplicates": 0},
         "cells": {"accept-PASS": 0, "accept-FAIL": 0, "nonaccept-PASS": 0,
                   "nonaccept-FAIL": 0},
         "taps": {}, "controls": [], "divergences": [],
         "limitations": ["parity proven"], "verdict": "INSUFFICIENT_EVIDENCE",
         "conclusion": "x",
         "accounting": {"total_reservations": 0, "eligible": 0,
                        "excluded": 0, "non_eligible": 0,
                        "void_contributing": 0, "reserved_open": 0}},
        False)
    add("N-required-metric-set-extra", "requiredMetricSet",
        ["ELIGIBLE_T1_COUNT", "EXTRA_METRIC"], False,
        "extra metric rejected")
    add("N-required-metric-set-missing", "requiredMetricSet",
        ["ELIGIBLE_T1_COUNT"], False, "missing metrics rejected")
    return V


def ownerauthz_v4_base(h):
    return {"approval_ref": "x",
            "run_set_id": "r", "freeze_hash": h, "decision_type":
            "FREEZE_APPROVAL", "decision_digest": h, "approver": "o",
            "approved_at": "2026-10-24T00:00:00Z", "evidence_ref": "e",
            "key_id": "k", "signed_digest": h, "algorithm": "Ed25519"}


def base_report_case(formulas):
    turns, receipts, records = base_scenario()
    controls = base_controls()
    report = recompute_report(
        turns, records, receipts, controls, ["L1", "L2", "L3"], [],
        "FEASIBILITY_DEMONSTRATED", True, formulas)
    freeze = freeze_record()
    digest = freeze_digest_of(freeze)
    freeze["freeze_hash"] = digest
    report["freeze_hash"] = digest
    return {
        "id": "RC-valid-terminal", "terminal": True, "turns": turns,
        "attempts": {"total": len(records), "reserved_open": 0,
                     "records": records},
        "receipts": receipts, "report": report, "expect": "accept",
        "cohort_fixtures": ["L1", "L2", "L3"],
        "controls": controls, "control_native_divergence": False,
        "capture_events": [], "freeze": freeze,
    }


def report_cases(formulas):
    cases = []
    base = base_report_case(formulas)
    cases.append(base)

    def clone(cid, expect, mutate=None):
        import copy as _copy
        c = _copy.deepcopy(base)
        c["id"] = cid
        c["expect"] = expect
        if mutate:
            mutate(c)
        cases.append(c)
        return c

    def metric(c, mid):
        for m in c["report"]["metrics"]:
            if m["id"] == mid:
                return m
        raise AssertionError(f"metric {mid} missing")

    def find_turn(c, pred):
        for t in c["turns"]:
            if pred(t):
                return t
        raise AssertionError("turn not found")

    def find_attempt(c, aid):
        for a in c["attempts"]["records"]:
            if a["id"] == aid:
                return a
        raise AssertionError(f"attempt {aid} missing")

    # -- single-field mutations of the valid report (each must fail) --
    def tamper_cell(c):
        metric(c, "CELL_ACCEPT_PASS")["numerator"] += 1
        c["report"]["cells"]["accept-PASS"] += 1
    clone("RC-mutated-cell-count", "reject", tamper_cell)

    def tamper_tap(c):
        metric(c, "PER_TAP_T1")["numerator"] -= 1
    clone("RC-tap-tamper", "reject", tamper_tap)

    def tamper_control_num(c):
        metric(c, "CONTROL_MATCH")["numerator"] += 1
    clone("RC-control-numerator-tamper", "reject", tamper_control_num)

    def tamper_formula(c):
        metric(c, "ELIGIBLE_T1_COUNT")["formula_ref"] = "sha256:" + "00" * 32
    clone("RC-formula-tamper", "reject", tamper_formula)

    def drop_metric(c):
        c["report"]["metrics"] = [
            m for m in c["report"]["metrics"] if m["id"] != "CELL_ACCEPT_FAIL"]
    clone("RC-missing-metric", "reject", drop_metric)

    def extra_metric(c):
        c["report"]["metrics"].append({
            "id": "EXTRA_METRIC", "numerator": 1, "denominator": None,
            "measure_kind": "COUNT", "exclusions": [],
            "formula_ref": formulas["ELIGIBLE_T1_COUNT"]["digest"]})
    clone("RC-extra-metric", "reject", extra_metric)

    # -- zero-denominator RATIO: both halves --
    def zero_ok(c):
        c["controls"] = []
        c["cohort_fixtures"] = []
        c["report"]["controls"] = []
        for m in c["report"]["metrics"]:
            if m["id"] == "CONTROL_MATCH":
                m.update({"numerator": 0, "denominator": 0})
        c["report"]["limitations"] = ["DIAGNOSTIC_ONLY_NO_PARITY",
                                      "RATIO_UNDEFINED_ZERO_DENOMINATOR"]
        c["report"]["verdict"] = "INSUFFICIENT_EVIDENCE"
    clone("RC-zero-denominator-ok", "accept", zero_ok)

    def zero_no_lim(c):
        zero_ok(c)
        c["report"]["limitations"] = ["DIAGNOSTIC_ONLY_NO_PARITY"]
    clone("RC-zero-denominator-no-limitation", "reject", zero_no_lim)

    def zero_nonzero(c):
        zero_ok(c)
        for m in c["report"]["metrics"]:
            if m["id"] == "CONTROL_MATCH":
                m.update({"numerator": 1, "denominator": 0})
    clone("RC-zero-denominator-nonzero-numerator", "reject", zero_nonzero)

    # -- open vs terminal --
    def open_hold(c):
        c["terminal"] = False
        c["report"]["verdict"] = "HOLD"
        c["report"]["accounting"]["reserved_open"] = 1
        c["report"]["accounting"]["total_reservations"] += 1
        c["attempts"]["reserved_open"] = 1
        c["attempts"]["total"] += 1
    clone("RC-open-hold-with-reservations", "accept", open_hold)

    def open_non_hold(c):
        open_hold(c)
        c["report"]["verdict"] = "INSUFFICIENT_EVIDENCE"
    clone("RC-open-non-hold", "reject", open_non_hold)

    def terminal_hold(c):
        c["report"]["verdict"] = "HOLD"
    clone("RC-terminal-hold-verdict", "reject", terminal_hold)

    def terminal_open(c):
        c["report"]["accounting"]["reserved_open"] = 1
        c["report"]["accounting"]["total_reservations"] += 1
        c["attempts"]["reserved_open"] = 1
        c["attempts"]["total"] += 1
    clone("RC-terminal-open-reservations", "reject", terminal_open)

    # -- ledger integrity: omit / duplicate / conflict / unlink / mismatch --
    def omit_attempt(c):
        c["attempts"]["records"] = c["attempts"]["records"][1:]
        c["attempts"]["total"] -= 1
    clone("RC-attempt-omitted", "reject", omit_attempt)

    def dup_attempt(c):
        c["attempts"]["records"].append(dict(c["attempts"]["records"][0]))
    clone("RC-attempt-duplicate", "reject", dup_attempt)

    def conflict_attempt(c):
        find_attempt(c, "a-0")["disposition"] = "EXCLUDED"
    clone("RC-attempt-conflict", "reject", conflict_attempt)

    def unlink_turn(c):
        c["turns"][0]["attempt_ref"] = "a-ghost"
    clone("RC-unlinked-turn", "reject", unlink_turn)

    def turn_mismatch(c):
        t0 = find_turn(c, lambda t: t["attempt_ref"] == "a-0")
        tx = find_turn(c, lambda t: t["attempt_ref"] == "a-x1")
        t0["attempt_ref"], tx["attempt_ref"] = "a-x1", "a-0"
    clone("RC-turn-attempt-mismatch", "reject", turn_mismatch)

    # -- receipt identity --
    def missing_receipt(c):
        c["receipts"] = [r for r in c["receipts"] if r["id"] != "e-5"]
    clone("RC-receipt-missing", "reject", missing_receipt)

    def reuse_receipt(c):
        c["turns"][1]["receipt_ref"] = "e-0"
        c["turns"][1]["payload_digest"] = c["turns"][0]["payload_digest"]
    clone("RC-receipt-reuse", "reject", reuse_receipt)

    def payload_mismatch(c):
        c["turns"][2]["payload_digest"] = "sha256:" + "ff" * 32
    clone("RC-receipt-payload-mismatch", "reject", payload_mismatch)

    # -- complete receipt identity (R1): run set, activity, iteration --
    def receipt_run_mismatch(c):
        c["receipts"][0]["run_set_id"] = "rs-002"
    clone("RC-receipt-runset-mismatch", "reject", receipt_run_mismatch)

    def receipt_activity_mismatch(c):
        c["receipts"][0]["activity"]["activity_id"] = "act-ghost"
    clone("RC-receipt-activity-mismatch", "reject", receipt_activity_mismatch)

    def receipt_duplicate_id(c):
        c["receipts"].append(dict(c["receipts"][0]))
    clone("RC-receipt-duplicate-id", "reject", receipt_duplicate_id)

    def receipt_workflow_mismatch(c):
        c["receipts"][0]["workflow_id"] = "w-ghost"
    clone("RC-receipt-workflow-mismatch", "reject", receipt_workflow_mismatch)

    def receipt_workflow_run_mismatch(c):
        c["receipts"][0]["workflow_run_id"] = "wr-ghost"
    clone("RC-receipt-workflow-run-mismatch", "reject",
          receipt_workflow_run_mismatch)

    def receipt_stray_mismatch(c):
        for r in c["receipts"]:
            if r["id"] == "e-cap-1":
                r["workflow_id"] = "w-ghost"
    clone("RC-receipt-stray-mismatch", "reject", receipt_stray_mismatch)

    def receipt_chain_mismatch(c):
        c["receipts"][1]["previous_digest"] = H("wrong-predecessor")
    clone("RC-receipt-chain-mismatch", "reject", receipt_chain_mismatch)

    # -- report-to-freeze binding: the report must name the recomputed
    # freeze digest of its own case freeze --
    def report_freeze_mismatch(c):
        c["report"]["freeze_hash"] = H("other-freeze")
    clone("RC-report-freeze-mismatch", "reject", report_freeze_mismatch)

    def freeze_self_hash_mismatch(c):
        c["freeze"]["tree_hash"] = H("other-tree")
    clone("RC-freeze-self-hash-mismatch", "reject", freeze_self_hash_mismatch)

    # -- catalog authority: a formula change paired with a consistently
    # updated vector digest must still fail against the pinned catalog --
    def tamper_formula_catalog(c):
        import copy as _copy
        fset = _copy.deepcopy(formulas)
        new_text = fset["CONTROL_MATCH"]["text"].replace(
            "MATCHED control pairs", "MATCHED control pairs (amended)")
        fset["CONTROL_MATCH"] = {"text": new_text,
                                 "digest": formula_digest(new_text)}
        c["formulas"] = fset
        metric(c, "CONTROL_MATCH")["formula_ref"] = \
            fset["CONTROL_MATCH"]["digest"]
    clone("RC-formula-catalog-mismatch", "reject", tamper_formula_catalog)

    # -- capture-health metric reconciliation (R4) --
    def capture_metric_mismatch(c):
        metric(c, "CAPTURE_HEALTH")["numerator"] += 1
    clone("RC-capture-metric-mismatch", "reject", capture_metric_mismatch)

    # -- control populations --
    def control_outside(c):
        c["controls"].append({
            "fixture_id": "L9", "fixture_version": H("L9"),
            "prompt_hash": H("prompt"), "code_revision": "c" * 40,
            "sequence": 4, "match": "MATCHED",
            "outcome_digest": H("out-L9"), "tenant": "probe-tenant",
            "database": "probe-db", "capture_hash": H("cap-L9"),
            "counterpart_ref": "shadow-rs-001-0099", "control_order": 4,
            "counterpart": control_counterpart("L9", 4, "native-L9")})
    clone("RC-control-outside-cohort", "reject", control_outside)

    def control_divergence(c):
        c["control_native_divergence"] = True
    clone("RC-control-divergence", "reject", control_divergence)

    def control_duplicate_pair(c):
        pair = [{
            "fixture_id": "L4", "fixture_version": H("L4"),
            "prompt_hash": H("prompt"), "code_revision": "c" * 40,
            "sequence": 4, "match": "MATCHED",
            "outcome_digest": H("out-L4"), "tenant": "probe-tenant",
            "database": "probe-db", "capture_hash": H("cap-L4"),
            "counterpart_ref": "shadow-rs-001-0010", "control_order": 4,
            "counterpart": control_counterpart("L4", 4, "native-L4-a"),
        }, {
            "fixture_id": "L4", "fixture_version": H("L4"),
            "prompt_hash": H("prompt"), "code_revision": "c" * 40,
            "sequence": 5, "match": "MATCHED",
            "outcome_digest": H("out-L4b"), "tenant": "probe-tenant",
            "database": "probe-db", "capture_hash": H("cap-L4"),
            "counterpart_ref": "shadow-rs-001-0011", "control_order": 5,
            "counterpart": control_counterpart("L4", 5, "native-L4-b"),
        }]
        c["controls"].extend(pair)
        c["cohort_fixtures"] = ["L1", "L2", "L3", "L4"]
        for m in c["report"]["metrics"]:
            if m["id"] == "CONTROL_MATCH":
                m["denominator"] = 4
        c["report"]["controls"] = [
            m for m in c["report"]["controls"]]
    clone("RC-control-duplicate-pair", "accept", control_duplicate_pair)

    # -- CONTROL_MATCH counterpart recompute: a MATCHED label is only
    # valid when the shadow-counterpart comparison agrees, never from
    # truthy counterpart_ref/order fields alone --
    def control_label_tamper(c):
        c["controls"][0]["counterpart"]["tenant"] = "other-tenant"
    clone("RC-control-label-tamper", "reject", control_label_tamper)

    def control_counterpart_missing(c):
        c["controls"][0]["counterpart"] = None
    clone("RC-control-counterpart-missing", "reject", control_counterpart_missing)

    def control_order_mismatch(c):
        c["controls"][0]["counterpart"]["control_order"] = 99
    clone("RC-control-order-mismatch", "reject", control_order_mismatch)

    def unknown_reason(c):
        for m in c["report"]["metrics"]:
            if m["id"] == "ELIGIBLE_T1_COUNT":
                m["exclusions"].append({"reason": "MYSTERY", "count": 1})
    clone("RC-unknown-exclusion-reason", "reject", unknown_reason)

    # -- VOID path end to end --
    def void_mapping(c):
        t = find_turn(c, lambda t: t["attempt_ref"] == "a-x1")
        t["eligibility"] = "VOID"
        t["reason"] = "CAPTURE_ERROR"
        find_attempt(c, "a-x1")["disposition"] = "VOID_CONTRIBUTING"
        c["report"]["accounting"]["excluded"] -= 1
        c["report"]["accounting"]["void_contributing"] += 1
        for m in c["report"]["metrics"]:
            if m["id"] == "ELIGIBLE_T1_COUNT":
                m["exclusions"] = [
                    e for e in m["exclusions"]
                    if e["reason"] != "TRUNCATED_AWAY"] + [
                    {"reason": "CAPTURE_ERROR", "count": 1}]
    clone("RC-void-mapping", "accept", void_mapping)

    # -- capture events end to end --
    def capture_events_case(c):
        c["capture_events"] = [{"kind": "evaluator_failure"},
                               {"kind": "dropped"},
                               {"kind": "duplicate"}]
        c["report"]["capture"]["evaluator_failures"] = 1
        c["report"]["capture"]["dropped"] = 1
        c["report"]["capture"]["duplicates"] = 1
        metric(c, "CAPTURE_HEALTH")["numerator"] += 3
    clone("RC-capture-events", "accept", capture_events_case)

    def capture_tamper(c):
        capture_events_case(c)
        c["report"]["capture"]["dropped"] = 0
    clone("RC-capture-tamper", "reject", capture_tamper)

    # -- reservation cap: 49 linked reservations against a freeze capped
    # at 48 must void even though every other recompute agrees --
    cases.append(over_cap_case(formulas))

    return cases


def over_cap_case(formulas):
    """One reservation over the frozen limit, all joins consistent."""
    base = base_report_case(formulas)
    turns = list(base["turns"])
    receipts = list(base["receipts"])
    records = list(base["attempts"]["records"])
    prev = receipts[-1]["payload_digest"]
    for i in range(25):
        order = 25 + i
        rid, aid, tag = f"e-ov-{i}", f"a-ov-{i}", f"ov-{i}"
        act = {"namespace": "n", "workflow_id": "w",
               "workflow_run_id": "wr", "activity_id": f"act-{tag}",
               "attempt": 1}
        pd = payload_digest(rid)
        receipts.append({"schema": "receipt/v2", "id": rid,
                         "run_set_id": COUNT_KEY_RUN,
                         "workflow_id": "w", "workflow_run_id": "wr",
                         "iteration": order, "order": order,
                         "activity": act, "event": "VERIFY_COMPLETED",
                         "capture_error": False,
                         "payload_digest": pd,
                         "payload_ref": f"artifacts/{rid}.json",
                         "previous_digest": prev})
        prev = pd
        turns.append({
            "cell": "accept-PASS", "eligibility": "ELIGIBLE",
            "reason": "JOIN_OK", "tap": "T1",
            "count_key": [COUNT_KEY_RUN, f"inv-ov-{i}", f"hyp-ov-{i}",
                          100 + i, "BOUND_FULL", H(f"env-ov-{i}")],
            "attempt_ref": aid, "receipt_ref": rid,
            "payload_digest": pd,
            "receipt_index": order, "iteration": order,
            "activity": act})
        records.append({"id": aid, "disposition": "ELIGIBLE"})
    controls = base["controls"]
    report = recompute_report(
        turns, records, receipts, controls, ["L1", "L2", "L3"], [],
        "FEASIBILITY_DEMONSTRATED", True, formulas)
    freeze = base["freeze"]
    report["freeze_hash"] = freeze["freeze_hash"]
    return {
        "id": "RC-reservation-over-freeze-cap", "terminal": True,
        "turns": turns,
        "attempts": {"total": len(records), "reserved_open": 0,
                     "records": records},
        "receipts": receipts, "report": report, "expect": "reject",
        "cohort_fixtures": ["L1", "L2", "L3"],
        "controls": controls, "control_native_divergence": False,
        "capture_events": [], "freeze": freeze,
    }


def main() -> int:
    import pathlib
    from jsonschema import Draft202012Validator
    reg = json.load(open(REGISTRY_PATH, encoding="utf-8"))
    required, catalog = load_formula_catalog()
    formulas = build_formulas(catalog)
    cases = report_cases(formulas)
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "duplicate case id"
    # Build-time self-check: the recomputed base report is schema-valid
    # against $defs/report, so the full-report vector below is proven,
    # not asserted.
    Draft202012Validator(
        {"$ref": "#/$defs/report", "$defs": reg["$defs"]}
    ).validate(cases[0]["report"])
    # Build-time self-check: the base freeze is schema-valid and its
    # self-hash equals the digest the bound report names.
    Draft202012Validator(
        {"$ref": "#/$defs/freeze", "$defs": reg["$defs"]}
    ).validate(cases[0]["freeze"])
    assert cases[0]["freeze"]["freeze_hash"] == freeze_digest_of(
        cases[0]["freeze"])
    assert cases[0]["report"]["freeze_hash"] == cases[0]["freeze"]["freeze_hash"]
    vectors = schema_vectors(formulas)
    base = cases[0]
    pipeline_vectors = [
        ("P-full-report", "report", base["report"], True,
         "recomputed base report validates end to end"),
        ("P-case-turn", "caseTurn", base["turns"][0], True,
         "join-contract turn with attempt/receipt refs and identity"),
        ("P-case-receipt", "caseReceipt", base["receipts"][0], True,
         "complete receipt identity plus payload binding"),
        ("P-case-attempt", "caseAttempt",
         base["attempts"]["records"][0], True,
         "ledger record with registry id key"),
        ("P-capture-event", "captureEvent", {"kind": "dropped"}, True,
         "closed capture-event vocabulary"),
    ]
    for vid, check, inst, valid, note in pipeline_vectors:
        vectors.append({"id": vid, "kind": "schema", "check": check,
                        "instance": inst, "valid": valid, "note": note})
    bad_receipt_chain = copy.deepcopy(base["receipts"][1])
    bad_receipt_chain["previous_digest"] = H("wrong-predecessor")
    vectors.append({"id": "N-case-receipt-bad-chain-digest", "kind": "schema",
                    "check": "caseReceipt", "instance": bad_receipt_chain,
                    "valid": True,
                    "note": "chain-link semantics are checked across report receipts"})
    bad_turn = dict(base["turns"][0])
    bad_turn["zzz_unknown"] = 1
    vectors.append({"id": "N-case-turn-extra", "kind": "schema",
                    "check": "caseTurn", "instance": bad_turn,
                    "valid": False, "note": "join contract is closed"})
    bad_receipt = {k: v for k, v in base["receipts"][0].items()
                   if k != "run_set_id"}
    vectors.append({"id": "N-case-receipt-no-runset", "kind": "schema",
                    "check": "caseReceipt", "instance": bad_receipt,
                    "valid": False,
                    "note": "receipt without run-set identity rejected"})
    doc = {
        "registry": REGISTRY_ID,
        "formulas": formulas,
        "vectors": vectors,
        "semantic_notes": [
            {"id": "S-payload-binding",
             "text": "Ed25519 over domain || 0x00 || canonical(full record "
                     "minus signature and signed_digest); every single-field "
                     "mutation changes the payload digest, so a copied "
                     "signature cannot verify. Covered executably by "
                     "check_d3_stagec_signatures.py (mutation, replay, "
                     "key-list, validity, use-constraint cases)."},
            {"id": "S-formula-pinning",
             "text": "Each metric formula is pinned text plus a golden "
                     "digest; the validator rebuilds the digest from the "
                     "text and requires an exact metric-id set match, so "
                     "removal, addition, or change of any formula is "
                     "rejected."},
            {"id": "S-join-contract",
             "text": "Report-checker inputs are schema-valid source/join "
                     "records (caseTurn/caseAttempt/caseReceipt/captureEvent "
                     "per the registry): the checker schema-gates every "
                     "case before recompute, and the R1 join verifies "
                     "run-set, order/index, activity, and payload identity "
                     "with duplicate-ID rejection."},
            {"id": "S-capture-metric",
             "text": "CAPTURE_HEALTH is a COUNT metric whose numerator is "
                     "the sum of the five capture-health counters; the "
                     "validator recomputes the counters from capture events "
                     "plus receipt flags and turn markers and requires the "
                     "metric, the report capture block, and the recomputed "
                     "total to agree exactly."},
            {"id": "S-freeze-binding",
             "text": "Every report case carries the freeze its report is "
                     "bound to: the validator schema-gates the freeze, "
                     "recomputes its self-hash, and requires the report "
                     "freeze_hash to equal that digest with a matching "
                     "run set, so a report recomputed for a different "
                     "freeze voids."},
            {"id": "S-control-counterpart",
             "text": "Every control record carries its shadow counterpart: "
                     "the validator recomputes the MATCHED/MISMATCH/UNMATCHED "
                     "label from the tuple comparison via compare_pair — "
                     "including agreement of both control_order values — and "
                     "requires the stored label to agree, so a MATCHED label "
                     "on divergent, order-disagreeing, or missing counterpart "
                     "evidence voids."},
            {"id": "S-reservation-cap",
             "text": "The reconciled attempt-ledger total must not exceed "
                     "the freeze's total_reservations_max: the validator "
                     "binds its total check to the frozen limit, so a "
                     "consistent report over the cap voids."},
        ],
        "report_cases": cases,
    }
    # Every case carries the explicit capture-event list (possibly empty).
    for c in doc["report_cases"]:
        c.setdefault("capture_events", [])
    p = pathlib.Path(OUT_PATH)
    p.write_text(json.dumps(doc, indent=1) + "\n")
    print(f"wrote {OUT_PATH}: {len(doc['vectors'])} schema vectors, "
          f"{len(doc['report_cases'])} report cases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
