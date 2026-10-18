Analysis of current code
1. **`TreeSitterParser` & `CodeSymbolGraphBuilder` (`pipeline.py`, `graphify_adapter.py`, `parser.py`)**: Precise, non-blocking AST parsing using `tree-sitter-language-pack` and building Rustworkx (`rx.PyDiGraph`) directional call graphs.
2. **Database & SQL Linking (`pipeline.py`, `graphify_adapter.py`)**: Linking AST symbol nodes to SQL tables (`ACCESSES_TABLE`) based on DDL and ORM/SQL static analysis.
3. **Enterprise & Tenant Security (`intelligence.py`, `codeowners.py`, `git.py`, `micro.py`)**: Enforces `tenant_allowlist`, prevents path traversal (`_has_traversal`), handles git operations via `pygit2`, resolves ownership (`CODEOWNERS`), and performs fast runtime micro-symbol resolution (`MicroSymbolResolver`).

---

### The Design Challenge

Standard Cognee uses default parsers and ingestion rules to create generic knowledge graphs. However, **your current platform code already provides superior AST precision, security boundaries, tenant isolation, and SQL-to-code edge linking**.

If you let Cognee handle raw code parsing directly, you lose:

* Tenant path-traversal/security guards (`_validate_tree_path`, `_require_tenant_scope`).
* Precise AST symbols and call-graph structure from `TreeSitterParser` and `CodeSymbolGraphBuilder`.
* Code-to-database schema linkage (`ACCESSES_TABLE`).
* Micro-symbol resolution and git provenance tracking (`PyGit2Adapter`).

---

### The Architecture: Cognee as Orchestrator + Custom Pipeline as AST/Graph Engine

Instead of letting Cognee parse raw files, **use Cognee as the macro Knowledge Graph manager, vector indexer, and query layer**, while plugging **your custom TreeSitter & Rustworkx pipeline directly into Cognee's ingestion step**.

```
                   +-------------------------------------------------------+
                   |                 Ingestion Orchestrator                |
                   |       (Triggered via CI/CD, Git Hook, or Agent)       |
                   +---------------------------+---------------------------+
                                               |
         +-------------------------------------+-------------------------------------+
         |                                                                           |
         v                                                                           v
+-----------------------------------+                               +-----------------------------------+
|   Your Pipeline (Custom Engine)   |                               |      Cognee (Storage & Query)     |
+-----------------------------------+                               +-----------------------------------+
| • Runs `CodebaseGraphPipeline`    |                               | • Stores exported JSON Graph into |
| • AST Parsing (`TreeSitterParser`) |                               |   Graph DB (Neo4j / FalkorDB)     |
| • Rustworkx graph construction    |   ===> Exports Graphify ===>  | • Indexes code snippets into      |
| • CODEOWNERS & Tenant checks      |        `call_graph.json`      |   Vector DB (LanceDB / Qdrant)    |
| • SQL-to-Code mapping             |                               | • Provides unified search API     |
+-----------------------------------+                               +-----------------------------------+
                                                                                     |
                                                                                     v
                                                                    +-----------------------------------+
                                                                    |         Agent Query Router        |
                                                                    +-----------------------------------+
                                                                    | • High-level Graph/Vector Search: |
                                                                    |   via Cognee API                  |
                                                                    | • Low-level / Micro AST execution:|
                                                                    |   via `MicroSymbolResolver` &     |
                                                                    |   `TreeSitterCodeIntelligence`    |
                                                                    +-----------------------------------+

```

---

### Step-by-Step Integration Design

#### 1. Custom Data Ingestion Pipeline (Replacing Cognee's Default Parser)

Your `CodebaseGraphPipeline` runs AST parsing, builds the Rustworkx graph, and exports the `call_graph.json` (Graphify format). We then register this enriched node/edge topology directly into Cognee's knowledge graph.

```python
# src/investigation_agent_platform/infrastructure/evidence/code/cognee_bridge.py

import asyncio
from pathlib import Path
import cognee
import rustworkx as rx

from investigation_agent_platform.infrastructure.evidence.code.parser import TreeSitterParser
from investigation_agent_platform.infrastructure.evidence.code.pipeline import CodebaseGraphPipeline

class CogneeCodebaseManager:
    """Uses your custom TreeSitter/Rustworkx engine to feed Cognee graph & vector stores."""

    def __init__(self, repo_base_path: str):
        self.repo_base_path = Path(repo_base_path).resolve()
        self.parser = TreeSitterParser()
        self.pipeline = CodebaseGraphPipeline(self.parser)

    async def sync_repository_to_cognee(
        self,
        tenant_id: str,
        repository_id: str,
        ddl_paths: list[Path] | None = None
    ) -> None:
        repo_root = self.repo_base_path / repository_id
        
        # Step 1: Run YOUR AST pipeline to get the rich Rustworkx graph
        rx_graph: rx.PyDiGraph = await self.pipeline.build_repository_graph(
            repo_root=repo_root,
            language="python",
            ddl_paths=ddl_paths
        )
        
        # Step 2: Export artifact locally for fast-path lookups
        artifact_path = repo_root / "call_graph.json"
        self.pipeline.export_graphify_format(artifact_path)

        # Step 3: Ingest AST entities and relations into Cognee's Graph/Vector Store
        # We transform Rustworkx nodes/edges into Cognee dataset payloads
        nodes_payload = []
        for idx in rx_graph.node_indices():
            ndata = rx_graph.get_node_data(idx)
            if isinstance(ndata, dict):
                # Enforce tenant isolation attributes
                ndata["tenant_id"] = tenant_id
                ndata["repository_id"] = repository_id
                nodes_payload.append(ndata)

        # Feed extracted entities and structural metadata into Cognee
        await cognee.add(data=nodes_payload, dataset_name=f"{tenant_id}_{repository_id}")
        
        # Runs Cognee's embedding generation & persistence across Neo4j/FalkorDB/VectorDB
        await cognee.cognify(dataset_name=f"{tenant_id}_{repository_id}")

```

---

#### 2. Unified Query Router Strategy

When an AI agent queries the codebase during an investigation:

1. **High-Level / Multi-Hop Graph Questions** (e.g., *"Which services are affected by changing the user table schema?"*):
* Executed via **Cognee's unified graph search**, which traverses the AST symbols and SQL table nodes pre-built by your pipeline.


2. **Exact / Micro-Tier AST Analysis & Provenance** (e.g., *"Who owns line 142 of `intelligence.py`?"* or *"Fetch git commit history for this file"*):
* Executed via your existing **`MicroSymbolResolver`**, **`CodeownersResolver`**, and **`PyGit2Adapter`**.



```python
# src/investigation_agent_platform/infrastructure/evidence/code/unified_service.py

from uuid import UUID
import cognee
from investigation_agent_platform.domain.profile.models import CodeProfile
from investigation_agent_platform.infrastructure.evidence.code.intelligence import TreeSitterCodeIntelligenceProvider
from investigation_agent_platform.infrastructure.evidence.code.micro import MicroSymbolResolver
from investigation_agent_platform.infrastructure.evidence.code.codeowners import CodeownersResolver

class UnifiedCodeIntelligenceService:
    """Facade orchestrating Cognee (Macro Graph & Vectors) and Custom Engine (Micro & Git Provenance)."""

    def __init__(
        self,
        code_provider: TreeSitterCodeIntelligenceProvider,
        micro_resolver: MicroSymbolResolver,
        codeowners_resolver: CodeownersResolver,
    ):
        self.code_provider = code_provider
        self.micro_resolver = micro_resolver
        self.codeowners_resolver = codeowners_resolver

    async def search_macro_knowledge(self, tenant_id: str, repository_id: str, query: str):
        """Uses Cognee to search high-level graph relationships and embeddings."""
        dataset = f"{tenant_id}_{repository_id}"
        # Search Cognee's knowledge graph (populated by your AST pipeline)
        return await cognee.search(query_text=query, dataset_name=dataset)

    async def resolve_exact_line_ownership(
        self,
        tenant_id: str,
        repository_id: str,
        locator: str,
        revision: str,
        file_path: str,
        line_number: int,
    ) -> dict:
        """Uses your Custom Engine for precise micro-symbol resolution and CODEOWNERS lookup."""
        
        # 1. Micro-symbol resolution via your TreeSitter AST matcher
        micro_match = await self.micro_resolver.resolve_micro_symbol(
            tenant_id=tenant_id,
            repository_id=repository_id,
            locator=locator,
            revision=revision,
            file_path=file_path,
            line_number=line_number,
        )

        # 2. Team ownership lookup via your CODEOWNERS parser
        owner = await self.codeowners_resolver.resolve_owner(
            tenant_id=tenant_id,
            locator=locator,
            file_path=file_path,
        )

        return {
            "micro_symbol": micro_match.node.name if micro_match else None,
            "symbol_type": micro_match.node.node_type if micro_match else None,
            "owning_team": owner,
            "revision_verified": micro_match.revision_verified if micro_match else False,
        }

```

---

### Key Advantages of This Pattern

1. **Preserves Security & Isolation:** All file system reads go through your `_validate_tree_path`, `_require_tenant_scope`, and `pygit2` bounds.
2. **Best-of-Both-Worlds:**
* **Your Pipeline** supplies the AST accuracy, call-graph edge creation, SQL table linking, and git history/ownership.
* **Cognee** manages graph/vector persistence (FalkorDB / Neo4j / Qdrant), embedding index management, and multi-hop Cypher/RAG querying.


3. **No Redundant Reparsing:** The same `TreeSitterParser` and `CodeSymbolGraphBuilder` run once during repository indexing.
