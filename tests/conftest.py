"""Shared pytest fixtures (Part 4, section 12.1 mandated fixture set).

Fixtures reuse the composition root's already-constructed, already-type-checked
objects from ``api/dependencies.py`` (``_DEFAULT_PROFILE``) and ``AppContext``
instead of inventing domain constructors by hand. The concrete domain fixtures
(sample_evidence / sample_hypothesis) are added by their owning test modules as
the Part 4 test suite is filled in, so no dead/invented object shapes live here.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

# Test-session environment pin. Importing `investigation_agent_platform.main`
# (as TestMainModule tests do) runs apply_layered_config(), which writes
# config/application.yaml values into os.environ for unset names — including
# IAP_ENVIRONMENT=production. Without this pin, every test executing after
# such an import runs with production semantics (auth fail-closed, production
# context builds) and fails. setdefault preserves an explicitly exported
# environment and only backfills the default.
os.environ.setdefault("IAP_ENVIRONMENT", "test")

from investigation_agent_platform.api.dependencies import (
    _DEFAULT_PROFILE,
    AppContext,
    get_app_context,
)
from investigation_agent_platform.domain.profile.models import ApplicationProfile


@pytest.fixture(autouse=True)
def _restore_environ() -> Iterator[None]:
    """Keep every test hermetic against os.environ mutation.

    Importing `investigation_agent_platform.main` (and any code path running
    apply_layered_config()) writes config/application.yaml values into
    os.environ for unset names (IAP_LLM_PROVIDER, timeouts, pool sizes...).
    Without per-test restoration, whichever test imports main first poisons
    all later config-resolution tests in the process. Snapshot/restore is
    cheap; explicit per-test patch.dict/monkeypatch keeps working on top.
    """
    with patch.dict(os.environ):
        yield


@pytest.fixture
def utc_now_factory() -> Callable[[], datetime]:
    """Returns a callable producing aware UTC timestamps for evidence freshness."""

    def _factory() -> datetime:
        return datetime.now(UTC)

    return _factory


@pytest.fixture
def app_context() -> AppContext:
    """The in-memory composition root shared by the whole test suite."""
    return get_app_context()


@pytest.fixture
def sample_profile() -> ApplicationProfile:
    """The canonical default ApplicationProfile seeded via the composition root."""
    return _DEFAULT_PROFILE
