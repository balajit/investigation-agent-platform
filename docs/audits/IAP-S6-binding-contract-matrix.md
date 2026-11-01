# IAP S6 Binding Contract Matrix

Source contract: `docs/design/DES-S6-binding-v1.0.md:7-24`. This matrix records current source evidence only; it is not an implementation plan authorization.

| Dimension | Accepted semantic | Current implementation | Production caller | Tests/evidence | Phase 0 result | Residual risk |
| --- | --- | --- | --- | --- | --- | --- |
| Range | Claimed region exists in cited pinned row at cited revision | `_is_range_read`, `definition_window_proof`, and `validate_location_claim` enforce range mode, file, revision, and symbol containment in `application/investigation/verification.py:3399-3421,3878-3915,5201-5334` | `application/worker/activities/verification.py:682-727` | `tests/unit/test_s6_binding.py:116-153` and related integration tests | Implemented and production-called | Full end-to-end conclusion proof remains unverified |
| Delta | Claimed change exists in hydrated diff/statistics for target path at cited revision | Generic history validation exists at `verification.py:4650`; no distinct Delta binding result or target-path before/after proof was found | History validation is called from `application/worker/activities/verification.py:653-667` | No dedicated Delta cases found in focused S6 tests | Not established; partially implemented at most | History/revision presence may be mistaken for target-path delta proof |
| Contrast | Old half at/before G, new half at/after T, with code row encoding broken assumption | `qualify_contrast_half`, `qualify_contrast_pair`, and `_contrast_coverage` enforce source/order/boundary checks at `domain/evidence/qualification.py:348-428`; rival comparison exists at `verification.py:5676-5710` | Qualification feeds S4 readiness; verification uses rival comparison | S6 tests cover rival/elimination behavior, not complete G/T broken-assumption conclusion binding | Partially implemented | A qualified pair may not establish the required code-backed broken assumption |
| Mechanism-shift | Conditional metric shifts across T while architecture-derived parents hold across incident boundary | Java window facts/results provide typed source analysis, but no mechanism-shift validator, parent binding, incident-boundary comparator, or conditional metric-shift result was found | No mechanism-shift production caller identified | Java construct tests do not cover mechanism-shift semantics | Absent or unverified | Mechanism claims may be inferred from temporal correlation or source facts without the accepted architecture relation |

## Focused test evidence

```text
pytest -q tests/unit/test_s6_binding.py tests/unit/test_j5_newer_constructs.py tests/unit/test_frontier.py tests/unit/test_j10_frontier.py
76 passed in 1.50s
```

This confirms focused tests pass; it does not upgrade the incomplete dimensions to accepted S6 compliance.

## Required closure evidence

1. A typed contract-to-code implementation for Delta and Mechanism-shift, or an owner-approved disposition.
2. Proof that Contrast requires the code-backed broken-assumption row at the final conclusion gate.
3. Production caller traces and tests for each dimension.
4. Adversarial tests for reversed/out-of-range windows, single observations, identical populations, missing parent relations, incomplete incident boundaries, malformed analysis, and disappearing citations.
5. Replay/determinism evidence for the integrated verification path.
