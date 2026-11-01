# DES-production-distillation-study v1.0

- Status: REVIEW
- Satisfies: `docs/requirements/REQ-investigation-loop-reengineering-v1.0` (G2, G3)
- Implements: `docs/architecture/ADR-PLANE-001-collect-distill-reason.md`; `DES-distill-brief-v1.0`
- Work package: WP-07

## 1. Study Boundary

This document records the WP-07 production study. It does not authorize implementation, dependency adoption, model rollout, prompt changes, data migration, or live battery execution. The current production path remains unchanged until the contract decisions and evaluation gates below are approved. The kept `build_evidence_brief` function is an explicitly scoped structural starting point, covered by `tests/unit/test_context_compiler.py:21-35`; it is not the accepted quality contract.

## 2. Current Production Path

```text
RunInvestigationWorkflow
  -> reason_activity
  -> ReasoningCoordinator._reason_inner
  -> compile_reasoning_context
  -> structured prompt envelope
  -> LLM gateway
```

The current production source map is:

| Capability | Current evidence |
| --- | --- |
| Deterministic ordering and bounded pools | `application/investigation/context_compiler.py:379-466` |
| Bound-evidence protection and tiering | `context_compiler.py:384-399,485-520,812-887` |
| Code excerpts with line/provenance metadata | `context_compiler.py:520-578` |
| Log grouping with member evidence IDs | `context_compiler.py:637-677` |
| Dormant-template rendering and budget labels | `context_compiler.py:934-1047` |
| Token budgeting and deterministic shedding | `context_compiler.py:812-913` |
| Visible evidence IDs constrained to the compiled projection | `application/investigation/reasoning.py:1302-1461` |
| Structural brief projection, starting point rather than the accepted contract | `context_compiler.py:919-965,1217`; `tests/unit/test_context_compiler.py:21-35` |

`build_evidence_brief` is the structural starting point, not the accepted contract. It provides deterministic ordering, citation visibility sets, dissent candidates, compression reporting, canonical digest, and structural faithfulness (`CITATION_NOT_VISIBLE`). Its `EVIDENCE_BRIEF_VERSION` tag (`evidence-brief-v1`) is a structural marker for the projection code, not the version of the accepted `EvidenceBrief` contract defined below. It does **not** provide corroboration counts, independent-artifact-type sets, recall fields, drill-down pointers, tenant/investigation identity, revision-pinned provenance, or semantic entailment adjudication. Closing exactly those gaps is Track D/E scope; anything else is scope creep.

## 3. Accepted Contract Gap

The accepted design requires:

1. Reranking in a cross-encoder class or an explicitly approved deterministic equivalent.
2. Sentence-level pruning while preserving citation support.
3. An extractive brief containing evidence IDs, provenance, corroboration counts, and dissent/contradiction sections.
4. A target operating envelope of 5-10% of raw tokens, measured as a curve rather than a single claimed point.
5. Recall-at-brief measurement against labeled relevant evidence, with a class-specific target where the global target is not appropriate.
6. Faithfulness as a hard gate: every brief claim must be entailed by its cited raw artifacts, and a failed gate must prevent the brief from becoming authoritative reasoning input.
7. Used/unused/wrong claim feedback flowing to collection and distillation scoring without changing proof truth.
8. Deterministic output under replay and identical inputs.

## 4. Proposed Brief Contract

The implementation study should define one versioned, JSON-safe `EvidenceBrief` value with:

- `brief_version` and source compiler/model configuration identifiers;
- investigation and tenant identity;
- ordered `claims` or `evidence_units`, each with bounded extractive text;
- one or more raw `evidence_ids` per unit;
- complete source/provenance references for every citation;
- `corroboration_count` and independent-artifact-type set;
- explicit `dissent` entries for contradictory or unresolved evidence;
- `raw_token_estimate`, `brief_token_estimate`, and compression ratio;
- `recall_measurement` fields when a labeled evaluation context exists;
- `faithfulness_status` and bounded failure codes;
- drill-down pointers for omitted raw content;
- deterministic ordering keys and a digest over the canonical brief payload.

The brief is a projection, never a new evidence source. It must not mint evidence IDs, alter raw evidence meaning, promote orientation notes to citations, or replace the existing verification gates.

## 5. Study Tracks

### A. Corpus and labels

- Build a synthetic and repository-safe evaluation corpus from generic technology/role fixtures. The repository currently contains 17 files in `tests/fixtures/battery_cases/`; the plan also references a 105-case battery. Track A must locate and justify the 105-case denominator or correct the number before it becomes a gate.
- The corpus must satisfy the repository's Analyzed-Repository Non-Reference rule: fixtures are synthetic, carry pinned synthetic revisions, and are rebuilt rather than scraped. Precedent: `tests/fixtures/python/manifest.json` uses `synthetic-fixtures-v1`, inline-built evidence, pinned revision `abc123`, and exact-span expectations. Refresh means regenerating synthetic variants, never ingesting banked investigation content; redaction is a safety net, not permission to harvest. Any proposal naming a real repository, host, schema, or user population requires explicit per-instance owner direction recorded in the session log and is otherwise out of scope.
- Label relevant evidence at the claim/group level, including supporting, refuting, dissenting, and required multi-hop chains.
- Include negative cases: empty search, stale evidence, malformed provenance, contradictory evidence, grouped logs, truncated code, and hidden citations.
- Preserve raw artifacts and expected evidence IDs separately from the brief implementation.

### B. Reranking

- Compare the current deterministic relevance/tier order with candidate reranker classes.
- Measure recall before token pruning, latency/cost, determinism, and sensitivity to evidence type.
- Decide whether a cross-encoder dependency is permitted. If not, specify a deterministic feature-based ranking and record that it is an approved substitute, not an implicit cross-encoder claim.
- Reranking may order candidates only; it cannot remove bound evidence or establish proof.

### C. Sentence pruning

- Define sentence segmentation and citation-span mapping for logs, documentation, and code.
- Retain every sentence required by a cited claim, causal chain, dissent explanation, or line-range proof.
- Preserve enough surrounding context to avoid changing assertion polarity, scope, temporal direction, or subject identity.
- Mark omitted sentences and provide drill-down pointers; never silently truncate a decisive range.

### D. Brief and dissent construction

- Separate extractive evidence units from optional single-source summaries.
- Keep multi-hop chains extractive unless each generated sentence passes the faithfulness gate against its cited sources.
- Preserve contradictory evidence even when it loses ranking; dissent is a required output section, not a relevance casualty.
- Keep orientation from knowledge/hub/fuzzy sources explicitly non-citable.

### E. Faithfulness gate

- Define the claim-entailment unit: claim, citation set, polarity, scope, and temporal/revision bounds.
- Start with deterministic structural checks for citation existence, source visibility, revision/path compatibility, and cited-span containment.
- Define the semantic adjudication method separately; it must not use the same generator as the brief without an independent control.
- A failed or unavailable faithfulness check yields a typed non-authoritative brief outcome and routes to raw drill-down or continued collection. Current behavior (structural slice only): the reasoner logs a warning naming the failure code and continues with the bounded compiler context; the brief never enters model input, so a failed brief cannot become authoritative, but no explicit drill-down reroute exists yet.

### F. Feedback

Capture bounded feedback events:

- `used`: a reasoning/verifier decision cited or opened the brief unit;
- `unused`: a retained unit was not used within the decision window;
- `wrong`: a unit was cited but contradicted by later verified evidence or adjudication.

Feedback may update future ranking weights or calibration datasets only. It must not mutate raw evidence, rewrite a prior brief, change a verification result, or make an unsupported claim pass.

## 6. Production Integration Study

The current `compile_reasoning_context` should remain the compatibility seam until the brief contract is proven. The proposed later integration is:

```text
banked evidence + hypotheses + claim bindings
  -> deterministic candidate projection
  -> optional semantic reranker
  -> sentence/citation-preserving pruner
  -> EvidenceBrief
  -> faithfulness gate
  -> ReasoningCoordinator brief-only input
  -> usage feedback events
```

The reasoner must receive the brief plus bounded drill-down pointers, not unrestricted full-lake context. The verifier and conclusion gate continue to read durable raw evidence and claim bindings directly; the brief cannot become proof authority.

## 7. Measurements

The study must report curves and distributions, not one favorable point:

- raw tokens, brief tokens, compression ratio, and retained evidence count;
- recall-at-brief by incident/question class;
- citation retention and dissent retention;
- faithfulness pass/fail/unavailable rate and failure codes;
- decision quality per total collection/reasoning cost;
- reranker/pruner latency and model/provider cost;
- used/unused/wrong feedback rates;
- deterministic digest equality across repeated compilation and replay.

The accepted target is recall at least 0.95 or an explicitly approved class-specific target at the selected operating point, within the 5-10% raw-token envelope, with faithfulness as a hard gate. No threshold may be invented from unit fixtures alone.

## 8. Acceptance Gates

- A versioned `EvidenceBrief` contract exists with citation, provenance, dissent, compression, recall, and faithfulness fields.
- The compiler produces identical canonical output for identical inputs and has replay evidence.
- Bound evidence, refuting evidence, multi-hop chains, and dissent survive pruning tests.
- Recall and faithfulness are evaluated on a labeled corpus with negative controls.
- A faithfulness failure cannot reach the authoritative reasoner path as a successful brief.
- Feedback events are tenant-scoped, bounded, append-only or idempotent, and have no proof authority.
- The reasoner consumes only the brief plus drill-down pointers in the approved integration mode.
- Current behavior remains unchanged until the study gates and a separate implementation authorization pass.

### Baseline Record

- **Artifact:** A versioned JSON manifest committed alongside this study, not under `reports/` or `tmp/`, containing the fixture-set revision, generation command, per-fixture verdicts, recall/compression numbers per incident class, negative-probe results, and a digest.
- **Producer:** One deterministic script or command that regenerates the manifest. Repeated runs must reproduce it byte-identically or explain the delta explicitly.
- **Gate rule:** No implementation PR lands without a clean baseline rerun attached. Updating the recorded baseline requires explicit owner approval with a stated reason; silent re-baselining to green is prohibited.
- **Sequencing:** The baseline must be recorded before the first Track B-F implementation PR. A missing or disputed baseline is a hard stop, not a follow-up item.

## 9. Open Decisions

- Brief schema and versioning strategy.
- Cross-encoder dependency versus deterministic reranking substitute.
- Sentence segmentation/pruning library and language coverage.
- Semantic faithfulness adjudicator and its independence requirement.
- Labeled-corpus ownership, anonymization, and refresh cadence. The corpus must satisfy the repository's Analyzed-Repository Non-Reference rule (synthetic fixtures, pinned synthetic revisions, rebuilt never scraped); any proposal naming a real repository, host, schema, or user population requires explicit per-instance owner direction recorded in the session log.
- Whether recall labels are claim-level, evidence-unit-level, or both.
- Feedback retention, privacy scope, calibration cadence, and collector API.
- Exact authority rollout: shadow brief, dual-read comparison, then brief-only reasoner.

## 10. Non-Claims

This study does not claim that reranking, pruning, semantic faithfulness adjudication, recall measurement, or feedback are implemented. A structural brief projection (`build_evidence_brief`) exists and is tested, but it is not the accepted `EvidenceBrief` contract. It does not claim that the current context compiler satisfies the accepted distillation contract or that WP-07 is ready for production rollout.
