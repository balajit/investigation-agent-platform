"""Capture and digest contract (§6, DIGEST-01, CAPTURE-01, §6.3).

Envelope digest preimage is exactly v1.9 §2.1: bundle digest, iteration,
receipt index, and the native snapshot (`verified`, `status`,
`confidence`, `hypothesis_id`, `producing_path`) — one normative formula,
never two. Receipt-chain verification enforces null-iff-genesis
`previous_digest` links, well-formed sha256 otherwise, and duplicate-order
rejection. Telemetry-omission classification implements the §6.3 Path A /
Path B table; the HARNESS_LOSS_RESIDUAL case is a stated limitation, not
a computed value.
"""

from __future__ import annotations

import re
from typing import Any

from investigation_agent_platform.harness.canonical import (
    canonical_bytes,
    sha256_digest,
)
from investigation_agent_platform.harness.registry import HarnessValidationError

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

NATIVE_SNAPSHOT_FIELDS = (
    "verified",
    "status",
    "confidence",
    "hypothesis_id",
    "producing_path",
)


def envelope_digest(
    bundle_digest: str,
    iteration: int,
    receipt_index: int,
    native: dict[str, Any],
) -> str:
    """Recompute the envelope digest under the single normative formula."""
    snapshot = {key: native[key] for key in NATIVE_SNAPSHOT_FIELDS}
    return sha256_digest(canonical_bytes([bundle_digest, iteration, receipt_index, snapshot]))


def verify_receipt_chain(receipts: list[dict[str, Any]]) -> None:
    """Verify receipt links, record digests, genesis shape, and unique order."""
    seen_orders: set[int] = set()
    ordered = sorted(receipts, key=lambda r: (r.get("order", 0), r.get("id", "")))
    previous_receipt: dict[str, Any] | None = None
    for position, receipt in enumerate(ordered):
        order = receipt.get("order")
        if not isinstance(order, int) or order < 0:
            raise HarnessValidationError(f"receipt has invalid order: {receipt.get('id')!r}")
        if order in seen_orders:
            raise HarnessValidationError(f"duplicate receipt order: {order}")
        seen_orders.add(order)
        if order != position:
            raise HarnessValidationError(
                f"receipt order gap before {receipt.get('id')!r}: expected {position}, got {order}"
            )
        previous = receipt.get("previous_digest")
        if position == 0:
            if previous is not None:
                raise HarnessValidationError(
                    f"genesis receipt {receipt.get('id')!r} must have null previous_digest"
                )
        else:
            if not isinstance(previous, str) or not _SHA256_RE.fullmatch(previous):
                raise HarnessValidationError(
                    f"receipt {receipt.get('id')!r} must link with a sha256 previous_digest"
                )
            expected = previous_receipt.get("payload_digest")
            if not isinstance(expected, str) or not _SHA256_RE.fullmatch(expected):
                raise HarnessValidationError(
                    f"preceding receipt {previous_receipt.get('id')!r} has invalid payload_digest"
                )
            if previous != expected:
                raise HarnessValidationError(
                    f"receipt {receipt.get('id')!r} previous_digest does not match "
                    f"preceding receipt {previous_receipt.get('id')!r}"
                )
        previous_receipt = receipt


def classify_telemetry_omission(
    telemetry_present: bool, valid_inband_flag: bool
) -> tuple[str, str]:
    """Classify a completed run under the §6.3 Path A / Path B table.

    Returns (disposition, reasonCode). A valid in-band flag contradicted by
    present telemetry, or any other malformed combination, fails closed.
    """
    if telemetry_present and valid_inband_flag:
        raise HarnessValidationError("in-band omission flag contradicted by present telemetry")
    if valid_inband_flag:
        return ("EXCLUDED", "TELEMETRY_OMISSION_PATH_A")
    if not telemetry_present:
        return ("VOID_CONTRIBUTING", "TELEMETRY_OMISSION_PATH_B_UNEXPLAINED")
    raise HarnessValidationError("telemetry present without omission: no omission to classify")


__all__ = [
    "NATIVE_SNAPSHOT_FIELDS",
    "classify_telemetry_omission",
    "envelope_digest",
    "verify_receipt_chain",
]
