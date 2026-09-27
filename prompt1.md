You are a Principal Backend Engineer building Layer 3 of an Incident Investigation Platform using Python, Pydantic, and Neo4j.

### Objective
Implement the schema models and an ingestion service that takes output from a custom AST parser and links it to a 5-tier organizational hierarchy graph in Neo4j.

### Structural Hierarchy
Domain -> GitOrg -> Repository -> Package -> ASTNode

### Requirements
1. Data Models (Pydantic v2):
   - `Domain`: id, name, owner_email.
   - `GitOrg`: id, name, domain_id.
   - `Repository`: id, name, git_org_id, repo_type (Enum: `MICROSERVICE`, `SHARED_LIBRARY`, `APPLICATION`, `MONOREPO`).
   - `Package`: id, path, repo_id.
   - `ASTNode`: id, name, node_type (Enum: `FUNCTION`, `CLASS`, `METHOD`), file_path, start_line, end_line, package_id.

2. Ingestion Pipeline (`ASTTopologyIngestor` class):
   - Provide a method `ingest_ast_payload(payload: dict, repo_id: str)` that parses JSON containing AST nodes and writes them to Neo4j.
   - Idempotently merge all nodes using Cypher `MERGE`.
   - Establish the following explicit edges:
     - `(:GitOrg)-[:BELONGS_TO]->(:Domain)`
     - `(:Repository)-[:BELONGS_TO]->(:GitOrg)`
     - `(:Package)-[:BELONGS_TO]->(:Repository)`
     - `(:ASTNode)-[:DECLARED_IN]->(:Package)`
     - `(:ASTNode)-[:CALLS]->(:ASTNode)` (for cross-function dependencies found in the AST payload).

3. Domain Lookup Service (`DomainAttributionService` class):
   - Method `resolve_ast_node_to_domain(file_path: str, line_number: int, repo_id: str) -> DomainAttributionResult`
   - Execute a Cypher traversal starting from the matching `ASTNode` up through `Package -> Repository -> GitOrg -> Domain`.
   - Return a typed dict containing the resolved `domain_id`, `repo_type`, `git_org_id`, and full ownership path.

Provide complete, production-ready Python code with async Neo4j driver calls, type hints, and error handling.
