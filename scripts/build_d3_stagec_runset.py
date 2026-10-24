#!/usr/bin/env python3
"""Build Stage C live run-set artifacts for a frozen §6.2i record.

Run-set: d3-stagec-2026-10-24-r1 under freeze record hash 9be13fa7...
(owner-signed 2026-10-24, commit fdb9e4a).

Creates artifacts/<RS>/ with rule texts (verbatim frozen sections),
decision records, key_list.json, freeze.json (registry freeze/v4),
ledger.json (6 reservations, one per live fixture), and RUN_LOG.md.

All *_hash/digest fields are `sha256:<hex>` over exact artifact bytes.
freeze_hash is the harness freeze_digest (canonical JSON minus self field).

Usage:
    uv run python scripts/build_d3_stagec_runset.py
"""

from __future__ import annotations

import datetime
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from investigation_agent_platform.harness.canonical import sha256_digest
from investigation_agent_platform.harness.freeze import (
    freeze_digest,
    generate_dev_keypair,
    verify_freeze_self_hash,
)
from investigation_agent_platform.harness.ledger import AttemptLedger

RS = "d3-stagec-2026-10-24-r1"
ART = ROOT / "artifacts" / RS
DEC = ART / "decisions"

V19 = ROOT / "docs" / "IAP-proof-kernel-d3-phase0-approval-package-v1.9.md"
A1 = ROOT / "docs" / "IAP-proof-kernel-d3-phase0-amendment-A1.md"
MANIFEST = ROOT / "docs" / "IAP-proof-kernel-d3-stageC-freeze-manifest-v1.json"
FREEZE_RECORD_HASH = "9be13fa7b9f730655aac28797218dca06f076c499feb802dcb0de57e95b99dc1"
FREEZE_COMMIT = "fdb9e4a121339f8fad49f23f977d32b43de55c2e"
GOVERNED_BASE = "0403a341421b82419fdd83239cf1acb73ccdd8ed"

# Fixture -> expected cell (a priori, from fixture `expect` + §6.1 taxonomy).
# L1-L5 expect ANSWERED terminals; L8 expects a clean ABSTAIN terminal.
PLAN: list[tuple[str, str, str]] = [
    ("L1.json", "L1", "accept-PASS"),
    ("L2.json", "L2", "accept-PASS"),
    ("L3.json", "L3", "accept-PASS"),
    ("L4.json", "L4", "accept-PASS"),
    ("L5.json", "L5", "accept-PASS"),
    ("L8.json", "L8", "nonaccept-PASS"),
]


def section_lines(path: Path, start: int, end: int) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return lines[start - 1 : end]


def rule_file(name: str, parts: list[tuple[str, Path, int, int]]) -> bytes:
    out: list[str] = [
        f"# Stage C run-set {RS} rule file: {name}",
        "# Verbatim frozen sections. Digest binds these exact bytes.",
        "",
    ]
    for label, path, start, end in parts:
        doc_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        out.append(f"## Source: {path.name} lines {start}-{end}")
        out.append(f"## Doc SHA-256: {doc_hash}")
        out.append(f"## ({label})")
        out.append("")
        out.extend(section_lines(path, start, end))
        out.append("")
    return ("\n".join(out)).encode("utf-8")


def utcnow() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def main() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    digests: dict[str, str] = manifest["battery_case_digests"]
    prompts = manifest["prompts"]
    form014 = prompts["reasoner_source"]["sha256"]
    form015 = prompts["compiler_source"]["sha256"]
    assert prompts["reasoner_prompt_version"] == "v3-claim-grounded-014"
    assert prompts["compiler_prompt_version"] == "v3-claim-grounded-015"

    # Frozen-doc integrity assertions (file bytes must match frozen hashes).
    v19_hash = "sha256:" + hashlib.sha256(V19.read_bytes()).hexdigest()
    assert v19_hash == (
        "sha256:547aee50446c0fcc674a67e5f239e15de6dcf5f73de52474f3f133ce2be965c3"
    ), v19_hash
    a1_hash = "sha256:" + hashlib.sha256(A1.read_bytes()).hexdigest()
    assert a1_hash == (
        "sha256:c96ea6c8d5a97f7d75d664d4f1e109840c7a2b2316805dbbd2f629f6d4d7302f"
    ), a1_hash

    # Governed code bytes unchanged since the frozen base commit.
    diff = subprocess.run(
        ["git", "diff", GOVERNED_BASE, "HEAD", "--", "src", "tests", "config",
         "pyproject.toml"],
        capture_output=True, text=True, cwd=ROOT, check=True,
    ).stdout
    assert diff == "", "governed paths differ from frozen base"

    ART.mkdir(parents=True, exist_ok=True)
    DEC.mkdir(parents=True, exist_ok=True)

    # ---- Rule texts (verbatim frozen sections). ----
    rules: dict[str, bytes] = {
        "comparison.txt": rule_file("comparison", [
            ("v1.9 §6.1 frozen definitions and outcome taxonomy", V19, 1431, 1538),
        ]),
        "budget.txt": rule_file("budget", [
            ("v1.9 §6.2e probe budget and go/no-go", V19, 1647, 1695),
            ("A1-3 reservation buckets (BUD-01)", A1, 34, 47),
        ]),
        "control.txt": rule_file("control", [
            ("v1.9 §6.2g shadow-off control arm", V19, 1731, 1752),
        ]),
        "exclusion.txt": rule_file("exclusion", [
            ("v1.9 §6.2b cohort and run-level eligibility", V19, 1586, 1613),
            ("A1-1 timeout disposition (OD-01)", A1, 14, 24),
            ("A1-2 superseded-attempt disposition (OD-02)", A1, 26, 32),
        ]),
        "capture.txt": rule_file("capture", [
            ("v1.9 §1.4 observation channel and persistence lifecycle", V19, 478, 755),
            ("v1.9 §6.2d capture model and analysis inputs", V19, 1625, 1646),
        ]),
    }
    for name, data in rules.items():
        (ART / name).write_bytes(data)

    # ---- Pairing plan appended to control.txt (operator-authored). ----
    plan_lines = [
        "",
        "## Pairing plan (operator-authored under freeze item 5, bound by control_hash)",
        f"## Run set {RS}. Shadow arm runs first (worker IAP_SHADOW_TYPED_DEFINITIONS=1),",
        "## then the control arm (worker flag unset) after a worker restart.",
        "## Per fixture: k-th shadow attempt pairs with k-th control run;",
        "## both sides carry control_order = per-fixture attempt ordinal (1-based),",
        "## which must agree per the v1.15 order rule. Stop on native divergence.",
        "",
    ]
    for i, (fixture_id, case_id, cell) in enumerate(PLAN, start=1):
        plan_lines.append(
            f"## {fixture_id}: shadow reservation seq {i} (bucket {cell}) <-> "
            f"control ordinal 1"
        )
    plan_lines.append("")
    with open(ART / "control.txt", "ab") as fh:
        fh.write(("\n".join(plan_lines)).encode("utf-8"))

    # ---- Dependency inventory (authored from frozen pins + measured env). ----
    import importlib.metadata as md

    dep_lines = [
        f"# Stage C run-set {RS} dependency inventory",
        "# Pinned harness + measured runtime dependencies (dev scope).",
        "",
        "harness: proposal-v1.15 registry-v12 vectors-v1.15",
        f"registry_sha256: {manifest['harness']['registry_sha256']}",
        f"vectors_sha256: {manifest['harness']['vectors_sha256']}",
        f"jsonschema: {md.version('jsonschema')}",
        f"cryptography: {md.version('cryptography')}",
        f"temporalio: {md.version('temporalio')}",
        "database_image: pgvector/pgvector:pg16",
        f"database_server: {manifest['tenant_database']['server_version']}",
        "profile: config/profiles/tranche-b-offer.yaml version 1",
        "tenant: iap-probe-2026-10-24",
        "",
    ]
    (ART / "dependency.txt").write_bytes(("\n".join(dep_lines)).encode("utf-8"))

    # ---- Decision records (transcribed from surviving sources, labeled). ----
    a1_lines = A1.read_text(encoding="utf-8").splitlines()
    decisions_src: dict[str, dict[str, str]] = {
        "OD-01": {
            "decision": "DEC-2026-10-24-A1 (covers OD-01, OD-02, BUD-01)",
            "text": "\n".join(a1_lines[15:24]),
        },
        "OD-02": {
            "decision": "DEC-2026-10-24-A1 (covers OD-01, OD-02, BUD-01)",
            "text": "\n".join(a1_lines[27:32]),
        },
        "BUD-01": {
            "decision": "DEC-2026-10-24-A1 (covers OD-01, OD-02, BUD-01)",
            "text": "\n".join(a1_lines[35:47]),
        },
        "D-01": {
            "decision": "DEC-2026-10-24-D01",
            "text": "D-01 6-tuple denominator; 6-tuple T1-only denominator "
            "confirmed. (Transcribed from A1 approval record line 58 and "
            "ledger 2026-10-24 line 445; full §12.1 sheets unrecoverable.)",
        },
        "DIGEST-01": {
            "decision": "DEC-2026-10-24-DIGEST",
            "text": "v1.9 §2.1 envelope formula normative; implementation "
            "must conform with byte-identical goldens. (Transcribed from A1 "
            "approval record line 58 and ledger 2026-10-24 line 445.)",
        },
        "ID-01": {
            "decision": "DEC-2026-10-24-ID",
            "text": "Signed ownerAuthorization + wrapper environment "
            "manifest trust structure approved. (Transcribed from A1 "
            "approval record line 58 and ledger 2026-10-24 line 445.)",
        },
        "CAPTURE-01": {
            "decision": "DEC-2026-10-24-CAP",
            "text": "Frozen artifact bundle approved as Layer-1/resident-byte "
            "source. (Transcribed from A1 approval record line 58 and ledger "
            "2026-10-24 line 445.)",
        },
    }
    for did, src in decisions_src.items():
        rec = {
            "decision_id": did,
            "owner_decision": src["decision"],
            "date": "2026-10-24",
            "approver": "Owner",
            "text": src["text"],
        }
        (DEC / f"{did}.json").write_bytes(
            (json.dumps(rec, sort_keys=True, separators=(",", ":"),
                        ensure_ascii=False) + "\n").encode("utf-8")
        )

    # ---- Dev key list (dev scope; private key never enters the repo). ----
    priv, pub = generate_dev_keypair()
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        PrivateFormat,
        PublicFormat,
        NoEncryption,
    )

    pub_hex = pub.public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    key_id = "dev-stagec-2026-10-24-01"
    keylist = {
        "schema": "key-list/v1",
        "keys": [{
            "key_id": key_id,
            "algorithm": "Ed25519",
            "valid_from": utcnow(),
            "valid_until": None,
            "revoked": False,
            "key_reference": f"run-set {RS} dev key (public bytes in RUN_LOG.md; "
            "private held by operator outside the repo, never committed)",
        }],
    }
    (ART / "key_list.json").write_bytes(
        (json.dumps(keylist, sort_keys=True, separators=(",", ":"),
                    ensure_ascii=False) + "\n").encode("utf-8")
    )
    keydir = ROOT / "tmp" / "keys"
    keydir.mkdir(parents=True, exist_ok=True)
    keypath = keydir / f"{RS}.key"
    keypath.write_bytes(priv.private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    keypath.chmod(0o600)

    def digest_of(name: str) -> str:
        return sha256_digest((ART / name).read_bytes())

    def digest_bytes(data: bytes) -> str:
        return sha256_digest(data)

    # ---- Cohort (live fixtures only) + prompts. ----
    cohort = [{
        "fixture_id": fid,
        "fixture_version": digests[fid],
        "case_id": cid,
        "prompt_hash": form014,
    } for fid, cid, _ in PLAN]
    prompt_entries = [
        {"prompt_id": "v3-claim-grounded-014", "hash": form014},
        {"prompt_id": "v3-claim-grounded-015", "hash": form015},
    ]

    # ---- Freeze object. ----
    tree = subprocess.run(
        ["git", "archive", "HEAD", "--", "src", "tests", "config",
         "pyproject.toml"],
        capture_output=True, cwd=ROOT, check=True,
    ).stdout
    tree_hash = "sha256:" + hashlib.sha256(tree).hexdigest()
    freeze = {
        "schema": "freeze/v4",
        "run_set_id": RS,
        "protocol_hash": v19_hash,
        "code_revision": FREEZE_COMMIT,
        "tree_hash": tree_hash,  # sha256 over `git archive HEAD` (governed paths) bytes
        "flags": {"IAP_SHADOW_TYPED_DEFINITIONS": "1"},
        "authority_flag_state": "MUST_BE_ABSENT",
        "cohort": cohort,
        "prompts": prompt_entries,
        "comparison_hash": digest_of("comparison.txt"),
        "cell_budget": {"accept-PASS": 12, "accept-FAIL": 12,
                        "nonaccept-PASS": 12, "nonaccept-FAIL": 12},
        "budget_rule_hash": digest_of("budget.txt"),
        "control_hash": digest_of("control.txt"),
        "exclusion_hash": digest_of("exclusion.txt"),
        "capture_hash": digest_of("capture.txt"),
        "dependency_hash": digest_of("dependency.txt"),
        "harness_version": "stagec-harness proposal-v1.15 registry-v12 vectors-v1.15",
        "decisions": {did: digest_bytes((DEC / f"{did}.json").read_bytes())
                      for did in decisions_src},
        "comparison_preimage": {"path": f"artifacts/{RS}/comparison.txt",
                                "digest": digest_of("comparison.txt")},
        "budget_preimage": {"path": f"artifacts/{RS}/budget.txt",
                            "digest": digest_of("budget.txt")},
        "control_preimage": {"path": f"artifacts/{RS}/control.txt",
                             "digest": digest_of("control.txt")},
        "exclusion_preimage": {"path": f"artifacts/{RS}/exclusion.txt",
                               "digest": digest_of("exclusion.txt")},
        "capture_preimage": {"path": f"artifacts/{RS}/capture.txt",
                             "digest": digest_of("capture.txt")},
        "dependency_preimage": {"path": f"artifacts/{RS}/dependency.txt",
                                "digest": digest_of("dependency.txt")},
        "key_list_ref": {"path": f"artifacts/{RS}/key_list.json",
                         "digest": digest_of("key_list.json")},
        "total_reservations_max": 48,
        "freeze_hash": "sha256:" + "0" * 64,  # placeholder, replaced below
    }
    freeze["freeze_hash"] = freeze_digest(freeze)
    verify_freeze_self_hash(freeze)
    (ART / "freeze.json").write_bytes(
        (json.dumps(freeze, sort_keys=True, separators=(",", ":"),
                    ensure_ascii=False) + "\n").encode("utf-8")
    )

    # ---- Ledger + 6 reservations (one per live fixture). ----
    ledger = AttemptLedger(run_set_id=RS, freeze_hash=freeze["freeze_hash"])
    attempt_ids: dict[str, str] = {}
    for i, (fid, cid, cell) in enumerate(PLAN, start=1):
        rec = ledger.reserve(
            fixture_id=fid,
            fixture_version=digests[fid],
            case_id=cid,
            expected_cell=cell,
            budget_token=f"{RS}-s{i:02d}",
        )
        attempt_ids[fid] = rec["id"]
    ledger_state = {
        "run_set_id": ledger.run_set_id,
        "freeze_hash": ledger.freeze_hash,
        "bucket_counts": ledger.bucket_counts,
        "records": ledger.records,
        "expected_cells": ledger.expected_cells,
        "by_seq": ledger.by_seq,
        "total": ledger.total,
        "total_cap": ledger.total_cap,
        "head": ledger.head(),
    }
    (ART / "ledger.json").write_bytes(
        (json.dumps(ledger_state, sort_keys=True, separators=(",", ":"),
                    ensure_ascii=False) + "\n").encode("utf-8")
    )

    # ---- Run log. ----
    log = [
        f"# Stage C run set {RS} — collection log",
        "",
        f"Freeze record hash: {FREEZE_RECORD_HASH} (commit {FREEZE_COMMIT}).",
        f"Freeze object digest: {freeze['freeze_hash']}.",
        f"Ledger head: {ledger_state['head']} (total {ledger.total}/48).",
        "",
        "## Expected-cell mapping (a priori)",
        "",
        "L1-L5 expect ANSWERED terminals -> accept-PASS. "
        "L8 expects a clean ABSTAIN terminal -> nonaccept-PASS.",
        "Mispredictions record as EXCLUDED/BUDGET_RESERVED_CELL_MISMATCH, never reassigned.",
        "",
        "## Cohort prompt pin",
        "",
        "Cohort prompt_hash is the formulate-definition prompt "
        "v3-claim-grounded-014 (case-driving prompt); both prompts are frozen "
        "globally in freeze.prompts.",
        "",
        "## Shadow attempt ids (pre-computed, deterministic)",
        "",
    ]
    for fid, cid, cell in PLAN:
        log.append(f"{fid}: {attempt_ids[fid]} (bucket {cell})")
    log += [
        "",
        "## Control arm",
        "",
        "After the shadow arm: restart worker with "
        "IAP_SHADOW_TYPED_DEFINITIONS unset; one control run per exercised "
        "fixture; k-th shadow pairs with k-th control (control_order = "
        "per-fixture ordinal); stop on native divergence. Control runs are "
        "excluded from the eligible denominator and consume no budget.",
        "",
        "## Dev key",
        "",
        f"key_id {key_id}; public bytes (hex): {pub_hex}.",
        f"Private key at tmp/keys/{RS}.key (0600, gitignored, never committed).",
        "",
        "## Verify (do NOT rerun casually: key valid_from refreshes the freeze)",
        "",
        "sha256sum artifacts/<RS>/*.txt artifacts/<RS>/*.json  # compare to digests in freeze.json",
        "(ledger attempt ids are deterministic in run_set_id+seq+fixture+case.)",
        "",
    ]
    (ART / "RUN_LOG.md").write_text("\n".join(log), encoding="utf-8")

    print(f"run set {RS}: freeze {freeze['freeze_hash']}")
    print(f"ledger head {ledger_state['head']} total={ledger.total} "
          f"buckets={ledger_state['bucket_counts']}")
    print(f"artifacts in {ART}")


if __name__ == "__main__":
    main()
