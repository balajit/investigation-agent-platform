#!/usr/bin/env python3
"""Validate Professor task/result JSON Schema contracts with representative cases."""

from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    from jsonschema import Draft202012Validator
except ImportError:
    print("BLOCKED: install the documented test dependency with python3 -m pip install -r requirements-test.txt", file=sys.stderr)
    raise SystemExit(2)


ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / ".opencode" / "schemas"


def load_schema(name: str) -> dict:
    with (SCHEMAS / name).open(encoding="utf-8") as source:
        schema = json.load(source)
    Draft202012Validator.check_schema(schema)
    return schema


def base_task() -> dict:
    return {
        "status": "READY",
        "task_id": "T-001",
        "task_type": "RESEARCH",
        "objective": "Answer a bounded research question.",
        "missing_context": [],
        "context_and_inputs": [],
        "scope": ["Official documentation"],
        "exclusions": ["Implementation"],
        "questions": ["What is the documented behavior?"],
        "execution_steps": ["Inspect the primary source."],
        "evidence_requirements": ["Record source URL and retrieval date."],
        "deliverable_contract": {"format": "Markdown", "required_sections": ["Findings"]},
        "acceptance_criteria": ["Material claims have traceable sources."],
        "safety_constraints": ["Read-only."],
        "assumptions": [],
    }


def base_result(result_type: str = "GENERAL") -> dict:
    return {
        "task_id": "T-001",
        "agent": "research-assistant",
        "result_type": result_type,
        "status": "COMPLETED",
        "summary": "Completed the assigned work.",
        "findings": [],
        "evidence": [],
        "limitations": [],
        "acceptance_criteria_check": [
            {"criterion": "Evidence is traceable.", "status": "MET", "rationale": "Source recorded."}
        ],
        "recommended_next_steps": [],
    }


def valid_finding(**overrides: object) -> dict:
    finding = {
        "id": "F-001",
        "claim": "A supported finding.",
        "classification": "OBSERVED_FACT",
        "confidence": "HIGH",
        "rationale": "Direct evidence was inspected.",
    }
    finding.update(overrides)
    return finding


def main() -> int:
    task_schema = load_schema("task-package.schema.json")
    result_schema = load_schema("agent-result.schema.json")
    checks: list[tuple[str, dict, dict, bool]] = []

    checks.append(("valid READY task package", task_schema, base_task(), True))
    needs_context = base_task()
    needs_context.update(status="NEEDS_CONTEXT", missing_context=["Target platform is unknown."], questions=["Which platform?"])
    checks.append(("valid NEEDS_CONTEXT task", task_schema, needs_context, True))
    invalid_task = base_task()
    invalid_task["unexpected"] = True
    checks.append(("reject unexpected task property", task_schema, invalid_task, False))
    missing_question = dict(needs_context, questions=[])
    checks.append(("reject NEEDS_CONTEXT without questions", task_schema, missing_question, False))
    missing_context = dict(needs_context, missing_context=[])
    checks.append(("reject NEEDS_CONTEXT without missing-context detail", task_schema, missing_context, False))
    false_ready = dict(base_task(), missing_context=["A blocking input is unknown."])
    checks.append(("reject READY task with blocking missing context", task_schema, false_ready, False))
    empty_ready_scope = dict(base_task(), scope=[])
    checks.append(("reject READY task without scope", task_schema, empty_ready_scope, False))
    empty_ready_evidence = dict(base_task(), evidence_requirements=[])
    checks.append(("reject READY task without evidence requirements", task_schema, empty_ready_evidence, False))
    empty_sections = dict(base_task(), deliverable_contract={"format": "Markdown", "required_sections": []})
    checks.append(("reject task without deliverable sections", task_schema, empty_sections, False))
    checks.append(("reject missing task objective", task_schema, {key: value for key, value in base_task().items() if key != "objective"}, False))

    checks.append(("valid generic result", result_schema, base_result(), True))
    blocked = base_result()
    blocked.update(status="BLOCKED", limitations=["Required tool is unavailable."])
    checks.append(("valid blocked result", result_schema, blocked, True))
    code = base_result("CODE_REVIEW")
    code["findings"] = [valid_finding(
        finding_id="CR-001", severity="HIGH", category="SECURITY", package_module="auth",
        file="src/auth.py", line_start=10, line_end=14, reviewed_revision="abc123", evidence_references=["E-001"],
        observed_behavior="Authorization is skipped.", expected_behavior="Protected operations require authorization.",
        trigger_conditions="An unauthenticated caller requests the protected route.",
        failure_mechanism="The route reaches the handler without a permission check.", root_cause="Missing authorization guard.",
        impact="Unauthorized access to protected data.", remediation="Enforce the authorization policy.",
        regression_test="Assert unauthenticated requests are denied.", limitations=[])]
    checks.append(("valid code-review finding", result_schema, code, True))
    code_without_detail = base_result("CODE_REVIEW")
    code_without_detail["findings"] = [valid_finding()]
    checks.append(("reject generic finding in code-review result", result_schema, code_without_detail, False))
    research = base_result("RESEARCH")
    research["findings"] = [valid_finding(
        research_question="What does the official specification state?", source_references=["E-001"],
        source_quality="PRIMARY", publication_date="2026-01-01", retrieval_date="2026-11-01",
        contradictions=[], limitations=[], evidence_references=["E-001"])]
    checks.append(("valid research finding", result_schema, research, True))
    research_without_detail = base_result("RESEARCH")
    research_without_detail["findings"] = [valid_finding()]
    checks.append(("reject generic finding in research result", result_schema, research_without_detail, False))
    architecture = base_result("ARCHITECTURE")
    architecture["findings"] = [valid_finding(
        design_decision="Use a bounded session-owned coordinator.", requirement_references=["REQ-1"],
        component_responsibilities=["Coordinate assigned tasks."], interface_definitions=["Task input and result output."],
        alternatives=["Manual coordination."], trade_offs=["Lower infrastructure cost; session state is not durable."],
        failure_behavior="Report partial progress when a task is blocked.", security_considerations=["Treat retrieved data as untrusted."],
        open_decisions=[])]
    checks.append(("valid architecture finding", result_schema, architecture, True))
    architecture_without_detail = base_result("ARCHITECTURE")
    architecture_without_detail["findings"] = [valid_finding()]
    checks.append(("reject generic finding in architecture result", result_schema, architecture_without_detail, False))
    adversarial = base_result("ADVERSARIAL_REVIEW")
    adversarial["findings"] = [valid_finding(
         review_disposition="NEEDS_MORE_EVIDENCE", counterarguments=["The observed behavior may be configuration-dependent."],
         evidence_references=["E-001"], limitations=["Runtime configuration was not inspected."], finding_id="AR-001",
         severity="MEDIUM", category="RELIABILITY", package_module="auth", file="src/auth.py",
        reviewed_revision="abc123", observed_behavior="The policy is not checked on one path.",
        expected_behavior="Protected operations enforce policy.", trigger_conditions="The affected route is invoked.",
        failure_mechanism="The route reaches the handler without checking policy.", root_cause="Missing policy guard.",
         impact="A protected operation may be reached.", remediation="Add the policy guard.",
         regression_test="Assert the affected route is denied.")]
    checks.append(("valid adversarial finding", result_schema, adversarial, True))
    adversarial_without_remediation = json.loads(json.dumps(adversarial))
    del adversarial_without_remediation["findings"][0]["remediation"]
    checks.append(("reject adversarial finding without remediation", result_schema, adversarial_without_remediation, False))
    adversarial_without_detail = base_result("ADVERSARIAL_REVIEW")
    adversarial_without_detail["findings"] = [valid_finding()]
    checks.append(("reject generic finding in adversarial result", result_schema, adversarial_without_detail, False))
    validation = base_result("VALIDATION")
    validation["findings"] = [valid_finding(
        requirement="Every required section is present.", validation_method="Check the report headings.",
        observed_result="All listed headings were found.", validation_status="MET", evidence=["The headings were checked directly."],
        evidence_references=["The headings were checked directly."], defects_identified=[], limitations=[])]
    checks.append(("valid validation finding", result_schema, validation, True))
    validation_without_detail = base_result("VALIDATION")
    validation_without_detail["findings"] = [valid_finding()]
    checks.append(("reject generic finding in validation result", result_schema, validation_without_detail, False))

    invalid_severity = json.loads(json.dumps(code))
    invalid_severity["findings"][0]["severity"] = "URGENT"
    checks.append(("reject invalid severity", result_schema, invalid_severity, False))
    missing_finding_field = json.loads(json.dumps(code))
    del missing_finding_field["findings"][0]["root_cause"]
    checks.append(("reject missing code-review root cause", result_schema, missing_finding_field, False))
    extra_result = dict(base_result(), extra=True)
    checks.append(("reject unexpected result property", result_schema, extra_result, False))
    invalid_acceptance = base_result()
    invalid_acceptance["acceptance_criteria_check"][0]["status"] = "DONE"
    checks.append(("reject invalid acceptance status", result_schema, invalid_acceptance, False))

    reversed_range = json.loads(json.dumps(code))
    reversed_range["findings"][0]["line_start"] = 20
    reversed_range["findings"][0]["line_end"] = 10
    checks.append(("reject reversed code-review line range", result_schema, reversed_range, False))

    failures = 0
    for label, schema, instance, expected_valid in checks:
        validator = Draft202012Validator(schema)
        errors = list(validator.iter_errors(instance))
        is_valid = not errors
        if is_valid and instance.get("result_type") == "CODE_REVIEW":
            for finding in instance.get("findings", []):
                if finding.get("line_end", finding.get("line_start", 0)) < finding.get("line_start", 0):
                    errors.append("line_end must be greater than or equal to line_start")
        is_valid = not errors
        passed = is_valid is expected_valid
        print(("PASS" if passed else "FAIL") + ": " + label)
        if not passed:
            for error in errors:
                print("  - " + "/".join(str(part) for part in error.absolute_path) + ": " + error.message)
        failures += not passed
    print(f"Schema checks: {len(checks) - failures} passed, {failures} failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
