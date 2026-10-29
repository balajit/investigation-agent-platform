# DES-S4-qualification v1.0

- Status: ACCEPTED
- Satisfies: REQ-investigation-loop-reengineering-v1.0 (S4, G3)
- Implements: IAP-investigation-architecture-v2.0 (stage map)

## 1. Range qualification

A code row qualifies iff all four pinned fields are present: file path, line-start, line-end, pinned revision. Start-line-only rows are not ready. Rationale: the previous start-line-only test admitted schema-legal but unusable rows.

## 2. History qualification

Positive history rows qualify iff: type is commit-history, source path and revision present, not discovery-only, zero-match excluded, and scope-relevant to the requested history scope. Complete empty-history proofs qualify on a separate branch requiring persisted walk-completeness, searched tip, structured scope, and zero match count. Prose-only snippet matches no longer qualify (after provider inventory confirms no dependent flow).

## 3. Runtime and contrast qualification

Runtime rows qualify on window pinning (inside the investigation window) and source identity. Contrast pairs qualify jointly: both halves present and window-ordered (old half at/before G, new half at/after T).

## 4. Readiness stays readiness

Qualification never judges answer quality. Thin banks (e.g., a single uninformative root commit) pass readiness and fail downstream with a targeted next fetch or honest abstention.
