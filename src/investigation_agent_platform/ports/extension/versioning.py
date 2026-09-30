# src/investigation_agent_platform/ports/extension/versioning.py
"""Persisted-payload schema versioning and upcasters (Part 11.1)."""

from typing import Any, Protocol, runtime_checkable

from investigation_agent_platform.domain.common.exceptions import PlatformConfigurationError


class UnknownSchemaVersionError(PlatformConfigurationError):
    """Raised when no upcaster chain reaches the requested schema version."""


@runtime_checkable
class SchemaUpcaster(Protocol):
    """Migrates one persisted payload from ``from_version`` to ``to_version``."""

    @property
    def from_version(self) -> str: ...

    @property
    def to_version(self) -> str: ...

    def upcast(self, payload: dict[str, Any]) -> dict[str, Any]: ...


def upcast_to(
    payload: dict[str, Any],
    target_version: str,
    upcasters: list[SchemaUpcaster],
    *,
    version_key: str = "schema_version",
    max_steps: int = 8,
) -> dict[str, Any]:
    """Chain upcasters until ``payload[version_key]`` reaches ``target_version``.

    Fail-closed: raises ``UnknownSchemaVersionError`` when no chain step
    applies or the step budget is exhausted (never silently returns a stale
    schema pretending to be current).
    """
    current = dict(payload)
    for _ in range(max_steps + 1):
        if current.get(version_key) == target_version:
            return current
        step = next(
            (u for u in upcasters if u.from_version == current.get(version_key)),
            None,
        )
        if step is None:
            raise UnknownSchemaVersionError(
                f"No upcaster from schema version {current.get(version_key)!r} "
                f"toward {target_version!r}"
            )
        current = step.upcast(current)
        current[version_key] = step.to_version
    raise UnknownSchemaVersionError(
        f"Upcast chain exceeded {max_steps} steps toward {target_version!r}"
    )
