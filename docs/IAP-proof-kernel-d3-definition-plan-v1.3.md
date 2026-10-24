# D3-Family[definition] Migration Design v1.3

> Status: design for owner approval — no implementation authorized by
> this document. Builds on the D1-spec (contract pattern) and the G6
> shadow-capture precedent (compute-beside, never feeds back).
> Prerequisite gates: G5-formal PASS (recorded), G6 accepted with
> observations (recorded), D1-impl green (committed).
> Evidence artifacts and versions for each prerequisite are recorded
> in §0.1; their presence here does not substitute for independent
> verification at authorization time.
>
> v1.1 amends v1.0 per adversarial review fb-1 (D3-01–D3-10):
> binding semantics made explicit, canonical target matching defined,
> D1 lineage/resolver requirements reused without weakening, explicit
> shadow-comparison contract with four-cell outcomes, Phase 1 recast
> as a separate approval package, family-scoped flag, hook isolation
> contract, shared security primitives enumerated. Phase 1 remains
> explicitly unauthorized; parser-based declaration resolution stays
> deferred to a separately reviewed capability.
>
> v1.2 amends v1.1 per adversarial review fb-2 (D3-11–D3-16):
> separate shadow vs authority flags, operational candidate-selection
> algorithm with explicit ambiguity semantics, executable per-variant
> invariants and cross-field rules, normative resolver digest
> preimages with verification points, restated Phase 0 exit bar with
> pre-declared sample policy, reproducible evidence-to-source
> binding procedure. Retained from v1.1 without change: limited v1
> semantics and the `RESOLVED_TARGET_BINDING` naming, fail-closed
> exact matching with no last-segment fallback, identity separation
> across digest kinds, shadow-only rollout requirements,
> independent family flags, separate authorization machinery with
> shared primitives, and separate Phase 1 approval.
>
> v1.3 amends v1.2 per adversarial review fb-3 (D3-17–D3-22 plus
> consistency checks), without reopening the approved direction:
> Option B v1 semantic contract (bound source-range association,
> explicitly not declaration proof; cannot satisfy a
> declaration-requiring gate), TargetReference vs BoundTargetIdentity
> separation, internally consistent partial-observation schema,
> runtime trust anchor with manifest derivation and loaded-code
> verification, byte-exact source-window contract, and an explicit
> two-layer acquisition/construction split reconciling source
> verification with the no-I/O shadow constraint. Phase 0
> implementation and Phase 1 authority remain unauthorized.

## 0.1 Prerequisite gate evidence (recorded, to be independently verified at authorization time)

| Gate | Verdict claimed | Evidence artifact | Version / identity |
|---|---|---|---|
| G5-formal | PASS | `docs/IAP-proof-kernel-g5-formal-protocol-v1.0.md`; verdict JSON operator-held; `g5_gate` DB at migration head | protocol v1.0; alembic head `032_hypothesis_version`; 22/22 checks |
| G6 | Accepted with observations (not a clean PASS) | `docs/IAP-proof-kernel-g6-measure-protocol-v1.0.md` incl. adopted thresholds; ledger entries 2026-10-24; pilot trace set operator-held | protocol v1.0; hook + harness as committed in `1389471`, *plus uncommitted working-tree enhancements* (owed/forced/evidence-delta/params stamping, `summarize_trace`, telemetry allowlist extensions, telemetry unit tests) — verify the working tree, not just the commit, when authorizing |
| D1-impl | Green, reviewed PASS | `docs/IAP-proof-kernel-d1-spec-v1.2.md`; `docs/IAP-proof-kernel-d1-negative-matrix-v1.0.md`; committed implementation + 67 tests | spec v1.2; commit `ba9fbfb` |

Listing here records *what* is claimed and *where*; authorization must independently verify each artifact rather than infer passage from this table.

## 1. Native path (as found)

A DEFINITION claim verifies today via
`VerificationEngine.check_claim_kind` → `definition_window_proof`
(`verification.py:1967,1779`): a banked *range read* whose window
contains the defined symbol (file-path match + line-span cover +
symbol-in-text). No revision binding (any row content qualifies), no
resolver attestation, and the claim kind itself may be prose-inferred
(`effective_claim_kind` matches "implemented in" phrasing when no
typed kind is recorded). Two call sites consume the gate
(`verification.py:2678` assessment filtering, `:3923` scoring gate);
both treat failure as demotion/exclusion, never as proof of absence.

## 2. Target shape (proposed)

A `definition/v1` observation record mirroring the D1 envelope
(tagged variant, content-hash identity over a `canon/v1` preimage with
domain `iap:definition`, `extra=forbid`, closed assumption/limitation
vocabularies, full-tuple lineage with scope excluded from identity):

```text
DefinitionObservation {
  observation_id, variant: RESOLVED_TARGET_BINDING | PARTIAL_OBSERVATION | UNRESOLVED_OBSERVATION,
  record_version: "definition/v1",
  payload: {
    symbol, declaring_type?, file_path, revision, start_line, end_line,
    repository, resolver, lineage, limitation_codes, assumptions,
  }
}
```

The terminal variant is deliberately named `RESOLVED_TARGET_BINDING`,
not `RESOLVED_DEFINITION`: v1 establishes that a source range is
bound to a target under explicit matching rules — never that the range
contains a syntactic declaration, that the code executes, or that any
claimed behavior is implemented. A symbol string in a comment,
string literal, reference, documentation block, or unrelated
declaration must never become a resolved observation. Anything less
than full binding abstains (`PARTIAL`/`UNRESOLVED`); no promotion via
retries, prose inference, or permissive fallback.

What a v1 observation establishes, exhaustively:

- binding to one evidence row, one source artifact, one repository
  revision, one path, one line range;
- the supplied target matched under the canonical rules below;
- binding and provenance checks passed.

What it does not establish, explicitly out of contract: syntactic
declarationhood, execution, success, behavior, or absence. Phase 1
must never grant stronger semantics than this contract supports.

v1 adopts the bound source-range contract (review Option B), not a
declaration-verification contract: a resolved observation establishes
a pinned, identity-bound association between a source range and a
target — it is *not* evidence that the range declares the symbol,
and it must never satisfy a gate that requires declaration
evidence. Exact text presence cannot distinguish a declaration from
a reference, comment, string literal, or documentation block, and
the normalizer performs no parsing; claiming otherwise would grant
authority through representation rather than review. Consequently,
Phase 1 as conceived — typed observations authorizing definition
verdicts — is reframed, not merely gated: typed authorization may
substitute the native gate *only within identical semantic bounds*
(bound, pinned source range showing the symbol, per the native
gate's own contract), while retiring prose-inferred kinds and
unpinned evidence. Any consumer requiring declaration semantics
needs the parser-backed resolver capability, which remains
deferred and separately reviewed. This document must not be read,
now or later, as establishing that a normalized observation proves
a declaration.

`authorize_definition_use` is a dedicated entry point implementing
the ordered ten-step predicate (§8); it reuses the shared security
primitives enumerated in owner decision 4 (canonicalization, digest
verification, exact binding lookup, scope checks, dispatch
verification, error taxonomy) and keeps definition-specific schema
and semantic checks separate. No persistence shape is created
(§9 decision 1 standing). The closed `RULE_REGISTRY` already carries
`definition/v1`; the category mapping already admits `definition`.

### 2.1 Canonical target construction and matching (D3-02)

A bound target is the tuple `(symbol, declaring_type | null,
file_path, revision)` where `symbol` is the exact identifier text
(stripped of surrounding whitespace, no case folding — code
identifiers are case-sensitive) and qualified names keep their full
form (`a::b`, `a.b`, `a#b` are distinct strings; matching compares
the full string only). The native `_symbol_in_text` last-segment
fallback is deliberately *not* carried over: v1 never infers aliases,
imports, or qualification-stripped matches.

Matching rules, in order: (1) file path equality after the D1 repo
rule (strip `./`, collapse `//`, no trailing slash, backslash
rejected); (2) exact symbol equality (no edit distance, no stemming);
(3) `declaring_type` equality when the target carries one, ignored
when omitted (omitted means unknown, never inapplicable);
(4) revision equality against full-length immutable commit identities
only (branch names, tags, `HEAD`, `UNPINNED`, short SHAs fail closed).
Aliased, imports, re-exports, and shadowing are **not** resolved in
v1: an ambiguous match (same symbol bound in two files, scopes,
types, or repositories; duplicate declarations; two plausible
targets) yields `PARTIAL` (`missing: ["target"]`) or
`UNRESOLVED_OBSERVATION` — never a best-effort pick. Adversarial
coverage is required for same-name symbols across files, scopes,
types, repositories, and revisions (see §7).

### 2.1a Candidate-selection algorithm (D3-12, operational)

Candidates are enumerated from the *supplied evidence only* — never
guessed, never retrieved beyond the rows handed to the normalizer:

1. Collect candidate rows: banked evidence rows bound to a file path
   with a line range and a content snippet. Rows without all three
   are not candidates.
2. Apply constraints in this order: repository identity, then
   revision, then file path (when the target supplies one; when the
   target omits it, no path filtering occurs), then exact symbol
   equality, then `declaring_type` equality when the target carries
   one (omitted type imposes no constraint, but it also confers no
   disambiguation: two candidates differing only in type with no
   target type remain ambiguous).
3. Require exactly one surviving candidate.
4. Zero survivors → `PARTIAL` with `missing: ["target"]` and code
   `MISSING_INPUT` (a referenced target nothing binds), or
   `UNRESOLVED_OBSERVATION` when there is no usable target
   reference at all. More than one survivor → `PARTIAL` with
   `missing: ["target"]` and code `AMBIGUOUS_TARGET`. The
   `missing: ["target"]` value is hereby defined to mean exactly
   "no unique binding" covering both cases; the limitation code
   distinguishes them. It is not a catch-all: any other unbound
   part uses its own closed `missing` value.

On the file-path question: a bound target file acts as a *filter*,
not a generator. A same-symbol row in a different file is excluded
by path equality — it is not an alternative match. Ambiguity arises
only among candidates surviving *all* supplied constraints:
duplicate symbols inside the supplied file (two ranges), or,
when the target omits the file, the same symbol across files.
Both cases are tested, inside and outside the supplied file.

### 2.1b TargetReference vs BoundTargetIdentity (D3-19)

The same object must never serve as both an incomplete search
constraint and a complete bound identity. Two distinct conceptual
types, with a one-way binding between them:

- `TargetReference`: possibly incomplete; the claim's search
  constraint and the *input* to candidate enumeration. Fields:
  `symbol` (required), `declaring_type` (optional),
  `file_path` (optional), `revision` (optional). Omission means
  "unconstrained", never a default value.
- `BoundTargetIdentity`: complete and canonical; the *output* of a
  successful binding and the only target form a resolved
  observation may carry. Fields, all required except as noted:
  `symbol`, `declaring_type` (may be explicitly null = unknown
  type, which is still a complete statement), `file_path`,
  `revision`.

Binding rule: the algorithm in §2.1a consumes a
`TargetReference` and the observation-level repository context;
it produces a `BoundTargetIdentity` only on exactly-one-candidate.
The repository constraint comes from the observation-level
repository (required on every constructed observation — see §2.2a),
never from the target: a target carries no repository field, so
there is no target-level repository to omit or fill. Likewise,
a missing target revision can never be filled in from whichever
candidate happens to survive: if the reference omits `revision`,
candidates are matched without a revision constraint, but the
resulting `BoundTargetIdentity.revision` must equal the
observation-level repository revision — otherwise the
observation-level repository itself is unbound and construction
fails closed. No silent completion, in either direction.

### 2.2 Per-variant payload contract (D3-07)

- `RESOLVED_TARGET_BINDING`: all payload fields required except
  `declaring_type` (optional); `target_declaration` is not a separate
  field — the declaration identity *is* the payload (see decision 5).
  `limitation_codes` must be empty; absolute forbidding of unknown
  fields at every nesting level (`extra="forbid"` posture).
- `PARTIAL_OBSERVATION`: bound endpoint fields present where bound,
  absent optionals omitted (never nulled); `limitation_codes`
  non-empty from the closed taxonomy; `missing` names unbound parts
  per the closed mapping in §2.2a (no other values exist); unknown
  `missing` entries fail closed. A partial record always carries
  `repository` and `resolver` (see §2.2a: their absence is a
  construction failure, not a partial state).
- `UNRESOLVED_OBSERVATION`: `observed_ref` only, per the shapes
  normatively defined in §2.2a; no bound endpoint fields at all —
  an unbound endpoint is inexpressible, so it cannot drift into a
  binding.
- Rejected in all variants: unknown fields, malformed digests,
  unsupported `record_version`, duplicate set members, unknown
  assumptions/codes, variant/payload mismatch.
- Identity: preimage `{"domain": "iap:definition",
  "record_version": "definition/v1", "variant": ..., <payload>}`
  with the D1 `canon/v1` byte layout (sorted keys, compact separators,
  UTF-8, NFC, float rejection; the variant is part of the preimage).
  For `UNRESOLVED_OBSERVATION` the preimage carries the full
  `observed_ref` object; its optional fields are omitted when
  absent. Duplicates are defined by exact preimage equality — no
  fuzzy or normalized comparison anywhere in the identity path.
  Line ranges use the D1 coordinate contract (1-indexed positions,
  exclusive ends; pure line spans, no columns). Read-time validation
  rejects invalid data — never repaired, never normalized into a
  valid record, never passed to the kernel.

### 2.2a Executable schema invariants (D3-13)

- `observed_ref` shapes (identical to D1 §2.1, reused verbatim):
  `RANGE_REFERENCE {file_path, revision, start_line, end_line}`
  with pure line-span semantics, or `SYMBOL_REFERENCE {symbol,
  declaring_type?, file_path?, revision}` where omitted means
  unknown, never inapplicable.
- Partial binding states: a `PARTIAL_OBSERVATION` carries exactly
  the bound subset — bound fields keep their full types and
  constraints; unbound fields are absent (never nulled, never
  placeholder-valued). Two fields are envelope prerequisites, not
  bindable parts, and are therefore excluded from the `missing`
  taxonomy (D3-18):
  - `resolver` is mandatory whenever a constructor is actually
    invoked — the resolver identity is needed to interpret the
    result at all. Its absence is a construction failure yielding
    external typed unavailability, never a malformed record and
    never a `PARTIAL`.
  - `repository` is mandatory for `RESOLVED_TARGET_BINDING` and
    `PARTIAL_OBSERVATION` (a bound observation without a bound
    repository is malformed, not partial). It is optional *only* in
    `UNRESOLVED_OBSERVATION`, where the observation may carry nothing
    but an `observed_ref` with file/revision hints; identity is then
    computed over the present fields, and a repository-less record
    can never satisfy a premise (authorization requires exact
    repository match).
- Closed `missing` ↔ limitation-code mapping (exhaustive):

  | missing value | permitted codes |
  |---|---|
  | `target` (zero candidates) | `MISSING_INPUT` |
  | `target` (multiple candidates) | `AMBIGUOUS_TARGET` |
  | `lineage-source` | `MISSING_INPUT`, `INPUT_NOT_FOUND` |

  Any other pair is rejected, including any use of `repository`
  or `resolver` as a `missing` value. Codes outside the closed
  taxonomy are rejected everywhere (never defaulted, never
  coerced). The closed limitation vocabulary for v1 is exactly:
  `AMBIGUOUS_TARGET`, `MISSING_INPUT`, `INPUT_NOT_FOUND`,
  `NOT_YET_RESOLVED`, `UNSUPPORTED_CONSTRUCT`,
  `TRANSIENT_FAILURE`, `INCOMPATIBLE_CONTEXT` (taxonomy
  `limitation/v1`, each code with stable category/retry/downstream
  semantics; see D1).
- No hidden or optional fields: the payload schemas list every
  permitted field. The complete list of conditionals is:
  `declaring_type` (bound-symbol form; absent, or explicitly null
  meaning unknown type — both still complete statements),
  `build_identity` (single-repo observations only), and the
  `observed_ref` optionals (`declaring_type?`, `file_path?` in
  `SYMBOL_REFERENCE`). Anything else present is a schema violation.
- Nested objects (`repository`, `resolver`, `lineage`,
  `lineage.source[]`) reuse the D1 field contracts verbatim:
  same names, same types, same maxima, same revision/pinning rules.
  The definition record adds no nested shape of its own.
- Cross-field invariants (each a named negative test):
  (a) every entry revision equals its repository revision;
  (b) single-repo lineages equal the observation-level repository;
  (c) entry `(evidence_id, evidence_digest, artifact_digest,
  repository.name, repository.revision)` tuples are unique;
  (d) `scope` never participates in identity or dedup keys;
  (e) `target.file_path` (when supplied) equals the repository file
  of every lineage entry used for binding;
  (f) `requires_target_declaration=true` rows never authorize a
  record whose declaration identity is absent (see decision 5);
  (g) `limitation_codes` empty iff variant is
  `RESOLVED_TARGET_BINDING`.
- A record is invalid unless its fields validate *and* every
  applicable invariant above holds: independent field validity is
  necessary but not sufficient.

### 2.3 Binding, lineage, and context (D3-03 — no weaker variant)

The definition record reuses the D1 lineage and resolver-identity
requirements wholesale; nothing below is relaxed for definitions.
Each lineage entry binds `(evidence_id, evidence_digest,
artifact_digest, scope, revision, repository{name, revision})` with
the same full-tuple identity, duplicate rejection, scope-excluded-
from-identity, and entry-revision-equals-repository-revision rules.
Each observation binds, all independently verified at read time:

- immutable repository identity and revision;
- source artifact digest (exact bytes consumed);
- evidence-row identity and content digest (distinct from the source
  artifact digest; never substituted for each other);
- file path (repo rule) and line-range semantics (§2.2);
- the evidence row's relationship to the referenced source artifact
  (which artifact table, which lookup, pinned how);
- the investigation's expected repository/revision/build context
  where applicable (cross-scope replay fails closed);
- every artifact materially affecting resolution, including
  generated-code maps or symbol indexes if used;
- the normalizer triple: reviewed normalizer version, normative
  contract digest, executable/package artifact digest plus its
  dependency/configuration manifest hash (see §3).

### 2.4 Evidence-to-source binding procedure (D3-16)

"Which artifact table, which lookup, pinned how" is executed as
follows, in order; any step failing to verify yields abstention,
never a weaker claim:

1. Resolve the row by `(evidence_id, tenant_id, investigation_id)`;
   reject on missing row, tenant/investigation mismatch, or
   ambiguous multiples.
2. Recompute the row's content digest from stored bytes and compare
   to `evidence_digest`; mismatch fails closed (a valid digest and
   valid content are insufficient independently).
3. Read the row's artifact pointer (`artifact_digest` plus source
   locator: artifact table, path, and pinned revision); reject rows
   with no usable pointer.
4. Fetch the source bytes at the pinned revision; recompute and
   compare to `artifact_digest`. Source unavailable → `PARTIAL`
   with `MISSING_INPUT`. Source bytes differ → the row is stale:
   `PARTIAL` with `MISSING_INPUT`; the specific sub-cause
   (`source-bytes-changed`) travels in the telemetry cause string,
   never as a record field — the closed limitation taxonomy gains
   no per-incident codes. A stale row no longer denotes its source;
   this is a binding failure, not a repairable mismatch.
5. Verify the row's range against the fetched bytes: window text
   equality (NFC-normalized), line span within bounds, symbol
   present at the claimed location per §2.1 matching. Any
   divergence fails closed.
6. Verify the investigation's expected repository/revision/build
   context matches the source's; cross-scope or cross-investigation
   replay fails closed (`INCOMPATIBLE_CONTEXT`).
7. Verify every additionally consumed artifact (generated-code
   maps, indexes) is itself pinned and digest-bound; unpinned
   auxiliaries fail the binding.
8. Only then construct the observation, embedding all verified
   digests; recompute `observation_id` and, on read, re-verify
   everything above (never trust the stored record's word for it).

Layer assignment for §4.0a: steps 1–4 and 7 are acquisition-layer
work (they resolve rows, fetch bytes, and validate external
artifacts, bounded by the mapping artifact's explicit budgets);
steps 5, 6, and 8 are pure-layer work (deterministic checks over
the validated bundle, no I/O). Step 5's window comparison
operates on bytes already resident from step 4 — it never
re-fetches.

### 2.4a Byte-to-line contract (D3-21)

The artifact digest always covers exact source bytes, including
BOM and line endings. Line windows are derived from those bytes
by a fixed, specified procedure — normalization may affect
comparison, never identity:

- Decode as UTF-8, strict, no fallback encodings in v1. Bytes
  that are not valid UTF-8 cannot be mapped to lines: abstain
  (`INPUT_NOT_FOUND`), do not guess an encoding.
- Strip a single leading UTF-8 BOM (`EF BB BF`) before line
  splitting; byte offsets account for the stripped prefix.
  Line numbers count logical lines after BOM removal.
- Split lines on universal newlines (`\r\n`, `\r`, `\n`); the
  newline characters themselves are terminators, not content.
  1-indexed positions, exclusive ends (D1 coordinate contract).
- NFC normalization is permitted *for comparison only* (window
  text vs symbol matching). The stored snippet must be an exact
  substring of the decoded source text: normalization is never
  applied to stored evidence, and the artifact digest is always
  over raw bytes — so normalization can never make a stale or
  mismatched source row appear bound.
- Snippet-to-window binding: the stored snippet, with its recorded
  `start_line`/`end_line`, must equal the exact decoded lines in
  that span. Off-by-encoding, off-by-newline, or off-by-BOM
  mismatches fail closed as unbound (`MISSING_INPUT`), not as
  approximate matches.

## 3. Resolver for v1: evidence-rebinding normalizer (proposed)

No parser-based resolution in v1. The constructor binds an
*already-bound* native evidence row (range read with file path,
immutable revision, line window, snippet containing the symbol) plus
the claim target into the typed envelope; anything less abstains
(`PARTIAL`/`UNRESOLVED` with the closed limitation vocabulary; see
§2.2 for the per-variant contracts). Soundness boundary = explicit
input requirements (fully unit-testable), not syntax recognition.
The normalizer carries the three-identity binding: reviewed
normalizer version, normative contract digest, and
executable/package artifact digest (verified at runtime per the D1
pinning model — artifact substitution, contract-only change, and
dependency/configuration drift all fail closed). Its test suite is
described as *binding and abstention conformance*, never as
declaration-semantic conformance. A Tree-sitter-backed declaration
resolver is a follow-up capability with its own language/version-
specific conformance vectors, explicitly not claimed here; it must
not be implied by sharing a resolver name, bundle, or dispatch row.

### 3.1 Runtime digest contract (D3-14)

Normative digest inputs (byte-exact):

- `contract_digest`: `sha256` over the canonical JSON bytes
  (sorted keys, compact separators, UTF-8) of the reviewed
  normative contract document for this resolver bundle version.
- `artifact_digest`: `sha256` over the exact executable bytes
  (single-file normalizer) or over the canonical manifest of the
  package bytes (multi-file: sorted `path → sha256(content)`
  entries, canonical JSON).
- dependency/configuration manifest hash: `sha256` over canonical
  JSON of the sorted `{path: digest}` dependency map plus the
  canonicalized configuration surface the normalizer reads. Any
  relevant dependency, configuration, or environment-pinned input
  change alters this hash by construction.

Verification points, all mandatory, none substitutable for another:

1. At dispatch-table load: the row's contract digest must equal
   the reviewed contract digest; mismatch fails closed before any
   evaluation.
2. At normalizer load: the running code's artifact digest plus its
   dependency/configuration manifest hash must equal the approved
   values; mismatch refuses to construct any observation.
3. Per construction: the record's embedded
   (`contract_digest`, `artifact_digest`) are re-verified against
   the loaded values; drift fails the single construction — typed
   unavailability, never silent fallback.

The record never supplies its own authority: a record-carried
digest that matches nothing approved is rejected; a declared
version string without digest match authenticates nothing.
Conformance tests must independently cover contract-only change,
executable substitution, dependency drift, and relevant
configuration drift — four distinct negative cases, not one
generic "mismatch" test.

### 3.2 Runtime trust anchor: manifest derivation and loaded-code verification (D3-20)

Hashing a manifest is meaningless unless the manifest faithfully
represents what is actually loaded. The trust flow is therefore
fixed in this direction only: reviewed dispatch row (expected)
← runtime introspection (actual) → fail closed on mismatch. The
record under evaluation is never an input to its own trust
decision.

- Manifest derivation (normative): at normalizer load, walk the
  loaded package files and hash exact bytes; read the dependency
  lock file when present, otherwise the installed distribution
  metadata (`name==pinned-version` entries); and canonicalize the
  explicitly enumerated configuration surface (see below) as
  sorted `key=value` UTF-8 lines. The three digests (artifact,
  dependency map, configuration surface) combine by canonical-JSON
  hashing in that fixed order. The derivation procedure itself is
  part of the reviewed contract: two builds of the same approved
  inputs must produce byte-identical manifests.
- Configuration/environment allowlist: the mapping artifact
  enumerates every environment variable, config file, and
  command-line surface the normalizer is permitted to read. The
  implementation is statically constrained to that list (a test
  asserts the module's environment/config reads are a subset of
  the declared set — the same static-import technique as the D1-G
  and G6-harness proofs). Canonicalization: sorted `KEY=value`
  lines, UTF-8, NFC; absent values are omitted, never defaulted.
- Unrecognized inputs fail closed: any environment variable,
  config key, or file the normalizer *reads* that is not in the
  allowlist or manifest is a contract violation — refuse
  construction, do not ignore-and-continue. Inputs the normalizer
  never reads are out of scope only while the static read-set test
  proves it.
- Mutable external modules, runtime-loaded plugins, and
  configuration reloads are forbidden unless pinned in the manifest
  *before* load: the manifest is snapshotted at import; any reload
  re-derives and re-verifies, and a mismatch refuses further
  construction (typed unavailability for in-flight evaluations,
  never silent continuation on changed code).
- Loaded-code verification (not manifest self-attestation): the
  verifier hashes the actually imported normalizer module bytes
  (and the resolved dependency artifacts on disk), and compares
  against the dispatch row's approved digests. Comparing a manifest
  supplied by the same deployment configuration being checked is
  explicitly insufficient — the expected values come from the
  reviewed row; the actual values come from runtime introspection;
  the record being evaluated contributes neither.

## 4. Phase 0 — shadow dual-run (no authority change)

A pure `evaluate_definition_typed(bundle, target,
resolver_id, dispatch_table, registry, binding)` runs beside the
native gate at the verify activity, where `bundle` is the validated
bundle defined in §4.0a (never raw rows): native outcome vs typed
(authorize) outcome are logged per definition claim. It never feeds
a verdict, never persists rows, never executes resolvers, and
reuses only already-available banked evidence — no extra evidence
fetch, no resolver invocation beyond the pure constructor, no
database write, no routing change, no authoritative decision.
Telemetry failures and typed-evaluation exceptions record a marker
and leave the native path exactly unchanged (fail-closed for the
shadow, fail-open for native). Inputs are bounded by the record
schemas (`max_length` caps throughout); source snippets and secrets
are never written to telemetry — digests and identifiers only. The
pure constructor performs no I/O and therefore cannot hang on it;
oversized input fails validation before any work. G6 is precedent
assessed per hook, not blanket authorization: this hook requires
the isolation checks above (exception isolation, no extra calls,
no extra persistence, exact flag-off behavior) as its own exit
evidence.

### 4.0a Two-layer evaluation: acquisition vs pure construction (D3-22)

Source binding (§2.4) performs I/O; the shadow evaluator must not.
Both requirements stand, so the work is explicitly split into two
layers with a validated bundle as the only interface between them:

1. **Acquisition/binding layer (may perform I/O, bounded).**
   Resolves the evidence row, retrieves the pinned source artifact,
   validates the full §2.4 procedure, and emits an immutable,
   digest-bound validated bundle: the row bytes, source bytes (or
   their verified digests plus the byte-range window needed for
   comparison), lineage entries, resolver identity, and a
   currency attestation (repo/revision match against the
   investigation's expected context, checked at acquisition time).
   Explicit budgets: per-fetch timeout, total acquisition budget,
   maximum row/window sizes — all enumerated in the mapping
   artifact, none left to implementation defaults. On timeout or
   unavailable artifact: no silent skip, no fallback fetch, no
   changed native verdict — record a shadow-side
   `unavailable-input` outcome and stop.
2. **Pure construction + authorization layer (no I/O).**
   Consumes *only* the validated bundle: deterministic checks,
   identity computation, dispatch lookup, and authorization. Pure
   function of its inputs; cannot hang on I/O because it performs
   none; oversized input fails validation before any work.

For Phase 0 the split has one additional, non-negotiable rule:
the shadow path performs **zero acquisition of its own**. It
consumes only rows and artifact bytes the native path already
fetched in the same turn (already-banked *and* already-loaded).
If the native path abstained before fetching, or the bytes are
not resident, the shadow records unavailable-input and stops —
it must not fetch, parse, or resolve on its own. This is what
makes "reuses already-available banked evidence" auditable
rather than aspirational: presence in memory is the criterion,
not presence in the store.

Currency without a freshness clock: the evaluator knows its
prevalidated inputs are current because the bundle carries the
repo/revision match attestation from acquisition time *and* the
pure layer re-verifies every digest it touches (row, artifact,
lineage) before use. A bundle whose digests no longer verify is
stale by definition, regardless of wall-clock age — rejected,
never refreshed implicitly.

### 4.0b Activation flags: shadow vs authority are separate controls (D3-11)

- `IAP_SHADOW_TYPED_DEFINITIONS`: enables non-authoritative
  shadow capture (dual-run + telemetry) only. Default off.
- `IAP_TYPED_DEFINITIONS`: enables typed definition-gate behavior,
  and only after separate Phase 1 authorization. Default off.
- Shadow mode never implies authority mode: enabling the shadow
  flag changes no verdict, route, or persisted row, with or
  without the authority flag. Enabling the authority flag without
  Phase 1 authorization is a configuration error and must fail
  closed at startup (refuse to run the governed path, loudly).
- Flag convention (the project's established model, stated here
  explicitly because the design depends on it): strict `"1"` only;
  empty, missing, `"true"`, `"yes"`, and any other value all mean
  off. Precedence: authority behavior requires its flag *and* its
  phase authorization; shadow capture requires only its own flag.
  Startup interaction, defined exhaustively (D3 consistency check):
  a malformed value in either position is treated as off *and*
  logged loudly at startup (visible misconfiguration, zero
  authority risk — malformed input can never activate anything);
  a valid `"1"` in the authority position without Phase 1
  authorization is a hard startup failure (refuse the governed
  paths, do not silently fall back to native, which would mask the
  misconfiguration). Telemetry records which flags were set for
  every measured run (flag configuration is part of the run
  identity, like the D1 resolver bundle version).

### 4.1 Shadow comparison contract (explicit outcome vocabulary)

Native and typed outcomes are recorded as explicit statuses, never
booleans. Separate statuses exist for native errors
(`NATIVE_ERROR`) and shadow-evaluation failures (`SHADOW_FAILED`)
so failures can never be misclassified as ordinary abstentions:

| Native outcome | Typed outcome | Required interpretation |
|---|---|---|
| Gate accepted | Authorized | Agreement; still not independent proof of the claim |
| Gate accepted | Abstained / rejected | Native-only; inspect stricter binding or contract differences |
| Gate rejected | Authorized | Typed-only; high-priority investigation for unintended acceptance |
| Gate rejected | Abstained / rejected | Agreement on non-acceptance; classify the cause |

Phase 0 exit requires, in addition to §7: all observed
disagreements carry a recorded cause and supporting trace; no
unexplained typed-only acceptance remains (typed-positive /
native-negative is investigated as potential false acceptance,
never filed as ordinary parity variance); all known unsafe or
out-of-contract inputs abstain; the golden matrix passes including
the *expected* differences between the legacy and typed paths
(parity means understood, bounded differences — the native gate
accepts unpinned ranges the typed path must reject, so 100%
equality is neither expected nor desired); the live run set has a
documented denominator with PASS and FAIL coverage plus capture-
health results; flag-off equivalence passes at every observable
boundary; the negative-traceability matrix is delivered. A green
unit-test suite alone is not evidence of live parity or safe
rollout.

## 5. Phase 1 — NOT AUTHORIZED (approval package required)

Phase 1 is not authorized by this document and is not authorized by
Phase 0 passing. Its separate approval package must define:

- the exact behavior replaced by typed authorization (the definition
  evidence gate at the `check_claim_kind` call sites — named
  explicitly — and nothing else; the existing gate stays a gate,
  never silently promoted into proof);
- the semantic-boundary demonstration: the substituted gate must
  accept and reject the *same semantic class* as the native gate
  it replaces (a bound, pinned source range showing the symbol).
  Because v1 establishes only binding — never declaration
  semantics (Option B, §2) — the approval package must show that
  no claim passing through the typed gate gains a stronger
  interpretation than it had under the native gate. Any use that
  requires declaration proof is out of scope for v1 and needs the
  separately reviewed parser-backed capability, not a broader
  reading of this contract;
- which claims are eligible and which must abstain;
- the behavior for missing, malformed, stale, unresolved, or
  unsupported observations;
- the prohibition on silent native fallback when the typed-definition
  flag governs the claim, with explicit abstention/demotion
  behavior for each case (a missing or rejected observation must
  never be converted into proof of absence);
- flag-off equivalence and rollback evidence;
- live-context and concurrency checks where verification state can
  change during evaluation;
- the precise owner authorization boundary for enabling typed
  definition observations.

An authorized observation may satisfy that gate *only within the
approved contract*; it must never independently establish
implementation behavior, execution, success, or absence. The
family-scoped flag is `IAP_TYPED_DEFINITIONS` (default off),
independent of `IAP_TYPED_EDGES`: enabling definition handling
must not activate call-edge behavior and vice versa. Defaults:
both off; neither implies the other; mixed-family runs evaluate
each claim kind under its own flag; when neither flag governs, the
native path applies unchanged (documented fallback, not a silent
default).

## 6. Owner decisions required before implementation

1. **Record schema**: approve the `definition/v1` envelope, the
   `RESOLVED_TARGET_BINDING` terminal variant, the per-variant
   contracts (§2.2), and the canonical target rules (§2.1) — or amend
   them. No schema work starts on unapproved fields. (Reviewer
   position: conditional approval subject to D3-01–D3-03 and D3-07,
   all now specified above; the field list alone was not sufficient.)
2. **Resolver scope**: evidence-rebinding normalizer for v1 (recommended;
   testable now, no new soundness claims, binding/abstention
   conformance only) vs Tree-sitter declaration resolver now
   (defensible, but needs its own conformance suite first). (Reviewer
   position: normalizer, with explicit limited semantics.)
3. **Phase 0 hook**: same risk class as the accepted G6 hook
   (compute-beside, logged, never feeds back) at the verify activity,
   *plus* the hook-specific isolation contract in §4
   (exception isolation, no extra calls, no extra persistence, exact
   flag-off behavior, bounded inputs, redacted telemetry). Confirm
   the precedent covers this new surface once those checks are
   evidenced. (Reviewer position: approve only after that
   hook-specific review.)
4. **Authorize-machinery shape**: dedicated `authorize_definition_use`
   entry point with definition-specific validation, reusing the
   shared security primitives — canonicalization + `canon/v1` layout,
   sha256 digest convention, exact row lookup, scope checks, dispatch
   verification, and the stable error-code taxonomy — wherever their
   contracts genuinely match. No generic abstraction merely to reduce
   line count; no forked integrity logic that can drift. Cross-family
   attack tests must show the definition path rejects the same
   binding/integrity attacks as D1, without implying shared helpers
   are an independent semantic checker. (Reviewer position: separate
   checks, shared primitives.)
5. **Dispatch rows**: reviewed set for (profile, definition) with
`requires_target_declaration=true`. For definitions the
declaration identity *is* the payload (symbol + file + range +
revision); there is no separate `target_declaration` schema field.
The flag is a dispatch requirement: a resolved record must carry a
bound declaration identity — null/absent never authorizes.
`PARTIAL` names it via `missing: ["target"]`; unresolvable
targets are `UNRESOLVED`, never best-effort matches. A future
claim type that genuinely needs no target gets a separately
reviewed contract; this one is never weakened in place.
(Reviewer position: `true`.)
6. **Flag model**: confirm the two-flag split
(`IAP_SHADOW_TYPED_DEFINITIONS` for capture-only dual-run,
`IAP_TYPED_DEFINITIONS` for gated authority, both default off,
strict `"1"`, shadow never implying authority, missing/invalid
treated as off with startup attestation). Telemetry records the
flag configuration of every measured run.
7. **Live-sample minimums**: approve the §7 floor (≥20
definition-claim turns spanning native accept/reject and PASS/FAIL
runs, zero capture errors) or set alternates before collection
begins. No measurement runs until this is decided.
8. **v1 semantic contract**: confirm Option B as specified — a
bound source-range association that explicitly does *not*
establish declaration semantics and cannot satisfy a
declaration-requiring gate — or direct Option A instead, which
requires first building the trusted upstream attestation
capability the normalizer would verify (a new, separately
reviewed dependency, not available today).

## 7. Acceptance (Phase 0 exit, D3-15: restated, no shorthand)

"Parity matrix green" alone does not pass this gate. Exit requires
*all* of the §4.1 conditions, evidenced, not asserted:

- every observed disagreement carries a recorded cause and
  supporting trace;
- no unexplained typed-only acceptance remains (typed-positive /
  native-negative is investigated as potential false acceptance,
  never filed as ordinary parity variance);
- all known unsafe or out-of-contract inputs abstain;
- the golden matrix passes *including the expected differences*
  between legacy and typed paths (the native gate accepts unpinned
  ranges the typed path must reject, so 100% equality is neither
  expected nor desired);
- the live run set has a documented denominator with PASS and FAIL
  coverage plus capture-health results (minimums below);
- native errors and shadow failures are reported separately —
  neither counts as agreement or ordinary abstention;
- flag-off equivalence passes at every observable boundary;
- the negative-traceability matrix is delivered (same bar as D1:
  every requirement maps to named tests).

Minimum live-run sample (proposed; owner approval required before
collection starts): at least 20 *eligible* definition-claim turns
spanning both native accept and native reject outcomes, drawn from
runs covering PASS and FAIL terminal states. Definitions, fixed
before collection: an *eligible* turn is one with both a native
outcome and a typed outcome recorded under the frozen comparison
rule; `NATIVE_ERROR` and `SHADOW_FAILED` turns are *excluded* from
the denominator and reported as separate counts (excluded-error
count is itself a required report field). The floor counts eligible
turns only — error turns never contribute to it, so the sample
cannot be met by silently dropping difficult cases.
`capture_errors == 0` (else the run-set is recovered or rerun,
never silently trimmed); native errors and shadow failures reported
separately from the denominator. Fewer eligible turns, missing
outcome coverage, or capture errors mean INSUFFICIENT EVIDENCE —
never a pass.

Acceptance evidence package (reviewer checklist, mapped to
deliverables):

| Evidence | Minimum acceptance condition |
|---|---|
| Schema and canonicalization | Golden vectors for all variants; invalid combinations and malformed records rejected |
| Target binding | Zero, unique, and ambiguous candidates tested, including omitted `declaring_type` |
| Provenance and context | Wrong artifact, revision, scope, investigation, or evidence-row relationship rejected |
| Resolver identity | Contract, executable, dependency, and configuration substitution tests fail closed |
| Authorization | Every §8 check has a negative test; cross-family binding attacks covered |
| Shadow isolation | Exception, telemetry failure, disabled flags, and oversized input cannot affect native behavior |
| Live comparison | Counts, denominators, PASS/FAIL coverage, capture health, and all disagreement causes recorded |
| Rollback and compatibility | Exact flag-off observable equivalence; no extra calls, writes, routing changes, or altered verdicts |
| Traceability | Each requirement maps to specific test names, test results, artifacts, and residual risks |

Adversarial attack classes (required, not optional): correct symbol
text in a comment, string, documentation block, or reference rather
than a declaration; same symbol in a different file, scope,
repository, revision, or build; two plausible targets or duplicate
declarations; valid evidence-row hash paired with the wrong source
artifact; source artifact changed while a cached evidence row
persists; wrong profile revision, dispatch row, policy digest,
resolver contract digest, or executable artifact digest; cross-scope
and cross-investigation replay; unsupported or ambiguous source
constructs; variant/payload mismatch, unknown fields, duplicate set
entries, malformed hashes, canonicalization edge cases; shadow
evaluator exception, timeout, telemetry failure, oversized input;
flag disabled with contractually exact observable equivalence
(no extra calls or writes). Include mutation tests that change one
binding at a time while keeping the remaining fields valid —
self-consistent but wrongly bound records must be rejected.

## 8. `authorize_definition_use` — ordered decision procedure

The predicate runs these checks in order; every check has an
explicit failure result and a negative test. A record is never
accepted merely for being well-formed with a matching hash:

1. Schema and variant validity (per §2.2 contracts).
2. Canonical content digest and record integrity (recomputed).
3. Exact observation and evidence-row identity resolution.
4. Source artifact, repository revision, and lineage integrity (§2.3).
5. Investigation scope and expected-context currency.
6. Exact profile/version/dispatch-row and policy binding.
7. Normalizer version, contract digest, and artifact digest
   verification (§3).
8. Claim-kind and target binding (§2.1 matching rules).
9. Assumption and limitation validation (closed vocabularies).
10. Family-specific eligibility and abstention rules (no silent
    fallback, no proof-of-absence from missing observations).
