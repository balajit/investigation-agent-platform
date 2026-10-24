"""Harness CLI: schema-gated recompute of report vectors."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from investigation_agent_platform.harness.cli import VECTORS_SHA256, main
from investigation_agent_platform.harness.registry import HarnessValidationError

DOCS = Path(__file__).resolve().parents[2] / "docs"
REGISTRY = str(DOCS / "IAP-proof-kernel-d3-stageC-harness-schemas-v1.13.json")
VECTORS = str(DOCS / "IAP-proof-kernel-d3-stageC-schema-vectors-v1.15.json")


def test_cli_check_case_accept(capsys) -> None:
    assert (
        main(
            [
                "check-case",
                "--registry",
                REGISTRY,
                "--vectors",
                VECTORS,
                "--case",
                "RC-valid-terminal",
            ]
        )
        == 0
    )
    assert "RC-valid-terminal" in capsys.readouterr().out


def test_cli_check_case_reject(capsys) -> None:
    assert (
        main(
            [
                "check-case",
                "--registry",
                REGISTRY,
                "--vectors",
                VECTORS,
                "--case",
                "RC-receipt-stray-mismatch",
            ]
        )
        == 0
    )
    assert "rejected as expected" in capsys.readouterr().out


def test_cli_check_case_rejects_receipt_chain_mismatch(capsys) -> None:
    assert (
        main(
            [
                "check-case",
                "--registry",
                REGISTRY,
                "--vectors",
                VECTORS,
                "--case",
                "RC-receipt-chain-mismatch",
            ]
        )
        == 0
    )
    assert "previous_digest does not match" in capsys.readouterr().out


def test_cli_check_case_unexpected_accept_fails() -> None:
    assert (
        main(
            [
                "check-case",
                "--registry",
                REGISTRY,
                "--vectors",
                VECTORS,
                "--case",
                "RC-receipt-stray-mismatch",
                "--expect",
                "accept",
            ]
        )
        == 1
    )


def test_cli_unknown_case_fails() -> None:
    assert (
        main(["check-case", "--registry", REGISTRY, "--vectors", VECTORS, "--case", "RC-nope"]) == 1
    )


def test_cli_vectors_digest_pin_matches_file() -> None:
    from investigation_agent_platform.harness.canonical import sha256_digest

    assert sha256_digest(Path(VECTORS).read_bytes()) == f"sha256:{VECTORS_SHA256}"


def test_cli_check_case_all_pinned_vectors(capsys) -> None:
    assert main(["check-case", "--registry", REGISTRY, "--vectors", VECTORS, "--all"]) == 0
    assert "44 passed, 0 failed" in capsys.readouterr().out


def test_cli_rejects_tampered_vectors(tmp_path) -> None:
    doc = json.loads(Path(VECTORS).read_bytes())
    doc["semantic_notes"].append({"id": "S-evil", "text": "x"})
    tampered = tmp_path / "vectors-tampered.json"
    tampered.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    with pytest.raises(HarnessValidationError, match="vectors digest mismatch"):
        main(["check-case", "--registry", REGISTRY, "--vectors", str(tampered), "--all"])
