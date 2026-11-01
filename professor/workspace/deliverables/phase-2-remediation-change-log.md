# Phase 2 Remediation Change Log

## G1-001

- **Finding/work package:** P2-001 / WP2-R1
- **Files:** `scripts/acceptance_tests.py`, `tests/test_g1_harness.py`
- **Previous behavior:** Result-contract checks returned unconditional PASS values.
- **New behavior:** The actual schema validator runs and its result/exit code is reported.
- **Rationale:** Local PASS must represent executed validation, not a hard-coded assertion.
- **Tests:** `test_schema_validator_failure_propagates`; acceptance harness execution.
- **Evidence:** `phase-2-harness-remediation.md`.
- **Regression risk:** Low; only local qualification reporting changed.

## G1-002

- **Finding/work package:** P2-002 / WP2-R1
- **Files:** `scripts/acceptance_tests.py`, `scripts/phase2_qualification.py`, `tests/test_g1_harness.py`
- **Previous behavior:** Validation commands overwrote durable reports.
- **New behavior:** Default commands are read-only; explicit report generation refuses existing paths.
- **Rationale:** Preserve historical qualification evidence and prevent silent data loss.
- **Tests:** `test_read_only_acceptance_does_not_write_report`; `test_existing_report_refuses_explicit_overwrite`; `test_phase2_read_only_fixture_does_not_write_report`.
- **Evidence:** `phase-2-harness-remediation.md`.
- **Regression risk:** Low; callers must opt into report generation.

## G1-003

- **Finding/work package:** P2-003 / WP2-R1
- **Files:** `scripts/acceptance_tests.py`, `tests/test_g1_harness.py`
- **Previous behavior:** All eight policy checks shared one predicate.
- **New behavior:** Each policy check evaluates independent terms and retains static-policy classification.
- **Rationale:** A missing scenario-specific instruction must fail only the affected check, not be hidden by a shared boolean.
- **Tests:** `test_policy_checks_are_independent`; acceptance harness execution.
- **Evidence:** `phase-2-harness-remediation.md`.
- **Regression risk:** Low; static checks may expose previously hidden policy omissions.

## Scope Boundary

G1 did not modify agents, workflows, schemas, provider configuration, application code, external systems, or qualification criteria. G2 and later gates remain unstarted.
