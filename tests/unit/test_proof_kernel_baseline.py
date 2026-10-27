# tests/unit/test_proof_kernel_baseline.py
"""WP-A1 baseline self-check (TEST-ONLY, stdlib only).

The frozen battery baseline reproduces byte-identically from the case
fixtures; every claim carries a kind, proof requirements, and an explicit
attribution state. Live per-claim traces stay pending until a live run.
"""

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import proof_kernel_baseline as baseline

FIXDIR = Path(__file__).resolve().parents[1] / "fixtures" / "proof_kernel"
CASEDIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "battery_cases"

VALID_KINDS = {"DEFINITION", "CALL_EDGE", "BEHAVIOR", "ABSENCE", "HISTORY"}


def test_baseline_check_passes():
    assert baseline.main(check_only=True) == 0


def test_baseline_is_frozen_and_reproducible():
    manifest = json.loads((FIXDIR / "baseline_manifest_v1.json").read_bytes())
    dataset = json.loads((FIXDIR / "baseline_claims_v1.json").read_bytes())
    fresh_manifest, fresh_dataset = baseline.build()
    assert baseline.comparable(manifest) == baseline.comparable(fresh_manifest), (
        "baseline content drifted: re-freeze deliberately"
    )
    assert dataset == fresh_dataset


def test_manifest_pins_inputs():
    manifest = json.loads((FIXDIR / "baseline_manifest_v1.json").read_bytes())
    assert manifest["version"] == "baseline/v1"
    assert manifest["git_rev"] and manifest["git_rev"] != "unknown"
    for name, claimed in manifest["battery_case_digests"].items():
        raw = (CASEDIR / name).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == claimed, name
    assert set(manifest["battery_cases"]) == {p.name for p in CASEDIR.glob("*.json")}, (
        "manifest must list the full inventory, no subset"
    )
    assert len(manifest["attribution_buckets"]) == 7


def test_every_claim_fully_specified():
    dataset = json.loads((FIXDIR / "baseline_claims_v1.json").read_bytes())
    assert dataset["claims"], "baseline without claims is vacuous"
    seen: set[str] = set()
    for claim in dataset["claims"]:
        assert claim["claim_id"] not in seen, claim["claim_id"]
        seen.add(claim["claim_id"])
        assert claim["claim_kind"] in VALID_KINDS, claim["claim_id"]
        assert claim["proof_requirements"], claim["claim_id"]
        assert claim["expected_terminal"] in ("ANSWERED", "ABSTAIN"), claim["claim_id"]
        attrib = claim["attribution"]
        assert attrib["status"] in ("live-pending", "offline-gradable"), claim["claim_id"]
        assert attrib["bucket"] is None, "frozen baseline carries no attribution yet"
    for abst in dataset["abstentions"]:
        assert abst["expected_terminal"] == "ABSTAIN", abst["case_id"]


def test_callers_expand_per_expected_caller():
    dataset = json.loads((FIXDIR / "baseline_claims_v1.json").read_bytes())
    l2 = [c for c in dataset["claims"] if c["case_id"] == "L2"]
    assert len(l2) == 2, "L2 names two expected callers; each gets its own obligation"
    assert {c["target"]["caller"] for c in l2} == {"OrderQualifyHelper", "OrderServiceRestProxy"}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
