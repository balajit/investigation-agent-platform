# DES-distill-brief v1.0

- Status: ACCEPTED
- Satisfies: REQ-investigation-loop-reengineering-v1.0 (G2, G3)
- Implements: ADR-PLANE-001 (distill plane)

## 1. Pipeline

Rerank (cross-encoder class) → sentence-level prune (target 50–80% pruned at ~zero answer loss) → extractive brief with citations, corroboration counts, and a dissent section. Selective pass-through: artifacts adding nothing emit empty. Query-focused abstractive synthesis only where single-source summarization suffices; multi-hop chains stay extractive.

## 2. Operating point

Target envelope 5–10% of raw tokens. Governed by one master curve: recall-at-brief vs compression ratio, swept per incident class; operating point at recall ≥ 0.95 or class target. Faithfulness (brief claims entailed by cited raw artifacts) is a hard gate: no compression ratio is acceptable below it.

## 3. Ordering and budgets

Relevant content ordered front (anti lost-in-the-middle); dynamic per-artifact budgets (relevant docs keep more tokens). Dedupe by content fingerprint before ranking.

## 4. Feedback

Brief usage signals (used/unused/wrong claims) flow back to collector and distiller scoring. Distill threshold recalibrated per workload shift.
