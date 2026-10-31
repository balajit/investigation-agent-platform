"""AD2: generic Kafka event logging logs the full payload.

The consumer logs an allowlisted envelope only, and logging never fails
the consumer. Written first, red before the fix.
"""

from __future__ import annotations

from datetime import UTC, datetime


def _envelope(**over):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.infrastructure.messaging.faststream import (
        InvestigationCreatedEvent,
    )

    base = {
        "event_id": "evt-1",
        "investigation_id": "inv-1",
        "tenant_id": "t",
        "application_id": "app-1",
        "timestamp": datetime.now(UTC),
        "correlation_id": "corr-1",
    }
    base.update(over)
    return InvestigationCreatedEvent(**base)


def test_ad2_t02_allowlisted_fields_are_present() -> None:
    from investigation_agent_platform.infrastructure.messaging.faststream import (
        _LOGGABLE_FIELDS,
        envelope_for_log,
    )

    logged = envelope_for_log(_envelope())
    # The created-event model carries every allowlisted field but
    # event_type; only allowlisted keys (plus the count) may appear.
    assert set(logged) == (set(_LOGGABLE_FIELDS) - {"event_type"}) | {"payload_keys"}
    assert "event_type" in _LOGGABLE_FIELDS
    assert all(len(str(v)) <= 128 for k, v in logged.items() if k != "payload_keys")


def test_ad2_t01_payload_values_are_never_logged() -> None:
    from investigation_agent_platform.infrastructure.messaging.faststream import (
        envelope_for_log,
    )

    class _WithPayload:
        def model_dump(self, mode="json"):  # type: ignore[no-untyped-def]
            return {
                "event_id": "evt-9",
                "event_type": "SomethingHappened",
                "investigation_id": "inv-9",
                "tenant_id": "t",
                "application_id": "app",
                "payload": {
                    "secret": "SECRET-PAYLOAD-7741",
                    "token": "Bearer abc.def.ghi",
                    "nested": {"deep": "SECRET-PAYLOAD-7741"},
                },
            }

    logged = envelope_for_log(_WithPayload())
    assert logged["payload_keys"] == 3
    assert logged["event_type"] == "SomethingHappened"
    assert "SECRET-PAYLOAD-7741" not in str(logged)
    assert "Bearer abc.def.ghi" not in str(logged)
    # Negative control: the raw dump carries both values.
    assert "SECRET-PAYLOAD-7741" in str(_WithPayload().model_dump())


def test_ad2_t03_logging_never_fails_the_consumer(caplog) -> None:
    import logging

    from investigation_agent_platform.infrastructure.messaging import faststream as fs

    class _NoDump:
        pass

    class _RaisingDump:
        def model_dump(self, mode="json"):  # type: ignore[no-untyped-def]
            raise RuntimeError("unserializable")

    with caplog.at_level(logging.INFO):
        assert fs.envelope_for_log(_NoDump()) == {"unreadable": True}
        assert fs.envelope_for_log(_RaisingDump()) == {"unreadable": True}
        before = len(caplog.records)
        fs.log_received(_NoDump())
        fs.log_received(_RaisingDump())
        fs.log_received(None)
        fs.log_received(object())
        # Four log records, no exception: logging cannot fail the consumer.
        assert len(caplog.records) - before == 4


def test_ad2_t04_redaction_applies_to_the_envelope() -> None:
    from investigation_agent_platform.infrastructure.messaging.faststream import (
        envelope_for_log,
    )

    out = envelope_for_log(_envelope(correlation_id="Bearer abc.def.ghi"))
    assert out["correlation_id"] == "[REDACTED]"
