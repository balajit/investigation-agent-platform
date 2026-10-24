"""Capture/digest contract (§6) and control arm comparison (§9)."""

from __future__ import annotations

import pytest

from investigation_agent_platform.harness.capture import (
    classify_telemetry_omission,
    envelope_digest,
    verify_receipt_chain,
)
from investigation_agent_platform.harness.control import (
    compare_pair,
    evaluate_fixture,
    match_tuple,
)
from investigation_agent_platform.harness.registry import HarnessValidationError


def _native(**overrides):
    rec = {
        "verified": True,
        "status": "s",
        "confidence": None,
        "hypothesis_id": "h",
        "producing_path": "g",
    }
    rec.update(overrides)
    return rec


def test_envelope_digest_single_formula() -> None:
    first = envelope_digest("sha256:" + "ab" * 32, 3, 7, _native())
    assert first == envelope_digest("sha256:" + "ab" * 32, 3, 7, _native())
    assert first.startswith("sha256:")
    assert first != envelope_digest("sha256:" + "ab" * 32, 3, 8, _native())
    assert first != envelope_digest("sha256:" + "ab" * 32, 3, 7, _native(status="other"))


def test_envelope_digest_ignores_extra_native_keys() -> None:
    assert envelope_digest("sha256:" + "ab" * 32, 0, 0, _native()) == envelope_digest(
        "sha256:" + "ab" * 32, 0, 0, _native(extra="ignored")
    )


def test_receipt_chain_genesis_and_links() -> None:
    genesis = {
        "id": "e-0",
        "order": 0,
        "payload_digest": "sha256:" + "bb" * 32,
        "previous_digest": None,
    }
    successor = {
        "id": "e-1",
        "order": 1,
        "payload_digest": "sha256:" + "cc" * 32,
        "previous_digest": genesis["payload_digest"],
    }
    receipts = [genesis, successor]
    verify_receipt_chain(receipts)
    with pytest.raises(HarnessValidationError, match="does not match preceding receipt"):
        verify_receipt_chain([genesis, {**successor, "previous_digest": "sha256:" + "aa" * 32}])
    with pytest.raises(HarnessValidationError, match="order gap"):
        verify_receipt_chain([genesis, {**successor, "order": 2}])
    with pytest.raises(HarnessValidationError, match="null previous_digest"):
        verify_receipt_chain([{"id": "e-0", "order": 0, "previous_digest": "sha256:" + "aa" * 32}])
    with pytest.raises(HarnessValidationError, match="sha256 previous_digest"):
        verify_receipt_chain(
            [
                {
                    "id": "e-0",
                    "order": 0,
                    "payload_digest": "sha256:" + "bb" * 32,
                    "previous_digest": None,
                },
                {"id": "e-1", "order": 1, "previous_digest": "not-a-digest"},
            ]
        )
    with pytest.raises(HarnessValidationError, match="duplicate receipt order"):
        verify_receipt_chain(
            [
                {"id": "e-0", "order": 0, "previous_digest": None},
                {"id": "e-1", "order": 0, "previous_digest": None},
            ]
        )


def test_classify_telemetry_omission_paths() -> None:
    assert classify_telemetry_omission(False, True) == ("EXCLUDED", "TELEMETRY_OMISSION_PATH_A")
    assert classify_telemetry_omission(False, False) == (
        "VOID_CONTRIBUTING",
        "TELEMETRY_OMISSION_PATH_B_UNEXPLAINED",
    )
    with pytest.raises(HarnessValidationError, match="contradicted"):
        classify_telemetry_omission(True, True)
    with pytest.raises(HarnessValidationError, match="no omission"):
        classify_telemetry_omission(True, False)


def _control(**overrides):
    rec = {
        "fixture_id": "L1",
        "fixture_version": "sha256:" + "ab" * 32,
        "prompt_hash": "sha256:" + "cd" * 32,
        "code_revision": "c" * 40,
        "tenant": "t",
        "database": "d",
        "capture_hash": "sha256:" + "ef" * 32,
        "counterpart_ref": "shadow-1",
        "control_order": 1,
        "match": "MATCHED",
        "counterpart": {
            "fixture_id": "L1",
            "fixture_version": "sha256:" + "ab" * 32,
            "prompt_hash": "sha256:" + "cd" * 32,
            "code_revision": "c" * 40,
            "tenant": "t",
            "database": "d",
            "capture_hash": "sha256:" + "ef" * 32,
            "counterpart_ref": "native-1",
            "control_order": 1,
        },
    }
    rec.update(overrides)
    return rec


def test_match_tuple_seven_fields() -> None:
    assert len(match_tuple(_control())) == 7


def test_compare_pair_outcomes() -> None:
    native, shadow = _control(), _control()
    assert compare_pair(native, shadow) == "MATCHED"
    assert compare_pair(native, None) == "UNMATCHED"
    assert compare_pair(native, _control(tenant="other")) == "MISMATCH"
    assert compare_pair(native, _control(counterpart_ref=None)) == "MISMATCH"
    assert compare_pair(native, _control(control_order=None)) == "MISMATCH"
    # Both orders recorded but disagreeing: different sequence slot.
    assert compare_pair(native, _control(control_order=2)) == "MISMATCH"


def test_evaluate_fixture_single_contribution() -> None:
    label, record = evaluate_fixture("L1", [_control()])
    assert label == "MATCHED" and record is not None
    label, record = evaluate_fixture("L1", [_control(), _control()])
    assert (label, record) == ("DUPLICATE", None)
    # Stored MISMATCH that agrees with the recomputed comparison stays
    # MISMATCH (never contributes).
    label, record = evaluate_fixture("L1", [_control(match="MISMATCH", counterpart=None)])
    assert (label, record) == ("MISMATCH", None)
    # Stored UNMATCHED with no counterpart agrees with the recomputation.
    label, record = evaluate_fixture("L1", [_control(match="UNMATCHED", counterpart=None)])
    assert (label, record) == ("UNMATCHED", None)
    label, record = evaluate_fixture("L1", [_control(counterpart_ref=None)])
    assert (label, record) == ("MISMATCH", None)


def test_evaluate_fixture_rejects_label_tamper() -> None:
    # MATCHED label on a divergent shadow counterpart: recomputed
    # MISMATCH disagrees with the stored label, so the fixture cannot
    # contribute.
    divergent = _control()
    divergent["counterpart"] = dict(divergent["counterpart"], tenant="other")
    label, record = evaluate_fixture("L1", [divergent])
    assert (label, record) == ("MISMATCH", None)
    # MATCHED label with no counterpart recorded at all.
    label, record = evaluate_fixture("L1", [_control(counterpart=None)])
    assert (label, record) == ("MISMATCH", None)
    # Honest MISMATCH (stored and recomputed agree) still never contributes.
    label, record = evaluate_fixture("L1", [dict(divergent, match="MISMATCH")])
    assert (label, record) == ("MISMATCH", None)
    # UNMATCHED label with no counterpart agrees with the recomputation.
    label, record = evaluate_fixture("L1", [_control(match="UNMATCHED", counterpart=None)])
    assert (label, record) == ("UNMATCHED", None)
