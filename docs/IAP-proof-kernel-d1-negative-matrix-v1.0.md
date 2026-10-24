# D1 Negative-Test Traceability Matrix v1.0

> Deliverable for `docs/IAP-proof-kernel-d1-spec-v1.2.md` §7: every
> rejection condition in §§2–6 maps to a named test + expected failure
> mode + gate. "Covered by the suite" alone never evidences a security
> or correctness invariant — each row below names the exact test.

## D1-A — Edge identity deterministic, non-self-referential

| Rejection condition (§2.1) | Test(s) | Expected failure mode |
|---|---|---|
| Non-deterministic identity | `test_d1_a02_deterministic_same_inputs` | same inputs → same id |
| Identity insensitive to a field | `test_d1_a03_per_field_sensitivity`, `test_d1_a03b_incoherent_repo_change_fails_closed` | any field change alters id or fails closed |
| Duplicate assumptions | `test_d1_a05_duplicate_assumptions_rejected` | `EdgeError` |
| Unknown assumption | `test_d1_a06_unknown_assumption_rejected` | `EdgeError` |
| Non-NFC strings hashed | `test_d1_a07_unicode_normalization` | `ValidationError` |
| Unknown fields at any nesting level | `test_d1_a08_unknown_fields_rejected_every_level` | `Exception` (forbid posture) |
| Unvalidated in-memory mutation | `test_d1_a08b_unvalidated_mutation_rejected_on_read` | `EdgeError` on read |
| Reversed/empty/out-of-range spans | `test_d1_a09_single_multi_empty_boundary_ranges` | `EdgeError`/`ValidationError` |
| Boolean line/column numbers (lax-coercion guard) | `test_d1_a15_expression_range_bool_lines_rejected` | `ValidationError` (`StrictInt`) |
| Path rule violations | `test_d1_a11_path_rule` | `EdgeError`/`ValidationError` |
| Mutable revision identities | `test_d1_a12_revision_pinning` | `EdgeError`/`ValidationError` |
| RESOLVED with limitation codes | `test_d1_a13_resolved_with_codes_rejected` | `EdgeError` |
| Hash mismatch / wrong variant | `test_d1_a14_verify_round_trip_and_tamper` | `EdgeError` |
| Lineage tuple identity, scope exclusion | `test_d1_d01_lineage_full_tuple_identity` | scope change → same id; row/artist change → new id |
| Duplicate lineage entries | `test_d1_d02_duplicate_source_rejected` | `EdgeError` |
| Entry/observation repo or revision mismatch | `test_d1_d03_repo_mismatch_and_revision_mismatch`, `test_d1_d04_single_repo_must_equal_observation_repo` | `EdgeError` |

## D1-B — Resolver gate vs checker (structured outputs, not text match)

| Condition (§2.2) | Test(s) | Attribution |
|---|---|---|
| Comment/string/import/name-mention decoys | `test_d1_b01_comment_string_import_decoys_not_resolved` | resolver gate (`adjudicate_resolution`) |
| Name-match (non-parser) binding | `test_d1_b02_name_match_binding_insufficient` | resolver gate |
| Unbound caller | `test_d1_b03_unbound_caller_partial` | resolver gate → PARTIAL |
| Ambiguous target | `test_d1_b04_ambiguous_target` | resolver gate → PARTIAL + `AMBIGUOUS_TARGET` |
| Missing / not-yet-resolved target | `test_d1_b05_missing_and_unresolved_target` | resolver gate → PARTIAL |
| Missing syntax kind | `test_d1_b06_missing_syntax_kind_unresolved` | resolver gate → UNRESOLVED |
| Full triple resolves | `test_d1_b07_true_triple_resolves` | resolver gate → RESOLVED |
| Malformed adjudication inputs (negative counts) | `test_d1_b08_adjudication_rejects_malformed_counts` | `EdgeError` (fail closed) |

Parser-execution conformance vectors (per-language, per-version, against
real Tree-sitter resolvers) are live-environment work and recorded OPEN;
the gate above pins the adjudication contract they must satisfy.

## D1-C — Output bound to reviewed contract + executable

| Condition (§4.1) | Test(s) | Expected failure mode |
|---|---|---|
| Artifact substitution, unchanged version/metadata | `test_d1_c01_artifact_substitution_refused` | `DispatchError` (digest drift) |
| Contract-only changes | `test_d1_c01_artifact_substitution_refused` | `DispatchError` |
| Schema incompatibility (unknown rule) | `test_d1_e03_unknown_rule_rejected` | `DispatchError` |
| Dependency/configuration changes | `test_d1_c01_artifact_substitution_refused` | `DispatchError` (surfaced as digest drift) |
| Unapproved environment | `test_d1_e15_unapproved_bundle_rejected`, `test_d1_c01_artifact_substitution_refused` | `DispatchError` |

## D1-D — Provenance resolves to exact immutable sources

Covered in D1-A rows above (`test_d1_d01_lineage_full_tuple_identity`–`test_d1_d04_single_repo_must_equal_observation_repo`): source bytes
changed without revision update, missing/altered rows, wrong repository
ownership, missing build identity, mixed-revision joins — all fail
closed. Single-repo equality is reader convenience, never an override.

## D1-E — Dispatch deterministic, revision-pinned, closed-world

| Condition (§3, §5) | Test(s) | Expected failure mode |
|---|---|---|
| Non-exact key (stale/unknown/missing) | `test_d1_e01_lookup_exact_match_only` | abstain (`None`), never nearest match |
| Duplicate keys | `test_d1_e02_duplicate_keys_rejected` | `DispatchError`/`ValidationError` |
| Wrong table version | `test_d1_e02b_table_version_pinned` | `DispatchError`/`ValidationError` |
| Unknown rule / tampered row | `test_d1_e03_unknown_rule_rejected`, `test_d1_e04_row_digest_reverified_at_read` | `DispatchError` |
| Policy digest drift | `test_d1_e05_policy_digest_binds_row_set`, `test_d1_e14_cross_policy_replay_rejected` | deny (`POLICY_MISMATCH`) |
| Category / claim-kind / premise-ID substitution | `test_d1_e09_category_substitution_rejected`, `test_d1_e10_claim_kind_substitution_rejected`, `test_d1_e11_premise_id_substitution_rejected`, `test_d1_e12_premise_digest_substitution_rejected` | deny |
| Resolver bundle substitution | `test_d1_e13_resolver_bundle_substitution_rejected` | deny at `resolver` check |
| Unapproved bundle | `test_d1_e15_unapproved_bundle_rejected` | deny |
| Target-declaration requirement | `test_d1_e16_target_declaration_requirement` | deny |
| Profile / version substitution | `test_d1_e07_wrong_profile_rejected`, `test_d1_e08_stale_profile_version_rejected` | deny at `dispatch` |

## D1-F — Partial/unresolved records never satisfy a premise

| Condition | Test(s) | Expected failure mode |
|---|---|---|
| Non-resolved variant at authorize | `test_d1_f05_partial_never_satisfies_premise` | deny (`FORBIDDEN_USE`) |
| PARTIAL without codes/missing | `test_d1_f01_partial_needs_codes_and_missing` | `ValidationError`/`EdgeError` |
| UNRESOLVED without codes | `test_d1_f02_unresolved_needs_codes` | `ValidationError` |
| Unknown limitation code; taxonomy dimensions (category/retry/downstream) | `test_d1_f03_limitation_taxonomy_closed` | `EdgeError`; all v1 downstream = `invalidates_use` |
| Null-declaration semantics | `test_d1_f04_null_target_means_exempt_only_at_authz` | null preserved; exemption decided by row, not by null |

## D1-G — Flag-off equivalence

| Boundary (§6) | Test(s) | Evidence |
|---|---|---|
| Flag default off; strict parse | `test_d1_g01_flag_default_off`, `test_d1_g02_flag_parses_strictly` | only exact `"1"` enables |
| No extra imports (no new resolver runs) | `test_d1_g03_d1_pair_imports_nothing_effectful` | allowlisted imports only |
| Env read is flag-only | `test_d1_g04_env_read_is_flag_only` | single `os.environ` read |
| Flag-on verdict invariance | `test_d1_g05_flag_on_changes_no_kernel_verdict` | publication + identity + shadow policy identical on/off |
| No persistence/execution surface | `test_d1_g06_modules_expose_no_execution_entrypoint` | no verb-named entrypoints |
| Reference equivalence baseline | full unit suite green at the flag-off commit (2,979 → current, identical set + D1 additions) | suite output |

## D1-H — No authoritative consumer before authorization

| Condition | Test(s) | Evidence |
|---|---|---|
| No kernel consumer imports D1 | `test_d1_h01_no_kernel_consumer_imports_d1` | repo-wide AST scan (only dispatch→edge) |
| No kernel module references the flag | `test_d1_h02_no_kernel_module_references_flag` | zero hits outside the D1 pair |
| Enabled flag / RESOLVED record / green suite never authorize use | `test_d1_g05_flag_on_changes_no_kernel_verdict` (+ static scans) | behavior identical; no consumer exists to change it |
