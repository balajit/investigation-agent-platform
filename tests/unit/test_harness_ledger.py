"""Run-set state machine and attempt/budget ledger (in-memory §§2–3 portion)."""

from __future__ import annotations

from pathlib import Path

import pytest

from investigation_agent_platform.harness.ledger import (
    AttemptLedger,
    attempt_id,
    transition,
)
from investigation_agent_platform.harness.registry import HarnessValidationError


def test_legal_path_to_reported_feasibility() -> None:
    state = "PREPARED"
    for edge in (
        "VERIFY_OWNER_AUTHORIZATION",
        "FIRST_RESERVATION",
        "RECORD_ATTEMPT",
        "CLOSE_FEASIBILITY",
        "REPORT_FEASIBILITY",
    ):
        state = transition(state, edge)
    assert state == "REPORTED_FEASIBILITY"


def test_initialize_is_creation_only() -> None:
    # INITIALIZE has an empty from-set: creation happens outside the
    # machine, so no existing state — including PREPARED — may take it.
    for state in (
        "PREPARED",
        "FROZEN",
        "COLLECTING",
        "VOID_TERMINAL",
        "CLOSED_INSUFFICIENT",
        "CLOSED_FEASIBILITY",
        "REPORTED_INSUFFICIENT",
        "REPORTED_FEASIBILITY",
    ):
        with pytest.raises(HarnessValidationError, match="impossible transition"):
            transition(state, "INITIALIZE")


def test_abort_path_never_reaches_feasibility() -> None:
    state = transition(transition("PREPARED", "VERIFY_OWNER_AUTHORIZATION"), "FIRST_RESERVATION")
    assert transition(state, "ABORT") == "CLOSED_INSUFFICIENT"
    assert transition("CLOSED_INSUFFICIENT", "REPORT_INSUFFICIENT") == "REPORTED_INSUFFICIENT"


def test_illegal_edges_rejected() -> None:
    with pytest.raises(HarnessValidationError, match="impossible transition"):
        transition("PREPARED", "FIRST_RESERVATION")
    with pytest.raises(HarnessValidationError, match="impossible transition"):
        transition("PREPARED", "CLOSE_FEASIBILITY")
    with pytest.raises(HarnessValidationError, match="unknown transition"):
        transition("COLLECTING", "REOPEN")


def test_terminal_never_reopens() -> None:
    for terminal in ("VOID_TERMINAL", "CLOSED_INSUFFICIENT", "CLOSED_FEASIBILITY"):
        for edge in ("VERIFY_OWNER_AUTHORIZATION", "RECORD_ATTEMPT", "INVALIDATE", "ABORT"):
            with pytest.raises(HarnessValidationError, match="impossible transition"):
                transition(terminal, edge)
    for reported in ("REPORTED_INSUFFICIENT", "REPORTED_FEASIBILITY"):
        with pytest.raises(HarnessValidationError, match="impossible transition"):
            transition(reported, "REPORT_INSUFFICIENT")


def test_invalidate_from_open_states() -> None:
    assert transition("PREPARED", "INVALIDATE") == "VOID_TERMINAL"
    assert transition("FROZEN", "INVALIDATE") == "VOID_TERMINAL"
    assert transition("COLLECTING", "INVALIDATE") == "VOID_TERMINAL"


def test_attempt_id_deterministic() -> None:
    first = attempt_id("rs", 1, "L1", "v", "c1")
    assert first == attempt_id("rs", 1, "L1", "v", "c1")
    assert first.startswith("sha256:")
    assert attempt_id("rs", 2, "L1", "v", "c1") != first


def test_attempt_id_domain_separated_golden() -> None:
    assert (
        attempt_id("rs", 1, "L1", "v", "c")
        == "sha256:4ada965ba26f3e7da5a497a4362f645d843b7f7cef0f1689c0c261c80d9ff07e"
    )


def test_reserve_rejects_unknown_bucket() -> None:
    ledger = AttemptLedger(run_set_id="rs-dev-001", freeze_hash="sha256:" + "ab" * 32)
    with pytest.raises(HarnessValidationError, match="outside frozen set"):
        ledger.reserve(
            fixture_id="L1",
            fixture_version="v",
            case_id="c",
            expected_cell="bucket-new",
            budget_token="tok",
        )


def test_reserved_record_binds_freeze_and_validates() -> None:
    from investigation_agent_platform.harness.registry import check_record, load_registry

    registry = load_registry(
        Path(__file__).resolve().parents[2]
        / "docs"
        / "IAP-proof-kernel-d3-stageC-harness-schemas-v1.13.json"
    )
    ledger = AttemptLedger(run_set_id="rs-dev-001", freeze_hash="sha256:" + "ab" * 32)
    rec = ledger.reserve(
        fixture_id="L1",
        fixture_version="sha256:" + "cd" * 32,
        case_id="c",
        expected_cell="accept-PASS",
        budget_token="tok",
    )
    assert rec["freeze_hash"] == "sha256:" + "ab" * 32
    check_record(registry, "attempt", rec)


def test_returned_records_are_isolated_copies() -> None:
    ledger = AttemptLedger(run_set_id="rs-dev-001", freeze_hash="sha256:" + "ab" * 32)
    rec = ledger.reserve(
        fixture_id="L1",
        fixture_version="v",
        case_id="c",
        expected_cell="accept-PASS",
        budget_token="tok",
    )
    rec["receipts"].append("e-1")
    rec["reason"] = "MUTATED"
    stored = ledger.records[rec["id"]]
    assert stored["receipts"] == []
    assert stored["reason"] == "JOIN_OK"
    recorded = ledger.record(rec["id"], disposition="ELIGIBLE", reason="JOIN_OK", receipts=["e-1"])
    recorded["receipts"].append("e-2")
    assert ledger.records[rec["id"]]["receipts"] == ["e-1"]
    external = ["e-9"]
    other = AttemptLedger(run_set_id="rs-dev-002", freeze_hash="sha256:" + "ab" * 32)
    rec2 = other.reserve(
        fixture_id="L1",
        fixture_version="v",
        case_id="c",
        expected_cell="accept-PASS",
        budget_token="tok",
    )
    other.record(rec2["id"], disposition="ELIGIBLE", reason="JOIN_OK", receipts=external)
    external.append("e-10")
    assert other.records[rec2["id"]]["receipts"] == ["e-9"]


def test_conflicting_record_voids_run_set() -> None:
    ledger = AttemptLedger(run_set_id="rs-dev-001", freeze_hash="sha256:" + "ab" * 32)
    rec = ledger.reserve(
        fixture_id="L1",
        fixture_version="v",
        case_id="c",
        expected_cell="accept-PASS",
        budget_token="tok",
    )
    ledger.record(rec["id"], disposition="ELIGIBLE", reason="JOIN_OK")
    assert ledger.run_set_void is False
    with pytest.raises(HarnessValidationError, match="different bytes"):
        ledger.record(rec["id"], disposition="EXCLUDED", reason="TRUNCATED_AWAY")
    assert ledger.run_set_void is True


def test_bucket_cap_enforced() -> None:
    ledger = AttemptLedger(run_set_id="rs-dev-001", freeze_hash="sha256:" + "ab" * 32)
    for index in range(12):
        ledger.reserve(
            fixture_id=f"L{index}",
            fixture_version="v",
            case_id="c",
            expected_cell="accept-PASS",
            budget_token="tok",
        )
    with pytest.raises(HarnessValidationError, match="bucket exhausted"):
        ledger.reserve(
            fixture_id="LX",
            fixture_version="v",
            case_id="c",
            expected_cell="accept-PASS",
            budget_token="tok",
        )


def test_total_cap_enforced_across_buckets() -> None:
    # A reduced cap isolates the total guard from the per-bucket cap while
    # keeping buckets restricted to the frozen expected-cell set.
    ledger = AttemptLedger(
        run_set_id="rs-dev-001",
        freeze_hash="sha256:" + "ab" * 32,
        total_cap=5,
    )
    cells = ["accept-PASS", "accept-FAIL", "nonaccept-PASS", "nonaccept-FAIL"]
    for index in range(5):
        ledger.reserve(
            fixture_id=f"L{index}",
            fixture_version="v",
            case_id="c",
            expected_cell=cells[index % 4],
            budget_token="tok",
        )
    assert ledger.total == 5
    with pytest.raises(HarnessValidationError, match="total reservation cap"):
        ledger.reserve(
            fixture_id="L5",
            fixture_version="v",
            case_id="c",
            expected_cell="accept-PASS",
            budget_token="tok",
        )


def test_total_cap_cannot_exceed_frozen_total() -> None:
    # The construction cap is test-configurable downward only: raising it
    # past the frozen 48-reservation run-set total fails closed at
    # construction, so reserve() can never succeed where the report
    # verifier (bound to freeze.total_reservations_max) must void.
    with pytest.raises(HarnessValidationError, match="exceeds frozen run-set total"):
        AttemptLedger(
            run_set_id="rs-dev-001",
            freeze_hash="sha256:" + "ab" * 32,
            total_cap=49,
        )
    ledger = AttemptLedger(
        run_set_id="rs-dev-001",
        freeze_hash="sha256:" + "ab" * 32,
        total_cap=48,
    )
    assert ledger.total_cap == 48


def test_cell_mismatch_stays_charged() -> None:
    ledger = AttemptLedger(run_set_id="rs-dev-001", freeze_hash="sha256:" + "ab" * 32)
    rec = ledger.reserve(
        fixture_id="L1",
        fixture_version="v",
        case_id="c",
        expected_cell="accept-PASS",
        budget_token="tok",
    )
    assert ledger.bucket_counts["accept-PASS"] == 1
    recorded = ledger.record(
        rec["id"], disposition="EXCLUDED", reason="BUDGET_RESERVED_CELL_MISMATCH"
    )
    assert recorded["disposition"] == "EXCLUDED"
    assert recorded["reason"] == "BUDGET_RESERVED_CELL_MISMATCH"
    assert ledger.bucket_counts["accept-PASS"] == 1
    assert ledger.expected_cells[rec["id"]] == "accept-PASS"


def test_double_record_same_bytes_idempotent() -> None:
    ledger = AttemptLedger(run_set_id="rs-dev-001", freeze_hash="sha256:" + "ab" * 32)
    rec = ledger.reserve(
        fixture_id="L1",
        fixture_version="v",
        case_id="c",
        expected_cell="accept-PASS",
        budget_token="tok",
    )
    first = ledger.record(rec["id"], disposition="ELIGIBLE", reason="JOIN_OK")
    second = ledger.record(rec["id"], disposition="ELIGIBLE", reason="JOIN_OK")
    assert first == second


def test_double_record_different_bytes_voids() -> None:
    ledger = AttemptLedger(run_set_id="rs-dev-001", freeze_hash="sha256:" + "ab" * 32)
    rec = ledger.reserve(
        fixture_id="L1",
        fixture_version="v",
        case_id="c",
        expected_cell="accept-PASS",
        budget_token="tok",
    )
    ledger.record(rec["id"], disposition="ELIGIBLE", reason="JOIN_OK")
    with pytest.raises(HarnessValidationError, match="different bytes"):
        ledger.record(rec["id"], disposition="EXCLUDED", reason="TRUNCATED_AWAY")


def test_void_contributing_marks_run_set() -> None:
    ledger = AttemptLedger(run_set_id="rs-dev-001", freeze_hash="sha256:" + "ab" * 32)
    rec = ledger.reserve(
        fixture_id="L1",
        fixture_version="v",
        case_id="c",
        expected_cell="accept-PASS",
        budget_token="tok",
    )
    assert ledger.run_set_void is False
    ledger.record(rec["id"], disposition="VOID_CONTRIBUTING", reason="CAPTURE_ERROR")
    assert ledger.run_set_void is True


def test_retry_rejects_dangling_parent() -> None:
    ledger = AttemptLedger(run_set_id="rs-dev-001", freeze_hash="sha256:" + "ab" * 32)
    with pytest.raises(HarnessValidationError, match="parent does not exist"):
        ledger.reserve(
            fixture_id="L1",
            fixture_version="v",
            case_id="c-retry",
            expected_cell="accept-PASS",
            budget_token="tok",
            parent_id="a-ghost",
        )


def test_reservation_replay_is_idempotent() -> None:
    ledger = AttemptLedger(run_set_id="rs-dev-001", freeze_hash="sha256:" + "ab" * 32)
    first = ledger.reserve(
        fixture_id="L1",
        fixture_version="v",
        case_id="c",
        expected_cell="accept-PASS",
        budget_token="tok",
    )
    replayed = ledger.reserve(
        fixture_id="L1",
        fixture_version="v",
        case_id="c",
        expected_cell="accept-PASS",
        budget_token="tok",
        reservation_seq=1,
    )
    assert replayed == first
    assert ledger.total == 1
    assert ledger.bucket_counts["accept-PASS"] == 1


def test_reservation_replay_different_bytes_voids() -> None:
    ledger = AttemptLedger(run_set_id="rs-dev-001", freeze_hash="sha256:" + "ab" * 32)
    ledger.reserve(
        fixture_id="L1",
        fixture_version="v",
        case_id="c",
        expected_cell="accept-PASS",
        budget_token="tok",
    )
    assert ledger.run_set_void is False
    with pytest.raises(HarnessValidationError, match="different expected cell"):
        ledger.reserve(
            fixture_id="L1",
            fixture_version="v",
            case_id="c",
            expected_cell="accept-FAIL",
            budget_token="tok",
            reservation_seq=1,
        )
    assert ledger.run_set_void is True
    with pytest.raises(HarnessValidationError, match="different bytes"):
        ledger.reserve(
            fixture_id="L1",
            fixture_version="v",
            case_id="c-changed",
            expected_cell="accept-PASS",
            budget_token="tok",
            reservation_seq=1,
        )
    assert ledger.run_set_void is True


def test_reservation_sequence_gap_voids() -> None:
    ledger = AttemptLedger(run_set_id="rs-dev-001", freeze_hash="sha256:" + "ab" * 32)
    with pytest.raises(HarnessValidationError, match="sequence gap"):
        ledger.reserve(
            fixture_id="L1",
            fixture_version="v",
            case_id="c",
            expected_cell="accept-PASS",
            budget_token="tok",
            reservation_seq=3,
        )
    assert ledger.run_set_void is True
    assert ledger.total == 0


def test_retry_links_parent() -> None:
    ledger = AttemptLedger(run_set_id="rs-dev-001", freeze_hash="sha256:" + "ab" * 32)
    parent = ledger.reserve(
        fixture_id="L1",
        fixture_version="v",
        case_id="c",
        expected_cell="accept-PASS",
        budget_token="tok",
    )
    child = ledger.reserve(
        fixture_id="L1",
        fixture_version="v",
        case_id="c-retry",
        expected_cell="accept-PASS",
        budget_token="tok",
        parent_id=parent["id"],
    )
    assert child["parent_id"] == parent["id"]
    assert child["id"] != parent["id"]


def test_ledger_head_stable_and_ordered() -> None:
    first = AttemptLedger(run_set_id="rs-dev-001", freeze_hash="sha256:" + "ab" * 32)
    second = AttemptLedger(run_set_id="rs-dev-001", freeze_hash="sha256:" + "ab" * 32)
    for ledger in (first, second):
        for index in range(3):
            rec = ledger.reserve(
                fixture_id=f"L{index}",
                fixture_version="v",
                case_id="c",
                expected_cell="accept-PASS",
                budget_token="tok",
            )
            ledger.record(rec["id"], disposition="ELIGIBLE", reason="JOIN_OK")
    assert first.head() == second.head()
    assert first.head().startswith("sha256:")
