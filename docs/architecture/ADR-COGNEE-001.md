# ADR-COGNEE-001: Cognee as Projection-Only Graph Store

- **Status:** Accepted
- **Date:** 2026-10-17
- **Deciders:** Chief Architect (IAP)
- **Scope:** Static code analysis recall (code understanding for investigations)

## Context

IAP needs code recall beyond substring search (semantic concept location,
caller/callee traversal, disambiguation). Two candidate suppliers: our
hand-built tree-sitter path (deterministic, exact) and Cognee 1.6.1
(framework: datasets, Neo4j + vector storage, six search types). An
evaluation spike (`scripts/cognee_spike.py`) tested "tree-sitter underneath,
Cognee on top" against battery cases 2/5/7.

## Decision

**Cognee is adopted narrowly as a typed-graph store fed exclusively by our
deterministic projections. The cognify pipeline, LLM-mediated retrieval, and
semantic search are declined for code.**

Concretely:
- KEEP: `map_payload_to_points` → `add_data_points(graph_only=True)` (existing
  pilot) — our tree-sitter walk produces typed points (symbols with ranges,
  CALLS/REFERENCES edges); Cognee stores them under salted tenant/repo/rev
  datasets with retrieval-only guards. No LLM in extraction, ever.
- KEEP: `DomainAttributionPort` on the Neo4j adapter until a re-verdict.
- DECLINE: `cognify()` for code, custom chunkers for code, `CODE`/`CHUNKS`/
  `SUMMARIES` search, NL-mediated `CYPHER` — all falsified by the spike.
- GATE: every other Cognee knob (cognify scheduling, summarization, embedding
  configs) stays default-off; the spike's `nvidia_nim` dimensions workaround
  is script-only and must never migrate into platform code.

## Evidence (spike findings, all reproduced live)

1. The `chunker=` seam is document-route only: `.py` files route to enola
   code-graph tasks, so a custom AST chunker never runs on code (proven: our
   working `TreeSitterChunker` produced 4 AST chunks standalone, zero via
   `cognify`).
2. Enola yielded generic `Node`s (10 for 5 files), zero `CodeSymbol`
   entities — `CODE` search cannot resolve seeds against this.
3. `CHUNKS`/`SUMMARIES` empty for code (nothing chunked → LanceDB tables
   absent); `CYPHER` search type routes NL through an LLM interpreter.
4. Hand-built path already covers the observed battery failures (exact
   lookup, callers, history); semantic recall has no proven miss to fix yet.

## Consequences

- Positive: zero LLM/embed spend on code recall; determinism (the product)
  untouched; citation grounding (`repo@rev:path#line` joins) unchanged.
- Negative: fuzzy concept location ("where is auth handled?") stays
  unserved — accepted explicitly; revisit only via a recall-leg bake-off
  (Cognee chunks vs pgvector-direct vs BM25) IF a battery failure demands it.
- Neutral: `scripts/cognee_spike.py` retained as the regression harness for
  future Cognee versions.

## Revisit triggers (this decline must not fossilize)

- A Cognee release routes code through custom chunkers (re-run spike).
- A battery PARTIAL/FAIL is shown fixable *only* by semantic recall.
- New item tracked in `docs/IAP_Arch_Eng_Recommendations.log`.

## References

- Spike: `scripts/cognee_spike.py` (ingest/probe/leakage, tree-sitter chunker)
- Ledger: Cognee spike verdict entry, 2026-10-17 session
- Related: architecture §10 (R1/R2/R4), §13 (compression), §15 (sharing)
