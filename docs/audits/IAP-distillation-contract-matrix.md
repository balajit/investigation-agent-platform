# IAP Distillation Contract Matrix

Source contract: `docs/design/DES-distill-brief-v1.0.md:7-21`. The matrix distinguishes the existing context compiler from the accepted distillation-quality contract.

| Requirement | Accepted behavior | Current evidence | Result | Risk/gap |
| --- | --- | --- | --- | --- |
| Selection | Select relevant evidence with provenance | `context_compiler.py:379-433` | Implemented in bounded form | Selection quality is not measured against recall |
| Tiering | Preserve bound/high-value evidence ahead of lower-value context | `context_compiler.py:384-399` | Implemented | Tiering is not equivalent to faithfulness validation |
| Cross-encoder reranking | Rerank evidence beyond lexical/heuristic selection | No implementation symbol or caller found | Not established | Ranking may miss semantically decisive evidence |
| Sentence-level pruning | Prune at sentence granularity while preserving support | No implementation symbol or caller found | Not established | Excerpts may be too coarse or omit decisive context |
| Extractive brief | Produce an extractive brief with citations and structured support | Compiler emits excerpts/context sections; no distinct accepted brief object found | Partially implemented | Context compilation may be mistaken for the required brief artifact |
| Citation preservation | Retain evidence IDs/provenance through the brief | Evidence IDs/provenance are preserved in compiler output | Partially implemented | End-to-end citation preservation in a generated brief is not proven |
| Dissent/contradiction | Preserve dissent and contradictory evidence | No dedicated dissent output in the compiler was identified | Partially implemented elsewhere at most | Distillation can erase disagreement while retaining a summary |
| Raw-token reduction | Operate at the accepted 5–10% raw-token point | Internal estimated token ceiling exists at `context_compiler.py:65-67,812-876`; no raw-token ratio measurement found | Not established | Compression cannot be evaluated against the accepted operating point |
| Recall-at-brief | Measure retained relevant evidence | No measurement path found | Not established | Reduced context may silently lose required evidence |
| Faithfulness | Treat faithfulness as a hard gate | No distillation-specific faithfulness gate found | Not established | Unsupported or altered meaning can reach reasoning |
| Used/unused/wrong feedback | Feed claim usage and correctness back into selection | No feedback path found in compiler/reasoning composition | Not established | Distiller cannot learn from downstream evidence use or errors |
| Determinism/replay | Stable output for identical input and replay | Compiler has deterministic budgeting tests; no complete distillation replay proof found | Partially implemented | Full workflow replay remains unverified |

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

1. A contract decision for reranking, sentence pruning, brief shape, dissent, and feedback.
2. Raw-token reduction and recall-at-brief measurement without inventing thresholds beyond the accepted design.
3. Faithfulness validation as an explicit gate.
4. Citation and contradiction preservation tests.
5. Replay-consistent production-path evidence.
