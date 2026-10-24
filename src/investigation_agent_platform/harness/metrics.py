"""Metrics recompute and report verification (R1–R8, §4).

Every value is recomputed from source records (turns, attempts, receipts,
controls, capture events, pinned formulas) — stored report values are never
trusted. The report is bound to its freeze (`report["freeze_hash"]` must
equal the recomputed freeze digest). Mirrors the report-checker semantics
as the package implementation:
metric-set equality with `requiredMetricSet`, formula text+digest equality
with the registry catalog plus golden rebuild, turn/attempt ledger linkage,
complete R1 receipt identity (pre-join self-consistency over every
receipt), R2 cells, R3 full-disposition accounting, R4 capture-health with
CAPTURE_HEALTH triple agreement, R5 run-set confinement, R6 formula_ref,
R7 open/terminal accounting both directions, R8 COUNT/RATIO rules,
single-contribution CONTROL_MATCH, and per-tap populations.
"""

from __future__ import annotations

from typing import Any

from investigation_agent_platform.harness.canonical import (
    sha256_digest,
)
from investigation_agent_platform.harness.capture import verify_receipt_chain
from investigation_agent_platform.harness.control import evaluate_fixture
from investigation_agent_platform.harness.freeze import (
    freeze_digest,
    verify_freeze_self_hash,
)
from investigation_agent_platform.harness.registry import (
    CELLS,
    HarnessValidationError,
    check_record,
    validate_pinned_registry,
)

EVENT_TO_COUNTER = {
    "evaluator_failure": "evaluator_failures",
    "capture_error": "capture_errors",
    "telemetry_omission": "telemetry_omissions",
    "dropped": "dropped",
    "duplicate": "duplicates",
}

TAPS = ("T1", "T2a", "T2b", "T3")

COUNT_CELLS = {
    "accept-PASS": "CELL_ACCEPT_PASS",
    "accept-FAIL": "CELL_ACCEPT_FAIL",
    "nonaccept-PASS": "CELL_NONACCEPT_PASS",
    "nonaccept-FAIL": "CELL_NONACCEPT_FAIL",
}

TERMINAL_VERDICTS = frozenset({"INSUFFICIENT_EVIDENCE", "FEASIBILITY_DEMONSTRATED"})

FORMULA_DOMAIN = b"iap:d3-stagec:metric-formula/v1"


def formula_digest_for(text: str) -> str:
    """Digest of a formula preimage (`domain || 0x00 || canonical bytes`)."""
    from investigation_agent_platform.harness.canonical import (
        formula_canonical_bytes,
    )

    return sha256_digest(FORMULA_DOMAIN + b"\x00" + formula_canonical_bytes(text))


def check_formula_catalog(
    formulas: dict[str, dict[str, str]],
    catalog: dict[str, dict[str, str]],
    required: list[str],
) -> None:
    """Set, exact text, and expected digest must equal the registry catalog."""
    if sorted(formulas.keys()) != sorted(required):
        raise HarnessValidationError("formula set does not match requiredMetricSet")
    for mid in sorted(formulas.keys()):
        pinned = catalog.get(mid)
        if pinned is None:
            raise HarnessValidationError(f"metric {mid} not in the registry formula catalog")
        if formulas[mid].get("text") != pinned["text"]:
            raise HarnessValidationError(
                f"formula text for {mid} does not match the pinned catalog"
            )
        if formulas[mid].get("digest") != pinned["digest"]:
            raise HarnessValidationError(
                f"formula digest for {mid} does not match the pinned catalog"
            )


def eligible_t1_turns(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Eligible T1 turns only — the single counted population."""
    return [t for t in turns if t.get("eligibility") == "ELIGIBLE" and t.get("tap") == "T1"]


def cell_counts(turns: list[dict[str, Any]]) -> dict[str, int]:
    """Cell assignment counts over eligible T1 turns."""
    cells: dict[str, int] = {}
    for turn in eligible_t1_turns(turns):
        cell = turn["cell"]
        cells[cell] = cells.get(cell, 0) + 1
    return cells


def attempt_tally(records: list[dict[str, Any]]) -> dict[str, int]:
    """Full-disposition tally over unique attempt identities."""
    seen: dict[str, dict[str, Any]] = {}
    for record in records:
        aid = record["id"]
        if aid in seen:
            raise HarnessValidationError(f"duplicate attempt identity in ledger: {aid}")
        seen[aid] = record
    tally = {"ELIGIBLE": 0, "EXCLUDED": 0, "NON_ELIGIBLE": 0, "VOID_CONTRIBUTING": 0}
    for record in seen.values():
        disposition = record["disposition"]
        if disposition not in tally:
            raise HarnessValidationError(f"unknown disposition {disposition}")
        tally[disposition] += 1
    return tally


def exclusion_breakdown(turns: list[dict[str, Any]], reason_codes: set[str]) -> dict[str, int]:
    """Exclusion counts by closed reason code over non-eligible turns."""
    breakdown: dict[str, int] = {}
    for turn in turns:
        if turn["eligibility"] in ("EXCLUDED", "NON_ELIGIBLE", "VOID"):
            reason = turn["reason"]
            if reason not in reason_codes:
                raise HarnessValidationError(
                    f"exclusion reason outside closed vocabulary: {reason}"
                )
            breakdown[reason] = breakdown.get(reason, 0) + 1
    return breakdown


def capture_totals(
    turns: list[dict[str, Any]],
    receipts: list[dict[str, Any]],
    capture_events: list[dict[str, Any]],
) -> dict[str, int]:
    """Capture-health counters from events + receipt flags + turn markers."""
    totals = {
        "evaluator_failures": 0,
        "capture_errors": 0,
        "telemetry_omissions": 0,
        "dropped": 0,
        "duplicates": 0,
    }
    for event in capture_events:
        kind = event.get("kind")
        if kind not in EVENT_TO_COUNTER:
            raise HarnessValidationError(f"unknown capture event kind: {kind}")
        totals[EVENT_TO_COUNTER[kind]] += 1
    for receipt in receipts:
        if receipt.get("capture_error"):
            totals["capture_errors"] += 1
    for turn in turns:
        if turn.get("reason") in (
            "TELEMETRY_OMISSION_PATH_A",
            "TELEMETRY_OMISSION_PATH_B_UNEXPLAINED",
        ):
            totals["telemetry_omissions"] += 1
    return totals


def tap_populations(turns: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Per-tap populations recomputed from turn tap labels."""
    populations = {}
    for tap in TAPS:
        scoped = [t for t in turns if t.get("tap") == tap]
        populations[tap] = {
            "population": len(scoped),
            "eligible": sum(1 for t in scoped if t.get("eligibility") == "ELIGIBLE"),
            "excluded": sum(1 for t in scoped if t.get("eligibility") != "ELIGIBLE"),
            "divergences": sum(1 for t in scoped if t.get("divergent")),
        }
    return populations


def control_match(
    controls: list[dict[str, Any]], cohort: set[str]
) -> tuple[int, int, list[dict[str, Any]]]:
    """Single-contribution CONTROL_MATCH: (numerator, denominator, matched).

    Labeling is owned by `control.evaluate_fixture`, which recomputes each
    record's label from its embedded shadow counterpart via `compare_pair`
    and requires the stored label to agree; out-of-cohort fixtures void
    here. Duplicates, mismatches, unmatched records, and label tamper
    never contribute.
    """
    by_fixture: dict[str, list[dict[str, Any]]] = {}
    for control in controls:
        by_fixture.setdefault(control["fixture_id"], []).append(control)
    outside = set(by_fixture) - set(cohort)
    if outside:
        raise HarnessValidationError(f"control fixtures outside frozen cohort: {sorted(outside)}")
    matched = []
    for fixture in sorted(by_fixture):
        label, record = evaluate_fixture(fixture, by_fixture[fixture])
        if label == "MATCHED" and record is not None:
            matched.append(record)
    return len(matched), len(by_fixture), matched


def _require_inputs(inputs: dict[str, Any]) -> None:
    """Closed-void inputs envelope: every shape failure is a HarnessValidationError.

    The envelope itself has no registry schema, so presence and container
    shapes are checked here before any indexing. Inner record shapes stay
    schema-gated by their own `check_record` calls below.
    """
    if not isinstance(inputs, dict):
        raise HarnessValidationError("report inputs must be an object")
    for key in ("turns", "attempts"):
        if key not in inputs:
            raise HarnessValidationError(f"report inputs missing required block: {key}")
    if not isinstance(inputs["turns"], list):
        raise HarnessValidationError("report inputs turns must be an array")
    if not isinstance(inputs["attempts"], dict):
        raise HarnessValidationError("report inputs attempts must be an object")
    for key in ("receipts", "controls", "capture_events", "cohort_fixtures"):
        if key in inputs and inputs[key] is not None and not isinstance(inputs[key], list):
            raise HarnessValidationError(f"report inputs {key} must be an array")


def verify_report(
    *,
    inputs: dict[str, Any],
    report: dict[str, Any],
    registry: dict[str, Any],
    terminal: bool,
    freeze: dict[str, Any],
) -> None:
    """Recompute R1–R8 from source records; any violation voids.

    Trust boundary: source/join records are schema-gated here, and policy
    (required set, formula catalog, reason codes) is derived from the
    registry. A run-embedded formula set (`inputs["formulas"]`) is still
    checked against the catalog. Source records and the closed report object
    are authority-scanned and schema-validated before recomputation. The
    report is bound to its frozen inputs: `report["freeze_hash"]` must equal
    the independently recomputed freeze digest, so a report recomputed from
    correct records but a different freeze still voids. Control `MATCHED`
    labels are recomputed from each record's embedded shadow counterpart
    (never trusted from truthy fields), and the reconciled ledger total is
    bound to the freeze's `total_reservations_max`, so a consistent report
    over the frozen cap still voids.
    """
    validate_pinned_registry(registry)
    _require_inputs(inputs)
    if not isinstance(report, dict):
        raise HarnessValidationError("report must be an object")
    defs = registry["$defs"]
    required_metrics = list(defs["requiredMetricSet"]["const"])
    catalog = defs["formulaCatalog"]["const"]
    reason_codes = set(defs["reasonCode"]["enum"])
    for turn in inputs["turns"]:
        check_record(registry, "caseTurn", turn)
    check_record(registry, "attemptLedger", inputs["attempts"])
    for receipt in inputs.get("receipts", []):
        check_record(registry, "caseReceipt", receipt)
    verify_receipt_chain(inputs.get("receipts", []))
    for control in inputs.get("controls", []):
        check_record(registry, "control", control)
    for event in inputs.get("capture_events", []):
        check_record(registry, "captureEvent", event)
    check_record(registry, "report", report)
    if not isinstance(freeze, dict):
        raise HarnessValidationError("freeze must be an object")
    check_record(registry, "freeze", freeze)
    verify_freeze_self_hash(freeze)
    freeze_hash = freeze_digest(freeze)
    if report["freeze_hash"] != freeze_hash:
        raise HarnessValidationError("report freeze_hash does not match recomputed freeze digest")
    if freeze.get("run_set_id") != report["run_set_id"]:
        raise HarnessValidationError("freeze run_set_id does not match report run_set_id")
    formulas = inputs.get("formulas") or {mid: dict(entry) for mid, entry in catalog.items()}
    check_formula_catalog(formulas, catalog, required_metrics)
    for key in (
        "metrics",
        "cells",
        "capture",
        "taps",
        "controls",
        "accounting",
        "limitations",
        "verdict",
    ):
        if key not in report:
            raise HarnessValidationError(f"report missing required block: {key}")
    turns = inputs["turns"]
    attempts = inputs["attempts"]
    receipts_list = inputs.get("receipts", [])
    controls = inputs.get("controls", [])
    cohort = set(inputs.get("cohort_fixtures", []))
    capture_events = inputs.get("capture_events", [])

    metrics = {m["id"]: m for m in report["metrics"]}
    if sorted(metrics) != sorted(required_metrics):
        raise HarnessValidationError("report metric set does not match requiredMetricSet")
    if len(metrics) != len(report["metrics"]):
        raise HarnessValidationError("duplicate metric in report")

    # R6 + formula identity: golden rebuild from the pinned text.
    for mid, metric in metrics.items():
        gold = formulas[mid]
        rebuilt = formula_digest_for(gold["text"])
        if rebuilt != gold["digest"]:
            raise HarnessValidationError(f"golden digest mismatch for {mid}")
        if metric["formula_ref"] != rebuilt:
            raise HarnessValidationError(f"formula_ref mismatch for {mid}")
        if metric["measure_kind"] == "COUNT" and metric["denominator"] is not None:
            raise HarnessValidationError(f"COUNT metric {mid} must have null denominator")
        if metric["measure_kind"] not in ("COUNT", "RATIO"):
            raise HarnessValidationError(f"unknown measure_kind for {mid}")
        if (
            metric["measure_kind"] == "RATIO"
            and metric["denominator"] == 0
            and (
                metric["numerator"] != 0
                or "RATIO_UNDEFINED_ZERO_DENOMINATOR" not in report.get("limitations", [])
            )
        ):
            raise HarnessValidationError(
                f"zero-denominator RATIO {mid} without explicit limitation"
            )

    # Turn/attempt linkage (registry `id` key; turns link via `attempt_ref`).
    by_attempt: dict[str, dict[str, Any]] = {}
    for turn in turns:
        aid = turn.get("attempt_ref")
        if not aid:
            raise HarnessValidationError(f"turn without attempt_ref: {turn.get('count_key')}")
        if aid in by_attempt:
            raise HarnessValidationError(f"attempt {aid} claimed by two turns")
        by_attempt[aid] = turn
    records = {r["id"]: r for r in attempts["records"]}
    if len(records) != len(attempts["records"]):
        raise HarnessValidationError("duplicate attempt identity in ledger")
    want_disposition = {
        "ELIGIBLE": "ELIGIBLE",
        "EXCLUDED": "EXCLUDED",
        "NON_ELIGIBLE": "NON_ELIGIBLE",
        "VOID": "VOID_CONTRIBUTING",
    }
    for aid, record in records.items():
        if record["disposition"] not in (
            "ELIGIBLE",
            "EXCLUDED",
            "NON_ELIGIBLE",
            "VOID_CONTRIBUTING",
        ):
            raise HarnessValidationError(f"unknown disposition {record['disposition']}")
        if aid not in by_attempt:
            raise HarnessValidationError(f"recorded attempt {aid} has no turn")
        if record["disposition"] != want_disposition[by_attempt[aid]["eligibility"]]:
            raise HarnessValidationError(f"turn/attempt disposition mismatch for {aid}")
    for turn in turns:
        if turn["attempt_ref"] not in records:
            raise HarnessValidationError(f"turn links to unknown attempt {turn['attempt_ref']}")

    # R1: complete receipt identity. Self-consistency first over EVERY
    # receipt (referenced or not), then the turn join.
    receipts: dict[str, dict[str, Any]] = {}
    for receipt in receipts_list:
        if receipt["id"] in receipts:
            raise HarnessValidationError(f"duplicate receipt id {receipt['id']}")
        receipts[receipt["id"]] = receipt
    for receipt in receipts_list:
        activity = receipt.get("activity", {})
        if receipt.get("workflow_id") != activity.get("workflow_id"):
            raise HarnessValidationError(
                f"receipt {receipt['id']} top-level workflow_id differs from activity identity"
            )
        if receipt.get("workflow_run_id") != activity.get("workflow_run_id"):
            raise HarnessValidationError(
                f"receipt {receipt['id']} top-level workflow_run_id differs from activity identity"
            )
        if receipt.get("run_set_id") != report["run_set_id"]:
            raise HarnessValidationError(f"receipt {receipt['id']} from another run set")
    seen: set[str] = set()
    for turn in turns:
        rid = turn.get("receipt_ref")
        if rid is None:
            if turn["eligibility"] == "ELIGIBLE":
                raise HarnessValidationError("eligible turn without receipt identity")
            continue
        receipt = receipts.get(rid)
        if receipt is None:
            raise HarnessValidationError(f"turn references missing receipt {rid}")
        if turn["count_key"][0] != report["run_set_id"]:
            raise HarnessValidationError("turn from another run set")
        if turn.get("payload_digest") != receipt.get("payload_digest"):
            raise HarnessValidationError(f"turn/receipt payload digest mismatch for {rid}")
        if receipt.get("order") != turn.get("receipt_index"):
            raise HarnessValidationError(f"receipt/turn index mismatch for {rid}")
        if receipt.get("iteration") != turn.get("iteration"):
            raise HarnessValidationError(f"receipt/turn iteration mismatch for {rid}")
        if receipt.get("activity") != turn.get("activity"):
            raise HarnessValidationError(f"receipt/turn activity mismatch for {rid}")
        if turn["eligibility"] == "ELIGIBLE" and turn["tap"] == "T1":
            if rid in seen:
                raise HarnessValidationError(f"receipt {rid} double-counted")
            seen.add(rid)

    # R2: cells from eligible T1 turns only.
    elig = eligible_t1_turns(turns)
    if metrics["ELIGIBLE_T1_COUNT"]["numerator"] != len(elig):
        raise HarnessValidationError("ELIGIBLE_T1_COUNT mismatch")
    cells = cell_counts(turns)
    for cell in CELLS:
        if metrics[COUNT_CELLS[cell]]["numerator"] != cells.get(cell, 0):
            raise HarnessValidationError(f"{COUNT_CELLS[cell]} mismatch")
    if report["cells"] != {c: cells.get(c, 0) for c in CELLS}:
        raise HarnessValidationError("report cell summary mismatch")

    # R3: full accounting against the attempt ledger.
    tally = attempt_tally(attempts["records"])
    attempts = inputs["attempts"]
    if attempts["total"] != len(records) + attempts["reserved_open"]:
        raise HarnessValidationError("attempt ledger total mismatch")
    if attempts["total"] > freeze["total_reservations_max"]:
        raise HarnessValidationError("attempt ledger total exceeds frozen reservation limit")
    accounting = report["accounting"]
    if accounting["eligible"] != tally["ELIGIBLE"]:
        raise HarnessValidationError("accounting eligible mismatch")
    if accounting["excluded"] != tally["EXCLUDED"]:
        raise HarnessValidationError("accounting excluded mismatch")
    if accounting["non_eligible"] != tally["NON_ELIGIBLE"]:
        raise HarnessValidationError("accounting non_eligible mismatch")
    if accounting["void_contributing"] != tally["VOID_CONTRIBUTING"]:
        raise HarnessValidationError("accounting void_contributing mismatch")
    if accounting["reserved_open"] != attempts["reserved_open"]:
        raise HarnessValidationError("accounting reserved_open mismatch")
    if accounting["total_reservations"] != attempts["total"]:
        raise HarnessValidationError("accounting total mismatch")
    excl_turns = exclusion_breakdown(turns, reason_codes)
    excl_metric: dict[str, int] = {}
    for entry in metrics["ELIGIBLE_T1_COUNT"]["exclusions"]:
        if entry["reason"] not in reason_codes:
            raise HarnessValidationError(
                f"metric exclusion reason outside closed vocabulary: {entry['reason']}"
            )
        if entry["reason"] in excl_metric:
            raise HarnessValidationError(f"duplicate exclusion reason {entry['reason']}")
        excl_metric[entry["reason"]] = entry["count"]
    if excl_metric != excl_turns:
        raise HarnessValidationError(f"exclusion breakdown mismatch {excl_metric} vs {excl_turns}")

    # R4: capture-health recompute + CAPTURE_HEALTH triple agreement.
    cap = capture_totals(turns, receipts_list, capture_events)
    for key, value in cap.items():
        if report["capture"].get(key) != value:
            raise HarnessValidationError(f"capture-health {key} mismatch")
    if metrics["CAPTURE_HEALTH"]["numerator"] != sum(cap.values()):
        raise HarnessValidationError(
            "CAPTURE_HEALTH metric does not equal the recomputed capture-health total"
        )
    if sum(report["capture"].values()) != metrics["CAPTURE_HEALTH"]["numerator"]:
        raise HarnessValidationError(
            "CAPTURE_HEALTH metric does not match the report capture counters"
        )

    # Per-tap populations.
    want_taps = tap_populations(turns)
    for tap in TAPS:
        if metrics[f"PER_TAP_{tap}"]["numerator"] != want_taps[tap]["population"]:
            raise HarnessValidationError(f"PER_TAP_{tap} mismatch")
    if report.get("taps") != want_taps:
        raise HarnessValidationError("report taps mismatch")

    # CONTROL_MATCH single-contribution.
    matched_count, cohort_count, matched = control_match(controls, cohort)
    if metrics["CONTROL_MATCH"]["numerator"] != matched_count:
        raise HarnessValidationError("CONTROL_MATCH numerator mismatch")
    if metrics["CONTROL_MATCH"]["denominator"] != cohort_count:
        raise HarnessValidationError("CONTROL_MATCH denominator mismatch")
    if inputs.get("control_native_divergence"):
        raise HarnessValidationError(
            "native divergence between matched control and shadow counterpart"
        )
    got = {(c["fixture_id"], c.get("control_order")) for c in report.get("controls", [])}
    want = {(c["fixture_id"], c.get("control_order")) for c in matched}
    if got != want:
        raise HarnessValidationError("report controls do not match recomputed matched pairs")

    # R7: open vs terminal, both directions.
    if terminal:
        if report["verdict"] not in TERMINAL_VERDICTS:
            raise HarnessValidationError("terminal report with non-terminal verdict")
        if accounting["reserved_open"] != 0:
            raise HarnessValidationError("terminal report with unresolved RESERVED attempts")
    elif report["verdict"] != "HOLD":
        raise HarnessValidationError("open report must carry verdict HOLD")

    # R5: no cross-run-set references.
    for turn in turns:
        if turn["count_key"][0] != report["run_set_id"]:
            raise HarnessValidationError("turn from another run set")


__all__ = [
    "CELLS",
    "COUNT_CELLS",
    "EVENT_TO_COUNTER",
    "FORMULA_DOMAIN",
    "TAPS",
    "TERMINAL_VERDICTS",
    "attempt_tally",
    "capture_totals",
    "cell_counts",
    "check_formula_catalog",
    "control_match",
    "eligible_t1_turns",
    "exclusion_breakdown",
    "formula_digest_for",
    "tap_populations",
    "verify_report",
]
