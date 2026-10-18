# src/investigation_agent_platform/ports/artifacts/__init__.py
"""Artifact storage port interfaces."""

from investigation_agent_platform.ports.artifacts.store import (
    ArtifactObject,
    ArtifactPage,
    ArtifactRef,
    ArtifactStorePort,
)

__all__ = [
    "ArtifactObject",
    "ArtifactPage",
    "ArtifactRef",
    "ArtifactStorePort",
]
