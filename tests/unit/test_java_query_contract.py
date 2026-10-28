"""Contract tests for the closed Java query vocabulary and A7 boundary."""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from investigation_agent_platform.application.analysis.java import query
from investigation_agent_platform.application.analysis.java.model import JavaModel

_ROOT = Path(__file__).resolve().parents[2]
_VOCABULARY = (_ROOT / "docs" / "IAP-java-query-vocabulary-v1.0.md").read_text()


def test_model_fields_have_a7_bucket_documentation():
    for field in JavaModel.model_fields:
        assert field in _VOCABULARY, field


def test_public_query_functions_are_documented():
    public = [
        name
        for name, value in inspect.getmembers(query, inspect.isfunction)
        if not name.startswith("_") and value.__module__ == query.__name__
    ]
    for name in public:
        assert re.search(rf"\b{re.escape(name)}\b", _VOCABULARY), name


def test_closed_argument_kind_prefixes_are_documented():
    for prefix in ("value:", "name", "complex", "literal:undecodable", "limitation:"):
        assert prefix in _VOCABULARY


def test_outcome_semantics_are_explicit():
    for state in ("RESOLVED", "PARTIAL", "UNRESOLVED", "Missing record", "Syntax-error window"):
        assert state in _VOCABULARY
    assert "does not mean the source globally lacks" in _VOCABULARY
