# src/investigation_agent_platform/infrastructure/configuration/mapping_registry.py
"""Operator-authored observability mapping registry (Part 10).

Loads `ObservabilitySourceMapping` documents from a directory of YAML files,
validates them fail-closed at startup, and resolves `mapping_source_id`
strings (stamped server-side onto requests) to mapping objects. Unknown ids
reject; `None` resolves to the built-in generic-ECS mapping. Never agent
input, never inferred from document contents.
"""

import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from investigation_agent_platform.domain.common.exceptions import PlatformConfigurationError
from investigation_agent_platform.domain.observability.mapping import (
    GENERIC_ECS_SOURCE_ID,
    ObservabilitySourceMapping,
    generic_ecs_mapping,
)

logger = logging.getLogger(__name__)


class MappingProfileRegistry:
    """Validated mapping documents keyed by `source_id`."""

    def __init__(self, mappings: dict[str, ObservabilitySourceMapping]) -> None:
        self._mappings = dict(mappings)

    def __len__(self) -> int:
        return len(self._mappings)

    @property
    def source_ids(self) -> frozenset[str]:
        """Configured ids (excluding the built-in generic-ECS fallback)."""
        return frozenset(self._mappings)

    def resolve(self, source_id: str | None) -> ObservabilitySourceMapping:
        """Resolve a stamped id; `None`/builtin resolves without configuration.

        Unknown ids fail closed. A YAML document claiming the builtin id is
        rejected at load, so the builtin can never be shadowed.
        """
        if source_id is None or source_id == GENERIC_ECS_SOURCE_ID:
            return generic_ecs_mapping()
        try:
            return self._mappings[source_id]
        except KeyError:
            raise PlatformConfigurationError(
                f"Unknown observability mapping source_id: {source_id!r}."
            ) from None


def _load_document(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PlatformConfigurationError(f"Invalid mapping YAML {path}: {exc!s}") from exc
    if not isinstance(raw, dict):
        raise PlatformConfigurationError(
            f"Invalid mapping YAML {path}: top-level mapping object required."
        )
    return raw


def load_mapping_profiles(directory: str | Path) -> MappingProfileRegistry:
    """Load and validate every `*.yaml` mapping document in a directory.

    Fail-closed on: missing directory? No — a missing directory yields an
    empty registry (generic-ECS still resolves); every *present* document
    must validate. Rejects: malformed YAML, schema violations, duplicate
    `source_id`, exact-duplicate `index_patterns` across different sources
    (ambiguous routing must never silently pick one; glob-subsumption beyond
    exact duplicates is a documented limitation), empty `index_patterns`
    (only the built-in generic-ECS mapping may synthesize patterns), and
    `legacy_index_synthesis: true` in file-loaded documents.
    """
    mappings: dict[str, ObservabilitySourceMapping] = {}
    seen_patterns: dict[str, str] = {}
    base = Path(directory)
    if not base.is_dir():
        logger.warning(
            "Mapping profiles directory missing; generic-ECS only",
            extra={"directory": str(base)},
        )
        return MappingProfileRegistry(mappings)
    for path in sorted(base.glob("*.yaml")) + sorted(base.glob("*.yml")):
        document = _load_document(path)
        try:
            mapping = ObservabilitySourceMapping.model_validate(document)
        except ValidationError as exc:
            raise PlatformConfigurationError(f"Invalid mapping document {path}: {exc!s}") from exc
        if mapping.source_id in mappings:
            raise PlatformConfigurationError(
                f"Duplicate mapping source_id {mapping.source_id!r} in {path}."
            )
        if mapping.source_id == GENERIC_ECS_SOURCE_ID:
            raise PlatformConfigurationError(
                f"Mapping in {path} claims the reserved builtin id {GENERIC_ECS_SOURCE_ID!r}."
            )
        if mapping.legacy_index_synthesis:
            raise PlatformConfigurationError(
                f"Mapping {mapping.source_id!r} in {path} claims legacy index "
                "synthesis, reserved for the built-in generic-ECS mapping."
            )
        if not mapping.index_patterns:
            raise PlatformConfigurationError(
                f"Mapping {mapping.source_id!r} in {path} declares no index patterns."
            )
        for pattern in mapping.index_patterns:
            owner = seen_patterns.get(pattern)
            if owner is not None and owner != mapping.source_id:
                raise PlatformConfigurationError(
                    f"Index pattern {pattern!r} claimed by both {owner!r} and "
                    f"{mapping.source_id!r} in {path}."
                )
            seen_patterns[pattern] = mapping.source_id
        mappings[mapping.source_id] = mapping
        if mapping.tenant_scope.strategy.value == "none_required":
            logger.warning(
                "Mapping source has no tenant isolation clause — verify intentional",
                extra={"source_id": mapping.source_id},
            )
    logger.info(
        "Mapping profiles loaded",
        extra={"directory": str(base), "source_ids": sorted(mappings)},
    )
    return MappingProfileRegistry(mappings)
