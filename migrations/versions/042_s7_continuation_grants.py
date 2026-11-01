"""Alembic migration: grants for S7 continuation tables (Review Stage C1+C2).

The 041 tables were created without role grants, so only the owner (the
migrator connection) can touch them — the worker's concluding-path UPDATE
on s7_continuations fails with InsufficientPrivilegeError on a least-
privilege dev stack. Grant least-privilege DML to the app/worker/test
roles; RLS policies (tenant_isolation, FORCE) already confine every
granted role to its own tenant. Downgrade revokes.
"""

revision = "042_s7_continuation_grants"
down_revision = "041_s7_continuations"
branch_labels = None
depends_on = None

import sqlalchemy as sa  # type: ignore[import-not-found]
from alembic import op  # type: ignore[import-not-found]

_TABLES = (
    "s7_continuations",
    "conclusion_history",
    "continuation_reasoning_leases",
)
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
