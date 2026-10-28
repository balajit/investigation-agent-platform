"""SourceRequest range-cap regression tests.

Guards the acquisition-time bound: ranged GET_CODE reads are capped at 501
lines (page via next_window), path-only reads stay allowed. Regression cover
for the wiring session, where the helper insertion stranded
``validate_range_width`` outside ``SourceRequest`` and the cap went inert
without any test failing.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from investigation_agent_platform.domain.evidence.requests import SourceRequest

_BASE = {"repository": "repo", "file_path": "src/Foo.java"}


def test_source_request_path_only_read_stays_allowed() -> None:
    req = SourceRequest(**_BASE)
    assert req.start_line is None and req.end_line is None


def test_source_request_range_at_cap_accepted() -> None:
    req = SourceRequest(**_BASE, start_line=1, end_line=501)
    assert (req.start_line, req.end_line) == (1, 501)


def test_source_request_range_over_cap_rejected() -> None:
    with pytest.raises(ValidationError):
        SourceRequest(**_BASE, start_line=1, end_line=502)


def test_source_request_inverted_range_rejected() -> None:
    with pytest.raises(ValidationError):
        SourceRequest(**_BASE, start_line=10, end_line=5)
