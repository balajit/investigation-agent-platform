# IAP Onboarding Agent — Provisional Design Contract

## 0. Status and authority

**Status:** `APPROVED — Agent design owner, 2026-10-22 (scoped; see Deferred Decisions Register §14)`. This document is approved as the binding provisional design contract. Approval covers the framework, all rows marked ACCEPTED, and every REJECTED prohibition. It does not authorize implementation: no status in this document authorizes building, and implementation remains gated on the phase plan (Phase 0R exit, then Phase 1B schemas). Items marked PROPOSED or DEFERRED are explicitly not approved; each has exactly one entry in §14 naming its decision, owner, sign-off, and the deadline before which it must be decided. Until then, entries in the discussion record (`docs/IAP-onboarding-agent-sp-thoughts.md`, Entries 1–8) remain the authoritative history of intent.

**Revision 2** incorporates the review-feedback round recorded in
`docs/IAP-onboarding-agent-design-fb-1.md`: contract/implementation split in
the status register, platform ownership of disposition and linkage contracts,
three separate invocation contracts, complete report linkage fields, and
sniffer/evidence/trust corrections. Open points the reviewers deferred are
marked as such; nothing was silently dropped. Revision-2 fidelity accepted by
the Agent design owner on 2026-10-22; remaining per-row approvals are tracked
in §10.

**Ledger:** No ledger update accompanies this draft, by explicit instruction.
Ledger entries for the onboarding agent are deferred until separately approved.

**Conflict rule:** Platform contracts referenced here are owned elsewhere. On
any contradiction between this document and the Luna onboarding design, its
three supplements, the DB-ROLES record, or the implementation plan, the
referenced platform document wins until a converged revision says otherwise.
This document restates no platform contract; it only cites.

**Scope guard (binding on this document):** The Entry 8 phase-insertion
material is reproduced here as a *contract dependency checklist* (§9). It is
not an instruction to expand Phase 0R's scope. The actual-role PostgreSQL
proof remains the priority gate; nothing in this design delays, reopens, or
re-scopes that gate. 0R exit criteria are owned solely by the implementation
plan. In particular, defining the registry/report *contracts* in this document
did not decide when their *database tables* are created: table placement in
the 0R wave (DEP-02b) was explicitly confirmed by plan-owner decision on
2026-10-22. No other database work enters 0R through this document.

## 1. Normative dependencies (references only)

- Onboarding platform design: `docs/IAP-TENANT-ONBOARDING-DESIGN-Luna.md`
  (decisions D1–D10).
- Security/concurrency, workflow-correctness, deployment/deletion-assurance
  supplements: `IAP-TENANT-ONBOARDING-SECURITY-CONCURRENCY.md`,
  `IAP-TENANT-ONBOARDING-WORKFLOW-CORRECTNESS.md`,
  `IAP-TENANT-ONBOARDING-DEPLOYMENT-DELETION-ASSURANCE.md`.
- Role/provisioning record: `docs/IAP-TENANT-ONBOARDING-DB-ROLES.md`.
- Active phase sequence: `docs/IAP-TENANT-ONBOARDING-IMPL-PLAN-luna.md`
  (Phases 0/1A complete; 0R gate; 1B–6).
- Discussion history: `docs/IAP-onboarding-agent-sp-thoughts.md` (Entries 1–8).

## 2. Mission and hard negative scope

**Mission:** The onboarding agent is a platform-internal, read-only advisor.
It identifies what an estate contains, measures it against the support
registry and capability catalog, and guides operators with evidence-backed
verdicts, remediation, and enhancement scoping. Tools do all the work; the
agent judges.

**The agent, as three separate stateless invocation contracts** (each with
its own inputs, outputs, and authorization boundary — they are different
operations and must not share one blanket contract):

1. **Coverage analysis:** pinned snapshot + deterministic sniffer evidence +
   capability catalog + support registry in → coverage report out. Runs under
   the read-only analysis grant; no control-store reads beyond the intake
   snapshot it was invoked with.
2. **Watchdog diagnosis:** an authorized, tenant-scoped control-store state
   snapshot in → ranked diagnosis out. Reads only the state the invoking
   workflow explicitly supplies; never queries control state on its own
   authority.
3. **Report Q&A:** a validated report plus resolvable evidence pointers in →
   cited answer out. Answers only from supplied, validated inputs.

Bridge execution, where permitted, belongs to a separately authorized workflow
activity running in a sandbox — never to the agent itself and never to an
agent-invoked tool. The agent may propose, verify, and narrate bridge
outputs; only the workflow may run them.

**Hard negative scope (binding):**

- No direct mutation of onboarding control state, repositories, profiles,
  intake revisions, dispatch rows, approvals, or manifests.
- No tool execution with side effects; all tools it invokes are read-only
  against pinned snapshots and scratch output.
- No authorization of transitions, no granting of capabilities, no approval
  of its own recommendations — including its own coverage verdicts.
- No bridge execution: the agent never runs converters, builds, or any other
  tool with side effects, even in a sandbox. Execution belongs to separately
  authorized workflow activities (§2).
- No bypassing of checklists; no invented evidence; no weakening of legal
  or trust boundaries for convenience.
- No shadow provisioning paths: agent-influenced provisioning flows
  exclusively through the platform intake/dispatch/activation contracts.
- Isolated scratch writes are permitted analysis mechanics (sniffer outputs,
  intermediate reports in designated scratch areas). Platform-state mutation
  and customer-visible side effects are prohibited — the distinction is where
  the bytes land and under whose authority, not whether bytes were written.

## 3. Agent/workflow boundary (binding)

**Memorable rule:** *The agent may change minds, never state.*

**Binding qualification:** "State" means onboarding control state and
authorization decisions. The agent *does* produce analysis artifacts, but only
through its designated workflow activity, which files them under workflow
authority. The slogan prohibits the agent from transitioning state itself; it
does not prohibit the report-writing behavior the design requires, because
that writing is performed by the workflow, not the agent.

| Concern | Responsible party | Enforcement |
|---|---|---|
| Analysis, verdicts, guidance, report content | Agent, via read-only analysis activity | Agent principal holds read-only grant scopes; no write handles issued |
| Filing the analysis artifact (report row, audit linkage) | Workflow activity + repository layer | Activity runs under workflow identity with narrow write grants; artifact references agent output by content hash |
| All lifecycle transitions (request/dispatch/approval/activation state) | Workflow activities (CAS-guarded) + human approvals | Agent output is advisory input only; transitions require workflow compare-and-set or dual-person approval |
| Grant/revocation decisions | Human operators under dual-person procedure | Agent may recommend; cannot mint, approve, or consume |
| Checklist execution and gate evaluation | Deterministic code + human ratification | Agent drafts; code and humans dispose |
| Audit of agent reads | Workflow (records what was read, when, under which grant) | Filed with the artifact, not by the agent |

**State-change test (normative):** If an action alters any row in the control
store, creates or consumes any approval, alters any grant, or advances any
state machine, the agent must not perform it. If the action produces bytes
that a workflow activity then files with its own authority and audit trail, it
is analysis output and permitted.

## 4. Inputs (field-level contracts, v1)

Conventions for every sniffer output: versioned envelope
`{tool, tool_version, input_sha, schema_version, produced_at}`; JSON only;
deterministic field ordering; redaction applied at the sniffer boundary (never
raw secrets downstream). Formal JSON Schema is deferred to implementation; the
field contracts below are binding on shape and semantics.

Determinism means: canonical serialization with fixed key ordering, sorted
collections, stable input hashes, and no environment-dependent fields inside
any hashed byte range. `produced_at` is metadata only and is always excluded
from content hashes. Failure is classified, never silent: an *unsupported*
input (unknown system, missing grammar) yields an explicit gap finding; a
*malformed or unsafe* input fails validation with a named code. These are
different outcomes with different handlers.

### 4.1 `census` — file inventory + parse census

| Field | Type | Required | Semantics / invariants |
|---|---|---|---|
| `files[]` | array | yes | Every regular file under the snapshot root, path relative, sorted. Excludes nothing silently: skipped paths (symlinks, oversize, unreadable) appear in `skipped[]` with reasons. |
| `files[].kind` | enum `text/binary` | yes | Text eligibility decided by content sniffing, not extension. Binary files skip parsing by rule, not by omission. |
| `files[].bytes` | integer ≥ 0 | yes | Byte size as stored, independent of text eligibility. |
| `files[].language` | string | yes | Extension-mapped language or `unknown`. `unknown` is a finding, never a default to ignore. |
| `files[].parse_status` | enum `parsed/syntax-errors/grammar-unavailable/skipped` | yes | Parser outcome only. `syntax-errors` carries error count + first-error location; it is not a verdict on support. |
| `files[].capabilities` | array | yes | Capabilities actually extracted for this file (e.g. `symbols`, `call-edges`, `none`), with extractor name + version. Tier assignment is downstream judgment, never a parser field. |
| `files[].loc` | integer ≥ 0 or null | yes | Lines of code under the stated counting rule (newline-terminated logical lines; rule versioned with the sniffer). Null for non-text files; null is valid and distinct from zero. Generated files flagged, not excluded (exclusion is a downstream policy decision). |
| `totals_by_language` | map | yes | LOC + file counts per language, including `unknown`. Must reconcile with `files[]` (validator checks sums). |
| Validation | — | — | Totals reconcile; every file classified exactly once; unknown bucket present even when empty. |

### 4.2 `buildmap` — build-system inventory

| Field | Type | Required | Semantics / invariants |
|---|---|---|---|
| `systems[]` | array | yes | One entry per detected build system instance (tool, version/range, confidence). |
| `systems[].tool` | closed enum + `unrecognized` | yes | e.g. `maven/gradle/npm/pip/poetry/cargo/go-modules/ant/make/cmake/bazel/sbt/msbuild/unrecognized`. Unrecognized entries carry excerpt paths for LLM inspection. |
| `systems[].modules[]` | array | yes | Declared modules with source roots, language(s), dependency edges between modules. |
| `systems[].dependencies[]` | array | yes | Declared direct dependencies with coordinates + versions where declared. |
| `systems[].warnings[]` | array | yes | Missing wrappers, unresolvable parents, custom plugin blocks, unparseable sections — each with file/line. Silence about a gap is a defect. |
| `directories[]` | array | yes | Explicit directory inventory (path, role if declared: source-root, test-root, resource, generated). Source roots validate against this inventory — or, when no explicit inventory exists, against the directories implied by `census.files[]` paths. File paths and directory paths are different domains; roots are never validated by file membership. |
| Validation | — | — | Module source roots resolve in `directories[]` or implied paths; cross-reference mismatches are findings, not dropped. |

### 4.3 `cimapper` — CI/CD discovery

| Field | Type | Required | Semantics / invariants |
|---|---|---|---|
| `pipelines[]` | array | yes | Detected pipelines (system, config path): what builds/tests/publishes, artifact destinations, environment names. |
| `test_commands[]` | array | yes | Extracted test invocations per pipeline, marked verified-present vs. inferred. |
| `environments[]` | array | yes | Named environments mapped to IAP environment vocabulary where possible; unmapped names preserved verbatim with an `unmapped` flag. |

### 4.4 `depscan` — dependency inventory

| Field | Type | Required | Semantics / invariants |
|---|---|---|---|
| `lockfiles[]` | array | yes | Lockfiles found with tool + path. |
| `resolved[]` | array | yes | Resolved coordinates; cross-checked against the cdxgen SBOM sidecar where available. |
| `declared_vs_locked` | enum `consistent/divergent/no-lockfile` | yes | Declared-but-unlocked or locked-but-undeclared sets listed explicitly on divergence. |

### 4.5 `hygscan` — hygiene scan (read-only, redacted)

| Field | Type | Required | Semantics / invariants |
|---|---|---|---|
| `findings[]` | array | yes | Secret-shaped strings, private registry URLs, hardcoded hosts. Values are redacted patterns + locations only — never secret material. |
| Validation | — | — | A validator rejects any output containing a value matching secret shapes beyond its redaction envelope. Redaction failure fails the run, not the finding. |
| Redaction limits | — | — | Pattern detection cannot guarantee every secret is found. When redaction confidence is uncertain for an input, that input is excluded from model context and the exclusion is recorded as a limitation. An un-redactable input never flows downstream on the assumption it is clean. |

### 4.6 `sizer` — scale and exclusion signals

| Field | Type | Required | Semantics / invariants |
|---|---|---|---|
| `loc_by_service` | map | yes | LOC per inferred service boundary with boundary basis stated (module, directory, build unit). |
| `generated[]`, `vendor[]`, `binaries[]` | arrays | yes | Paths recommended for index exclusion, with byte counts and rationale. Recommendation only — exclusion is a downstream policy decision. |

### 4.7 Support registry input (read side)

The agent reads the support registry (schema owned by the registry design):
capability key, handling strategy (`NATIVE/GENERIC/BRIDGE/UNSUPPORTED`),
tier/limits, trust state, evidence requirements, version. The agent never
writes registry rows. Unregistered capabilities encountered in an estate
yield an explicit registry-gap finding. Seed rows derive only from the actual
capability catalog and verified platform support — never populated
hypothetically to make the registry look complete, and never promoting a
bridge past its verified trust state for coverage.

## 5. Outputs (field-level contracts, v1)

### 5.1 Coverage Report (normative artifact shape)

| Field | Type | Required | Semantics / invariants |
|---|---|---|---|
| `report_id` | UUID string | yes | Stable identity for this report row. One row per analysis run, never reused. Maps to the row primary key in storage (referenced here as `report_id`; no second identity column exists). |
| `request_id`, `revision` | UUID string, integer | yes | The onboarding request revision analyzed. The report describes exactly one revision. |
| `generation` | integer ≥ 1 | yes | Analysis generation for this revision. First analysis is 1; each re-analysis (new evidence, new sniffers, new catalog) increments. Uniqueness is tenant-scoped: `(tenant_id, request_id, revision, generation)` is unique, matching the enforced DB constraint. Retry of the same generation reuses its identity and never creates a duplicate row. |
| `report_version` | string | yes | Report schema version, independent of registry/catalog versions. |
| `registry_version`, `catalog_version` | strings | yes | Versions evaluated against; a report without pins is invalid. |
| `estate_sha` | string | yes | Pinned snapshot the report describes. Any input change invalidates the report (no silent carry-forward). |
| `input_manifest_hash` | string (hex) | yes | SHA-256 over the canonical bytes of the input manifest: sorted sniffer-output content hashes, their tool/schema versions, `estate_sha`, `registry_version`, `catalog_version`, and this report's `generation`. Volatile metadata (`produced_at`, durations, hostnames) is excluded by rule — identical inputs must hash identically regardless of when or where the analysis ran. |
| `report_content_hash` | string (hex) | yes | SHA-256 over the canonical JSON of the report body (all fields except `report_content_hash` itself and the audit/filing envelope added by the workflow). Same exclusion rule as the input manifest. The hash covers verdicts, confidence, evidence pointers, bridges, gaps, remediation, and limitations — so any silent alteration invalidates it. |
| `analyzer_version`, `model_version` | strings | yes | Tool + model versions for reproducibility. `model_version` records which LLM produced narrative fields. |
| `verdicts[]` | array | yes | Per service/component: languages, tier (`deep/generic/counted/invisible`), disposition per capability with reasons. |
| `verdicts[].claims[]` | array | yes | Confidence attaches to individual claims (or clearly scoped verdicts), never to a whole service as a single label. A service may carry VERIFIED facts alongside SPECULATIVE conclusions; each claim carries its own confidence and probe. |
| `verdicts[].evidence[]` | array | yes | Stable references: sniffer-output content hash + structured field path (e.g. `buildmap.systems[2].modules[0]`). Line numbers may be included as hints but are never the reference — outputs shift, hashes don't. Every factual claim resolves; unresolvable claims fail report validation. |
| `bridges[]` | array | yes | Bridge usages with tool, version pin, trust state, verification evidence, caveats. A TRUSTED bridge mechanism does not by itself prove any particular converted output correct for this estate — per-use verification evidence is still required. |
| `gaps[]` | array | yes | Enhancement items with sizing evidence (Entry: scoped ask + transfer analysis). |
| `remediation[]` | array | yes | Ordered operator recommendations with exact commands, expected outcomes, verification steps. Manual-apply default. Recommendations are never executable payloads: no shell chaining, parameters passed as data with safe handling, operator review mandatory, and report output must never become a shell command automatically. |
| `limitations[]` | array | yes | What was not examined and why (unreachable paths, tier-3 unknowns, redacted inputs). A report without limitations is invalid. |

**Canonicalization version + unknown-field policy (binding):** Every hashed preimage carries a canonicalization version, and any report-body key outside the approved field set is rejected rather than hashed silently — a producer/consumer version skew fails loud, never mints a hash over unknown fields. Bumping the version intentionally invalidates prior hashes.

**Link-not-embed (binding):** The report is platform-derived evidence, never
operator-supplied intake. It is linked by `(request_id, revision, generation)`
with its own content hash, referenced — not embedded — by readiness/activation
decisions. The request `content_hash` covers intake only. The precise linkage
fields, hash canonicalization, generation semantics, and consuming API shape
are ACCEPTED (AG-02b approved 2026-10-22); the link-not-embed constraint itself is ACCEPTED (AG-02a).

**Contract ownership (binding):** Disposition vocabulary is platform-owned
(D2.1/F-11); this document consumes it and defines nothing parallel. Coverage
linkage lives in an approved shared contract agreed with the Phase 1B owner.
Neither side redefines the other's contract; divergence fails review only
after that ownership and contract are approved. Do not add agent-owned
disposition or linkage types.

### 5.2 Guidance and Q&A outputs

Guidance inherits the report's evidence-pointer discipline: every
recommendation cites report entries; every answer to "why is D unsupported"
returns the verdict, top evidence pointers, and what would change it. Unknowns
are answered with "unknown, plus the probe that would resolve it" — never a
guess presented as fact. Customer-facing renderings omit internals (adapter
names, thresholds, staging details).

Repository excerpts sent to an LLM are bounded (stated line/window caps),
redacted, audited per excerpt, and governed by the approved data-handling
policy. Sniffer JSON is the default input; raw excerpts require an explicit,
logged exception each time. No standing permission to "read the repo" exists.

### 5.3 Failure semantics (binding)

- Sniffer failure fails the run stage with a named code; partial outputs are
  labeled partial and never silently completed.
- Report validation failure (unresolvable evidence pointer, missing pins,
  missing limitations section, missing confirming probe on any PROJECTED or
  SPECULATIVE claim) rejects the report; the agent may retry generation, not
  relax validation.
- No silent defaults anywhere: `unknown` buckets, `unmapped` flags, and
  `unrecognized` tools are mandatory members of their enums.

## 6. Trust model

- **Verdict confidence:** `VERIFIED` (evidence in hand) / `PROJECTED`
  (strong proxy inference, confirming probe named) / `SPECULATIVE` (weak
  signals, confirming probe mandatory). Confidence rises only on named probe
  evidence; versions must show confidence changes with reasons. Confidence
  attaches per claim (§5.1), never as one label for a whole service. Static
  findings never wear runtime confidence language: evidence from static
  analysis cannot support a claim about runtime behavior unless the report
  identifies the actual runtime evidence behind it.
- **Bridge trust:** `UNVERIFIED → PROVISIONAL → TRUSTED`, promoted by
  verification evidence or human sign-off, demoted automatically on estate
  change, tool-version change, or verification failure. PROVISIONAL gates
  planning only; TRUSTED status and any activation-gating role for bridges
  are policy decisions (see §11, safely open).
- **Confidence thresholds for gating are policy, not inference:** which
  confidence levels may gate activation is set by policy (DEFERRED — §11).
  The agent reports confidence; it does not set the bar.

## 7. Verification loops (read-only)

1. **Estate-vs-registry conformance:** every component mapped to a registry
   strategy or an explicit registry-gap finding. No silent gaps.
2. **Watchdog diagnosis (the diagnosis invocation contract in §2):** read only the
   authorized, tenant-scoped state snapshot the workflow supplies; check
   progress (stage transitions, stuck bounds); diagnose from evidence with
   ranked causes and distinguishing tests; guide, never remediate directly.
3. **Bridge-vs-checklist:** every bridge re-verified per use (deterministic
   re-run + independent-signal match + semantic diff narration); checklist
   results appended to the run record, never overwritten.

## 8. Authority model

The agent acts under a dedicated principal holding read-only grant scopes
(tenant/app/environment/operation, validity-bounded), resolved through the
Phase 0R grant model. It reads intake snapshots and sniffer outputs under
tenant context; it holds no cross-tenant privilege and no registry-admin
path. JWT `sub` (or the platform's verified principal) is the principal;
the tenant claim remains identity context, never entitlement. Enforcement is
by grant design (Phase 0R), not by agent self-restraint — the architecture
assumes the agent is compromised-or-buggy and still safe.

## 9. Integration: contract dependency checklist (from Entry 8)

Binding interpretation: each item is a prerequisite the consuming phase may
assume *once satisfied*; it is not authorization to expand any phase's scope,
and in particular does not expand Phase 0R.

| # | Dependency | Needed before | Rationale (rework cost if late) |
|---|---|---|---|
| DEP-01 | Agent read-only grant scopes in grant-table design; dedicated agent principal | 0R exit | Resolver + role matrix retrofit |
| DEP-02a | `support_registry` + `coverage_reports` *contracts* (shapes, versions, linkage) locked | 0R exit | 1B designs against moving shapes |
| DEP-02b | *Table creation* for registry/report rows in the 0R migration wave (024) | CONFIRMED by plan-owner decision 2026-10-22 — tables land in 024, no fallback migration needed | Piecemeal DDL churn if late; no gate impact either way |
| DEP-03 | Agent needs no cross-tenant privilege (design statement) | 0R exit | Prevents "helpful" privilege grants later |
| DEP-04 | F-11/D2.1 closed disposition enum final | 1B | Intake validation + policy references |
| DEP-05 | Link-not-embed (§5.1); intake `content_hash` covers intake only | 1B | Acceptance-transaction/idempotency redesign |
| DEP-06 | Redaction rules aligned with `SecretReference` shapes | 1B | Prompt/input contract mismatch |
| DEP-07 | Analysis-activity slot (inputs by reference: snapshot SHA + evidence hashes; idempotency key `request_id + revision + generation`; failure behavior; report filing authority) in workflow definitions | Phase 2 | Workflow version bump + reconciler changes |
| DEP-08 | Watchdog read scopes from the 0R grant design (verify, don't reinvent) | Phase 2 | Duplicate/conflicting privilege paths |
| DEP-09 | Activation-gating policy: coverage as gate vs. advisory | Phase 3 | Transition-table change under load (DEFERRED — §11) |
| DEP-10 | Report `GET` surface | Phase 4/6 (deferrable) | None if schemas (§5) are respected |

## 10. Traceability matrix

Owner and sign-off are recorded separately. `ACCEPTED` appears only where the
named decision-maker has actually approved; whiteboard agreement alone
authorizes nothing, and no status authorizes implementation.

| # | Contract | Consuming phase | Status | Decision owner | Required sign-off | Notes |
|---|---|---|---|---|---|---|
| AG-01a | Read-only advisor principle; §3 boundary ("change minds, never state") | 0R (scopes), all | ACCEPTED | Agent design owner | Security (grant enforcement at build) | Principle agreed; enforcement mechanism needs Security sign-off before build. Three invocation contracts (§2) bound the principle. |
| AG-01b | Dedicated agent principal, exact read-only scopes, enforcement details | 0R (grant model) | PROPOSED | 0R plan owner | Owner acting as Security; platform owners (outstanding) | Content approved by owner 2026-10-22 as sufficient: finalized read-only scope set (intake snapshots, sniffer outputs, own coverage_reports, support_registry, background_jobs read-only, tenant registry direct tenant-scoped SELECT), JWT with fixed sub, SSO/OAuth deferred; no mediated-interface requirement (uniform worker-pattern RLS); MCP-for-agent tracked separately as deferred (AG-17/D-10) with no-silent-upgrade guardrail. Stays PROPOSED until platform-owners sign-off is recorded. Decision D-01 in §14. |
| AG-02a | Link-not-embed constraint; intake hash covers intake only | 1B | ACCEPTED | Agent design owner | — (design) | Established constraint. |
| AG-02b | Linkage fields, content-hash canonicalization, generation/retry semantics, consuming API shape (§5.1) | 1B | ACCEPTED | Phase 1B design owner | Phase 1B design owner | Approved by owner 2026-10-22: field set, canonical JSON bytes (sorted keys, no whitespace, UTF-8, float rejection), tenant-scoped generation uniqueness, idempotent retry / new-evidence-new-generation semantics. Decision D-03 resolved. |
| AG-03 | Coverage Report schema (§5.1) | 1B (linkage), 3 (gating ref) | ACCEPTED | Agent design owner | Agent design owner (design); Security (before gating use) | Accepted by owner 2026-10-22 with markup incorporated (tenant-scoped generation uniqueness; report_id ≡ row PK mapping); field contracts binding; formal JSON Schema deferred. Decision D-02 resolved. |
| AG-04 | Sniffer output contracts v1 (§4) | 1B, 3 | ACCEPTED | Agent design owner | Agent design owner | Accepted 2026-10-22. Schemas before implementations; parser-vs-capability split included. |
| AG-05a | Support strategy + trust-state vocabulary | Review | ACCEPTED | Agent design owner | Agent design owner | Accepted 2026-10-22. Closed vocabulary; independent of storage. |
| AG-05b | Registry/report table schemas, seed data, ownership, trust-promotion procedure | 0R wave (024) | ACCEPTED | 0R plan owner | Platform owners | Confirmed 2026-10-22: tables in 024 with RLS/policies/grants; no hypothetical seed rows; no grant seeding. 024 executes cleanly end-to-end on live PostgreSQL (disposable instance, faithful 023-shaped stubs); tenant isolation, worker least-privilege, and cross-tenant denial verified as actual roles 2026-10-22. |
| AG-06a | Bridge trust states + verification loops (§6–§7) | 3+ | ACCEPTED | Agent design owner | Security (executing bridges, sandbox policy) | Accepted 2026-10-22 as data-only default with per-use re-verification. No bridge is complete until a sandbox environment is created and tested; executing bridges stay under AG-06b. |
| AG-06b | Executing-bridge policy (sandbox, pins, audit) | First executing-bridge proposal | DEFERRED | Agent design owner | Security | Trigger: first proposal; default data-only. Sandbox creation and testing is a prerequisite for any executing bridge — no executing bridge is complete without it. |
| AG-07 | Activation-gating policy | Pre-3 | DEFERRED | Agent design owner | Product + Security | Parking confirmed 2026-10-22. Trigger: Phase 3 planning; default advisory until decided. |
| AG-08 | Registry governance (who approves entries/promotions) | Ongoing | DEFERRED | Agent design owner | Platform owners | Parking confirmed 2026-10-22. Trigger: first real entry. |
| AG-09 | Hermetic/on-prem execution | On demand | DEFERRED | Agent design owner | Security + Legal | Parking confirmed 2026-10-22. Trigger: first customer demand; default bounded-role ask. |
| AG-10 | Model selection/lock-in policy | Standing default (revisit before first model-backed implementation) | ACCEPTED | Agent design owner | Agent design owner | Accepted 2026-10-22 as default: model-agnostic harness, versions recorded. |
| AG-11 | Shadow provisioning paths | — (never) | REJECTED | Agent design owner | — | Prohibited behavior: unproven demos, audit gaps. |
| AG-12 | Agent self-approval / self-grant | — (never) | REJECTED | Agent design owner | — | Prohibited behavior: breaks dual-control and SoD. |
| AG-13 | Weakening legal/trust boundaries for convenience | — (never) | REJECTED | Agent design owner | — | Prohibited behavior: bounded-role ask is the product. |
| AG-14 | Producing silent gaps (unregistered strategies, missing unknown buckets) | All | REJECTED | Agent design owner | — | Prohibited behavior: fail closed but explained, always. |
| AG-15 | Correction-record reuse beyond tenant scope | On explicit approval | DEFERRED | Agent design owner | Data governance | Parking confirmed 2026-10-22. Trigger: explicit approval; default tenant-scoped forever. |
| AG-16 | Remediation auto-apply | On separately approved proposal | DEFERRED | Agent design owner | Security + platform owners | Parking confirmed 2026-10-22. Trigger: proposal with audit design; default manual-first. |
| AG-17 | MCP for agent (future tool access) | On first MCP tool proposal | DEFERRED | Agent design owner | Owner acting as Security | New tools arrive only via explicit proposal with sandbox + authorization story; must not silently upgrade the agent from reader to actor. |

## 11. Decision register

### 11.1 Rework-critical (locked by phase deadline)

- **Agent grant scopes + dedicated principal** — locked at 0R exit (DEP-01).
  Late cost: resolver/role retrofit.
- **Registry + coverage contracts** — locked at 0R exit (DEP-02a); late cost
  if contracts slip: linkage designed blind.
- **Registry + coverage tables in the 0R wave (024)** — CONFIRMED by
  plan-owner decision 2026-10-22 (DEP-02b, AG-05b ACCEPTED); tables land with
  RLS/policies/grants, no seed rows, no grant seeding.
- **Link-not-embed + intake-hash scope** — locked before 1B (DEP-05). Late
  cost: acceptance-transaction redesign.
- **Linkage fields + hash canonicalization + generation semantics** —
  AG-02b ACCEPTED 2026-10-22. Late cost if revisited: intake/acceptance rework.
- **Disposition enum final** — locked before 1B (DEP-04). Late cost: intake
  validation + policy references rewritten.
- **Analysis-activity slot** — locked before Phase 2 (DEP-07). Late cost:
  workflow version bump.
- **Redaction/`SecretReference` alignment** — locked before 1B (DEP-06).

### 11.2 Safely open (parked with triggers)

Every item names its reopening event, decision owner, and required sign-off.
"Build time" always means a specific milestone, never "whenever". Binding
decision deadlines live in the Deferred Decisions Register (§14); this section
records rationale and defaults.

- **Activation gating (AG-07):** advisory default. Reopens at Phase 3
  planning; owner: agent design owner; sign-off: Product + Security.
- **Registry governance (AG-08):** reopens at first real entry; owner: agent
  design owner; sign-off: platform owners.
- **Hermetic execution (AG-09):** reopens at first customer demand; owner:
  agent design owner; sign-off: Security + Legal; default stays bounded-role
  ask.
- **Model policy (AG-10):** reopens before the first model-backed
  implementation is approved; owner: agent design owner; sign-off: agent
  design owner; default model-agnostic with recorded versions.
- **Bridge execution policy (AG-06b):** data-only vs executing; reopens at
  first executing-bridge proposal; owner: agent design owner; sign-off:
  Security sandbox sign-off.
- **Correction-record reuse beyond tenant scope (AG-15):** reopens on
  explicit data-governance approval; owner: agent design owner; sign-off:
  data governance; default tenant-scoped forever.
- **Remediation auto-apply (AG-16):** manual-first default stands until a
  separately approved proposal with audit design exists; owner: agent design
  owner; sign-off: Security + platform owners.
- **MCP tool access for the agent (AG-17):** no MCP tools until an explicit
  proposal with sandbox + authorization story is approved; owner: agent design
  owner; sign-off: owner acting as Security. Must not silently upgrade the
  agent from reader to actor.

## 12. Open questions rollup (phase/event tagged)

1. Report persistence and linkage: operator doc plus linked stored artifact
   (AG-02b ACCEPTED 2026-10-22, §5.1)? Resolved — this is distinct from gating below.
2. Activation gating: advisory-only default stands; resolve at Phase 3
   planning with Product + Security (AG-07). Not the same decision as (1).
3. Remediation authority: manual apply first, auto-apply later by separate
   proposal? (Default manual; tracked as AG-16 with trigger = proposal with audit design.)
4. First Tier-2 target: sbt/Scala vs Ant/Make? (Trigger: first real estate
   need.)
5. Is the Scala customer real or standing in? (Trigger: pipeline visibility.)
6. Customer-facing verdict vocabulary? (Trigger: first customer-facing render.)
7. Enhancement-item destination? (Trigger: first backlog export; default typed
   artifact first.)
8. Grant store home/table shape beyond §8 sketch? (Owned by 0R design, not
   this document.)
9. Test-coverage "grounding-grade" threshold? (Trigger: first grounding use.)
10. SPECULATIVE→VERIFIED promotion rule: confirming probe evidence required;
   human acknowledgment recorded separately where policy requires. Settle at
   the first promotion event, owned by the agent design owner with Security
   concurrence on the evidence bar.
11. Hackathon posture: win vs participate; IP clearance for public showing.
    (Trigger: application decision; tracked in thoughts Entry 7.)
12. Muse provider compatibility with Meta Model API in code. (Trigger: first
    Muse-backed run.)
13. MCP tool access for the agent (AG-17/D-10): deferred until an explicit
    proposal with sandbox + authorization story; must not silently upgrade
    the agent from reader to actor.

## 13. Design acceptance criteria

Accepted 2026-10-22 against these criteria. Criteria 1, 3, 4, and 5 hold as
written. Criterion 2 now holds in full: report contract (AG-03 ACCEPTED
2026-10-22, D-02 resolved) and linkage ownership (AG-02b ACCEPTED 2026-10-22,
D-03 resolved). Likewise AG-01b enforcement approval
(D-01) is tracked, not assumed. Approval of
this document covers the framework, all ACCEPTED rows, and all REJECTED
prohibitions — it does not pre-approve any unresolved §14 decision, and no
§14 item may be treated as decided until its owner and sign-off are recorded.

1. **Authority boundary is consistent:** the agent reasons, the workflow files
   artifacts under its own authority, and only separately authorized
   components perform side effects. No action in §3 is owner-ambiguous and no
   path lets agent output become a state transition without workflow or human
   authority.
2. **The Coverage Report contract is complete:** identity, request linkage,
   generation/retry semantics, input-manifest and content hashes with stated
   canonicalization and volatility exclusions, provenance, and per-claim
   confidence — with the linkage ownership (AG-02b) approved by the Phase 1B
   owner. Hash definitions that admit identical-input/different-hash outcomes
   fail this condition.
3. **Sniffer contracts distinguish unsupported inputs, incomplete evidence,
   and execution failures** without silently losing information: `unknown`
   buckets, `unrecognized` tools, gap findings vs. validation failures, and
   redaction-uncertainty exclusions are all present and testable in prose.
4. **The status matrix distinguishes accepted principles from proposed
   contracts:** every row has exactly one status, an accountable role owner,
   and the required sign-off; no principle-level ACCEPTED smuggles in
   unapproved implementation detail; every §11.2 parked decision appears in
   §10 with trigger and sign-off.
5. **The Phase 0R checklist is explicitly non-expanding:** contract
   definitions needed by later phases are locked, and the sole database
   addition (DEP-02b: two tables in 024) was explicitly confirmed by the
   plan owner on 2026-10-22 — nothing else is assumed into 0R, and the
   actual-role PostgreSQL proof gate is unaffected.

## 14. Deferred Decisions Register (binding)

Every PROPOSED row and every parked decision has exactly one entry here. Each
entry states the decision to be made, who makes it, whose sign-off is required,
and the impacted item or phase *before which* the decision must be recorded.
"Before X" means: the impacted work does not start until the decision row is
resolved. Standing defaults apply until then; defaults never substitute for a
required approval.

| ID | Decision (AG ref) | Decided by | Required sign-off | Must be decided before | Default posture until decided |
|---|---|---|---|---|---|
| D-01 | Agent grant scopes + dedicated principal model (AG-01b) | 0R plan owner | Owner acting as Security (recorded); platform owners (outstanding) | 0R exit / any agent grant issuance or build depending on it | Owner-side content approval recorded 2026-10-22; no agent principal exists and no agent activity runs until platform-owners sign-off is also recorded. Resolver/role design proceeds without agent scopes. |
| D-02 | Coverage Report schema acceptance (AG-03) | Agent design owner | Agent design owner (design); Security (before gating use) | Phase 1B schemas finalize | [RESOLVED 2026-10-22 — AG-03 ACCEPTED] Schema usable for 1B planning and linkage work; formal JSON Schema still deferred; Security sign-off still required before gating use. |
| D-03 | Coverage linkage contract: fields, hash canonicalization, generation/retry semantics, consuming API (AG-02b) | Phase 1B design owner | Phase 1B design owner | Phase 1B schemas finalize | [RESOLVED 2026-10-22 — AG-02b ACCEPTED] Field set, canonical JSON bytes, tenant-scoped generation uniqueness, idempotent retry / new-evidence-new-generation semantics approved. AG-02a constraint holds (link-not-embed, intake hash covers intake). Luna cleared to implement intake linkage per this contract. |
| D-04 | Executing-bridge policy: sandbox, pins, audit (AG-06b) | Agent design owner | Security | First executing-bridge proposal | Data-only bridges only; executing bridges prohibited. Sandbox creation and testing is a prerequisite, not a parallel track. |
| D-05 | Activation gating: coverage as gate vs. advisory (AG-07) | Agent design owner | Product + Security | Phase 3 planning (activation CAS changes) | Advisory-only: reports inform, never gate. |
| D-06 | Registry governance: who approves entries/promotions (AG-08) | Agent design owner | Platform owners | First real registry entry | No entries; registry-gap findings only. Seed rows are never hypothetical. |
| D-07 | Hermetic/on-prem execution (AG-09) | Agent design owner | Security + Legal | First customer demand for it | Bounded-role ask only; hermetic execution prohibited without this decision. |
| D-08 | Correction-record reuse beyond tenant scope (AG-15) | Agent design owner | Data governance | Any cross-tenant correction use | Tenant-scoped forever. |
| D-09 | Remediation auto-apply (AG-16) | Agent design owner | Security + platform owners | Any automated remediation | Manual-first; output is recommendation text, never executable action. |
| D-10 | MCP tool access for the agent (AG-17) | Agent design owner | Owner acting as Security | First MCP tool proposal | No MCP tools; agent remains read-only. Any MCP proposal needs its own sandbox + authorization story and must not silently upgrade the agent from reader to actor. |

Standing defaults that require no decision (recorded here so silence is not
mistaken for an open item): model-agnostic harness with recorded versions;
advisory-only activation; bounded-role data access; manual remediation. Each
carries its revisit trigger in §11.2.

Resolution rule: a decision is resolved only by an explicit recorded approval
from the Decided-by role with the Required sign-off attached — never by
elapsed time, whiteboard consensus, or implementation pressure. Resolved rows
move to ACCEPTED (or REJECTED with rationale); this register is never deleted,
only resolved.
