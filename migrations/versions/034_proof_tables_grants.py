"""Alembic migration: grants for proof tables (Review Stage C1+C2).

The 033 tables were created without role grants, so only the owner (the
dev superuser connection) can touch them. Grant least-privilege
DML to the app/worker/test roles; SELECT-only stays out of scope until a
read path needs it. RLS policies (tenant_isolation, FORCE) already
confine every granted role to its own tenant. Downgrade revokes.
"""

# revision identifiers, used by Alembic.
revision = "034_proof_tables_grants"
down_revision = "033_proof_publication_commit"
branch_labels = None
depends_on = None

import sqlalchemy as sa  # type: ignore[import-not-found]
from alembic import op  # type: ignore[import-not-found]

_TABLES = ("proof_derivations", "proof_publication_commits")
_ROLES = ("iap_app", "iap_worker", "iap_test1")


def _role_exists(role: str) -> bool:
    bind = op.get_bind()
    try:
        return bool(
            bind.execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).scalar()
        )
    except Exception:
        return False


def upgrade() -> None:
    for table in _TABLES:
        for role in _ROLES:
            if _role_exists(role):
                op.execute(sa.text(f"GRANT SELECT, INSERT, UPDATE ON {table} TO {role}"))


def downgrade() -> None:
    for table in _TABLES:
        for role in _ROLES:
            if _role_exists(role):
                op.execute(sa.text(f"REVOKE ALL ON {table} FROM {role}"))
