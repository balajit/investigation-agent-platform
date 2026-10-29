# DES-log-layout-discovery v1.0

- Status: ACCEPTED
- Satisfies: REQ-investigation-loop-reengineering-v1.0 (G4, C5)
- Implements: IAP-investigation-architecture-v2.0 (evidence hierarchy)

## 1. Pipeline (cheap plane, read-only)

Sampler (stratified by service name: N records per service per endpoint over recent window W; paginated, capped) → analyzer → writer. No LLM anywhere in this path.

## 2. Analysis

Field inventory with dotted-object expansion and depth-capped descent into string-encoded JSON payloads. Deterministic slot filling: timestamp, message, service, severity (+ observed value-normalization map), trace correlation, code location (absence recorded as the attribution gap). Identifier discovery: known-alias matching first, then open discovery by value-shape plus cardinality analysis (near-unique, cross-service = join key), scored on uniqueness × joinability × alias similarity into the identifier budget. Mapping records names and shapes only — raw values never persisted.

## 3. Default writing and operator edits

Discovery writes the default mapping document directly with `managed_by: auto` plus per-field provenance (confidence + match basis). Auto-runs rewrite only auto-managed documents; hand-edited documents (any edit implies `managed_by: manual`) receive a sidecar suggestion diff, never an overwrite. No certification workflow in v1.

## 4. Drift

Scheduled re-sample plus mapping-miss trigger. Auto-managed rewrites log INFO with the field diff; structural breaks (a canonical slot loses all candidates, wholesale layout change) log ERROR. Manual mappings get sidecar + ERROR, never auto-rewrite.

## 5. Downstream feeds

Mapping documents unblock runtime queries; the identifier scoreboard feeds readiness (join-key coverage) and the per-service effectiveness contract; the absence inventory feeds the customer ingest prerequisite spec.
