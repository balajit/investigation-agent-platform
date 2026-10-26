# tests/unit/test_logger_names.py
"""Logger display-name compression is log customization only (D9 addendum).

Dotted logger names render via longest dot-boundary prefix match; record
names, imports, and runtime behavior are untouched.
"""

import logging

from investigation_agent_platform.infrastructure.telemetry.logging import (
    StructuredJsonFormatter,
)


def _record(name: str) -> logging.LogRecord:
    return logging.LogRecord(name, logging.INFO, __file__, 1, "m", (), None)


def test_verbatim_by_default():
    fmt = StructuredJsonFormatter()
    assert (
        fmt.display_logger("investigation_agent_platform.infrastructure.configuration.layers")
        == "investigation_agent_platform.infrastructure.configuration.layers"
    )


def test_exact_alias_match():
    fmt = StructuredJsonFormatter(
        logger_aliases={
            "investigation_agent_platform.infrastructure.configuration.layers": "i.p.i.c.layers"
        }
    )
    assert (
        fmt.display_logger("investigation_agent_platform.infrastructure.configuration.layers")
        == "i.p.i.c.layers"
    )


def test_prefix_match_appends_remainder():
    fmt = StructuredJsonFormatter(
        logger_aliases={"investigation_agent_platform.application.worker.activities": "ACTVTY"}
    )
    assert (
        fmt.display_logger("investigation_agent_platform.application.worker.activities.execution")
        == "ACTVTY.execution"
    )


def test_longest_prefix_wins_and_dot_boundary():
    fmt = StructuredJsonFormatter(
        logger_aliases={
            "a.b": "SHORT",
            "a.b.c": "LONG",
            "a.bc": "WRONG",
        }
    )
    assert fmt.display_logger("a.b.c.d") == "LONG.d"
    # "a.bc" must not match "a.bcd" (dot boundary required).
    assert fmt.display_logger("a.bcde") == "a.bcde"


def test_alias_in_rendered_payload():
    import json

    fmt = StructuredJsonFormatter(logger_aliases={"x.y": "XY"})
    payload = json.loads(fmt.format(_record("x.y.z")))
    assert payload["logger"] == "XY.z"
