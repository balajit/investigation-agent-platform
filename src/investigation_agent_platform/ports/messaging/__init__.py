# src/investigation_agent_platform/ports/messaging/__init__.py
"""Messaging ports and event publication contracts."""

from investigation_agent_platform.ports.messaging.publisher import EventEnvelope, EventPublisher

__all__ = ["EventEnvelope", "EventPublisher"]