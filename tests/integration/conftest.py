"""Guard opt-in onboarding PostgreSQL tests against non-disposable databases.

Module-level opt-in (`IAP_RUN_ONBOARDING_DB_TESTS=1`) plus an inherited or
default `IAP_TEST_PG_URL` once pointed a migration stamp at the persistent
dev database. This autouse fixture fails those tests loudly on a protected
or remote URL instead of running against it. Unreachable disposable URLs
keep the existing per-file skip behavior.
"""

from __future__ import annotations

import os

import pytest

from investigation_agent_platform.infrastructure.persistence.pg_guard import (
    require_disposable,
)

_OPT_IN_ENV = "IAP_RUN_ONBOARDING_DB_TESTS"
_URL_ENV = "IAP_TEST_PG_URL"
_DEFAULT_URL = "postgresql://iap:iap@localhost:5432/iap_test"


def _is_onboarding_pg_test(path_name: str) -> bool:
    return path_name.startswith("test_onboarding_") and (
        "postgres" in path_name or "roles" in path_name
    )


@pytest.fixture(autouse=True)
def _require_disposable_onboarding_pg(
    request: pytest.FixtureRequest,
) -> None:
    if os.environ.get(_OPT_IN_ENV, "") != "1":
        return
    path_name = request.node.path.name
    if not _is_onboarding_pg_test(path_name):
        return
    url = os.environ.get(_URL_ENV, _DEFAULT_URL)
    require_disposable(url, purpose=f"integration test {path_name}")
