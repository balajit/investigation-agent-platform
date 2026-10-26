# tests/unit/test_story_logging.py
"""Contract tests for story logging (D1/D5/D8/D9/D10/D11).

Envelope shape, caps, redaction, template rendering, level resolution,
TRACE registration, hierarchical logger levels, and context capture.
Global state (config cache, contextvars, logger levels) is always restored.
"""

import logging

import pytest

from investigation_agent_platform.infrastructure.telemetry import story, story_config
from investigation_agent_platform.infrastructure.telemetry.logging import (
    _apply_hierarchical_levels,
    _StoryContextFilter,
)


@pytest.fixture(autouse=True)
def _clean_story_state(monkeypatch):
    story_config.reset_story_config()
    story.clear_story_context()
    for var in (
        "IAP_STORY_TEMPLATE",
        "IAP_STORY_MAX_KEYS",
        "IAP_STORY_MAX_STR",
        "IAP_STORY_MAX_LIST",
        "IAP_STORY_MAX_KEY_LEN",
        "IAP_STORY_LEVELS",
        "IAP_STORY_STAGE_CEILING",
        "IAP_LOGGING_YAML",
    ):
        monkeypatch.delenv(var, raising=False)
    yield
    story_config.reset_story_config()
    story.clear_story_context()


def test_envelope_key_set_and_legacy_event_default():
    extra = story.story_event(
        comp="ACTVTY",
        func="reason_activity",
        stage="reason",
        step="decide",
        outcome="started",
        tenant_id="t",
        investigation_id="i",
        event="reason_activity started",
    )
    assert extra["comp"] == "ACTVTY"
    assert extra["func"] == "reason_activity"
    assert extra["stage"] == "reason"
    assert extra["step"] == "decide"
    assert extra["outcome"] == "started"
    assert extra["tenant_id"] == "t"
    assert extra["investigation_id"] == "i"
    assert extra["event"] == "reason_activity started"
    assert "inputs" not in extra and "outputs" not in extra


def test_envelope_event_derived_when_missing():
    extra = story.story_event(
        comp="WFLW",
        func="RunInvestigationWorkflow",
        stage="execute",
        step="iterate",
        outcome="completed",
    )
    assert extra["event"] == "RunInvestigationWorkflow completed"


def test_caps_bound_inputs_outputs():
    big = {f"k{i}": "x" * 5000 for i in range(40)}
    extra = story.story_event(
        comp="SVC",
        func="f",
        stage="execute",
        step="s",
        outcome="completed",
        inputs=big,
        outputs={"rows": list(range(100))},
    )
    assert len(extra["inputs"]) == 20
    assert all(len(v) <= 1000 for v in extra["inputs"].values() if isinstance(v, str))
    assert len(extra["outputs"]["rows"]) == 20


def test_redaction_applies_to_inputs_outputs():
    extra = story.story_event(
        comp="ACTVTY",
        func="f",
        stage="execute",
        step="s",
        outcome="started",
        inputs={"api_key": "sekret", "action": "QUERY_STATE"},
    )
    assert extra["inputs"]["api_key"] == "[REDACTED]"
    assert extra["inputs"]["action"] == "QUERY_STATE"


def test_extra_redacted_keys_append_only(tmp_path, monkeypatch):
    yaml_path = tmp_path / "logging.yaml"
    yaml_path.write_text("story:\n  extra_redacted_keys: [session_token]\n", encoding="utf-8")
    monkeypatch.setenv("IAP_LOGGING_YAML", str(yaml_path))
    story_config.reload_story_config()
    extra = story.story_event(
        comp="ACTVTY",
        func="f",
        stage="execute",
        step="s",
        outcome="started",
        inputs={"session_token": "abc", "api_key": "sekret", "n": 1},
    )
    # Configured key redacted AND built-in policy still applies.
    assert extra["inputs"]["session_token"] == "[REDACTED]"
    assert extra["inputs"]["api_key"] == "[REDACTED]"
    assert extra["inputs"]["n"] == 1


def test_message_template_default_and_override(tmp_path, monkeypatch):
    head = story.story_message(
        comp="ACTVTY", func="f", stage="execute", step="d", outcome="started"
    )
    assert head == "[ACTVTY] f · execute/d · started"
    yaml_path = tmp_path / "logging.yaml"
    yaml_path.write_text(
        "story:\n  message_template: '{comp}|{func}|{outcome}'\n", encoding="utf-8"
    )
    monkeypatch.setenv("IAP_LOGGING_YAML", str(yaml_path))
    story_config.reload_story_config()
    head = story.story_message(
        comp="REPO", func="g", stage="persist", step="bank", outcome="completed", note="banked=3"
    )
    assert head == "REPO|g|completed — banked=3"


def test_bad_template_fails_closed():
    story_config.load_story_config()["message_template"] = "{nonexistent"
    head = story.story_message(comp="SVC", func="f", stage="reason", step="s", outcome="started")
    assert head == "[SVC] f · reason/s · started"


def test_story_level_resolution():
    # REPO defaults to DEBUG; persist stage is capped at DEBUG.
    assert story.story_level("REPO", "execute", logging.INFO) == logging.DEBUG
    assert story.story_level("ACTVTY", "persist", logging.INFO) == logging.DEBUG
    assert story.story_level("ACTVTY", "execute", logging.INFO) == logging.INFO
    # An explicit DEBUG ask is never promoted.
    assert story.story_level("ACTVTY", "execute", logging.DEBUG) == logging.DEBUG


def test_trace_level_registered():
    story.register_trace_level()
    assert logging.getLevelName(story.TRACE_LEVEL) == "TRACE"
    assert story.TRACE_LEVEL == 5
    assert hasattr(logging.Logger, "trace")


def test_hierarchical_levels_package_and_class():
    _apply_hierarchical_levels(
        {
            "loggers": {
                "iap_test_hier": "INFO",
                "iap_test_hier.sub.mod": "DEBUG",
            }
        }
    )
    try:
        pkg_child = logging.getLogger("iap_test_hier.other")
        exact = logging.getLogger("iap_test_hier.sub.mod")
        assert pkg_child.getEffectiveLevel() == logging.INFO
        assert exact.getEffectiveLevel() == logging.DEBUG
    finally:
        logging.getLogger("iap_test_hier").setLevel(logging.NOTSET)
        logging.getLogger("iap_test_hier.sub.mod").setLevel(logging.NOTSET)
        logging.getLogger("iap_test_hier.other").setLevel(logging.NOTSET)


def test_env_loggers_override(monkeypatch):
    monkeypatch.setenv("IAP_LOG_LOGGERS", "iap_test_env=WARNING")
    _apply_hierarchical_levels({})
    try:
        assert logging.getLogger("iap_test_env").getEffectiveLevel() == logging.WARNING
    finally:
        logging.getLogger("iap_test_env").setLevel(logging.NOTSET)


def test_context_filter_fills_missing_never_overwrites():
    story.bind_story_context(tenant_id="t1", investigation_id="i9")
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "m", (), None)
    assert _StoryContextFilter().filter(record) is True
    assert record.__dict__["tenant_id"] == "t1"
    assert record.__dict__["investigation_id"] == "i9"

    explicit = logging.LogRecord("x", logging.INFO, __file__, 1, "m", (), None)
    explicit.__dict__["tenant_id"] = "explicit"
    assert _StoryContextFilter().filter(explicit) is True
    assert explicit.__dict__["tenant_id"] == "explicit"


def test_context_fallback_in_envelope():
    story.bind_story_context(tenant_id="tctx")
    extra = story.story_event(comp="ACTVTY", func="f", stage="execute", step="s", outcome="started")
    assert extra["tenant_id"] == "tctx"


def test_vocabularies():
    assert set(story.STAGES) == {
        "intake",
        "reason",
        "force",
        "execute",
        "verify",
        "conclude",
        "persist",
        "publish",
    }
    for code in ("API", "TMP-WRKR", "ACTVTY", "WFLW", "SVC", "DOMAIN", "REPO", "PROV", "TOOL"):
        assert code in story.COMPONENTS


def test_tick_elapsed():
    start = story.tick()
    assert story.elapsed_ms(start) >= 0
