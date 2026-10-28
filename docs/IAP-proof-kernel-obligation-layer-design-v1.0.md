# Obligation-Layer Design Proposal v1.3 (DRAFT — for owner review, no code)

Amends v1.2 per review (checker-input completeness, snapshot/TOCTOU,
disagreement genesis, repository-scope semantics). Sections changed from
v1.2 are marked **[v1.3]**. Still design only; implementation needs a
separate explicit GO after design acceptance.

## Amendments in v1.3 (review resolution)

1. **Checker input contract.** The guarded entry names the complete
   immutable `CheckRequest` construction, including rule-bundle version,
   model encoding, reconciliation state, mapping authority, known
   normalization operations, derivation dependencies, and expected
   context. Every value is either derived from the certificate/policy or
   supplied by a trusted policy/context input; no omitted checker field
   is left to an unstated default. (§Gate)
2. **Snapshot integrity.** The guarded entry accepts composite evidence
   and trusted-context payloads as canonical byte sequences, not mutable
   caller-owned Python mappings. It parses and validates those bytes once
   into a deeply immutable snapshot. The checker, obligation
   expansion, evaluator, and freshness comparisons consume that same
   snapshot; no caller-owned mutable nested collection is read again.
   Hash and manifest checks validate that snapshot. (§Snapshot, §Gate)
3. **Conflict genesis.** The existing checker already conjoins its two
   validator paths and rejects disagreement before returning VALID.
   Therefore validator-disagreement mutant vectors cannot generate a
   `CONFLICTED` obligation after checker acceptance. v1.3 removes dual
   obligation evaluators and `CONFLICTED`/adjudication promotion from
   v1. The v1 evaluator produces `SATISFIED`, `OPEN`, or
   `UNSUPPORTED`; `CONFLICTED` is deferred until a separate,
   checker-valid conflict source is specified. (§States, §Evaluation,
   §Gate)
4. **Repository scope.** Define repository identifiers as the
   profile/registry-owned canonical `repository_id` values already used
   by the application, not absolute checkout paths. Each identity is
   bound to evidence ID and content digest by trusted provenance before
   the kernel performs exact equality. No textual prefix matching or
   inferred repository containment is allowed. (§Policy, §Gate)

v1.1/v1.2 amendments retained where applicable: required evidence,
`OBLIGATIONS_ABSENT`, exact promotion states, derivation-bound IDs,
trusted in-kernel derivation, trusted `DecisionPolicy`, and internal
checker execution. Obligation adjudication is not an active v1.3 path.

## Problem

K13 ("fast-path/confidence bypass of open obligations denied") remains
PARTIAL pending implementation and the separate legacy-writer closure.
This proposal defines the missing in-kernel promotion obligations; it
does not claim the gate is implemented or authoritative. The A–T F row
(wrong witness) remains blocked until that implementation is separately
authorized and verified.

## Contract

### ProofObligation (new frozen model, `domain/proof/obligations.py`)

`obligation_id` = `obl:<derivation-id>:<rule-id>:<ordinal>`; `kind`;
`statement`; closed v1.3 `status` vocabulary (`OPEN`, `SATISFIED`,
`UNSUPPORTED`); `evidence`; and `derived_from`. Records are immutable;
no timestamps, no confidence scores, no caller-asserted status, and no
v1.3 `supersedes` chain. `CONFLICTED` and `INVALIDATED` are deferred
status extensions and are not valid v1.3 model values.

### Kind table (`expand_obligations`)

| Rule | Obligations (each must be SATISFIED before promotion) |
|---|---|
| definition/v1 | O1 single RANGE_READ premise bound; O2 symbol shown in cited text; O3 premise revision joins claim scope (pinned revision required for promotion) |
| call_edge/v1 | O1 all premises NORMALIZED_CALLSITE, unique IDs; O2 caller/callee bound in cited text; O3 each premise revision joins claim scope (pinned revision required for promotion) |
| behavior/v1 | O1 code/runtime/mapping triple present, unique; O2 joins hold (target, repo, revision, build, execution, environment); O3 mapping authority approved |
| absence/v1 | O1 complete-empty coverage; O2 zero matches; O3 scope/config/exclusion agreement; O4 no relevant exclusion |
| history/v1 | O1 single commit-history premise; O2 full-SHA pins; O3 repo/revision/commit/file/subject equality; no locators |

`expand_obligations(rule_id, claim, premises, derivation_id)` is pure,
deterministic, and assigns ordinals in table order. Unknown kinds
(including CAUSAL) expand to one `UNSUPPORTED` obligation and deny before
the checker.

### DecisionPolicy — trusted configuration **[v1.3]**

The v1.1 request-carried adjudication allowlist and obligation
adjudication are deferred because v1.3 has no conflict-promotion path.
The policy owns trusted mapping-provenance configuration for the guarded
BEHAVIOR check:

- `DecisionPolicy` is a recursively immutable trusted value:
  `policy_id` (stable string, e.g.
  `policy:<tenant>:<name>:<version>`); `repositories` (non-empty tuple of
  canonical `repository_id` values this policy speaks for); and
  `approved_mapping_provenance_refs` (immutable tuple used to construct the
  checker's `mapping_authority` input for BEHAVIOR; default empty). The
  v1.3 promotion path has no obligation-adjudication input; that path
  and its authority allowlist are deferred with conflict adjudication.
- Constructed by the operator (file/config, owned by the B2 wiring
  work: the wiring selects the tenant's policy and passes it alongside
  the tenant's evidence). Pure-domain v1 constructs it explicitly in
  tests — no ambient config, no environment lookup, no singleton.
- Trust boundary, exact: the caller supplies evidence
  (transport, verified, never trusted); the operator supplies the policy
  and trusted checker context, including repository bindings from the
  tenant-owned `RepositoryIdentity`/profile registry selected for the
  evidence provider. The gate binds them (§Gate, step 2). Tenant-to-policy
  selection is OUTSIDE the kernel (B2 wiring): the kernel binds
  evidence repositories to the selected policy's `repositories` list,
  and denies `POLICY_DENIED` (reason `policy-scope:<repo>`) on mismatch.
  Kernel inputs carry no tenant marker by design (purity); the wiring
  is where tenant isolation lives, and the design says so instead of
  pretending otherwise.

#### Repository identity and scope matching **[v1.3]**

- A repository identity is the profile/registry-owned canonical
  `repository_id` already used in application evidence, distinct from
  `CodeProfile.repository` access/build locators. It is an opaque,
  tenant-owned identity, not a local checkout path, URL, display name,
  or inferred parent root. The proof kernel does not resolve aliases or
  locators; upstream trusted repository resolution must supply it.
- Canonicality means a non-empty ID that exactly matches the trusted
  profile/registry record selected for the investigation. The ID must be
  bound to each premise artifact via trusted provenance metadata keyed
  by evidence ID and content digest; the artifact's own `repository`
  field is not sufficient to authorize itself. If that trusted binding
  is unavailable, ambiguous, or digest-mismatched, deny
  `POLICY_DENIED` (`policy-repository-unbound`). This trusted provenance
  input is part of `EvidenceSnapshot` and its identity is bound to the
  policy decision. The trusted binding is supplied by the
  provider/application boundary; it cannot be inferred from the untrusted
  artifact alone.
- For each premise, the artifact's repository field must exactly match
  the trusted `repository_bindings` entry for that evidence ID and
  content digest. Proposition-scope repositories must also equal the
  trusted publication context's selected repository IDs; the caller
  cannot authorize a different repository by changing claim scope.
- The gate compares the evidence value with that trusted identity; it
  does not case-fold, trim, parse URLs, or normalize paths. There is no
  repository-ID contract version in the current model, so v1.3 does not
  invent one.
- Policy membership is exact string equality:
  `evidence_repository_id ∈ policy.repositories`. Prefix or ancestor
  matching is forbidden (`repo-a` does not authorize `repo-ab`). Every
  repository ID in proposition scope and every premise artifact must be
  explicitly listed. Repository-relative `file_path` values remain
  governed by the existing path-validation contract and do not create
  policy repository IDs.
- Policy construction validates a non-empty, duplicate-free tuple of
  canonical IDs. No wildcard, prefix, or implicit global policy exists
  in v1.3.

### States and genesis **[v1.3]**

The v1.3 promotion table is deliberately smaller than v1.1; each
supported status names its mechanical producer:

| Latest status | Producer (how evaluation reaches it) | Promotion |
|---|---|---|
| `SATISFIED` | The deterministic obligation evaluator establishes the predicate from the immutable, checker-accepted snapshot | permits |
| `OPEN` | A required agreement input is absent where the checker contract permits absence. Concrete case: revision-unpinned definition/call_edge certificates are checker-VALID because the K11 join is skipped when unpinned; O3 remains OPEN because promotion requires a pinned revision and an actual equality check. | denies `OPEN_OBLIGATION` |
| `UNSUPPORTED` | Pre-checker expansion of an unsupported kind (CAUSAL and anything unregistered); explicitly denies without consulting the checker. A VALID certificate cannot accompany this state. | denies `UNSUPPORTED_OBLIGATION`, always |
Evaluation runs only on checker-VALID inputs. For unsupported rule kinds,
expansion denies before the checker. For registered rules, checker denial
is returned without obligation evaluation. A checker-VALID certificate
can reach `OPEN` (unpinned revision). v1.3 emits only `SATISFIED`,
`OPEN`, or `UNSUPPORTED`; `CONFLICTED` and `INVALIDATED` are not status
values in this revision. Promotion is allowed only for `SATISFIED`.

### Adjudication — deferred from v1.3

No obligation-adjudication object, authority allowlist, or `PROCEED`
route is part of this design revision. The prior proposal depended on
`CONFLICTED` arising after checker acceptance, but the existing checker
conjunction rejects validator disagreement first. Reintroduce
adjudication only with a separately reviewed, checker-valid conflict
source and an explicit trusted-authority contract. Reconciliation
adjudication remains the distinct scalar input to the existing
publication linearization step; it does not adjudicate proof obligations.

### Identity and lineage

Obligation IDs remain derivation-bound:
`obl:<derivation-id>:<rule-id>:<ordinal>`. The v1.3 evaluator emits only
one immutable genesis record per obligation and does not persist or read
status chains. Digest-chained supersession and fork detection are
deferred to the persisted G5 layer; when introduced, forks must deny
stale rather than selecting a convenient head.

### Snapshot and evaluation — trusted, single evaluator **[v1.3]**

- The guarded entry accepts canonical byte sequences for composite
  evidence and trusted checker context, not mutable caller-owned
  dictionaries. It parses and validates them once into an immutable
  `EvidenceSnapshot`. Policy, expected context, reconciliation flag,
  and policy scalars are immutable typed values copied into the same
  snapshot before evaluation. Non-canonical encodings, unsupported
  values, duplicate keys, or schema violations deny closed. Retain the
  canonical bytes and digest for every bound input.
- All subsequent operations consume only that snapshot: repository
  scope validation, `CheckRequest` construction, checker execution,
  obligation expansion/evaluation, freshness assertions, and the
  linearization sub-step. No later read from caller-owned objects is
  permitted; no caller-owned mutable composite collection is accepted or
  remains aliased. The public byte inputs are immutable.
- `expand_obligations` + `evaluate_obligations` are one deterministic
  kernel implementation over the snapshot. The checker already has an
  independent second validator path; it must return `VALID` before
  obligation evaluation. The obligation evaluator is not duplicated in
  v1.3. Its outcomes are `SATISFIED`, `OPEN`, or `UNSUPPORTED` as
  defined above; evaluator exceptions deny closed as `CHECKER_FAILURE`,
  never as `CONFLICTED`.
- The v1.0 caller checklist stays withdrawn. No caller input can omit
  obligations or assert a status. Obligation adjudications and authority
  allowlists are not inputs in v1.3; see the deferred adjudication
  contract above.

### Gate — guarded promotion entry **[v1.3]**

New public function (pure domain, no production contact):

```
decide_guarded_promotion(
    policy, evidence_bytes, trusted_context_bytes,
) -> (PublicationDecision, reason)
```

`evidence_bytes` encodes `rule_id`, proposition, certificate (optional
only to produce the explicit `OBLIGATIONS_ABSENT` denial), manifest, and
artifact mapping as one canonical JSON document. `trusted_context_bytes`
encodes `rule_bundle_version`, `model_encoding`, `reconciliation_state`,
`known_normalization_ops`, `derivation_deps`, `repository_bindings`
(evidence-ID/content-digest to trusted `repository_id` provenance),
  `expected_context_id`, `reconciliation_adjudicated`, `policy_eligible`,
  and `policy_reason`. The application obtains those trusted bytes from the
operator-owned context/provider boundary, not from caller evidence. The
context producer binds `repository_bindings` to the authenticated
provider/profile selection, binds `expected_context_id` to the current
publication context, and derives reconciliation and policy eligibility
from their trusted services; callers cannot set those scalars. The
operator supplies `policy` as a recursively immutable object. The gate
derives `mapping_authority` as
`{"approved_provenance_refs": policy.approved_mapping_provenance_refs}`
from trusted policy; callers cannot supply it. Obligation inputs come
only from the snapshot: `rule_id` from
evidence; claim projection from the snapshot proposition; premise
envelopes from the certificate; and semantic premise data from artifacts
matched to those certificate evidence IDs. Manifest/certificate digest
and inventory joins are checked by `check_certificate`. No checker input
is read from ambient state or defaulted silently. The policy allowlist
replaces caller-supplied mapping authority only on this guarded path;
standalone checker behavior is unchanged.

Order, exact:

1. **Snapshot inputs.** The public guarded entry receives canonical bytes
   for composite caller evidence and trusted checker context, plus the
   operator-created immutable `DecisionPolicy`.
   It rejects non-canonical encodings, unsupported values, duplicate
   keys, and invalid schemas before semantic field inspection. Parse
   once into a recursively immutable `EvidenceSnapshot`. The snapshot
   includes evidence, trusted checker context (including the
   evidence-ID/content-digest repository bindings, expected context,
   reconciliation result, and policy eligibility), and policy; retain
   canonical bytes and digests for every included
   value. All subsequent steps use only this snapshot. No caller-owned
   composite object is accepted or reread; callers cannot race snapshot
   creation by mutating an aliased dictionary.
2. **Bind evidence to policy.** Resolve every premise's repository
    identity from trusted `repository_bindings`, matching both evidence
    ID and content digest. Require exactly one trusted binding for every
    premise evidence ID; reject duplicates, missing bindings, extra
    bindings, or digest mismatches. Require the artifact repository field
    to match those trusted identities, and the proposition scope
    repository to match when present (definition scopes carry no
    repository key; the artifact binding alone governs then). Validate
    canonical form, include repository
    identities from proposition scope, require at least one identity,
    and require every identity to equal a member of `policy.repositories`
    exactly; otherwise deny
    `POLICY_DENIED` (`policy-scope:<repository_id>` or
    `policy-scope:no-repository`). No policy denies `POLICY_DENIED`
    (`no-policy`). Tenant-to-policy selection is the B2 wiring's job.
3. **Expand.** `expand_obligations(rule_id, ...)`; any `UNSUPPORTED`
   in the set denies `UNSUPPORTED_OBLIGATION` immediately —
   the checker is not consulted (CAUSAL ordering fixed; abstention
   is explicit). `certificate=None` with a registered rule denies
   `OBLIGATIONS_ABSENT` (nothing to evaluate).
 4. **Check internally.** Construct `CheckRequest` using the snapshot
   and every field below. The guarded entry alone constructs it; the
   caller cannot supply a `CheckRequest` or outcome.

   | `CheckRequest` field | Source/binding |
   |---|---|
    | `certificate`, `proposition`, `manifest`, `artifacts` | Private typed materialization of `EvidenceSnapshot`; certificate IDs/digests are validated by checker |
    | `rule_bundle_version`, `model_encoding`, `reconciliation_state` | `trusted_context_bytes`; rule bundle must be supported; context values participate in recomputed context identity |
    | `expected_context_id` | `trusted_context_bytes`; must match certificate/context recomputation |
    | `known_normalization_ops`, `derivation_deps` | `trusted_context_bytes`; immutable closed inputs for their checker validation |
    | `mapping_authority` | Derived by the gate from `policy.approved_mapping_provenance_refs` for BEHAVIOR; absent for other rules; caller input is forbidden |
    | Remaining fields | None: the rows above enumerate all `CheckRequest` fields; no ambient default is used |

   The checker consumes a private typed materialization of the snapshot's
   canonical bytes (because `CheckRequest` validators require ordinary
   JSON mappings/lists). It is not shared with the caller or mutated.
   The obligation evaluator later receives a separate materialization
   from the same snapshot bytes, not a reread of the original request.

    Run `check_certificate` on that request. The envelope `rule_id`
    must equal the certificate `rule_id`; mismatch denies
    `INVALID_INFERENCE` (rule mismatch) before the checker runs, so
    expansion and checking can never disagree on the rule. Non-VALID →
    deny with the scalar mapping (`NOT_CHECKER_ACCEPTED:<outcome>:
    <first-reason>`), obligations unevaluated — assert evaluation was
    not reached. **[v1.2: the supplied outcome class is eliminated —
    there is no `checker_outcome` request field on this entry, so
    binding is structural, not asserted.]**
 5. **Assert snapshot integrity.** Recompute canonical digests from the
   immutable snapshot’s retained values and compare to its entry-time
   digests. Any mismatch denies `STALE_OBLIGATION`; it is an internal
   invariant failure, not an `INVALIDATED` obligation. Because no
   caller-owned object is reread and snapshot values are recursively
   immutable, this closes mutation between checking and evaluation by
   construction. It does not claim protection against arbitrary process
   memory corruption.
6. **Evaluate.** Run the deterministic obligation evaluator over an
   isolated materialization of the exact snapshot checked in step 4.
 7. **Promotion rule:** COMMIT iff every obligation is `SATISFIED`.
    `OPEN` and `UNSUPPORTED` deny as specified. Reserved states
    `CONFLICTED` and `INVALIDATED` are not representable in v1.3. Then
    run the scalar linearization sub-step with gate-derived values
    (VALID outcome, trusted reconciliation result, and trusted policy
    eligibility); those checks run last and cannot override an obligation
    denial. COMMIT only if the sub-step also commits.

The scalar `decide_publication` is no longer a public promotion entry.
It becomes a private sub-step callable only by
`decide_guarded_promotion`, using an internal typed value constructed by
the guarded function after checker and obligation success. Its current
caller-supplied `checker_outcome` request shape is removed. Tests may
continue to pin the ordering helper through a private test seam, but no
application caller can obtain a `COMMIT` promotion decision by supplying
a checker outcome or bypassing the guarded function. Production
publication code must call only `decide_guarded_promotion`.

### Test plan (for the implementation GO, not this proposal)

Prior test plan retained where applicable, plus:

- Policy binding: wrong-repository evidence denies `POLICY_DENIED`
  (`policy-scope:`); None policy denies; tenant selection is a B2
  wiring test, not a kernel test (say so in the test).
- Outcome binding (structural): request-shape test asserting the
  guarded entry has NO `checker_outcome` field (like the
  checker import-separation test); internal-check-denies vector
  (defective evidence denies with the checker's reason, obligations
  unevaluated — assert evaluation was not reached, e.g. via a
  reason prefix or a sentinel).
- Scalar bypass closure: no public `decide_publication` symbol can
  produce an authoritative promotion decision; import/API scan confirms
  production callers use only the guarded entry; direct internal-step
  invocation is not available across the application boundary.
- Genesis vectors: OPEN via revision-unpinned VALID definition cert
  (checker VALID, obligation O3 OPEN → `OPEN_OBLIGATION`;
  pinned-revision twin promotes, proving the promotion bar is stricter
  than the checker bar); UNSUPPORTED via CAUSAL rule ID without
  consulting the checker (assert checker was not called).
- Snapshot integrity vectors: composite public inputs are canonical
  bytes, so there are no caller-owned nested request objects to mutate
  after entry. Prove the checker, evaluator, and decision consume only
  the one parsed frozen snapshot; assert digest stability across repeated
  materialization.
- Repository-policy vectors: exact repository-ID match passes; non-member ID,
  prefix-confusable repository, absent repository, non-canonical
  identity, missing evidence-ID/content-digest trusted binding, swapped
  binding digest, and empty policy deny. No prefix semantics are accepted.
- Checker-request construction vectors: each required `CheckRequest`
  field is asserted sourced from the specified evidence/trusted context;
  missing required trusted context denies closed rather than using an
  undocumented default. Envelope `rule_id` vs certificate `rule_id`
  mismatch denies before the checker runs.
- Registered-rule invalid checker input denies before obligation
  evaluation; assert evaluator was not called. Evaluator exception denies
  closed and never creates `CONFLICTED`.
- Fingerprint addition: `obligations.py` joins the fingerprinted set; no
  independent obligation-evaluator module is required in v1.3.

### Explicit non-goals

- No persistence; no legacy-writer closure; no real-world mapping
  authority — BEHAVIOR mapping-authority O3 stays unsatisfiable in
  production until its separate policy/checker contract is reviewed; no
  obligation-evaluator duplication in v1.3.
- The standalone `check_certificate` contract still has its legacy
  `mapping_authority` input and hardcoded authority constraint. The
  guarded promotion entry constructs that input from trusted
  `DecisionPolicy.approved_mapping_provenance_refs`; this does not revise
  standalone checker callers. A separate checker-contract amendment is
  required before any other caller can claim the same trusted-policy
  boundary. Real-world mapping authority remains deferred; the default
  policy list is empty.
- The independent second validator path (`second_path.py`) pins the
  test-only mapping authority in its own hardcoded set, in addition to
  checking the supplied mapping-authority list. A future approved
  authority therefore passes path 1 while path 2 denies, and the
  conjunction denies (fail-closed, correct per B3-deferral) — so
  approving a new authority is a two-path change, not a policy-only
  change. Named here so it is not discovered at that future review.
- Scalar demotion migrates exactly one existing production caller:
  `application/proof/publication_commit_service.py:237` (grandfathered,
  G5-owned, itself callerless) calls scalar `decide_publication` today.
  Implementation moves it to `decide_guarded_promotion` or retires it
  with G5 coordination; the API-scan gate in the test plan covers the
  migration (it fails while the scalar call site remains).

## Decision requested (REVIEW GATE R2, third round)

1. Accept the v1.3 contract as stated, amend it, or reject.
2. On acceptance: separate GO for the implementation (new module(s)
   + guarded entry + scalar demotion + tests + fingerprint addition,
   pure domain only). No code starts before that GO.
