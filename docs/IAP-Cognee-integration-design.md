# Design: Cognee Integration Boundary

## Context

The platform already has a narrow, proven Cognee path and an explicit verdict constraining it. `src/investigation_agent_platform/infrastructure/topology/cognee_adapter.py` projects validated `ASTTopologyPayload`s into `TopologySymbolPoint` `DataPoint`s via `map_payload_to_points` and writes them with `add_data_points(graph_only=True)` under opaque salted datasets (`derive_dataset_id`), behind `IAP_COGNEE_ENABLED=false` default. Reads are retrieval-only (`RETRIEVAL_ONLY_SEARCH_TYPES` with non-overridable `ALLOW_COMPLETION=false`), and attribution serving stays on the Neo4j adapter until a re-verdict. `docs/architecture/ADR-COGNEE-001.md` records the accepted decision plus, since the 2026-10-20 amendment, the full P1–P8 disposition of the external proposal.

That proposal's durable insight is the gateway ordering: fuzzy semantic discovery as an untrusted hypothesis generator with structural truth holding veto power, and fuzzy output never leaving the evidence gateway. Its overreach — domain models inheriting `DataPoint`, `cognify()` over AST data, direct agent search, LLM-derived flows, default vectors — is declined in the ADR and stays declined here.

This design wires the adopted subset into the investigation loop as a structural-first fallback: deterministic recall runs first, defined miss classes escalate to a retrieval-only Cognee lookup inside the evidence gateway, candidates are re-verified against the topology authority, and only verified identities enrich reasoning. No claim, evidence row, or conclusion may originate from fuzzy output.

## References

- [docs/architecture/ADR-COGNEE-001.md](./architecture/ADR-COGNEE-001.md) — Adopted projection-only verdict, spike evidence, and the P1–P8 proposal record this design implements without reopening.
- External review (working notes, unshipped) — The adapter, dual-engine, and verify-and-enforce patterns adopted here; the declined items stay declined per the ADR (P1–P8 disposition table).
- [docs/IAP-Source-Code-model-enhancement.md](./IAP-Source-Code-model-enhancement.md) — Deterministic identity and edge shapes the fallback re-verifies against; companion design, no shared code changes.
- [Cognee custom data models](https://docs.cognee.ai/guides/custom-data-models) — Verified `add_data_points` semantics: `DataPoint`-valued fields become edges, scalars stay properties, only `metadata.index_fields` are embedded, and standalone calls leave nodes globally unassociated unless a pipeline context links the dataset; informs the dataset, edge-label, and vector-off decisions below.

## Goals / Non-Goals

**Goals:**

- Structural-first recall with a typed fuzzy fallback inside the evidence gateway for defined miss classes only.
- Every fuzzy candidate re-verified against `ASTTopologyPayload`/Neo4j node identity before enriching anything.
- Fuzzy output never banked as `Evidence`, never cited, never concluded from.
- Bounded cost: fallback fires on miss classes, with per-run caps and telemetry.
- Tenant and revision isolation preserved end to end (salted datasets, scoped reads, janitor drops).
- An explicit benchmark gate for ever updating ADR-COGNEE-001.

**Non-Goals:**

- Fuzzy-first claim paths, direct agent `cognee.search()`, or any `*_COMPLETION` output reaching reasoning.
- `cognify()` on code, custom code chunkers, LLM-derived flows, or default vector indexing of AST data.
- New persistence tables, new REST endpoints, or serving attribution from Cognee.
- Semantic recall over docs, tickets, or post-mortems (a later track, if ever, with its own design).
- Reopening the P4–P8 declines.

## Decisions

### D1: Structural-first fallback with a closed miss taxonomy

**Decision:** Deterministic recall always runs first. The gateway escalates to Cognee only on a closed set of miss classes, each with a distinct reason code. Anything outside the taxonomy never triggers fallback, no matter how vague the question.

```python
# src/investigation_agent_platform/application/evidence/code_recall.py
from __future__ import annotations

from enum import StrEnum


class CodeRecallMiss(StrEnum):
    NO_MISS = "NO_MISS"
    ZERO_EXACT_HITS = "ZERO_EXACT_HITS"
    AMBIGUOUS_SYMBOL = "AMBIGUOUS_SYMBOL"
    AMBIGUOUS_OVERLOAD = "AMBIGUOUS_OVERLOAD"
    CONCEPT_LOCATION_NO_ANCHOR = "CONCEPT_LOCATION_NO_ANCHOR"


CONCEPT_LOCATION_MARKERS: tuple[str, ...] = (
    "where is",
    "where are",
    "which service handles",
    "who owns",
    "handles rate limiting",
    "handles auth",
)


def classify_code_recall_miss(
    question_shape: str,
    exact_hits: int,
    candidate_count: int,
    problem: str,
) -> CodeRecallMiss:
    """Closed miss taxonomy: only these classes may escalate to fuzzy fallback."""
    lowered = f" {(problem or '').lower()} "
    if exact_hits > 0:
        return CodeRecallMiss.NO_MISS
    if candidate_count > 1:
        return CodeRecallMiss.AMBIGUOUS_SYMBOL
    if question_shape == "code" and any(m in lowered for m in CONCEPT_LOCATION_MARKERS):
        return CodeRecallMiss.CONCEPT_LOCATION_NO_ANCHOR
    if exact_hits == 0:
        return CodeRecallMiss.ZERO_EXACT_HITS
    return CodeRecallMiss.NO_MISS
```

Overload ambiguity is reported by the resolver layer (unique-match-or-abstain) and mapped to `AMBIGUOUS_OVERLOAD` at the call site; the classifier above handles the shape-level classes. A miss outside this enum is a code bug, not a fallback.

**Alternative considered:** Fuzzy-first for all concept questions (Gemini's gateway as written). Rejected — pays vector/LLM cost on every vague query, exposes the reasoner to untrusted candidates first, and routes NL through the LLM-backed `CYPHER` interpreter before any structural truth exists.

---

### D2: Fallback executes inside the gateway with retrieval-only guards

**Decision:** The fallback is a gateway-internal function, not an agent action. It reuses `assert_retrieval_only`, favors `CODE` and `CHUNKS_LEXICAL` search types, treats `CYPHER` as LLM-mediated and disfavored (allowed only when explicitly requested with a logged reason), and caps candidates per call and per run. Completion types remain unreachable by construction through the existing `ALLOW_COMPLETION=false` constant.

```python
# src/investigation_agent_platform/application/evidence/code_recall.py
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from uuid import UUID

logger = logging.getLogger(__name__)

FALLBACK_SEARCH_ORDER: tuple[str, ...] = ("CODE", "CHUNKS_LEXICAL", "CHUNKS", "SUMMARIES")
FALLBACK_CANDIDATE_CAP = 10
FALLBACK_RUN_CAP = 3


@dataclass
class FuzzyFallbackOutcome:
    miss: CodeRecallMiss
    candidates_seen: int = 0
    verified_node_ids: list[UUID] = field(default_factory=list)
    discarded_count: int = 0
    search_types_used: list[str] = field(default_factory=list)


async def fuzzy_fallback_candidates(
    query_text: str,
    dataset: str,
    run_used: int,
    search_fn: object,
) -> FuzzyFallbackOutcome:
    """Gateway-internal fuzzy lookup. Returns verified identities only downstream.

    `search_fn` is an injected retrieval-only search callable so this logic is
    unit-testable without Cognee. Callers re-verify returned IDs against the
    topology authority; this function never returns symbols, only IDs.
    """
    from investigation_agent_platform.infrastructure.topology.cognee_adapter import (
        assert_retrieval_only,
    )

    outcome = FuzzyFallbackOutcome(miss=CodeRecallMiss.ZERO_EXACT_HITS)
    if run_used >= FALLBACK_RUN_CAP:
        return outcome
    seen: list[str] = []
    for search_type in FALLBACK_SEARCH_ORDER:
        assert_retrieval_only(search_type)
        results = await search_fn(query_text=query_text, search_type=search_type, dataset=dataset)  # type: ignore[operator]
        outcome.search_types_used.append(search_type)
        for row in results or []:
            node_id = (row or {}).get("node_id")
            if node_id and node_id not in seen:
                seen.append(str(node_id))
            if len(seen) >= FALLBACK_CANDIDATE_CAP:
                break
        if seen:
            break
    outcome.candidates_seen = len(seen)
    return outcome
```

`CYPHER` is deliberately absent from `FALLBACK_SEARCH_ORDER`; adding it later requires its own design note and an explicit per-call reason, because verified Cognee docs and our spike agree it interprets NL with an LLM.

**Alternative considered:** Exposing fallback as a reasoner action (`SEARCH_SEMANTIC`). Rejected — agent-invokable fuzzy search escapes budget control and invites direct citation of unverified candidates.

---

### D3: Re-verification against the topology authority

**Decision:** Candidate `node_id`s are looked up in the revision-pinned authority (the ingested `ASTTopologyPayload` node index or the Neo4j adapter read path) with tenant, repository, and revision equality. Anything unmapped, revision-mismatched, micro-tier, or path-unsafe is discarded with a counter. Verified IDs enrich the deterministic result set (e.g. as additional range-read targets); the fuzzy payload itself — scores, snippets, summaries — is dropped at the boundary.

```python
# src/investigation_agent_platform/application/evidence/code_recall.py
from __future__ import annotations

from uuid import UUID


def verify_fuzzy_candidates(
    candidate_ids: list[str],
    authority_index: dict[str, object],
    tenant_id: str,
    repository_id: str,
    revision: str,
) -> tuple[list[UUID], int]:
    """Keep only IDs present in the revision-pinned authority. Returns (kept, discarded)."""
    kept: list[UUID] = []
    discarded = 0
    for raw in candidate_ids:
        node = authority_index.get(str(raw))
        if node is None:
            discarded += 1
            continue
        if (
            getattr(node, "tenant_id", None) != tenant_id
            or getattr(node, "repository_id", None) != repository_id
            or getattr(node, "revision", None) != revision
        ):
            discarded += 1
            continue
        if getattr(node, "granularity", "macro") != "macro":
            discarded += 1
            continue
        try:
            kept.append(UUID(str(raw)))
        except ValueError:
            discarded += 1
    return kept, discarded
```

The authority index is built from the same payload the deterministic path uses, so fuzzy and exact recall share one definition of truth. A fully discarded fallback is a normal outcome (logged, counted), never an error and never abstention evidence.

**Alternative considered:** Trusting Cognee payload fields (paths, lines) directly. Rejected — that is precisely the hallucination vector (invented paths, wrong lines) the gateway exists to close.

---

### D4: Orientation-only channel for fuzzy-first steering

**Decision:** The one permitted fuzzy-first use is orientation: a labeled, non-citable hint object attached to reason context (precedent: `hub_orientation`), carrying candidate symbols and an explicit `unverified=True` marker. Orientation may steer which deterministic reads fire next; it may not be cited by claims, banked as evidence, or referenced by conclusions. The validator rejects any claim whose only support traces to orientation.

```python
# src/investigation_agent_platform/application/evidence/code_recall.py
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class FuzzyOrientation(BaseModel):
    """Non-citable steering hint. Never evidence, never cited, never concluded from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unverified: bool = True
    miss: str = Field(..., max_length=64)
    suggested_symbols: list[str] = Field(default_factory=list, max_length=10)
    steering_note: str = Field(default="", max_length=512)
```

**Alternative considered:** No fuzzy-first at all. Rejected — operators explicitly want concept-steering for anchorless questions, and an orientation channel with validator enforcement captures the value without the claim risk.

---

### D5: Telemetry and budget discipline

**Decision:** Every fallback emits typed counters through the existing observability hooks: miss class, search types used, candidates seen, verified kept, discarded count, and run-budget exhaustion. The per-run cap (`FALLBACK_RUN_CAP = 3`) and per-call cap (`FALLBACK_CANDIDATE_CAP = 10`) are code constants, not env knobs, matching the `ALLOW_COMPLETION` precedent — budget escapes must require a code change and review, not a flag flip.

**Alternative considered:** Env-configurable caps. Rejected — silent budget expansion in production is how a bounded fallback becomes an unbounded spend path.

---

### D6: Writes stay graph-only; vectors stay gated

**Decision:** Projection continues through `map_payload_to_points` → `add_data_points(graph_only=True)` exactly as built. Enabling vector indexing (any `index_fields` embedding beyond the current `qualified_name` metadata, any embedder configuration) requires passing the update gate in D7 first, plus a leakage test proving tenant datasets do not cross-read and a cost note with per-revision projection spend. Dataset derivation (`derive_dataset_id`), janitor drops (`drop_projection`), and the `IAP_COGNEE_ENABLED` default-off flag are unchanged.

**Alternative considered:** Turning on embeddings now for concept recall quality. Rejected — no battery miss yet demands it (spike finding 4 stands), and verified Cognee docs confirm `add_data_points` embeds `index_fields` into vector storage, which is a spend and isolation decision, not a metadata nicety.

---

### D7: ADR update gate

**Decision:** `ADR-COGNEE-001` may be revisited only on this evidence, in this order: a battery `PARTIAL`/`FAIL` reproduced as fixable only by semantic recall, measured on `tests/benchmarks/test_topology_enrichment.py` with precision, latency, cost, cross-tenant leakage, and replay-determinism reported separately; no metric improves by weakening verification; the revision is recorded as a new ADR amendment with its own proposal table, not an edit of history.

**Alternative considered:** Time-based re-evaluation ("revisit in Q1"). Rejected — calendar triggers re-litigate without evidence; the gate stays failure-driven per the ADR's own triggers.

---

## Data Storage

No new tables, no migration, no new Neo4j labels. Projections reuse `TopologySymbolPoint` and the existing salted datasets. Fallback outcomes are telemetry and in-memory reason context only; nothing about a fuzzy lookup is persisted except aggregate counters through existing observability. The janitor's `drop_projection` path covers fallback-read datasets identically to pilot datasets since they are the same datasets.

## Data Structures

Inputs are the existing `ASTTopologyPayload` node index, the reason-context question shape, and the deterministic recall result. New structures are `CodeRecallMiss`, `FuzzyFallbackOutcome`, and `FuzzyOrientation` in the Decisions above — all frozen, all bounded, none of them evidence. No claim, evidence, or conclusion schema changes: verified IDs flow back into existing range-read and attribution inputs.

## Interfaces

No new REST endpoints, CLI commands, or MCP tools. The pilot CLI (`scripts/cognee_pilot.py --repo/--checkout/--drop`) is unchanged. Internal interfaces:

| Interface | Signature | Description |
| --------- | --------- | ----------- |
| `classify_code_recall_miss` | `(question_shape, exact_hits, candidate_count, problem) -> CodeRecallMiss` | Closed miss taxonomy; only non-`NO_MISS` classes may escalate. |
| `fuzzy_fallback_candidates` | `(query_text, dataset, run_used, search_fn) -> FuzzyFallbackOutcome` | Gateway-internal retrieval-only lookup with caps. |
| `verify_fuzzy_candidates` | `(candidate_ids, authority_index, tenant_id, repository_id, revision) -> (kept, discarded)` | Authority re-verification; drops scores and snippets. |
| `FuzzyOrientation` | frozen model | Non-citable steering hint for reason context. |

## Implementation Detail

Land in this order; each step independently green with the flag default-off:

1. Add `CodeRecallMiss`, `classify_code_recall_miss`, and unit tests over the taxonomy (exact hits suppress fallback; anchorless concept questions escalate; everything else stays `NO_MISS`).
2. Add `fuzzy_fallback_candidates` with an injected search callable; test caps, search-type order, run-budget exhaustion, and `assert_retrieval_only` enforcement without Cognee installed.
3. Add `verify_fuzzy_candidates` with tenant/repo/revision/granularity checks; test unmapped, mismatched, micro-tier, and malformed IDs as discards.
4. Wire the three into the evidence gateway behind `IAP_COGNEE_ENABLED`: deterministic recall → miss classification → capped fallback → verification → verified IDs as range-read targets. Emit the D5 counters at each stage.
5. Add `FuzzyOrientation` to reason context with validator enforcement (orientation-only support fails claim validation); test that a claim citing only orientation never verifies.
6. Extend `tests/benchmarks/test_topology_enrichment.py` with fallback-on/fallback-off comparisons on the fixed fixture set, recording precision, latency, and cost deltas for the D7 gate.

### `docs/dev/architecture.md` changes

No `docs/dev/architecture.md` exists in this project; the architecture source of truth is `docs/IAP-architecture-v1.0.md`. No edits to that file are required by this design. When step 4 lands, append a short paragraph to the topology chapter naming the miss taxonomy and the retrieval-only fallback with a pointer to this design.

## Migrations

None. All new structures are in-memory or telemetry. Dataset, projection, and janitor behavior are unchanged. Rollback is a flag flip (`IAP_COGNEE_ENABLED=false`) plus code revert of the gateway wiring; no stored state depends on the fallback.

## Testing Philosophy

### Miss taxonomy

Test each taxonomy class independently: exact hits suppress fallback even for concept-worded questions; ambiguous symbols map correctly; anchorless concept questions escalate; run-budget exhaustion returns empty without calling search. A question outside the taxonomy must never produce a non-`NO_MISS` result — the taxonomy is closed by test, not by convention.

### Guardrail enforcement

Test that completion search types are rejected before any network call, that `CYPHER` is absent from the default order, and that caps truncate candidate lists deterministically. All of this runs without the `cognee` package installed via the injected search callable.

### Verification strictness

Test every discard class: unmapped IDs, cross-tenant IDs, cross-revision IDs, micro-tier nodes, malformed UUIDs. Test that scores, snippets, and summaries never survive verification — only IDs cross the boundary. A fully discarded fallback asserts normal outcome with counts, not failure.

### Isolation and determinism

Add cross-tenant leakage tests (dataset A queries never surface dataset B nodes) and replay tests (same revision, same query, identical verified sets). These run in the pilot environment with the dependency present and skip cleanly without it, following the existing `test_cognee_pilot_live.py` pattern.

### Benchmark honesty

Fallback-on versus fallback-off comparisons run on the fixed enrichment fixture set with separate precision, latency, and cost numbers. Verification gates stay fixed across both arms; any improvement bought by weakening a gate fails review.

## Documentation Plan

### `docs/IAP-Cognee-integration-design.md`

**Audience:** Implementers and reviewers

This file. It carries the miss taxonomy, the gateway wiring, the guardrails, and the update gate. It is updated as each implementation step lands, with deviations and landing tests recorded. It is the only integration doc implementers need alongside the adapter module and the ADR.

### `docs/IAP-architecture-v1.0.md`

**Audience:** Platform engineers and operators

One short paragraph in the topology chapter when the gateway wiring lands, naming the miss taxonomy and the retrieval-only fallback, pointing here. No other edits from this design.

### `docs/IAP_Arch_Eng_Recommendations.log`

**Audience:** Architecture and engineering stewards

The `OPEN` integration-boundary entry flips to `IMPLEMENTED` as steps land with test evidence, per the ledger rule. A battery miss proven fixable only by semantic recall opens a new `OPEN` item that carries the benchmark numbers into any future ADR amendment.

## Risks / Trade-offs

### Fallback becomes the default path

**Risk:** Reasoners learn that vague questions get answered via fallback, deterministic recall atrophies, and every run pays fuzzy cost with weaker provenance.

**Mitigation:** Closed taxonomy, per-run caps as code constants, miss-class telemetry reviewed against deterministic hit rates, and orientation that steers but never cites. If fallback rates climb without measured precision gains, the fallback is throttled, not the taxonomy widened.

### Candidate anchoring

**Risk:** Even discarded candidates anchor the reasoner if their text leaks into context alongside verified results.

**Mitigation:** Only IDs cross the verification boundary; scores, snippets, and summaries are dropped. Orientation carries symbols with an explicit unverified marker and validator enforcement, never raw fuzzy text.

### Cross-tenant leakage through shared graph

**Risk:** A shared Neo4j Community instance holding multiple tenants' projections could serve one tenant's nodes to another's fallback query on mis-scoped reads.

**Mitigation:** Opaque salted dataset IDs, tenant-filtered reads, dataset-scoped fallback queries, janitor drops per tenant and revision, and dedicated leakage tests. Vectors stay off, removing the second (harder to scope) retrieval surface entirely.

### Benchmark gaming

**Risk:** Pressure to justify semantic recall leads to fixtures or metrics chosen to make the fallback look necessary.

**Mitigation:** Fixed enrichment fixture set, separate precision/latency/cost/leakage/determinism numbers, frozen verification gates across both arms, and the ADR amendment requirement that records its own proposal table for future review.
