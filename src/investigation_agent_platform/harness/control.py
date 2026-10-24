"""Control arm comparison procedure (§9, SC-M-09).

A control run is comparable to its shadow counterpart if and only if every
field of the match tuple is equal — `(fixture_id, fixture_version,
prompt_hash, code_revision, tenant, database, capture_hash)` — and
`match` is `MATCHED` with a non-null counterpart record and agreeing
`control_order` on both sides: each side must record a sequence position,
and the two positions must be equal, so a native run cannot be paired
with a shadow run from a different sequence slot. `UNMATCHED` (no
counterpart), `DUPLICATE` (more than one control per fixture), or
`MISMATCH` (any tuple field differs, order unrecorded or disagreeing,
counterpart missing, or the stored label disagrees with the recomputed
comparison) is reported as NON_COMPARABLE, never parity. A valid matched
native divergence stops the probe.
`evaluate_fixture` recomputes the label from the record's embedded shadow
counterpart via `compare_pair` and requires the stored label to agree, so a
`MATCHED` label on divergent or missing counterpart evidence never
contributes. This module owns the labeling procedure; `metrics.control_match`
counts the labeled pairs.
"""

from __future__ import annotations

from typing import Any

MATCH_TUPLE_FIELDS = (
    "fixture_id",
    "fixture_version",
    "prompt_hash",
    "code_revision",
    "tenant",
    "database",
    "capture_hash",
)


def match_tuple(control: dict[str, Any]) -> tuple[Any, ...]:
    """The seven-field comparability tuple for one control record."""
    return tuple(control.get(field) for field in MATCH_TUPLE_FIELDS)


def compare_pair(native: dict[str, Any], shadow: dict[str, Any] | None) -> str:
    """Compare one native control against its shadow counterpart.

    Returns `MATCHED`, `UNMATCHED` (no counterpart), or `MISMATCH` (any
    tuple field differs, order unrecorded or disagreeing, or counterpart
    missing).
    """
    if shadow is None:
        return "UNMATCHED"
    if any(native.get(field) != shadow.get(field) for field in MATCH_TUPLE_FIELDS):
        return "MISMATCH"
    for side in (native, shadow):
        if not side.get("counterpart_ref") or not side.get("control_order"):
            return "MISMATCH"
    if native.get("control_order") != shadow.get("control_order"):
        return "MISMATCH"
    return "MATCHED"


def evaluate_fixture(
    fixture_id: str, records: list[dict[str, Any]]
) -> tuple[str, dict[str, Any] | None]:
    """Label one fixture: at most one matched pair ever contributes.

    The label is recomputed from the record's embedded shadow counterpart
    via `compare_pair`, and the stored `match` label must agree with the
    recomputation: a `MATCHED` label on divergent, order-disagreeing, or
    missing counterpart evidence yields `MISMATCH`. Exactly one record whose
    stored and recomputed labels are both `MATCHED` yields
    `(MATCHED, record)`; duplicates, mismatches, and unmatched records yield
    `(DUPLICATE | MISMATCH | UNMATCHED, None)`.
    """
    if len(records) != 1:
        return ("DUPLICATE", None)
    record = records[0]
    label = compare_pair(record, record.get("counterpart"))
    if record.get("match") != label:
        return ("MISMATCH", None)
    if label != "MATCHED":
        return (label, None)
    return ("MATCHED", record)


__all__ = [
    "MATCH_TUPLE_FIELDS",
    "compare_pair",
    "evaluate_fixture",
    "match_tuple",
]
