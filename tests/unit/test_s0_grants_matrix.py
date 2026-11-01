"""S0: provision script matches the least-privilege matrix (static, no DB).

Parses scripts/provision-onboarding-db-roles.sql and pins:
- every matrix (table, role) has exactly the required verbs;
- no TRUNCATE/REFERENCES/ALL/DDL grants to runtime roles;
- no PUBLIC grants on core tables; no passwords in the script;
- runtime roles carry NOBYPASSRLS/NOSUPERUSER.
"""

from __future__ import annotations

import re
from pathlib import Path

# table -> {role: verbs}; mirrors docs/IAP-TENANT-ONBOARDING-DB-ROLES.md §S0.
_RW = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"})
_UPD = frozenset({"SELECT", "INSERT", "UPDATE"})
_INS = frozenset({"SELECT", "INSERT"})

MATRIX: dict[str, dict[str, frozenset[str]]] = {
    t: {"iap_app": _RW, "iap_worker": _RW}
    for t in [
        "investigations", "evidence", "timeline_events", "investigation_conclusions",
        "hypotheses", "idempotency_keys", "background_jobs", "batch_intake_records",
        "reference_document_chunks", "reference_index_generations", "proof_derivations",
        "proof_publication_commits", "input_requirements", "quota_counters",
        "knowledge_artifacts",
    ]
}
MATRIX.update(
    {t: {"iap_app": _UPD, "iap_worker": _UPD} for t in [
        "investigation_contradictions", "finding_clusters", "finding_cluster_assignments",
        "finding_embeddings", "outbox_events",
    ]}
)
MATRIX.update(
    {t: {"iap_app": _INS, "iap_worker": _INS} for t in [
        "investigation_facts", "investigation_checkpoints", "investigation_transitions",
        "evidence_relationships", "findings", "action_executions", "input_fulfillments",
        "investigation_sessions", "code_issue_index", "log_exemplars",
        "evidence_bank_interest",
    ]}
)
MATRIX["application_profiles"] = {"iap_app": frozenset({"SELECT", "INSERT", "DELETE"}), "iap_worker": frozenset({"SELECT"})}
MATRIX["chat_sessions"] = {"iap_app": frozenset({"SELECT", "INSERT", "DELETE"})}
MATRIX["chat_messages"] = {"iap_app": frozenset({"SELECT", "INSERT", "DELETE"})}

# Tables whose grants predate S0 (migration 024 section + registry admin).
_ONBOARDING_OK = {
    "tenant_registry", "onboarding_requests", "onboarding_dispatches",
    "onboarding_grants", "onboarding_grant_audits", "onboarding_dispatch_audits",
    "onboarding_support_registry", "onboarding_coverage_reports",
}


def _script() -> str:
    return (Path(__file__).resolve().parents[2] / "scripts" / "provision-onboarding-db-roles.sql").read_text()


def _grants(sql: str) -> dict[tuple[str, str], set[str]]:
    flat = re.sub(r"--[^\n]*", "", sql)
    flat = re.sub(r"\s+", " ", flat)
    out: dict[tuple[str, str], set[str]] = {}
    for m in re.finditer(r"GRANT\s+(.+?)\s+ON\s+(.+?)\s+TO\s+(.+?)(?:;|$)", flat, re.IGNORECASE):
        verbs = {v.strip().upper() for v in m.group(1).split(",")}
        targets = [t.strip().lower().split(".")[-1] for t in m.group(2).split(",")]
        roles = [r.strip().lower() for r in m.group(3).split(",")]
        if "sequences" in targets or "schema" in m.group(2).lower():
            continue
        for table in targets:
            if not re.fullmatch(r"[a-z_]+", table):
                continue
            for role in roles:
                out.setdefault((table, role), set()).update(verbs)
    return out


def test_s0_t01_matrix_covered_exactly() -> None:
    grants = _grants(_script())
    for table, roles in MATRIX.items():
        for role, verbs in roles.items():
            got = grants.get((table, role), set())
            assert got == set(verbs), f"{table}/{role}: script={sorted(got)} matrix={sorted(verbs)}"


def test_s0_t02_no_extra_runtime_table_grants() -> None:
    grants = _grants(_script())
    for (table, role) in grants:
        if role not in ("iap_app", "iap_worker"):
            continue
        assert table in MATRIX or table in _ONBOARDING_OK, f"unexpected grant: {table} to {role}"


def test_s0_t03_no_dangerous_verbs_for_runtime_roles() -> None:
    grants = _grants(_script())
    for (table, role), verbs in grants.items():
        if role in ("iap_app", "iap_worker", "iap_registry_admin"):
            assert verbs.isdisjoint({"TRUNCATE", "REFERENCES", "ALL", "CREATE", "DROP", "ALTER"}), (
                table, role, sorted(verbs))


def test_s0_t04_no_public_no_passwords_safe_roles() -> None:
    sql = _script()
    assert "PASSWORD" not in sql.upper() or "must assign login secrets" in sql
    assert not re.search(r"(?i)create\s+role\s+\w+[^;]*\bpassword\b", sql)
    assert "BYPASSRLS" not in sql.replace("NOBYPASSRLS", "")
    assert "SUPERUSER" not in sql.replace("NOSUPERUSER", "")
    flat = re.sub(r"--[^\n]*", "", sql)
    assert not re.search(r"(?i)GRANT\s+.+\s+ON\s+.+\s+TO\s+PUBLIC", flat.replace("REVOKE ALL ON TABLE", "REVOKE"))
