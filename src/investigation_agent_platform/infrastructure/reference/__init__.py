# src/investigation_agent_platform/infrastructure/reference/__init__.py
"""Reference-document infrastructure (Part 11.6)."""

from investigation_agent_platform.infrastructure.reference.fetchers import (
    GitReferenceFetcher,
    LocalPathReferenceFetcher,
    S3ReferenceFetcher,
    fetcher_for,
)

__all__ = [
    "GitReferenceFetcher",
    "LocalPathReferenceFetcher",
    "S3ReferenceFetcher",
    "fetcher_for",
]
