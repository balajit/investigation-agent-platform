"""Shared pytest fixtures (Part 4, section 12.1 mandated fixture set).

Fixtures reuse the composition root's already-constructed, already-type-checked
objects from ``api/dependencies.py`` (``_DEFAULT_PROFILE``) and ``AppContext``
instead of inventing domain constructors by hand. The concrete domain fixtures
(sample_evidence / sample_hypothesis) are added by their owning test modules as
the Part 4 test suite is filled in, so no dead/invented object shapes live here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

import pytest

from investigation_agent_platform.api.dependencies import (
    _DEFAULT_PROFILE,
    AppContext,
    get_app_context,
)
from investigation_agent_platform.domain.profile.models import ApplicationProfile


@pytest.fixture
def utc_now_factory() -> Callable[[], datetime]:
    """Returns a callable producing aware UTC timestamps for evidence freshness."""

    def _factory() -> datetime:
        return datetime.now(timezone.utc)

    return _factory


@pytest.fixture
def app_context() -> AppContext:
    """The in-memory composition root shared by the whole test suite."""
    return get_app_context()


@pytest.fixture
def sample_profile() -> ApplicationProfile:
    """The canonical default ApplicationProfile seeded via the composition root."""
    return _DEFAULT_PROFILE
