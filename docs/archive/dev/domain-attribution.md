# Domain attribution (dual-frame policy)

Audience: investigation and platform developers. Authority: `docs/IAP-implemenation-part5-v1.md` D8; implementation: `application/topology/attribution.py`.

## Policy

Attribution resolves two frames — the innermost failure frame and its outer caller — to owning domains via static topology, then applies an explicit versioned rule (`attribution_rule_version`, currently `v2`) using runtime boundary evidence. Static ownership proposes candidates; runtime evidence decides fault. The result is `DomainAttributionEvidence` through the gateway; it can never finalize a conclusion (`ConclusionGate` remains authoritative).

## Classifications

| Classification | Meaning |
|---|---|
| `LIBRARY_DEFECT` | Internal invariant failed or defect uncaught inside a shared library; owner is the library domain |
| `CALLER_MISUSE` | Caller violated the contract (invalid input, misuse); owner is the caller domain |
| `NON_LIBRARY_DEFECT` | Non-library code owns the failure frame; owner is the failure-frame domain |
| `INCONCLUSIVE` | Evidence cannot distinguish the above; always preferred over a confident wrong answer |

## Confidence semantics

Confidence is capped by tier: AST-precise matches carry full computed confidence; `SOURCE_FILE`/`REPOSITORY` fallbacks reduce it; the `CODEOWNERS` tier caps at 0.3 and is marked `ownership_source=codeowners`. A 0.3-capped result cannot pass the default 0.8 conclusion threshold (proven by test). Every result records `alternatives`, `limitations`, supporting/contradicting evidence IDs, graph snapshot, revision, parser version, and `resolution_tier` (`macro` graph vs `micro` on-demand parse).

## Evidence requirements

A defect-vs-misuse verdict requires runtime boundary evidence (contract payloads, trace spans, state diffs) linked by evidence ID. Static ownership alone, a CODEOWNERS hit alone, or an uncorroborated cross-repository hop yields at most a lead (`alternatives` + `limitations`), never a verdict.

## Worked examples

- `LIBRARY_DEFECT`: stack shows `payments-lib/charge.py:41` raising on valid input; boundary payload matches the documented schema; caller frame is a thin pass-through. Culprit: library domain.
- `CALLER_MISUSE`: same frame, but the payload violates the schema (negative amount); library docs/validators reject it. Culprit: caller domain.
- `INCONCLUSIVE`: shared-library frame with no boundary payload and contradictory ownership signals. The result names both candidate domains with confidence split and states what evidence would decide it.

## Limitations

Attribution is per revision — historical frames resolve against their own snapshot, never HEAD. Monorepos and shared ownership dilute precision to repository tier. Cross-repository hops require trace corroboration (ISSUE-5/8); without it, attribution stays single-repository.
