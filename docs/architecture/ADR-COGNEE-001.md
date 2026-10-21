# ADR-COGNEE-001: Cognee as Projection-Only Graph Store

- **Status:** Accepted (amended 2026-10-20 — proposal record appended; decision unchanged)
- **Date:** 2026-10-17
- **Amendment:** 2026-10-20
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

## Proposal record (2026-10-20, external proposal, working notes unshipped)

A later external review proposed layering Cognee over the validated topology as a
semantic discovery and reasoning engine, with an adapter plus a verify-and-enforce
gateway. Each item was dispositioned below so future readers need not re-litigate
the proposal. The decision above is unchanged: only the ADOPT rows are live, and
the fallback design in `docs/IAP-Cognee-integration-design.md` stays behind
`IAP_COGNEE_ENABLED` until the update gate in that doc passes.

| # | Proposal item | Verdict | Reason |
| --- | --- | --- | --- |
| P1 | Adapter translates validated `ASTTopologyPayload` into Cognee `DataPoint`s; Cognee acts as semantic enrichment over our ground truth | ADOPT | Already the Part 8 shape (`map_payload_to_points` → `add_data_points(graph_only=True)`). Ground-truth-first ordering preserved. |
| P2 | Verify-and-enforce gateway: fuzzy output is an untrusted hint, structural truth has veto, verified nodes re-looked-up before reasoning | ADOPT | Compatible with the evidence gateway; adopted as structural-first-with-fuzzy-fallback (see below), never fuzzy-first for claims. |
| P3 | `metadata` identity/index fields on the projection class | ADOPT, adapter-only | `TopologySymbolPoint.metadata` carries `identity_fields`/`index_fields`. Never on domain models. |
| P4 | Domain models (`CodeSymbol`, `ASTNodeIdentity`) inherit Cognee `DataPoint` | DECLINE | Would pull the `cognee` dependency and vector-indexing behavior into the domain, break deterministic UUIDv5 identity, and kill the dependency-free test mirror. The adapter owns all `DataPoint` subclasses. |
| P5 | `cognify()` / `cognee.add()` on AST payloads; LLM-derived `CodeFlow` with `flow_entry` / `flow_stage` / `flow_route` edges | DECLINE | Falsified by the spike: `add()` normalizes toward text extraction, `cognify()` runs LLM graph extraction over deterministic structure, the `chunker=` seam is document-route only for code, and enola yields generic nodes. No consumer exists for LLM-derived flows; `ROUTE` / `MESSAGE_HANDLER` plus trace-corroborated hops cover the proven cases. |
| P6 | Vectors/embeddings over AST nodes by default | DECLINE until benchmarked | Pilot is `graph_only=True` by design (zero embed spend). Enabling vectors is a cost, latency, and cross-tenant-leakage decision gated by the benchmark in `docs/IAP-Cognee-integration-design.md`. |
| P7 | Agent calls `cognee.search()` directly, including `*_COMPLETION` types | DECLINE | Bypasses authorization, sanitization, provenance, and persistence; LLM-written strings must never become evidence. Reads stay behind `RETRIEVAL_ONLY_SEARCH_TYPES` with `ALLOW_COMPLETION=false`. |
| P8 | `CodeSymbol` / `CodeFlow` / Modules / Services-Routes-Storage auto-extraction as the code model | DECLINE as extraction source | Our tree-sitter walk plus `TopologyNodeType` remains the extraction authority. Cognee stores projections; it does not parse. |

## Fuzzy placement decision (2026-10-20)

Two orderings were considered for the gateway. **Adopted: structural-first with
fuzzy fallback.** Deterministic recall (symbol lookup, callers/callees, ownership,
history) runs first; only defined miss classes (zero exact hits, ambiguous
overload/symbol, concept-location question shape with no symbol anchor) escalate
to a retrieval-only Cognee lookup inside the evidence gateway, whose candidates
are re-verified against `ASTTopologyPayload`/Neo4j before enriching anything.
Fuzzy-first (query Cognee, then verify) is permitted only as orientation — a
labeled, non-citable hint in the style of `hub_orientation` — because the `CYPHER`
search type itself routes NL through an LLM interpreter, invoking an LLM before
any structural truth exists. In all cases fuzzy output is never banked as
`Evidence` rows. Rationale and miss taxonomy live in
`docs/IAP-Cognee-integration-design.md`.

## References

- Spike: `scripts/cognee_spike.py` (ingest/probe/leakage, tree-sitter chunker)
- Ledger: Cognee spike verdict entry, 2026-10-17 session
- Proposal reviewed: external Cognee proposal (2026-10-20 session; working notes, not a committed contract)
- Follow-up designs: `docs/IAP-Source-Code-model-enhancement.md`, `docs/IAP-Cognee-integration-design.md`
- Related: architecture §10 (R1/R2/R4), §13 (compression), §15 (sharing)
