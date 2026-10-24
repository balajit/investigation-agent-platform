# IAP Proof-Kernel D1 Spec v1.2 — Typed Edges (Design-Only)

> Status: specification, not implementation authorization. No code, schema,
> flag, or behavior change is authorized by this document. D1-impl requires
> G5-unit green plus explicit owner go-ahead; authoritative use additionally
> requires G5-formal. Builds on `docs/IAP-proof-kernel-design-v1.0.md` (§2),
> `docs/IAP-proof-kernel-contracts-v1.0.md` (§3.2), and
> `docs/IAP-proof-kernel-forward-plan-v1.0.md` (Step 2).
> v1.2 closes the second-round findings D1-R1–R5, the P2 corrections, the
> tightened D1-A–H criteria, and the governance/owner-decision items.
> Amended 2026-10-23 for D1-V12-01–05 (in-place closure edits, no
> renumbering; see §§2.1, 2.3, 3, 4.1, 5, 7) and D1-V13-01–03
> (premise digest binding, lineage identity key, normative/pending
> split; see §§2.3, 2.4, 5, 9).
> Non-goals: no BEHAVIOR/ABSENCE/HISTORY/Causal edge semantics; no witness
> authority; no change to heuristic evidence rows, resolvers, or verdicts.

## 0. Trust model (D1-R1, owner decision D4)

Combination, stated exactly:

- The checker **independently validates admissibility**: variant value,
  schema conformance, all digests recomputed, scope and revision
  immutability, dispatch authorization predicate (§5), limitation-code
  emptiness for `RESOLVED_EDGE`, assumption-vocabulary membership, and
  context currency. No admissibility fact is taken on trust.
- The checker **trusts a reviewed, digest-pinned resolver for the
  semantic relation** (syntax recognition, caller binding, target
  resolution per §3.2). A forged but well-formed `RESOLVED_EDGE` from
  an approved artifact is therefore a resolver-conformance failure, not
  a checker bypass — and G3 exists to bound that trust. ("G3" throughout this document
means sixth-review gap G3, resolver soundness — not acceptance-gate
G3, which covers the retrieval ledger.)

The checker verifies admissibility under a reviewed resolver contract,
not independent semantic proof of the source-code relation. Protection
against a semantically wrong `RESOLVED_EDGE` is testable at two layers:
resolver conformance negatives (D1-B: the resolver must not emit it)
and checker admissibility (D1-F: anything the resolver emits outside
the contract is rejected). What G3 must establish: per-language and
per-language-version conformance vectors including all D1-B negative
cases; explicit supported-construct boundaries (what the resolver may
resolve vs must abstain on); parser-version compatibility rules; and
re-review triggers (new constructs, new language versions, digest
changes).

## 1. Problem

The kernel's `call_edge/v1` rule consumes `NORMALIZED_CALLSITE_FACT`
premises, but production has no such record: call evidence today is a
mix of `CALL_SITE_RANGE` / `TARGET_DECLARATION_RANGE` evidence families,
`CALL_TARGET_FACT` analyzer resolutions, and heuristic verifier prose.
Nothing binds a normalized caller→callee edge to immutable provenance
with a closed vocabulary, and nothing decides *which* resolver may speak
for a given tenant profile, language, and claim kind. D1 closes both
gaps without touching existing behavior.

Governing principle: D1 may produce a better-typed, better-provenanced
representation of analyzer evidence; it must not turn a resolver's
assertion into proof merely by normalizing, hashing, or storing it. A
typed, immutable, correctly hashed call-edge observation is
better-controlled evidence — not proof by virtue of its representation.

## 2. Typed observation envelope and edge record (frozen schema)

A new, separate record — never a reinterpretation of existing evidence
rows. The envelope is tagged; the tag is the status (no separate
`status` field, so variant/status drift is unrepresentable):

```text
EdgeObservation {
  observation_id: string       # content hash, see §2.1 (never self-referential)
  variant: RESOLVED_EDGE | PARTIAL_OBSERVATION | UNRESOLVED_OBSERVATION
  record_version: "edge/v1"
  ...variant payload...
}
```

### 2.1 Canonical payloads per variant (D1-R2)

Each variant has an exact field list. Required unless marked optional.
`UNKNOWN` sentinel is allowed only where explicitly listed; elsewhere
unknown values fail closed.

`RESOLVED_EDGE` payload — all required except where noted:

```text
  caller: { symbol, declaring_type, file_path, revision }
  callee: { symbol, declaring_type, file_path, revision }
  call_site: {
    file_path, revision, start_line, end_line,          # 1-indexed lines, end exclusive
    expression_range: { start_line, start_col, end_line, end_col },
    window_text: string,                                # NFC-normalized before hashing
    syntax_kind: string
  }
  target_declaration: { file_path, revision, start_line, end_line } | null
  repository: { name, revision, build_identity: string | null }
  resolver: { bundle, bundle_version, contract_digest, artifact_digest,
              parser_version, language, language_version, analysis_model }
  limitation_codes: []                                  # always empty here
  lineage: {
    sources: [{
      evidence_id, evidence_digest, artifact_digest,
      scope, revision,
      repository: { name, revision, build_identity: string | null },
    }],
    analyzer_run_id, execution_identity
  }
  assumptions: [...]                                     # closed vocabulary §2.6
```

`PARTIAL_OBSERVATION` payload: whichever of caller/callee/call_site/
target_declaration are bound (same shapes; unbound endpoints omitted,
never nulled), `limitation_codes` non-empty, `missing: [string]`
naming the unbound parts from a closed list
(`caller | callee | call_site | target_declaration`),
plus repository/resolver/lineage/assumptions as above.

`UNRESOLVED_OBSERVATION` payload: `observed_ref`, one of two tagged
forms (no untagged union):
`RANGE_REFERENCE { file_path, revision, start_line, end_line }` with
line-span semantics per §2.1, or `SYMBOL_REFERENCE { symbol,
declaring_type: string | omitted, file_path: string | omitted,
revision }`. Both forms require the enclosing `repository` identity;
omitted means unknown, never inapplicable. A reference whose source
cannot be located keeps the observation unresolvable with an
`AMBIGUOUS_TARGET` or `MISSING_INPUT` code — malformed, ambiguous, or
mismatched references are never coerced into a resolved edge and never
accepted as a kernel premise. No caller/callee fields at all — an
unbound endpoint is inexpressible, so it cannot drift into an edge.

Hash preimage per variant: `{"domain": "iap:call-edge",
"record_version": "edge/v1", "variant": ..., <variant payload fields>}`,
excluding `observation_id`. Byte layout mirrors `canon/v1`
(`domain/proof/canonical.py`): sorted keys, compact separators, UTF-8,
NFC-normalized strings, float rejection. Set-valued fields are sorted
with duplicates rejected; unknown fields rejected at every nesting
level (`extra="forbid"` posture); absent optionals omitted, never
nulled (the sole exception is `target_declaration: null`, see §2.5).
Schema validation and `observation_id` recomputation precede
availability on every read: a malformed or hash-mismatched record
becomes typed unavailability — never repaired, never silently
normalized into a different record, never passed to the kernel.

Coordinate and path conventions (normative): positions are
1-indexed; ends are exclusive; reversed or empty ranges fail closed.
Line spans (`call_site` outer span, `target_declaration`) use a pure
line-span convention `[start_line, end_line)` — a single-line span on
line 12 is `[12, 13)`, with no column semantics. `expression_range`
uses a pair of source positions `(start_line, start_col)` to
`(end_line, end_col)` with a lexicographic exclusive end — a call
expression beginning and ending on line 12 is `(12, c1)` to `(12, c2)`.
The two conventions are independent: a line span never carries columns
and an expression range never borrows line-span end semantics. Paths
use the repo rule (strip leading `./`, collapse `//`, strip trailing
`/`, case preserved, backslash rejected). `window_text` is
NFC-normalized before hashing; the digest covers the normalized form.
Two representations hash identically only when the contract declares
them equivalent; any semantically meaningful change alters identity.

The hash establishes record integrity relative to the payload. It does
not establish the truth of the edge.

### 2.2 Resolution contract: syntax + binding + target

Text containing both names is explicitly insufficient. A
`RESOLVED_EDGE` requires all three, established by the resolver under
its declared analysis model:

1. **Syntactic call-site evidence**: the parser identified a call
   expression at `expression_range` with the reported `syntax_kind`.
   Comments, string literals, imports, and bare name mentions never
   satisfy this.
2. **Caller binding**: the expression lies within the body or
   permitted scope of the declared caller (nesting, decorators,
   overloads, aliases resolved by the parser, not by name match).
3. **Target resolution**: the resolver established that the expression
   resolves to the declared callee under `analysis_model`.

Unresolvable targets (dynamic dispatch, DI, runtime binding) abstain
or emit `PARTIAL_OBSERVATION`. A static approximation never becomes a
runtime claim: a resolved static edge does not establish execution,
success, or behavior. The kernel checker validates admissibility per
the rule (§5); semantic correctness of the relation is the resolver's
conformance obligation (§0, D1-B). `analysis_model` values come from a
closed per-resolver vocabulary declared in the D1-impl mapping
artifact; unlisted models fail closed.

### 2.3 Provenance binding

Four identities, never substituted for one another:

- **Source artifact digest**: exact immutable bytes of the file or
  generated artifact as consumed — git blob bytes for committed
  sources; the pinned generated artifact bytes for generated code;
  parser input only where the resolver declares no preprocessing, else
  the pre-preprocessing bytes plus the preprocessing description in
  `analysis_model`.
- **Evidence-row digest**: canonical bytes of the evidence record
  consumed, bound per row in `lineage.sources` as `evidence_digest` —
  distinct from the source artifact digest below; the two are never
  substituted for one another.
- **Resolver artifact digest**: the approved executable environment
  (§4.1).
- **Observation ID**: canonical identity of the resulting payload
  (§2.1). It proves neither faithful evidence representation nor
  correct interpretation.

Per-resolver authoritative-source table (D1-impl artifact): for each
supported resolver, the exact artifact hashed, encoding, preprocessing
boundary, and the mapping artifact → repository revision → evidence
row. `lineage.sources` entries carry `(evidence_id, evidence_digest,
artifact_digest, scope, revision, repository)` where each entry
self-identifies its owning repository (`{ name, revision,
build_identity }`) — no unqualified revision strings, no reliance on
the observation-level `repository` field when entries span
repositories. When all entries share one repository it must equal the
observation-level `repository` field (mismatch fails closed); the
observation-level field is reader convenience, never an override.
Each entry binds the exact source artifact consumed via
`artifact_digest` (per the authoritative-source table) **and** the
evidence row via `evidence_digest`. Entry identity is the full
`(evidence_id, evidence_digest, artifact_digest, repository.name,
repository.revision)` tuple — duplicates on the full tuple rejected.
Same ID under a different row digest, artifact digest, or owning
repository/revision is a distinct entry (it represents change or a
different source, not duplication); sharing only part of the key never
merges entries. The entry-level `revision` field must equal
`repository.revision` (kept for reader convenience; mismatch fails
closed). `scope` is deliberately excluded from identity: scope is the
consumer's view of identical bytes, so the same bytes under two scopes
listed twice is a duplicate. Sources must include **every** artifact
influencing the result: directly cited rows
plus symbol indexes, build metadata, generated-code mappings, and
configuration artifacts consumed. `execution_identity` is mandatory
for all `RESOLVED_EDGE` records; `build_identity` is mandatory
whenever any path is generated or spans repositories.
Path→owning-repository resolution for submodules, generated trees, and
multi-repo joins is defined per resolver in the authoritative-source
table; ambiguous ownership abstains. Changing a consumed source
artifact or its owning repository/build identity invalidates the
provenance binding even when the evidence-row ID is unchanged.

`repository.revision` must be an immutable commit identity; branch
names, mutable tags, `HEAD`, and `UNPINNED` fail closed.

### 2.4 Policy context binding

The edge record stays generic; the **consuming certificate** binds the
admission policy: `dispatch_binding: { profile_id, profile_version,
dispatch_row_digest, policy_digest, claim_kind, premises: [{
observation_id, content_digest, fact_category }] }`. Each covered
premise names the exact observation record ID, the digest of its
canonical bytes, and the category asserted for it; §5 check 3 verifies
every element, and the checker recomputes and compares the digest
rather than trusting the ID alone. A record accepted under one policy
is never silently reused under another.

### 2.5 Null semantics (exact)

`target_declaration: null` means "not required" only when the dispatch
row's `requires_target_declaration` field (explicit, versioned,
per-row) is false; otherwise a missing declaration forces
`PARTIAL_OBSERVATION`. "Not found" and "not yet resolved" are
limitation codes, never nulls. Absent optionals are omitted from the
payload; `UNKNOWN` appears only where §2.1 lists it.

### 2.6 Assumptions vocabulary

`assumptions/v1` (closed, versioned): `static-model-only`,
`no-execution-claim`. Semantics: each narrows what a discharge proves;
none authorizes discharge. A future assumption never silently broadens
an existing rule — new assumptions require a rule-version review.

## 3. Profile dispatch table (closed, version-pinned)

- A dispatch row applies to exactly one profile revision. Newer
  revisions inherit nothing; rows are created by explicit reviewed
  migration. Stale rows, duplicate keys, unknown enums, and unreviewed
  bundles fail closed. The policy digest over the reviewed row set is
  recorded and validated wherever dispatch is used.
- Row canonical form: canonical bytes over the ordered tuple
  `(profile_id, profile_version, language, claim_kind, fact_category,
  resolver_bundle, bundle_version, contract_digest, artifact_digest,
  rule_id, requires_target_declaration)` with the §2.1 byte layout;
  `row_digest` bound the same way. The policy artifact carries all row
  digests; row digests are re-verified against the policy digest at
  read time, not merely at review time.
- Claim kinds, categories, languages not in the table abstain. The
  category enum is tied to its schema version; a new category fails
  closed until its disposition row is reviewed.
- Dispatch rows carry `requires_target_declaration: bool` (versioned);
  the checker reads the exemption from the row, never infers it from a
  null declaration.
- `CATEGORY_PROOF_RULES.required_evidence_families` do **not** become
  `unsupported_cases` by copying. D1-impl produces an explicit,
  reviewed mapping table as a standalone artifact.

## 4. Resolver soundness boundaries

Each resolver bundle satisfies: typed in/out (no prose conclusions);
limitation discipline per §4.1; provenance discipline per §2.3;
forbidden outputs (`CAUSAL_CLAIM`, confidence scores, undeclared
cross-scope joins); three-identity binding (§4.1).

### 4.1 Three identities and the runtime boundary (D1-R5)

- `resolver_contract_digest` — normative contract.
- `resolver_artifact_digest` — exact executable/package or immutable
  image **plus** a declared dependency/configuration manifest hash.
- `resolver_bundle_version` — human-managed reviewed release.

Artifact boundary per deployment mode (D1-impl defines each mode used):
content-addressed package or immutable image + dependency/configuration
manifest; the execution environment records the expected artifact digest
and refuses to run the resolver on mismatch (startup attestation, not a
load-time assertion), and re-verifies at every resolver load — i.e.
whenever the resolver module is (re)loaded into the executing process
or its configuration is re-read, not by wall-clock inference — so a
valid dispatch row or a single startup check never covers later
execution with different code or configuration. Dynamically loaded modules, plugins,
environment-dependent configuration, and mutable external services are
forbidden for `RESOLVED_EDGE` output unless pinned by digest in the
manifest and covered by the reviewed contract. Metadata assertions
alone never satisfy the substitution test: the artifact-substitution
acceptance test runs a different executable (and a semantically
relevant configuration change) under an approved digest and must fail
the binding.

### 4.2 Limitation taxonomy

Each code: stable code + taxonomy schema/version (`limitation/v1`
initial); category from `MISSING_INPUT | TRANSIENT_FAILURE |
UNSUPPORTED_CONSTRUCT | AMBIGUOUS_TARGET | INCOMPATIBLE_CONTEXT`;
retryability (meaningful or not; which changed inputs could resolve);
downstream scope (invalidates all use vs telemetry-only). Stored
records referencing codes unknown to the current checker default to
unavailable/abstain. Retryability never determines truth.

### 4.3 Legacy vs kernel outputs

Legacy heuristic output unchanged under the flag contract with existing
`max_verdict` semantics. Kernel eligibility determined exclusively by
the typed record, validated provenance, exact rule contract, supported
claim form, scope, and current context. `RESOLVED` is a
record-resolution status, not a claim verdict.

## 5. Single authorization predicate (D1-R4)

Normative: `authorize_edge_use(certificate, record, dispatch_table,
registry)` checks all of the following together and rejects or reports
unavailable on any failure:

1. Exact `profile_id` and `profile_version` from the certificate's
   `dispatch_binding`.
2. Exact reviewed dispatch-row digest and policy digest; row digest
   re-verified against the policy digest at read time.
3. Record language, claim kind, and category against the selected row.
   `claim_kind` and `fact_category` are **not** stored in the edge.
   Each covered premise ID resolves to exactly one object: the edge
   observation record, addressed by `(observation_id, content_digest)`
   — both carried in the premise binding (§2.4), both verified. The
   certificate's `dispatch_binding` carries, per covered premise, the
   asserted `(observation_id, content_digest, fact_category)` triple
   plus the certificate-level `claim_kind`. The checker verifies: the
   premise resolves to the observation with the asserted ID and digest
   (digest recomputed over the canonical bytes, ID alone never
   sufficient); the
   asserted category equals the row's category; the certificate's
   claim kind equals the row's claim kind; the observation's resolver
   bundle equals the row's bundle. Three categories stay distinct:
   the source evidence artifact's category (lineage-informational
   only, from `lineage.sources`, never authoritative), the normalized
   edge fact's category (the asserted triple, row-checked), and the
   consuming certificate's claim kind (row-checked). The edge's
   category is never inferred from its source evidence types, and no
   caller-supplied label is trusted.
4. Resolver bundle, version, contract digest, and artifact digest
   against that row — the row's canonical form (§3) carries the
   expected bundle version and both digests, so the row is the source
   of the expected values, not the record under test (artifact
   reverified at execution per §4.1).
5. Consuming rule ID against the row and the closed rule registry
   (unknown rule → reject even with a valid row).
6. Target-declaration requirement for the category from the row's
   `requires_target_declaration` field.

Acceptance: swapping profile, dispatch row, category, resolver, or rule
while holding other inputs fixed causes rejection or typed
unavailability. Certificate replay against another policy is rejected
by checks 1–2.

## 6. Flag contract (`IAP_TYPED_EDGES`, proposed — not created here)

Off by default. Equivalence defined over: serialized outputs and
persisted row contents; resolver routing and evidence selection;
verdicts, tiers, explanations, confidence; error and abstention
behavior; ordering where contractually significant; side effects
including database writes and resolver invocations. Permitted
telemetry-only differences enumerated explicitly; the contract forbids
persisting a new record or running an additional resolver when off.
On (test/operator contexts only until G5-formal): construction
exercised; no verifier consumes records to change a verdict until
gates and authorization are satisfied. Separate attribution from the
retrieval-ledger path.

## 7. D1-impl test enumeration and acceptance table

Negative tests required for every abstention boundary; a
negative-test traceability matrix is a D1-impl deliverable (every
rejection condition in §§2–6 → named test + expected failure mode +
gate). "Covered by the suite" alone never evidences a security or
correctness invariant.

| ID   | Required invariant | Evidence needed |
| ---- | ------------------ | --------------- |
| D1-A | Edge identity deterministic, non-self-referential | Golden vectors per variant and optional-field combination, including single-line, multi-line, empty, and boundary-position ranges for each range kind (line spans agree with expression ranges only where the contract says so); per-field identity sensitivity; set-reorder invariance; duplicate rejection; Unicode normalization; unknown fields at every nesting level; lineage/resolver/repository changes alter identity; invalid and reversed ranges fail closed |
| D1-B | Resolved edge = parsed + bound relation, not text match | Comment/string decoys, shadowed symbols, imports/aliases, overloads, nested functions, unresolved imports, dynamic dispatch, parser-version mismatch, cross-file binding; semantically wrong but syntactically valid edges; each states whether rejection comes from the resolver gate or the checker |
| D1-C | Output bound to reviewed contract + executable | Artifact substitution with unchanged version/metadata; contract-only changes; schema incompatibility; dependency/configuration changes; execution under an unapproved environment |
| D1-D | Provenance resolves to exact immutable sources | Source bytes changed without revision update; missing/altered rows; wrong repository ownership; stale generated artifacts; missing build identity; mixed-revision joins — all fail closed |
| D1-E | Dispatch deterministic, revision-pinned, closed-world | Full key-space boundaries; duplicate keys; stale revisions; missing rows; unknown categories; resolver/rule mismatch; category substitution, premise-ID substitution, and claim-kind substitution each rejected with resolver bundle and source evidence held fixed; cross-policy certificate replay; no implicit inheritance on profile bump |
| D1-F | Partial/unresolved records never satisfy a premise | Rejection for every non-resolved variant and every limitation category; no promotion via fallback, coercion, ranking, or legacy heuristic output |
| D1-G | Flag-off equivalence at all §6 boundaries | Persisted-state, resolver-call, evidence-selection, ordering, error, and abstention comparisons; no-extra-write and no-extra-run tests |
| D1-H | No authoritative consumer before authorization | Actual verifier and publication paths exercised; enabled flag, RESOLVED record, or green suite alone never permits authoritative use |

Reference baseline for D1-G: the unit suite at the flag-off commit,
the frozen fixture snapshot, seeded randomness/clocks, and a
deterministic field-by-field comparison method — an "outputs are
equivalent" assertion alone is insufficient.

## 8. Entry/exit criteria

- Entry: G5-unit green; owner go-ahead for D1-impl.
- Exit: §§2–5 implemented behind the off-flag; §7 green including the
  negative-test matrix; flag-off equivalence proven at §6 boundaries;
  no heuristic-path behavior change; G5-formal and D3 still pending.
- Out-of-scope disputes go to the owner, not into the implementation.

## 9. Owner questions and decisions

Normative-vs-pending split (binding on implementers): the schema,
identity, digest, dispatch, authorization, and flag contracts in
§§0–7 are normative now — a pending owner decision never permits
inventing a different identity model, and no schema requirement is
optional pending approval. The flag's contract is normative; only the
flag's creation and enablement are pending (the flag does not exist
yet — §6). What remains pending is the owner's choice of persistence
shape, release/versioning policy, and per-language release management
within those contracts.

§8-v1.1 recommendations stand as recommended-not-decided (new table,
version+digests, per-language versioning) except where v1.2 made them
normative contract text above (three-identity binding is now specified,
not optional). Three decisions require explicit owner approval before
D1-impl treats them as settled:

1. Record persistence in a new append-only observation table
   (condition: confirm against persistence/migration conventions).
2. Bundle versioning as version + contract digest + artifact digest.
3. Independent per-language resolver versioning with shared
   conformance vectors where equivalence is claimed.
4. **Trust model (§0)**: checker-validated admissibility plus a
   reviewed, digest-pinned resolver as the accepted evidence producer
   for the semantic relation, bounded by the G3 conformance suite.

## 10. Governance reconciliation

- **G5-unit is a sub-gate**, distinct from G5-formal: invalidation
  closure, conflict denial, alternative-route survival, stale-
  publication denial, raced-CAS safety, mutation sensitivity.
- **Evidence verification notes** (per review §5): CAS tests exercise
  stale-version conflicts including the insert race; invalidation
  tests cover transitive closure with unrelated preservation;
  alternative-route survival uses a checker-accepted survivor — and the
  gate additionally requires the survivor to be current-context
  accepted (a stale or invalid derivation never survives via
  alternatives; `test_gate_alternative_route_commits_after_root_loss`
  pins publication against the live context).
- **Disposition accepted**: G5-unit reported green (not independently
  verified by the reviewer); G5-formal pending (live DB/RLS interleave
  + commit-path role review); D1 design substantially improved with
  P1 contracts now closed in §§0–5; D1-impl not authorized by any
  review — requires explicit owner go-ahead plus entry conditions;
  authoritative typed-edge use not authorized (requires formal gate +
  D3 rollout). A green unit gate never authorizes implementation.
- **Second-round disposition accepted as stated**: D1 design near
  closure with D1-V12-01–05 closed above; G5-unit reported green;
  G5-formal pending; D1-impl and authoritative use not authorized;
  the four §9 owner decisions remain owner decisions. Approval of the
  §0 trust model means accepting a reviewed resolver as the semantic
  evidence producer within the G3 boundary — not claiming the checker
  independently proves the source-code call relation.
