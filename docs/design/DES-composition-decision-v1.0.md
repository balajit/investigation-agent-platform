# DES-composition-decision v1.0

- Status: DRAFT (decision pending measurement inputs)
- Satisfies: REQ-investigation-loop-reengineering-v1.0 (G1, G2)
- Implements: ADR-PLANE-001

## 1. Candidates

- A: trace-spectrum ranking first (pre/post-onset trace contrast over code ranges), mechanism-shift confirmation second, propagation walk for the human-readable path.
- B: mechanism-shift first on instrumented metrics, spectrum ranking as fallback where traces lack attribution.

## 2. Deciding inputs (research-gated, not yet measured)

Trace→range attribution coverage (do spans carry code filepath/line), retention reach for pre-onset mechanism estimation, parent-set curation cost per metric family. Candidate A needs attribution; candidate B needs metric parents and longer G windows.

## 3. Decision rule

Score both on the three governing metrics (DES-measurement) once the inputs are measured. Data-gating decides before merit: any layer whose required telemetry is absent is specified as data-gated (activates on presence, degrades explicitly otherwise) rather than chosen or rejected. Code-side layers build unconditionally; runtime layers unlock per the telemetry contract.
