# RS — Budgeted gathering and cheap-then-expensive reasoning (2026-10-29)

- Research date: 2026-10-29. Claims checked against current sources via web search at that time.
- Informs: DES-collect-frontier-v1.0, DES-distill-brief-v1.0, DES-reason-workorders-v1.0, DES-S7-handoff-v1.0.

## 1. Value of Information and optimal experimental design

Lindley (1956): experiment value = expected entropy reduction (EIG). Howard (1966): VOI = expected decision-utility gain; EIG is the information special case. Operational form for "which fetch next": maintain hypothesis distribution P(H), score each fetch by EIG/cost (or EIG − λ·cost), pick argmax, update, repeat (sequential BOED). Deployable approximations: myopic greedy (one-step, standard practice, with new approximation-ratio results), submodular batch greedy (1−1/e guarantee for set selection), Thompson sampling, knowledge gradient (decision-aware, closer to true VOI). Avoid: non-myopic planning (exponential), exact nested-Monte-Carlo EIG over LLM posteriors (intractable).

## 2. Active-learning query strategies transferred to fetching

Uncertainty sampling (fetch the artifact the investigator is most uncertain about given current hypotheses); query-by-committee (diverse cheap judges vote, fetch max-disagreement evidence); expected model change (predicted belief shift); batch-mode diversity and cost-sensitive variants. Caveat: raw LLM confidence is miscalibrated — calibrate on labeled incidents or use a cheap classifier head.

## 3. Retrieve-then-rerank and context distillation

RECOMP (2023): extractive compression to ~5% tokens on QA at <2–4 EM loss; abstractive better only for single-source synthesis (multi-hop chains stay extractive). LLMLingua/LongLLMLingua (2023/2024): up to 20× compression; question-aware scoring and relevance ordering matter. Provence (2025): unified rerank+prune head, 50–80% pruned at ~zero QA drop, pruning ~free inside the rerank step — current best practice for QA briefs. Measure: compression ratio, downstream EM/F1 retention, citation faithfulness (RAGAS-style); sweep thresholds to draw the frontier.

## 4. Cascade/triage architectures

FrugalGPT (2023): cascade over models with learned router, up to 98% cost reduction at matched quality; diversity metric (MPI) quantifies headroom. AutoMix (2023): cheap draft + self-verification + POMDP escalation router. RouteLLM (2024): router zoo with win-probability thresholds. Threshold calibration on held-out cost-vs-quality Pareto; guards: overconfident cheap tier (dominant residual loss — require disagreement concurrence and audit escalation), correlated errors (genuinely diverse cheap tier), distribution shift (OOD → default-escalate).

## 5. Budgeted graph exploration and stopping

No incident-specific budgeted-search literature exists; transfer from focused crawling (relevance-scored frontier), best-first/budgeted search (UCT/MCTS analogues), and information foraging (marginal-value patch-leaving). Stopping on first of: budget, fixpoint, diminishing returns, depth/TTL cap, recall-saturation estimate, or confidence-with-corroboration.

## 6. Proposed 3-plane shape at survey time

Collect (deterministic, wide, best-first frontier by myopic EIG/cost with diversity) → distill (rerank → prune → extractive brief, 5–10% tokens, faithfulness floor) → reason (brief-only, work orders back to collection, escalation-gated). Governed by: recall-at-brief vs compression frontier; decision quality per dollar; faithfulness floor.
