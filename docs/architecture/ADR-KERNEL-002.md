# ADR-KERNEL-002: Four-Identity Scheme with canon/v1 Canonicalization

- **Status:** Accepted
- **Date:** 2026-10-23
- **Deciders:** Sole engineer (owner); assistant as chief architect (draft)
- **Scope:** Proposition, context, derivation, and publication-decision identity across domain, persistence, and audit

## Context

A single hash doing proposition + context + cache-validity work lets unrelated changes silently preserve or destroy proof validity. The design requires four typed IDs over one canonical serialization so each invalidation rule keys off the correct ID. Current `ClaimKind`/`ClaimTarget` (`domain/hypothesis/models.py:35-76`) routes claims but carries no quantifier, scope, assumption, or context identity. Package: `docs/IAP-proof-kernel-design-v1.0.md` §§1.2–1.3.

## Decision

- Canonical proposition schema (predicate, normalized target, form, quantifier, scope, modality, relation, completeness, assumptions) with `canon_version` (`canon/v1`) in every hashed preimage; JSON, UTF-8, sorted keys, no whitespace, float rejection.
- `PropositionID = SHA-256(Canonicalize(C))`; context, derivation, and decision IDs layered as specified in the design doc.
- Identity fixtures pinned before Stage B: predicate strengthening changes proposition ID; line move keeps proposition ID + changes context ID; rename without validated normalization creates a new ID; quantifier variants never collide; scope broadening creates new obligations.
- Persisted derived statuses carry `context_id`; mismatched reads are rejected. `*_validated` flags are certificate references, never independent authority.

## Consequences

- Positive: invalidation, migration, dedup, and cache correctness each key off the correct ID; silent satisfaction transfer becomes impossible by construction.
- Negative: four IDs plumbed through in-memory schema from day one; fixture + migration cost.
- Neutral: `ClaimKind` stays the router; display text is never identity.
