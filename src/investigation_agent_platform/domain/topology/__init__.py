# src/investigation_agent_platform/domain/topology/__init__.py
"""Layer 3 topology bounded context: organizational and static-code topology.

This package defines tenant-scoped, revision-aware domain models for
organizational ownership (Domain -> GitOrganization -> Repository -> Package
-> SourceFile -> ASTNode) and dual-frame failure attribution. See
``docs/IAP-implemenation-part5-v1.md`` for the full design.
"""
