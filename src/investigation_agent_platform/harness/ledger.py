"""Run-set state machine and attempt/budget ledger (in-memory §§2–3 portion).

This module is the in-memory state machine plus the budget/attempt ledger.
Crash recovery and persistence (§2.1: temp files, fsync, atomic rename,
CAS on `state_version`, replay from anchored bytes) and the fault-injection
table (§2.2) are NOT implemented here; `reservation_seq` replay covers
re-submission idempotency, not restart recovery. That work is a separately
scoped package.

Legal run-set transitions (closed set — no other edge is valid)::

    (creation) --INITIALIZE--> PREPARED
    PREPARED --VERIFY_OWNER_AUTHORIZATION--> FROZEN
    FROZEN --FIRST_RESERVATION--> COLLECTING
    PREPARED|FROZEN|COLLECTING --INVALIDATE--> VOID_TERMINAL
    COLLECTING --RECORD_ATTEMPT--> COLLECTING
    COLLECTING --ABORT--> CLOSED_INSUFFICIENT
    COLLECTING --EXHAUST_INSUFFICIENT--> CLOSED_INSUFFICIENT
    COLLECTING --CLOSE_FEASIBILITY--> CLOSED_FEASIBILITY
    VOID_TERMINAL|CLOSED_INSUFFICIENT --REPORT_INSUFFICIENT--> REPORTED_INSUFFICIENT
    CLOSED_FEASIBILITY --REPORT_FEASIBILITY--> REPORTED_FEASIBILITY

A terminal identity never reopens. Budget (Amendment A1 §A1-3): 12
reservations per expected-cell bucket plus a hard run-set total of 48.
The reservation charges the frozen expected cell at reservation time;
observed-cell mismatches stay charged under the reserved bucket with
reason `BUDGET_RESERVED_CELL_MISMATCH`. Excluded and non-eligible attempts
consume budget; no refill, refund, transfer, or post-hoc reassignment.
Exact replay of identical attempt bytes is idempotent; same ID with
different bytes voids.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from investigation_agent_platform.harness.canonical import (
    canonical_bytes,
    sha256_digest,
)
from investigation_agent_platform.harness.registry import CELLS, HarnessValidationError

BUCKET_CAP = 12
TOTAL_CAP = 48

ATTEMPT_ID_DOMAIN = b"iap:d3-stagec:attempt-id/v1"

TERMINAL_STATES = frozenset({"VOID_TERMINAL", "CLOSED_INSUFFICIENT", "CLOSED_FEASIBILITY"})
REPORTED_STATES = frozenset({"REPORTED_INSUFFICIENT", "REPORTED_FEASIBILITY"})

TERMINAL_DISPOSITIONS = frozenset({"ELIGIBLE", "EXCLUDED", "NON_ELIGIBLE", "VOID_CONTRIBUTING"})

_TRANSITIONS: dict[str, tuple[frozenset[str], str]] = {
    # INITIALIZE is the creation edge: a run set comes into existence as
    # PREPARED. No existing state may take it (empty from-set), so creation
    # happens outside the machine and every live run set starts at PREPARED.
    "INITIALIZE": (frozenset(), "PREPARED"),
    "VERIFY_OWNER_AUTHORIZATION": (frozenset({"PREPARED"}), "FROZEN"),
    "FIRST_RESERVATION": (frozenset({"FROZEN"}), "COLLECTING"),
    "INVALIDATE": (frozenset({"PREPARED", "FROZEN", "COLLECTING"}), "VOID_TERMINAL"),
    "RECORD_ATTEMPT": (frozenset({"COLLECTING"}), "COLLECTING"),
    "ABORT": (frozenset({"COLLECTING"}), "CLOSED_INSUFFICIENT"),
    "EXHAUST_INSUFFICIENT": (frozenset({"COLLECTING"}), "CLOSED_INSUFFICIENT"),
    "CLOSE_FEASIBILITY": (frozenset({"COLLECTING"}), "CLOSED_FEASIBILITY"),
    "REPORT_INSUFFICIENT": (
        frozenset({"VOID_TERMINAL", "CLOSED_INSUFFICIENT"}),
        "REPORTED_INSUFFICIENT",
    ),
    "REPORT_FEASIBILITY": (frozenset({"CLOSED_FEASIBILITY"}), "REPORTED_FEASIBILITY"),
}


def transition(state: str, transition_name: str) -> str:
    """Apply one closed-set transition; fail closed on any other edge."""
    if transition_name not in _TRANSITIONS:
        raise HarnessValidationError(f"unknown transition: {transition_name!r}")
    allowed_from, to_state = _TRANSITIONS[transition_name]
    if state not in allowed_from:
        raise HarnessValidationError(f"impossible transition {transition_name} from {state}")
    return to_state


def attempt_id(
    run_set_id: str,
    reservation_seq: int,
    fixture_id: str,
    fixture_version: str,
    case_id: str,
) -> str:
    """Domain-separated attempt ID.

    Preimage: `domain || 0x00 || canonical JSON` of
    `(run_set_id, reservation_seq, fixture_id, fixture_version, case_id)`.
    """
    return sha256_digest(
        ATTEMPT_ID_DOMAIN
        + b"\x00"
        + canonical_bytes([run_set_id, reservation_seq, fixture_id, fixture_version, case_id])
    )


@dataclass
class AttemptLedger:
    """Append-only attempt ledger with BUD-01 budget enforcement.

    `freeze_hash` binds every reserved attempt to its freeze (registry
    `attempt` requires it). `total_cap` defaults to the frozen
    48-reservation run-set total; it is a construction parameter so the
    guard itself is unit-testable. `by_seq` maps reservation sequences to
    attempt IDs so re-submission can replay a specific sequence
    idempotently instead of charging a fresh reservation. Restart recovery
    from persisted bytes (§2.1) is out of scope for this in-memory ledger.
    """

    run_set_id: str
    freeze_hash: str
    bucket_counts: dict[str, int] = field(default_factory=dict)
    records: dict[str, dict[str, Any]] = field(default_factory=dict)
    expected_cells: dict[str, str] = field(default_factory=dict)
    by_seq: dict[int, str] = field(default_factory=dict)
    total: int = 0
    total_cap: int = TOTAL_CAP
    run_set_void: bool = False

    def __post_init__(self) -> None:
        """Fail closed when the construction cap exceeds the frozen total.

        `total_cap` stays a construction parameter so the guard itself is
        unit-testable with lower caps, but it can never be raised above the
        frozen 48-reservation run-set total: a ledger that could reserve
        past the freeze would let `reserve()` succeed where the report
        verifier (bound to `freeze.total_reservations_max`) must void.
        """
        if self.total_cap > TOTAL_CAP:
            raise HarnessValidationError(
                f"total_cap {self.total_cap} exceeds frozen run-set total {TOTAL_CAP}"
            )

    def head(self) -> str:
        """Ledger-head digest over record digests in append order."""
        digests = [rec["digest"] for rec in self.records.values()]
        return sha256_digest(canonical_bytes([self.run_set_id, digests]))

    def reserve(
        self,
        *,
        fixture_id: str,
        fixture_version: str,
        case_id: str,
        expected_cell: str,
        budget_token: str,
        parent_id: str | None = None,
        state_version: int = 1,
        reservation_seq: int | None = None,
    ) -> dict[str, Any]:
        """Reserve one attempt, charging the frozen expected-cell bucket.

        The bucket must be one of the four frozen expected cells; the
        binding is part of the reserved record. A retry must name an
        existing earlier reservation as its parent — dangling links are
        refused. Re-submission replays by passing the original
        `reservation_seq`: identical bytes return the original record
        with no second charge; different bytes, or a sequence gap, void
        the run set.
        """
        if expected_cell not in CELLS:
            raise HarnessValidationError(f"expected cell outside frozen set: {expected_cell!r}")
        if parent_id is not None and parent_id not in self.records:
            raise HarnessValidationError(f"retry parent does not exist: {parent_id!r}")
        if reservation_seq is None:
            seq = self.total + 1
        else:
            if reservation_seq < 1:
                raise HarnessValidationError(f"invalid reservation sequence: {reservation_seq!r}")
            if reservation_seq in self.by_seq:
                return self._replay(
                    reservation_seq,
                    fixture_id,
                    fixture_version,
                    case_id,
                    expected_cell,
                    budget_token,
                    parent_id,
                    state_version,
                )
            if reservation_seq != self.total + 1:
                self.run_set_void = True
                raise HarnessValidationError(
                    f"reservation sequence gap at {reservation_seq}: run set void"
                )
            seq = reservation_seq
        if self.bucket_counts.get(expected_cell, 0) >= BUCKET_CAP:
            raise HarnessValidationError(f"expected-cell bucket exhausted: {expected_cell}")
        if self.total >= self.total_cap:
            raise HarnessValidationError("run-set total reservation cap reached")
        aid = attempt_id(self.run_set_id, seq, fixture_id, fixture_version, case_id)
        record = {
            "schema": "attempt/v1",
            "run_set_id": self.run_set_id,
            "freeze_hash": self.freeze_hash,
            "seq": seq,
            "id": aid,
            "fixture_id": fixture_id,
            "fixture_version": fixture_version,
            "case_id": case_id,
            "budget_token": budget_token,
            "parent_id": parent_id,
            "state_version": state_version,
            "disposition": "RESERVED",
            "reason": "JOIN_OK",
            "receipts": [],
        }
        record["digest"] = sha256_digest(canonical_bytes(record))
        self.bucket_counts[expected_cell] = self.bucket_counts.get(expected_cell, 0) + 1
        self.records[aid] = record
        # Expected-cell assignment lives in a side table so the frozen
        # record keeps exactly the registry `attempt` shape.
        self.expected_cells[aid] = expected_cell
        self.by_seq[seq] = aid
        self.total += 1
        return copy.deepcopy(record)

    def _replay(
        self,
        seq: int,
        fixture_id: str,
        fixture_version: str,
        case_id: str,
        expected_cell: str,
        budget_token: str,
        parent_id: str | None,
        state_version: int,
    ) -> dict[str, Any]:
        """Idempotent reservation replay: same bytes return the original.

        No second charge, no totals change. Different bytes void the run
        set (same-ID conflict), like the post-record conflict path.
        """
        stored = self.records[self.by_seq[seq]]
        if expected_cell != self.expected_cells.get(stored["id"]):
            self.run_set_void = True
            raise HarnessValidationError(
                f"reservation seq {seq} replayed with a different expected cell: run set void"
            )
        candidate = {
            "schema": "attempt/v1",
            "run_set_id": self.run_set_id,
            "freeze_hash": self.freeze_hash,
            "seq": seq,
            "id": stored["id"],
            "fixture_id": fixture_id,
            "fixture_version": fixture_version,
            "case_id": case_id,
            "budget_token": budget_token,
            "parent_id": parent_id,
            "state_version": state_version,
            "disposition": stored["disposition"],
            "reason": stored["reason"],
            "receipts": list(stored["receipts"]),
        }
        candidate.pop("digest", None)
        if sha256_digest(canonical_bytes(candidate)) == stored.get("digest"):
            return copy.deepcopy(stored)
        self.run_set_void = True
        raise HarnessValidationError(
            f"reservation seq {seq} replayed with different bytes: run set void"
        )

    def record(
        self,
        attempt_id_value: str,
        *,
        disposition: str,
        reason: str,
        receipts: list[str] | None = None,
    ) -> dict[str, Any]:
        """Move a RESERVED attempt to exactly one terminal disposition.

        Exact replay of identical bytes is idempotent; same ID with
        different bytes voids the run set (the flag is set before raising
        so the single enforced caller path transitions to VOID_TERMINAL).
        `VOID_CONTRIBUTING` likewise marks the run set void. Stored and
        returned records are defensive copies: caller-held lists can never
        mutate ledger content behind a frozen digest.
        """
        if disposition not in TERMINAL_DISPOSITIONS:
            raise HarnessValidationError(f"unknown terminal disposition: {disposition!r}")
        stored = self.records.get(attempt_id_value)
        if stored is None:
            raise HarnessValidationError(f"record for unknown attempt: {attempt_id_value!r}")
        if stored["disposition"] != "RESERVED":
            candidate = dict(
                stored,
                disposition=disposition,
                reason=reason,
                receipts=list(receipts) if receipts is not None else list(stored["receipts"]),
            )
            candidate.pop("digest", None)
            if sha256_digest(canonical_bytes(candidate)) == stored.get("digest"):
                return copy.deepcopy(stored)
            self.run_set_void = True
            raise HarnessValidationError(
                f"attempt {attempt_id_value!r} already recorded with different bytes; run set void"
            )
        stored["disposition"] = disposition
        stored["reason"] = reason
        if receipts is not None:
            stored["receipts"] = list(receipts)
        stored["digest"] = sha256_digest(
            canonical_bytes({k: v for k, v in stored.items() if k != "digest"})
        )
        if disposition == "VOID_CONTRIBUTING":
            self.run_set_void = True
        return copy.deepcopy(stored)


__all__ = [
    "BUCKET_CAP",
    "REPORTED_STATES",
    "TERMINAL_DISPOSITIONS",
    "TERMINAL_STATES",
    "TOTAL_CAP",
    "AttemptLedger",
    "attempt_id",
    "transition",
]
