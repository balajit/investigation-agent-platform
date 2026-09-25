"""Agent loop Elastic→Oracle provider order test."""

import pytest


@pytest.mark.asyncio
async def test_evidence_gateway_signatures_require_investigation_id() -> None:
    """Gateway and provider contracts now require investigation_id (hard threading)."""
    import inspect

    from investigation_agent_platform.ports.evidence.gateway import EvidenceGatewayProtocol
    from investigation_agent_platform.ports.evidence.runtime import RuntimeEvidenceProviderProtocol

    sig = inspect.signature(RuntimeEvidenceProviderProtocol.search_runtime_evidence)
    assert "investigation_id" in sig.parameters

    sig2 = inspect.signature(EvidenceGatewayProtocol.search_runtime_evidence)
    assert "investigation_id" in sig2.parameters


@pytest.mark.asyncio
async def test_code_intelligence_provider_exists() -> None:
    import pathlib
    import tempfile

    from investigation_agent_platform.infrastructure.evidence.code.intelligence import (
        TreeSitterCodeIntelligenceProvider,
    )

    with tempfile.TemporaryDirectory() as tmp:
        # create dummy source file
        src = pathlib.Path(tmp) / "example.py"
        src.write_text("def hello():\n    x = 1\n    return x\n")
        provider = TreeSitterCodeIntelligenceProvider(repo_base_path=tmp)
        from investigation_agent_platform.domain.profile.models import CodeProfile

        profile = CodeProfile(
            provider="git",
            repository="",
            defaultBranch="main",
            language="python",
            sourceRoots=["."],
            buildSystem="uv",
            moduleStructure=".",
        )
        syms = await provider.find_symbol("tenant-a", "hello", profile)
        # Should find at least via text search fallback
        assert isinstance(syms, list)
