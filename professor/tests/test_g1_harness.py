#!/usr/bin/env python3
"""Focused G1 regression tests for the Professor local qualification harness."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
import types


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class G1HarnessTests(unittest.TestCase):
    def test_policy_checks_are_independent(self) -> None:
        module = load_module("acceptance_tests", SCRIPTS / "acceptance_tests.py")
        try:
            target = module.ROOT / ".opencode" / "agents" / "professor.md"
            original = target.read_text(encoding="utf-8")
            target.write_text(original.replace("specialists disagree", "specialists differ"), encoding="utf-8")
            checks = module.failure_policy_checks()
            self.assertGreater(len({check.status for check in checks}), 1)
            self.assertTrue(any(check.status == "FAIL" for check in checks))
        finally:
            target.write_text(original, encoding="utf-8")

    def test_read_only_acceptance_does_not_write_report(self) -> None:
        report = ROOT / "workspace" / "deliverables" / "phase-1-validation-report.md"
        before = report.read_bytes()
        result = subprocess.run([sys.executable, str(SCRIPTS / "acceptance_tests.py")], cwd=ROOT, check=False, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(before, report.read_bytes())
        self.assertIn("READ_ONLY", result.stdout)

    def test_existing_report_refuses_explicit_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.md"
            report.write_text("sentinel\n", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(SCRIPTS / "acceptance_tests.py"), "--write-report", "--report-path", str(report)],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(report.read_text(encoding="utf-8"), "sentinel\n")

    def test_schema_validator_failure_propagates(self) -> None:
        result = subprocess.run([sys.executable, str(SCRIPTS / "validate_schemas.py")], cwd=ROOT, check=False, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("Schema checks: 28 passed, 0 failed.", result.stdout)

    def test_phase2_read_only_fixture_does_not_write_report(self) -> None:
        report = ROOT / "workspace" / "deliverables" / "phase-2-local-qualification.json"
        before = report.read_bytes()
        result = subprocess.run([sys.executable, str(SCRIPTS / "phase2_qualification.py")], cwd=ROOT, check=False, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(before, report.read_bytes())
        self.assertIn("READ_ONLY", result.stdout)


if __name__ == "__main__":
    unittest.main()
