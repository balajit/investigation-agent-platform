# migrations/versions/028_onboarding_request_cancel.py
"""Grant the worker role narrow request-state updates for cancellation, plus
app INSERT on background jobs for acceptance.

Phase 2 reconciliation completes cancellations (QUEUED/CANCEL_REQUESTED to
CANCELED) and therefore needs UPDATE on the lifecycle columns. The grant is
column-limited to (state, state_version, updated_at); row reads and tenant
scoping continue under the existing tenant_isolation policy, which already
covers iap_worker.

The acceptance transaction must also create the job row its dispatch FK
references, and no existing grant permits any runtime role to INSERT into
background_jobs (verified against the full grant matrix; only the table
owner could). GRANT INSERT TO iap_app closes that gap; the existing
tenant-scoped RLS policy constrains the writes. No other Phase 2 path
needed new privileges.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "028_onboarding_request_cancel"
down_revision = "027_onboarding_request_accept"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "GRANT UPDATE (state, state_version, updated_at) ON onboarding_requests TO iap_worker"
        )
    )
    op.execute(sa.text("GRANT INSERT ON background_jobs TO iap_app"))


def downgrade() -> None:
    # Preserve the reviewed grant; disable the reconciler path by application
    # control rather than revoking a reviewed privilege mid-flight.
    return None
