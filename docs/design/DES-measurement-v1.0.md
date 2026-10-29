# DES-measurement v1.0

- Status: ACCEPTED
- Satisfies: REQ-investigation-loop-reengineering-v1.0 (G1–G4)
- Implements: IAP-investigation-architecture-v2.0 (feedback edges)

## 1. Battery protocol

Fixed question bank with pre-written expected values, run live, graded by rubric. Retries distinguish transients (instant create-race deaths) from honest abstentions. Per-case records: verdict, stage diagnoses, evidence inventory.

## 2. Shape-aware diagnosis

The stage probe grades with the question's shape (history branches evaluate history rows, not ranges). Known rubric bugs (e.g., negative probes expecting absence scored as "want answered") are fixed in the rubric, not worked around in the pipeline.

## 3. Governing metrics

Recall-at-brief vs compression ratio (the master tradeoff curve); end-to-end decision quality per cost (correct cause + fix location per total spend); faithfulness floor (claim-entailment rate, hard gate). Reported at fixed thresholds with the frontier drawn, never single points.

## 4. Baselines and guards

Current-code-shape baselines are recorded before each work chunk; chunk acceptance requires re-grade movement on the targeted stages plus a negative-probe regression guard (absence behavior must not regress).
