# src/investigation_agent_platform/ports/evidence/code.py
"""Source code and code intelligence evidence provider port protocols."""

from typing import Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.domain.evidence.models import Evidence
from investigation_agent_platform.domain.profile.models import CodeProfile


class CodeLocation(BaseModel):
    """Represents a specific source location in code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    file_path: str
    line_number: int
    column_number: int | None = None
    snippet: str | None = None


class CodeSymbol(BaseModel):
    """Structured code intelligence symbol representation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    kind: str
    location: CodeLocation
    signature: str | None = None
    docstring: str | None = None


class CallGraphNode(BaseModel):
    """Node in a source code call graph."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    caller_symbol: str
    callee_symbol: str
    location: CodeLocation


class CodeDiffResult(BaseModel):
    """Structured diff result between code refs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_ref: str
    target_ref: str
    files_changed: list[str]
    diff_contents: dict[str, str] = Field(default_factory=dict)


@runtime_checkable
class CodeEvidenceProviderProtocol(Protocol):
    """Port for querying raw source code repositories and historical versions."""

    async def get_source(
        self, tenant_id: str, investigation_id: UUID, file_path: str, profile: CodeProfile
    ) -> Evidence:
        ...

    async def get_code_history(
        self, tenant_id: str, investigation_id: UUID, path: str, profile: CodeProfile
    ) -> list[Evidence]:
        ...

    async def compare_versions(
        self, tenant_id: str, source_ref: str, target_ref: str, profile: CodeProfile
    ) -> CodeDiffResult:
        ...


@runtime_checkable
class CodeIntelligenceProviderProtocol(Protocol):
    """Port for deep code-intelligence operations (AST, symbol graphs, call hierarchies)."""

    async def search_code(self, tenant_id: str, query: str, profile: CodeProfile) -> list[Evidence]:
        ...

    async def find_symbol(self, tenant_id: str, symbol_name: str, profile: CodeProfile) -> list[CodeSymbol]:
        ...

    async def find_callers(self, tenant_id: str, symbol_name: str, profile: CodeProfile) -> list[CallGraphNode]:
        ...

    async def find_callees(self, tenant_id: str, symbol_name: str, profile: CodeProfile) -> list[CallGraphNode]:
        ...

    async def find_exception_handlers(
        self, tenant_id: str, exception_class: str, profile: CodeProfile
    ) -> list[CodeLocation]:
        ...

    async def find_database_operations(
        self, tenant_id: str, entity_or_table: str, profile: CodeProfile
    ) -> list[CodeLocation]:
        ...