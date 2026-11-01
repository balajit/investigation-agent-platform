#!/usr/bin/env python3
"""Run deterministic local acceptance checks.

Default execution is read-only. Durable report generation requires the explicit
``--write-report`` flag and refuses to overwrite an existing report.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_AGENTS = {
    "professor": "primary",
    "prompt-gen": "subagent",
    "research-assistant": "subagent",
    "code-analyst": "subagent",
    "architecture-agent": "subagent",
    "adversarial-reviewer": "subagent",
    "validation-agent": "subagent",
}
OBSERVED_RUNTIME_SMOKE = {
    "research workflow delegation": ("PASS", "Professor delegated task RA-001 to Research Assistant using github-copilot/gpt-5.6-luna"),
    "webfetch availability": ("PASS", "Research Assistant retrieved https://opencode.ai/docs/agents/"),
    "websearch availability": ("NOT_VERIFIED", "websearch was not exposed in the observed runtime"),
}


@dataclass
class Check:
    name: str
    status: str
    detail: str


def check_workflow_contract(path: Path, required: tuple[str, ...]) -> Check:
    content = path.read_text(encoding="utf-8")
    missing = [heading for heading in required if heading not in content]
    if missing:
        return Check(path.name, "FAIL", "missing required sections: " + ", ".join(missing))
    return Check(path.name, "PASS", "required stages and acceptance contract are present")


def discovery_check() -> Check:
    executable = shutil.which("opencode")
    if not executable:
        return Check("agent discovery", "NOT_VERIFIED", "opencode CLI is unavailable")
    try:
        result = subprocess.run(
            [executable, "agent", "list", "--pure"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return Check("agent discovery", "NOT_VERIFIED", str(error))
    discovered = dict(re.findall(r"^(\S+) \((primary|subagent|all)\)$", result.stdout + "\n" + result.stderr, re.MULTILINE))
    mismatches = [
        f"{name}={discovered.get(name, 'missing')}"
        for name, mode in EXPECTED_AGENTS.items()
        if discovered.get(name) != mode
    ]
    if result.returncode or mismatches:
        return Check("agent discovery", "FAIL", "; ".join(mismatches) or "agent list failed")
    return Check("agent discovery", "PASS", "all seven project agents discovered with expected modes")


def failure_policy_checks() -> list[Check]:
    cases = {
        "no evidence": ("PARTIAL", "report limitation", ("essential evidence", "limitation")),
        "incomplete findings": ("PARTIAL", "preserve missing acceptance criteria", ("incomplete output", "acceptance")),
        "specialist disagreement": ("PARTIAL", "preserve disagreement for adjudication", ("specialists disagree", "conflict")),
        "adversarial rejection": ("PARTIAL", "record disposition and re-evaluate", ("adversarial reviewer", "challenge")),
        "validation gap": ("PARTIAL", "do not deliver as fully validated", ("validation cannot be performed", "acceptance")),
        "unavailable search": ("BLOCKED", "explain missing capability", ("unavailable", "capability")),
        "blocked specialist": ("BLOCKED", "do not convert to success", ("blocked actions", "failed")),
        "malformed output": ("FAILED", "reject malformed output", ("unsupported findings", "findings")),
    }
    content = (
        (ROOT / ".opencode" / "agents" / "professor.md").read_text(encoding="utf-8")
        + "\n"
        + (ROOT / ".opencode" / "workflows" / "research.md").read_text(encoding="utf-8")
    ).lower()
    return [
        Check(
            "static policy check: " + name,
            "PASS" if all(term in content for term in terms) else "FAIL",
            f"expected {status}; {behavior}; independent terms={terms}",
        )
        for name, (status, behavior, terms) in cases.items()
    ]


def result_contract_checks() -> list[Check]:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "validate_schemas.py")],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    detail = "actual validate_schemas.py executed; " + (result.stdout.strip().splitlines()[-1] if result.stdout else result.stderr.strip())
    return [Check("local result contract: schema fixtures", "PASS" if result.returncode == 0 else "FAIL", detail)]


def render_report(checks: list[Check], runtime_checks: list[tuple[str, str, str]]) -> str:
    failures = [result for result in checks if result.status == "FAIL"]
    lines = [
        "# Phase 1 Validation Report",
        "",
        f"**Validation date:** {date.today().isoformat()}",
        "**Scope:** deterministic local contract, discovery, independent static policy checks, and recorded bounded runtime smoke evidence.",
        "",
        "## Local Checks",
        "",
        "| Check | Status | Evidence |",
        "|---|---|---|",
    ]
    lines.extend(f"| {result.name} | {result.status} | {result.detail} |" for result in checks)
    lines.extend(["", "## Runtime Checks", "", "| Check | Status | Limitation |", "|---|---|---|"])
    lines.extend(f"| {name} | {status} | {detail} |" for name, status, detail in runtime_checks)
    lines.extend([
        "",
        "## Interpretation",
        "",
        "Local PASS results establish workflow structure, independent static policy checks, and actual local schema-fixture execution. They do not establish provider-backed workflow behavior or live failure injection.",
        "",
        "## Overall Status",
        "",
        "**PASS WITH GAPS**" if not failures else "**FAIL**",
        "",
        "Research delegation and known-URL webfetch are verified only for the recorded bounded smoke test. Research and Design, Code Review, live fault injection, and websearch remain `NOT_VERIFIED`.",
    ])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run read-only Professor acceptance checks.")
    parser.add_argument("--write-report", action="store_true", help="explicitly write the Phase 1 validation report")
    parser.add_argument("--report-path", type=Path, default=ROOT / "workspace" / "deliverables" / "phase-1-validation-report.md")
    args = parser.parse_args()

    checks = [
        check_workflow_contract(ROOT / ".opencode" / "workflows" / "research.md", ("Runtime Capability Gate", "Gather Evidence", "Acceptance Criteria")),
        check_workflow_contract(ROOT / ".opencode" / "workflows" / "research-and-design.md", ("Requirements-to-Design Traceability", "Develop Candidate Designs", "Acceptance Criteria")),
        check_workflow_contract(ROOT / ".opencode" / "workflows" / "code-review.md", ("Establish Review Scope", "Finding Contract and Severity", "Independent Adversarial Review", "Independent Validation", "Acceptance Criteria")),
        discovery_check(),
    ]
    checks.extend(failure_policy_checks())
    checks.extend(result_contract_checks())
    runtime_checks = [
        ("research workflow delegation", *OBSERVED_RUNTIME_SMOKE["research workflow delegation"]),
        ("research-and-design workflow delegation", "NOT_VERIFIED", "no provider-backed OpenCode session was executed by this local harness"),
        ("code-review workflow delegation", "NOT_VERIFIED", "no provider-backed OpenCode session was executed by this local harness"),
        ("websearch availability", *OBSERVED_RUNTIME_SMOKE["websearch availability"]),
        ("webfetch availability", *OBSERVED_RUNTIME_SMOKE["webfetch availability"]),
    ]
    for result in checks:
        print(f"{result.status}: {result.name} - {result.detail}")
    for name, status, detail in runtime_checks:
        print(f"{status}: {name} - {detail}")

    failures = [result for result in checks if result.status == "FAIL"]
    if args.write_report:
        if args.report_path.exists():
            print(f"FAIL: report already exists; refusing overwrite: {args.report_path}")
            return 2
        args.report_path.parent.mkdir(parents=True, exist_ok=True)
        args.report_path.write_text(render_report(checks, runtime_checks), encoding="utf-8")
        print(f"WROTE: {args.report_path}")
    else:
        print("READ_ONLY: no durable report written; use --write-report for explicit report generation")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
