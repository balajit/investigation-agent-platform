# IAP S6 Binding Contract Matrix

Source contract: `docs/design/DES-S6-binding-v1.0.md:7-24`. This matrix records current source evidence only; it is not an implementation plan authorization.

| Dimension | Accepted semantic | Current implementation | Production caller | Tests/evidence | Phase 0 result | Residual risk |
| --- | --- | --- | --- | --- | --- | --- |
| Range | Claimed region exists in cited pinned row at cited revision | `_is_range_read`, `definition_window_proof`, and `validate_location_claim` enforce range mode, file, revision, and symbol containment in `application/investigation/verification.py:3399-3421,3878-3915,5201-5334` | `application/worker/activities/verification.py:682-727` | `tests/unit/test_s6_binding.py:116-153` and related integration tests | Implemented and production-called | Full end-to-end conclusion proof remains unverified |
| Delta | Claimed change exists in hydrated diff/statistics for target path at cited revision | cbv4 typed anchors and explicit diff-witness rows; gateway `compare_code_versions`; GET_CODE_HISTORY execution hook | `application/worker/activities/execution.py` cbv4 history branch; verifier group choke point | Dedicated cbv4 adversarial tests pass | Implemented; live provider acceptance unverified | Provider must return a target-path witness; unavailable providers abstain |
| Contrast | Old half at/before G, new half at/after T, with code row encoding broken assumption | cbv4 `contrast_binding_proof` requires ordered pair, broken-assumption input, cited pinned SOURCE_CODE row, and target-path match; cbv4 group conjunction invokes it | `VerificationEngine.enforce_group_conjunction` | Dedicated cbv4 contrast tests pass | Implemented; live conclusion acceptance unverified | Semantic encoding remains a typed claim input |
| Mechanism-shift | Conditional metric shifts across T while architecture-derived parents hold across incident boundary | cbv4 typed input, frozen thresholds, parent-hold check, timestamped telemetry split, bounded rate-ratio proof, and typed abstention outcomes | cbv4 group conjunction invokes the validator when mechanism fields are present | Dedicated threshold and abstention tests pass | Implemented as data-gated; live telemetry acceptance unverified | Current telemetry density means most cases abstain; thresholds remain provisional |

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
