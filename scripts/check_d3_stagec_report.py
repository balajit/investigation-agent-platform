#!/usr/bin/env python3
"""Semantic report validator for the Stage C harness (SC-V15-02, SC-V16-02,
SC-V17-02, SC-V18-01/02/03).

Every report case is first schema-gated against the registry's source/join
contract ($defs reportCase: caseTurn/caseAttempt/caseReceipt/captureEvent
inputs plus the report under test), then R1–R8 and every metric are
recomputed from those source records (turns, attempts, receipts, controls,
capture events, formulas) instead of inferred from report totals:

- R1: complete receipt identity — every counted envelope resolves to a
  receipt carrying run-set, workflow, iteration, order, and activity
  identity plus a matching payload digest; duplicate receipt IDs are
  rejected, not overwritten; eligible T1 turns never share one receipt.
- R2: cell counts and the eligible total recomputed from eligible T1 turns.
- R3: every reservation reconciled exactly once from the attempt ledger:
  unique attempt IDs, each recorded attempt linked by exactly one turn
  with a matching disposition class, no orphan turns, totals match, and
  the exclusion breakdown matches turns by closed reason code.
- R4: capture-health counters recomputed from explicit capture events plus
  receipt capture flags and turn omission markers; the CAPTURE_HEALTH
  metric numerator must equal that recomputed total exactly (it is the
  sum of the five counters, a single representable integer).
- R5: no cross-run-set references; every turn receipt and attempt belongs
  to the report's run set.
- Freeze binding: the case freeze is schema-gated, its self-hash is
  recomputed, and the report freeze_hash must equal that digest with a
  matching run set.
- R6: formula_ref recomputed from rebuilt canonical formula bytes and
  matched against the pinned golden digest and the report value; the
  report's metric set must equal the registry's requiredMetricSet exactly.
- R7: open reports must carry verdict HOLD; terminal reports must carry a
  non-HOLD verdict with zero open reservations.
- R8: COUNT metrics require null denominators; zero-denominator RATIO
  metrics require numerator zero plus the explicit limitation.
- CONTROL_MATCH numerator/denominator recomputed from control records
  against the frozen cohort fixture list: a fixture contributes at most
  one matched pair, and only when it has exactly one control record whose
  stored MATCHED label agrees with the recomputed shadow-counterpart
  comparison (tuple equality plus agreeing counterpart/order evidence on
  both sides); label tamper, duplicates, mismatches, and out-of-cohort
  controls never contribute.
- Per-tap populations recomputed from turn tap labels, including the
  explicit no-double-count rule.

Exit 0 iff every report-case matches its expected outcome.
Usage:
  .venv/bin/python scripts/check_d3_stagec_report.py REGISTRY VECTORS
"""
import hashlib
import json
import re
import sys

DOMAIN = b"iap:d3-stagec:metric-formula/v1"
EVENT_TO_COUNTER = {"evaluator_failure": "evaluator_failures",
                    "capture_error": "capture_errors",
                    "telemetry_omission": "telemetry_omissions",
                    "dropped": "dropped",
                    "duplicate": "duplicates"}
REASONS = set()
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def fail(msg):
    raise AssertionError(msg)


def canonical_formula_bytes(text: str) -> bytes:
    body = text.replace("\r\n", "\n")
    if not body.endswith("\n"):
        body += "\n"
    return DOMAIN + b"\x00" + body.encode("utf-8")


def check_receipt_chain(receipts: list[dict]) -> None:
    ordered = sorted(receipts, key=lambda r: (r.get("order", 0), r.get("id", "")))
    seen_orders = set()
    previous_receipt = None
    for position, receipt in enumerate(ordered):
        order = receipt.get("order")
        if not isinstance(order, int) or order < 0:
            fail(f"receipt has invalid order: {receipt.get('id')!r}")
        if order != position:
            fail(f"receipt order gap before {receipt.get('id')!r}: expected {position}, got {order}")
        if order in seen_orders:
            fail(f"duplicate receipt order: {order}")
        seen_orders.add(order)
        previous = receipt.get("previous_digest")
        if position == 0:
            if previous is not None:
                fail(f"genesis receipt {receipt.get('id')!r} must have null previous_digest")
        else:
            if not isinstance(previous, str) or not SHA256_RE.fullmatch(previous):
                fail(f"receipt {receipt.get('id')!r} must link with a sha256 previous_digest")
            expected = previous_receipt.get("payload_digest")
            if not isinstance(expected, str) or not SHA256_RE.fullmatch(expected):
                fail(f"preceding receipt {previous_receipt.get('id')!r} has invalid payload_digest")
            if previous != expected:
                fail(f"receipt {receipt.get('id')!r} previous_digest does not match "
                     f"preceding receipt {previous_receipt.get('id')!r}")
        previous_receipt = receipt


def freeze_digest_of(record: dict) -> str:
    body = {k: v for k, v in record.items() if k != "freeze_hash"}
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def check_case(case: dict, formulas: dict, reason_codes: set,
               required_metrics: list, catalog: dict,
               case_validator) -> None:
    # SC-V18-01: schema-gate every case's source/join inputs against the
    # registry contract before any recompute. The report object itself is
    # the unit under test (validated by recompute, not by schema).
    case_validator.validate(case)
    turns = case["turns"]
    attempts = case["attempts"]
    report = case["report"]
    terminal = case["terminal"]
    cohort = set(case.get("cohort_fixtures", []))
    # Report-to-freeze binding: the freeze is schema-gated above; its
    # self-hash must recompute and the report must name exactly that
    # digest with a matching run set.
    freeze = case["freeze"]
    if freeze.get("freeze_hash") != freeze_digest_of(freeze):
        fail("freeze_hash does not match recomputed freeze digest")
    if report.get("freeze_hash") != freeze["freeze_hash"]:
        fail("report freeze_hash does not match recomputed freeze digest")
    if freeze.get("run_set_id") != report.get("run_set_id"):
        fail("freeze run_set_id does not match report run_set_id")
    # A case may carry run-embedded formula definitions; otherwise the
    # vectors-level set applies. Either way it must equal the registry
    # catalog (SC-V19-02).
    effective = case.get("formulas") or formulas

    # Catalog authority: set, exact text, and expected digest must match
    # the registry catalog before any recompute. A formula change paired
    # with a consistently updated digest still fails here.
    if sorted(effective.keys()) != sorted(required_metrics):
        fail("formula set does not match requiredMetricSet")
    for mid in sorted(effective.keys()):
        pinned = catalog.get(mid)
        if pinned is None:
            fail(f"metric {mid} not in the registry formula catalog")
        if effective[mid].get("text") != pinned["text"]:
            fail(f"formula text for {mid} does not match the pinned catalog")
        if effective[mid].get("digest") != pinned["digest"]:
            fail(f"formula digest for {mid} does not match the pinned catalog")
    metric_ids = [m["id"] for m in report["metrics"]]
    if sorted(metric_ids) != sorted(required_metrics):
        fail("report metric set does not match requiredMetricSet")
    if len(set(metric_ids)) != len(metric_ids):
        fail("duplicate metric in report")

    def metric(mid):
        ms = [m for m in report["metrics"] if m["id"] == mid]
        if len(ms) != 1:
            fail(f"metric {mid} not present exactly once")
        return ms[0]

    # R6 + formula identity: rebuild canonical bytes from the pinned text,
    # compare the recomputed digest with the golden value and the report.
    for m in report["metrics"]:
        gold = effective.get(m["id"])
        if gold is None:
            fail(f"metric {m['id']} has no pinned formula")
        rebuilt = "sha256:" + hashlib.sha256(
            canonical_formula_bytes(gold["text"])).hexdigest()
        if rebuilt != gold["digest"]:
            fail(f"golden digest mismatch for {m['id']}")
        if m["formula_ref"] != rebuilt:
            fail(f"formula_ref mismatch for {m['id']}")
        if m["measure_kind"] == "COUNT" and m["denominator"] is not None:
            fail(f"COUNT metric {m['id']} must have null denominator")
        if m["measure_kind"] not in ("COUNT", "RATIO"):
            fail(f"unknown measure_kind for {m['id']}")
        if (m["measure_kind"] == "RATIO" and m["denominator"] == 0
                and (m["numerator"] != 0 or
                      "RATIO_UNDEFINED_ZERO_DENOMINATOR" not in
                      report.get("limitations", []))):
            fail(f"zero-denominator RATIO {m['id']} without explicit "
                  "limitation and zero numerator")

    # Turn<->attempt linkage: every recorded attempt is claimed by exactly
    # one turn with a matching disposition class; every turn links to one.
    # The registry identifies attempt records by `id`; turns link via
    # `attempt_ref`.
    by_attempt: dict = {}
    for t in turns:
        aid = t.get("attempt_ref")
        if not aid:
            fail(f"turn without attempt_ref: {t['count_key']}")
        if aid in by_attempt:
            fail(f"attempt {aid} claimed by two turns")
        by_attempt[aid] = t
    records = {a["id"]: a for a in attempts.get("records", [])}
    if len(records) != len(attempts.get("records", [])):
        fail("duplicate attempt identity in ledger")
    for aid, a in records.items():
        if a["disposition"] not in ("ELIGIBLE", "EXCLUDED", "NON_ELIGIBLE",
                                    "VOID_CONTRIBUTING"):
            fail(f"unknown disposition {a['disposition']}")
        if aid not in by_attempt:
            fail(f"recorded attempt {aid} has no turn")
        want = by_attempt[aid]["eligibility"]
        if a["disposition"] != {"ELIGIBLE": "ELIGIBLE",
                                "EXCLUDED": "EXCLUDED",
                                "NON_ELIGIBLE": "NON_ELIGIBLE",
                                "VOID": "VOID_CONTRIBUTING"}[want]:
            fail(f"turn/attempt disposition mismatch for {aid}: "
                 f"turn {want} vs attempt {a['disposition']}")
    for t in turns:
        if t["attempt_ref"] not in records:
            fail(f"turn links to unknown attempt {t['attempt_ref']}")

    # R1: complete receipt identity with payload-digest binding.
    # SC-V18-02: duplicate receipt IDs are rejected (never silently
    # overwritten), and the join verifies run-set, order/index,
    # iteration, activity, and payload identity — not just the ID.
    receipts: dict = {}
    receipt_list = case.get("receipts", [])
    check_receipt_chain(receipt_list)
    for r in receipt_list:
        if r["id"] in receipts:
            fail(f"duplicate receipt id {r['id']}")
        receipts[r["id"]] = r
    # SC-V20-01: receipt self-consistency is validated for EVERY receipt
    # in the case before any turn join — referenced or not — so an
    # unreferenced receipt (e.g. a stray capture-error receipt) cannot
    # smuggle a top-level/nested workflow mismatch past the join.
    for r in case.get("receipts", []):
        if r.get("workflow_id") != r.get("activity", {}).get("workflow_id"):
            fail(f"receipt {r['id']} top-level workflow_id differs from "
                 "activity identity")
        if r.get("workflow_run_id") != r.get("activity", {}).get(
                "workflow_run_id"):
            fail(f"receipt {r['id']} top-level workflow_run_id differs from "
                 "activity identity")
    seen_receipts = set()
    for t in turns:
        rid = t.get("receipt_ref")
        if rid is None:
            if t["eligibility"] == "ELIGIBLE":
                fail("eligible turn without receipt identity")
            continue
        r = receipts.get(rid)
        if r is None:
            fail(f"turn references missing receipt {rid}")
        if r["run_set_id"] != report["run_set_id"]:
            fail(f"receipt {rid} from another run set")
        if t["count_key"][0] != report["run_set_id"]:
            fail("turn from another run set")
        if t.get("payload_digest") != r.get("payload_digest"):
            fail(f"turn/receipt payload digest mismatch for {rid}")
        if r.get("order") != t.get("receipt_index"):
            fail(f"receipt/turn index mismatch for {rid}")
        if r.get("iteration") != t.get("iteration"):
            fail(f"receipt/turn iteration mismatch for {rid}")
        if r.get("activity") != t.get("activity"):
            fail(f"receipt/turn activity mismatch for {rid}")
        if t["eligibility"] == "ELIGIBLE" and t["tap"] == "T1":
            if rid in seen_receipts:
                fail(f"receipt {rid} double-counted")
            seen_receipts.add(rid)

    # R2: cells from eligible T1 turns only.
    elig = [t for t in turns
            if t["eligibility"] == "ELIGIBLE" and t["tap"] == "T1"]
    cells = {}
    for t in elig:
        cells[t["cell"]] = cells.get(t["cell"], 0) + 1
    if metric("ELIGIBLE_T1_COUNT")["numerator"] != len(elig):
        fail("ELIGIBLE_T1_COUNT mismatch")
    for cell in ("accept-PASS", "accept-FAIL", "nonaccept-PASS",
                 "nonaccept-FAIL"):
        mid = "CELL_" + cell.replace("-", "_").upper()
        if metric(mid)["numerator"] != cells.get(cell, 0):
            fail(f"{mid} mismatch")
    if report["cells"] != {c: cells.get(c, 0) for c in (
            "accept-PASS", "accept-FAIL", "nonaccept-PASS",
            "nonaccept-FAIL")}:
        fail("report cell summary mismatch")

    # R3: full accounting against the attempt ledger.
    tally = {"ELIGIBLE": 0, "EXCLUDED": 0, "NON_ELIGIBLE": 0,
             "VOID_CONTRIBUTING": 0}
    for a in records.values():
        tally[a["disposition"]] = tally.get(a["disposition"], 0) + 1
    if attempts["total"] != len(records) + attempts["reserved_open"]:
        fail("attempt ledger total mismatch")
    if attempts["total"] > freeze.get("total_reservations_max"):
        fail("attempt ledger total exceeds frozen reservation limit")
    acc = report["accounting"]
    if acc["eligible"] != tally["ELIGIBLE"]:
        fail("accounting eligible mismatch")
    if acc["excluded"] != tally["EXCLUDED"]:
        fail("accounting excluded mismatch")
    if acc["non_eligible"] != tally["NON_ELIGIBLE"]:
        fail("accounting non_eligible mismatch")
    if acc["void_contributing"] != tally["VOID_CONTRIBUTING"]:
        fail("accounting void_contributing mismatch")
    if acc["reserved_open"] != attempts["reserved_open"]:
        fail("accounting reserved_open mismatch")
    if acc["total_reservations"] != attempts["total"]:
        fail("accounting total mismatch")
    excl_turns: dict = {}
    for t in turns:
        if t["eligibility"] in ("EXCLUDED", "NON_ELIGIBLE", "VOID"):
            if t["reason"] not in reason_codes:
                fail(f"exclusion reason outside closed vocabulary: "
                     f"{t['reason']}")
            excl_turns[t["reason"]] = excl_turns.get(t["reason"], 0) + 1
    excl_metric: dict = {}
    for e in metric("ELIGIBLE_T1_COUNT")["exclusions"]:
        if e["reason"] not in reason_codes:
            fail(f"metric exclusion reason outside closed vocabulary: "
                 f"{e['reason']}")
        if e["reason"] in excl_metric:
            fail(f"duplicate exclusion reason {e['reason']}")
        excl_metric[e["reason"]] = e["count"]
    if excl_metric != excl_turns:
        fail(f"exclusion breakdown mismatch {excl_metric} vs {excl_turns}")

    # R4: capture-health from explicit capture events + receipt flags +
    # turn omission markers. SC-V18-03: the CAPTURE_HEALTH metric is the
    # sum of the five counters (one representable integer); the metric,
    # the report capture block, and the recomputed total must agree.
    cap = {"evaluator_failures": 0, "capture_errors": 0,
           "telemetry_omissions": 0, "dropped": 0, "duplicates": 0}
    for e in case.get("capture_events", []):
        if e["kind"] not in EVENT_TO_COUNTER:
            fail(f"unknown capture event kind: {e['kind']}")
        cap[EVENT_TO_COUNTER[e["kind"]]] += 1
    for r in case.get("receipts", []):
        if r.get("capture_error"):
            cap["capture_errors"] += 1
    for t in turns:
        if t["reason"] in ("TELEMETRY_OMISSION_PATH_A",
                           "TELEMETRY_OMISSION_PATH_B_UNEXPLAINED"):
            cap["telemetry_omissions"] += 1
    for k, v in cap.items():
        if report["capture"].get(k) != v:
            fail(f"capture-health {k} mismatch")
    if metric("CAPTURE_HEALTH")["numerator"] != sum(cap.values()):
        fail("CAPTURE_HEALTH metric does not equal the recomputed "
             "capture-health total")
    if sum(report["capture"].values()) != \
            metric("CAPTURE_HEALTH")["numerator"]:
        fail("CAPTURE_HEALTH metric does not match the report capture "
             "counters")

    # Per-tap populations recomputed from turn tap labels.
    want_taps = {}
    for tap in ("T1", "T2a", "T2b", "T3"):
        tt = [t for t in turns if t["tap"] == tap]
        want_taps[tap] = {
            "population": len(tt),
            "eligible": sum(1 for t in tt if t["eligibility"] == "ELIGIBLE"),
            "excluded": sum(1 for t in tt if t["eligibility"] != "ELIGIBLE"),
            "divergences": sum(1 for t in tt if t.get("divergent")),
        }
        if metric(f"PER_TAP_{tap}")["numerator"] != len(tt):
            fail(f"PER_TAP_{tap} mismatch")
    if report.get("taps") != want_taps:
        fail("report taps mismatch")

    # CONTROL_MATCH: one contributing fixture at a time. A fixture
    # contributes exactly one matched pair, and only when it has exactly
    # one control record whose stored MATCHED label agrees with the
    # recomputed shadow-counterpart comparison: same match-tuple fields on
    # both sides plus counterpart/order evidence, inside the frozen cohort.
    # Label tamper, duplicates, mismatches, and out-of-cohort fixtures
    # never contribute.
    TUPLE_FIELDS = ("fixture_id", "fixture_version", "prompt_hash",
                    "code_revision", "tenant", "database", "capture_hash")

    def control_label(record):
        shadow = record.get("counterpart")
        if shadow is None:
            return "UNMATCHED"
        if any(record.get(f) != shadow.get(f) for f in TUPLE_FIELDS):
            return "MISMATCH"
        if not record.get("counterpart_ref") or record.get("control_order") is None:
            return "MISMATCH"
        if not shadow.get("counterpart_ref") or shadow.get("control_order") is None:
            return "MISMATCH"
        if record.get("control_order") != shadow.get("control_order"):
            return "MISMATCH"
        return "MATCHED"

    ctrls = case.get("controls", [])
    by_fx: dict = {}
    for c in ctrls:
        by_fx.setdefault(c["fixture_id"], []).append(c)
    if not set(by_fx) <= set(cohort):
        fail("control fixture outside frozen cohort")
    if set(by_fx) - set(cohort):
        fail("control fixture outside frozen cohort")
    matched = []
    for fx in sorted(by_fx):
        recs = by_fx[fx]
        if len(recs) == 1 and control_label(recs[0]) == "MATCHED" \
                and recs[0].get("match") == "MATCHED":
            matched.append(recs[0])
    cm = metric("CONTROL_MATCH")
    if cm["numerator"] != len(matched):
        fail("CONTROL_MATCH numerator mismatch")
    if cm["denominator"] != len(by_fx):
        fail("CONTROL_MATCH denominator mismatch")
    if case.get("control_native_divergence"):
        fail("native divergence between matched control and shadow counterpart")
    got = {(c["fixture_id"], c.get("control_order")) for c in
           report.get("controls", [])}
    want = {(c["fixture_id"], c.get("control_order")) for c in matched}
    if got != want:
        fail("report controls do not match recomputed matched pairs")

    # R7: open vs terminal accounting, both directions.
    if terminal:
        if report["verdict"] not in ("INSUFFICIENT_EVIDENCE",
                                      "FEASIBILITY_DEMONSTRATED"):
            fail("terminal report with non-terminal verdict")
        if acc["reserved_open"] != 0:
            fail("terminal report with unresolved RESERVED attempts")
    else:
        if report["verdict"] != "HOLD":
            fail("open report must carry verdict HOLD")

    # R5: no cross-run-set references.
    for t in turns:
        if t["count_key"][0] != report["run_set_id"]:
            fail("turn from another run set")


def main() -> int:
    global REASONS
    from jsonschema import Draft202012Validator

    reg = json.load(open(sys.argv[1], encoding="utf-8"))
    vec = json.load(open(sys.argv[2], encoding="utf-8"))
    assert vec["registry"] == reg["$id"], "vectors target a different registry"
    required = reg["$defs"]["requiredMetricSet"]["const"]
    assert sorted(vec["formulas"].keys()) == sorted(required), \
        "formula set does not match requiredMetricSet"
    catalog = reg["$defs"]["formulaCatalog"]["const"]
    REASONS = set(reg["$defs"]["reasonCode"]["enum"])
    case_validator = Draft202012Validator(
        {"$ref": "#/$defs/reportCase", "$defs": reg["$defs"]})
    formulas = vec["formulas"]
    passed = failed = 0
    for case in vec["report_cases"]:
        try:
            check_case(case, formulas, REASONS, required, catalog,
                       case_validator)
            ok = case["expect"] == "accept"
            detail = "recomputed exactly" if ok else "UNEXPECTEDLY ACCEPTED"
        except AssertionError as e:  # noqa: BLE001 - vector data
            ok = case["expect"] == "reject"
            detail = (f"rejected as expected ({e})" if ok
                      else f"UNEXPECTED REJECT ({e})")
        except Exception as e:  # noqa: BLE001 - schema gate or bug
            ok = False
            detail = f"UNEXPECTED ERROR ({type(e).__name__}: {e})"
        print(f"{'PASS' if ok else 'FAIL'} {case['id']}: {detail}")
        passed, failed = passed + ok, failed + (not ok)
    print(f"{passed} passed, {failed} failed, "
          f"semantic report validator R1-R8 + terminal accounting")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
