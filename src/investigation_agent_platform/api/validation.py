# src/investigation_agent_platform/api/validation.py
"""Shared API-boundary validation constants (F-061 edge-input hardening).

``NO_NUL_PATTERN`` rejects NUL bytes in free-text query params at FastAPI
validation time (→ 422 with the standard ValidationError envelope) instead
of letting them reach the database, where asyncpg raises
``CharacterNotInRepertoireError`` → unhandled 500. JSON-body strings are
covered by the ``DBAPIError`` safety net in ``api.app``.
"""

from __future__ import annotations

from typing import Final

NO_NUL_PATTERN: Final[str] = r"^[^\x00]*$"
