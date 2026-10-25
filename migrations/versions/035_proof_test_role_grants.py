"""Alembic migration: core-table DML for the least-privilege test role.

Extends the 034 pattern (which covered only the proof tables) to the core
tables the proof-publication live suite touches: the suite seeds fixtures
(INSERT into investigations/hypotheses/evidence) and cleans its scratch
tenant (DELETE across all eight tables) through `rls_session`. Without
these grants the suite cannot run as `iap_test1`, and the G5 exit criterion
requiring a least-privilege run is unclosable.

Scope is deliberately narrow: one existing test role (`iap_test1`, no new
roles), eight named tables, DML only (SELECT/INSERT/UPDATE/DELETE — DELETE
is required by the fixture cleanup; no DDL, no sequence ownership changes,
all PKs are client-supplied UUIDs). RLS policies (ENABLE + FORCE) already
confine every granted role to its own tenant; this migration changes no
policy. Dev-stack only, like 033/034. Downgrade revokes.
"""

# revision identifiers, used by Alembic.
revision = "035_proof_test_role_grants"
down_revision = "034_proof_tables_grants"
branch_labels = None
depends_on = None

import sqlalchemy as sa  # type: ignore[import-not-found]
from alembic import op  # type: ignore[import-not-found]

_TABLES = (
    "investigations",
    "hypotheses",
    "evidence",
    "investigation_contradictions",
    "findings",
    "investigation_conclusions",
    "proof_derivations",
    "proof_publication_commits",
)
_ROLES = ("iap_test1",)
_PRIVS = "SELECT, INSERT, UPDATE, DELETE"


def _role_exists(role: str) -> bool:
    bind = op.get_bind()
    try:
        return bool(
            bind.execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).scalar()
        )
    except Exception:
        return False


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    try:
        return sa.inspect(bind).has_table(table)
    except Exception:
        return False


def upgrade() -> None:
    for table in _TABLES:
        if not _table_exists(table):
            continue
        for role in _ROLES:
            if _role_exists(role):
                op.execute(sa.text(f"GRANT {_PRIVS} ON {table} TO {role}"))


def downgrade() -> None:
    for table in _TABLES:
        for role in _ROLES:
            if _role_exists(role):
                op.execute(sa.text(f"REVOKE ALL ON {table} FROM {role}"))
