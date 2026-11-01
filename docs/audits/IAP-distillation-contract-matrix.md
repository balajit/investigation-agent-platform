# IAP Distillation Contract Matrix

Source contract: `docs/design/DES-distill-brief-v1.0.md:7-21`. The matrix distinguishes the existing context compiler from the accepted distillation-quality contract.

| Requirement | Accepted behavior | Current evidence | Result | Risk/gap |
| --- | --- | --- | --- | --- |
| Selection | Select relevant evidence with provenance | `context_compiler.py:379-433` | Implemented in bounded form | Selection quality is not measured against recall |
| Tiering | Preserve bound/high-value evidence ahead of lower-value context | `context_compiler.py:384-399` | Implemented | Tiering is not equivalent to faithfulness validation |
| Cross-encoder reranking | Rerank evidence beyond lexical/heuristic selection | No implementation symbol or caller found; study contract in `DES-production-distillation-study-v1.0.md` §5B | Not established; study defined | Ranking may miss semantically decisive evidence |
| Sentence-level pruning | Prune at sentence granularity while preserving support | No implementation symbol or caller found; study contract in `DES-production-distillation-study-v1.0.md` §5C | Not established; study defined | Excerpts may be too coarse or omit decisive context |
| Extractive brief | Produce an extractive brief with citations and structured support | `context_compiler.py` now emits deterministic `evidence_brief` (`evidence-brief-v1`) with evidence units, citations, dissent, compression accounting, digest, and structural faithfulness status | Implemented structural slice; semantic quality unverified | Brief is still a projection and not proof authority |
| Citation preservation | Retain evidence IDs/provenance through the brief | `evidence_brief.citations` derives from visible evidence units; source/provenance remains in each unit | Implemented structural slice | Full reasoning/replay path remains unverified |
| Dissent/contradiction | Preserve dissent and contradictory evidence | No dedicated dissent output in the compiler was identified | Partially implemented elsewhere at most | Distillation can erase disagreement while retaining a summary |
| Raw-token reduction | Operate at the accepted 5–10% raw-token point | Brief reports raw/brief token estimates and compression ratio; no labeled operating-point sweep exists | Measurement instrumentation added; target unverified | Ratio alone does not establish quality |
| Recall-at-brief | Measure retained relevant evidence | No measurement path found; labeled-corpus and curve protocol in `DES-production-distillation-study-v1.0.md` §§5A, 7 | Not established; measurement study defined | Reduced context may silently lose required evidence |
| Faithfulness | Treat faithfulness as a hard gate | Structural `evidence_brief.faithfulness_status`/failure code are emitted; semantic adjudication and authoritative brief gating are not implemented | Structural slice implemented; semantic gate not established | Unsupported or altered meaning can reach reasoning until semantic gate integration |
| Used/unused/wrong feedback | Feed claim usage and correctness back into selection | No feedback path found; event contract in `DES-production-distillation-study-v1.0.md` §5F | Not established; study contract defined | Distiller cannot learn from downstream evidence use or errors |
| Determinism/replay | Stable output for identical input and replay | Brief canonical digest is deterministic; focused compiler test covers identical input equality | Implemented pure slice; workflow replay unverified | Temporal replay and model-path evidence remain open |

## Production source path

```text
RunInvestigationWorkflow
  -> reason_activity
  -> ReasoningCoordinator._reason_inner
  -> compile_reasoning_context
  -> LLM gateway
```

Evidence: `application/worker/workflows/run_investigation.py:812-835`, `application/worker/activities/reasoning.py:521-730`, and `application/investigation/reasoning.py:1258-1271,1419-1473`.

## Focused evidence

`tests/unit/test_context_compiler.py` covers source bodies, tiering, truncation, bound evidence protection, deterministic shedding, and purge ledgers. These tests establish current compiler behavior, not the full accepted distillation contract.

## Required closure evidence

1. Approve or revise the study contract for reranking, sentence pruning, brief shape, dissent, and feedback.
2. Raw-token reduction and recall-at-brief measurement without inventing thresholds beyond the accepted design.
3. Faithfulness validation as an explicit gate.
4. Citation and contradiction preservation tests.
5. Replay-consistent production-path evidence.
6. Labeled-corpus evidence for the selected operating point and an explicit implementation authorization pass.
