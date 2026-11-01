# Phase 2 G1 Harness Remediation

**Gate:** G1 / WP2-R1  
**Status:** `PASS WITH GAPS` for local harness trustworthiness; live qualification remains `NOT_VERIFIED`.  
**Scope:** P2-001, P2-002, and P2-003 only. G2 and later gates were not started.

## Remediated Findings

### P2-001 — Misleading acceptance harness results

**Previous behavior:** `result_contract_checks()` returned four unconditional PASS results without loading or executing the actual schema validator.

**New behavior:** The acceptance harness invokes `scripts/validate_schemas.py`, reports its actual terminal summary, and propagates a nonzero validator exit code as a failed local check.

**Evidence:** `scripts/acceptance_tests.py`; `scripts/validate_schemas.py`; `tests/test_g1_harness.py::test_schema_validator_failure_propagates`.

### P2-002 — Silent durable-report overwrite

**Previous behavior:** Read-only acceptance execution unconditionally wrote `workspace/deliverables/phase-1-validation-report.md`.

**New behavior:** Acceptance and Phase 2 qualification commands are read-only by default. Durable report output requires explicit `--write-report`; existing destinations cause exit code `2` and are never overwritten.

**Evidence:** `scripts/acceptance_tests.py`; `scripts/phase2_qualification.py`; `tests/test_g1_harness.py::test_read_only_acceptance_does_not_write_report`; `tests/test_g1_harness.py::test_existing_report_refuses_explicit_overwrite`; `tests/test_g1_harness.py::test_phase2_read_only_fixture_does_not_write_report`.

### P2-003 — Shared failure-policy predicate

**Previous behavior:** Eight named policy checks shared one `policy_present` boolean based on three phrases.

**New behavior:** Each scenario has an independent set of policy terms and produces its own result. The checks remain static policy checks and are not represented as live behavior qualification.

**Evidence:** `scripts/acceptance_tests.py::failure_policy_checks`; `tests/test_g1_harness.py::test_policy_checks_are_independent`.

## Commands And Semantics

| Command | Behavior |
|---|---|
| `python3 scripts/acceptance_tests.py` | Read-only local validation; no durable report write. |
| `python3 scripts/acceptance_tests.py --write-report --report-path <new-path>` | Explicit report generation; refuses an existing path. |
| `python3 scripts/phase2_qualification.py` | Read-only procedural Phase 2 fixture/schema checks; no durable report write. |
| `python3 scripts/phase2_qualification.py --write-report --report-path <new-path>` | Explicit JSON report generation; refuses an existing path. |
| `python3 scripts/validate_schemas.py` | Actual local JSON Schema fixture validation with exit-code propagation. |

## Verification

- Focused G1 tests: **5 passed**.
- Acceptance harness: local checks pass; runtime-only checks remain `NOT_VERIFIED`.
- Phase 2 local qualification: **9 passed**, explicitly procedural/local.
- Python syntax compilation: passed.
- Existing durable reports were not overwritten.
- No agent, workflow, schema, or provider runtime configuration was modified.

## Remaining Limitations

- These changes do not create live Professor failure injection.
- These changes do not prove delegation-boundary contract enforcement.
- The eight failure scenarios remain provider-independent local fixtures until G6.
- Research and Design, Code Review runtime qualification, manifests, and independent final qualification remain later gates.

## G1 Decision

**`LOCAL_QUALIFICATION_TRUSTWORTHY; G2 NOT_STARTED`**

P2-001, P2-002, and P2-003 are remediated within the authorized G1 scope. The harness now accurately distinguishes local checks from live qualification and does not silently overwrite durable reports.
