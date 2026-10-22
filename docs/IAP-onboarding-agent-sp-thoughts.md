# IAP Onboarding Agent — Discussion Document

**Status:** Thinking record, not a decision and not a design. Entries are appended
date-stamped as the discussion evolves. Nothing here is implementation
authorization. Ledger updates for the onboarding agent are **explicitly deferred**
until separate approval is given.

**Framing constraint (agreed):** The agent is platform-internal. It exists to
identify and guide. Tools do all the work. The agent never touches what flows
through the onboarding pipes.

**Session protocol (agreed 2026-10-22):** This is a whiteboarding session, not a
build order. Ideas, counter-ideas, questions, scenarios, and half-formed
thoughts all go into this document — nothing is too early to write down and
nothing here authorizes implementation. Entries are date-stamped and appended;
past entries are not rewritten, only superseded by newer ones that say so.
Open questions accumulate per entry and are rolled up during the
review/refine/revise cycle at the end. Ledger updates happen only after that
cycle completes *and* explicit approval is given. Until then: think in ink,
build nothing.

---

## 2026-10-22 — Entry 1: The Onboarding Concierge (supervised, demo-grade provisioning path)

**Problem:** Customer demos start with tenant setup — ingesting a sample repo and
creating the tenant profile. That first activity must be failure-proof. Manual,
script-driven setup is where demos die.

**Proposal:** A supervised execution path over primitives the platform already
owns (`ingest_github.py`, `provision_tenant.py`, `seed_profile.py`, immutable
profile revisions, readiness probes). Not a new agent framework — a Temporal
workflow that orchestrates existing services, because a demo that provisions
through a side door proves nothing and rots immediately.

**Three gated stages:**

1. **Preflight (no mutation):** credentials resolve, destinations reachable,
   quotas available, sample bundle pinned (repo URL + commit SHA + expected
   profile content-hash). Red preflight refuses to start and names exactly what
   is red. Most demo failures should die here, before the customer sees anything.
2. **Provision:** idempotent, resumable, checkpointed per stage. A mid-demo
   hiccup resumes; it never duplicates.
3. **Verify:** executable acceptance, not vibes — profile validates against
   schema, mandatory probes return fresh `VERIFIED`, a tenant-isolation probe
   passes (tenant-A canary write, tenant-B read fails), and 2–3 canned demo
   queries return expected evidence. The workflow reports a signed checklist,
   gate by gate.

**"Zero tolerance" engineered as three properties:**

- *Deterministic inputs:* a versioned **demo bundle** — vendored repo snapshot
  (no live clone at showtime), pinned profile, expected evidence fixtures. Live
  cloning during a customer demo is the largest single failure surface;
  eliminate it.
- *Refuse-to-start:* the preflight gate converts unknown-unknowns into a calm
  "environment not ready" before showtime, run ~30 minutes prior by whoever sets
  up the call.
- *MTTR≈0 over MTBF=∞:* assume something external can still fail; optimize for
  instant, legible recovery (resume-from-checkpoint, per-gate diagnosis with the
  exact remediation command) rather than pretending failure is impossible.

**Supervised autonomy:** The workflow proposes, the operator approves stage
transitions — at least until N consecutive green runs. Approval points map 1:1
onto the existing separation-of-duties model (requester ≠ approver), so the demo
story demonstrates the governance story. Fully autonomous provisioning is a later
policy choice, not an architectural accident.

**Dogfooding:** The concierge creates a real onboarding request revision and
passes through intake validation, dispatch, readiness gates, and activation. It
acts under capability grants like everything else (time-boxed `tenant:onboard`
for the human driving the demo; re-authorization at each privileged side
effect). The demo then proves the production path works.

**Sequencing vs. current Phase work:** Do not start until Phase 0R + 1B land —
the concierge needs scoped grants, the request/revision store, and typed intake,
or it calcifies around script-level access and becomes the shadow path the
onboarding design exists to kill. What can start now: **Slice 0** — the versioned
demo bundle plus a read-only preflight checklist script (DB/Temporal reachable,
bundle hash matches, quotas). Zero mutation, immediately useful for every demo
until concierge delivery. Then Slice 1 (workflow on real intake/dispatch APIs),
Slice 2 (probe-driven verify gates + canned queries), Slice 3 (self-service
variant — explicitly a later policy decision).

**Risks named:** demo-bundle drift (re-verify in CI on a schedule, or the
"deterministic" demo rots silently); scope creep into self-service (keep the
operator-grant + approval boundary load-bearing from day one); diagnosis quality
is the product (budget real effort for per-gate failure messages with remediation
commands, not just codes).

**Open questions (Entry 1):** vendored snapshot vs. clone-and-pin at build time?
CLI-first surface? Is "N consecutive green runs including 1 chaos-injected run"
the acceptance gate before first customer use?

---

## 2026-10-22 — Entry 2: Coverage intelligence (the agent's real product is the Coverage Report)

**Refinement:** The agent's highest-value output is measuring the customer estate
against IAP's actual language-support ladder and speaking plainly:
*"A, B, C are included for investigation; D and E cannot be supported due to
languages used and need IAP enhancement."*

**Triggering exhibit (verified in-repo):** `scripts/provision_tenant.py`'s
`_EXT_LANGUAGE` map counts 15 extensions including Rust (`.rs`) but has **no
`.scala`/`.sc` entry**. A Scala service today is not "unsupported with an
explanation" — it is invisible: not counted, not profiled, silently absent. A
demo could sail through setup and discover the gap mid-investigation. The agent
exists to kill exactly this failure mode.

**The three-tier reality the agent must narrate (verified against the tree):**

| Tier | Meaning | Today |
|---|---|---|
| **Deep** | Full fact pipelines: dataflow, call graphs, framework awareness (`application/analysis/python/`, `.../java/`) | Python, Java |
| **Generic** | Tree-sitter symbols, code search, callers/callees, DB-op discovery, plus sidecars (cdxgen SBOM, Noir endpoints, fast pass) | Any grammar in the language pack — Rust, Go, JS/TS… |
| **Counted** | File-extension census only; dominant-language guess | 15 mapped extensions |
| **Invisible** | Not even counted | Scala, anything else unmapped |

So coverage becomes a precise per-service, per-feature claim: *"payments-py:
deep — reachability, dataflow, Django-aware. auth-rs: generic — symbol search
and endpoint inventory, no dataflow. billing-scala: invisible — excluded from
investigation scope, enhancement required."* Partial support must always name
which investigation features work and which go dark — a Rust service with no
dataflow must never silently produce a "no findings" verdict a customer reads as
"no problems."

**Value-add stack (priority order):**

1. **Coverage cartography (headline).** Per-repo/per-service languages by LOC,
   frameworks via imports/manifests, build systems, Tier 1–3 mapping, and the
   supported/partial/unsupported verdict with reasons. This output is the
   evidence behind D2 catalog dispositions per tenant — it turns the static
   capability catalog into a living per-customer coverage matrix. Without it,
   dispositions are asserted; with it, they are measured.
2. **Enhancement backlog generation (commercial engine).** Every unsupported
   portion becomes a scoped enhancement item (*"Scala: grammar + symbol pipeline
   + call-graph, est. N weeks, unblocks billing-scala"*) with effort shaped by
   how much generic-tier machinery transfers. Feeds roadmap, prices PS
   engagements, converts "you can't do D and E" into an expansion conversation.
   Re-run on every IAP language release: *"Rust dataflow shipped — D just lit
   up."*
3. **Degradation honesty enforcement.** Partial coverage is certified as
   declared in the tenant profile (per-capability disposition, not a footnote),
   so downstream investigators and the UI never present absence-of-evidence as
   evidence-of-absence.
4. **Estate risk signals (free with the same pass):** polyglot service
   boundaries (a trace crossing Python→Scala is a blind handoff — flag it);
   generated code/vendor dirs (exclude from indexing — cost control); secrets
   committed in repos (hygiene warning before ingestion copies them into
   platform state); unbuildable checkouts and missing manifests (SBOM/endpoint
   sidecars will be thin — say so upfront); monorepo scale (quota and
   indexing-time sizing, feeds fairness config).
5. **Sizing and tier recommendation.** LOC per language/service → recommended
   support tier per app, initial quota envelope, expected indexing cost/time.
   Intake stops being a form and starts being a plan.
6. **Demo weaponization.** The coverage report is itself a demo artifact: "we
   mapped your 14 services in 10 minutes; here's exactly what lights up and
   why." Honesty about D and E, delivered with a priced path to support them,
   builds more trust than a flawless narrow demo.

**Build guidance (token budget is a non-issue at launch, discipline still
matters):** deterministic first, LLM for judgment. Extension census,
tree-sitter parses, import/manifest scans, sidecar outputs — cheap and
reproducible. Spend tokens on framework identification from ambiguous signals,
mapping findings onto the capability matrix, writing the narrative report a
human signs, and a second-pass adversarial self-review ("what did the census
miss?"). Pin everything: repo SHAs, analyzer versions, grammar versions,
catalog version — the report attaches to the onboarding request revision as
reproducible evidence. A coverage claim that can't be re-derived is marketing,
not engineering.

**Sequencing:** now — fix the Scala-class gap minimally (extension map plus an
"unrecognized language" bucket so nothing is ever silently invisible) and define
the Coverage Report schema; after 0R/1B — deterministic census + sidecar pass
as an intake analysis stage; after Phase 2/3 — LLM synthesis, disposition
certification, enhancement-item export; ongoing — CI rehearsal against a
polyglot fixture estate (Python + Java + Rust + Scala + Go).

**Open questions (Entry 2):** customer-facing verdict vocabulary (plain
Supported/Partial/Unsupported vs. catalog REQUIRED/OPTIONAL/UNSUPPORTED terms)?
Enhancement-item destination (typed report artifact first, export later)?
Is there a real near-term Scala customer, or is Scala standing in for "the
unknown"?

---

## 2026-10-22 — Entry 3: Read-only advisor architecture + the sniffer suite (the tools do the work)

**Constraint adopted:** The agent identifies and guides only. It never touches
what flows through the onboarding pipes. Formalized as: **the agent is a pure
function — `(sniffer outputs + capability catalog + pinned repo snapshot) →
guidance report`.**

**Enforcement (not aspiration):** the agent's identity carries read-only
capabilities (`tenant:read` scoped to the intake snapshot); under the Phase 0R
grant model it cannot authorize a write. Sniffers run against a pinned immutable
repo snapshot, write versioned JSON to scratch, never touch platform state. The
onboarding *workflow* invokes the agent as an analysis activity and files its
output; the agent holds no write handles. Every report claim traces to a
sniffer output line — a hallucinated "Gradle 8.2" fails evidence-pointer
resolution and the report validator rejects it. Hallucination containment by
construction. Token spend buys insight; it cannot buy side effects.

**The sniffer suite is the real build; the agent is its first consumer.**
Agent intelligence without sniffer coverage is a brain with no senses. Proposed
suite (small, versioned, single-purpose, JSON contracts):

1. **`census`** — file inventory by extension *plus* tree-sitter parse attempt
   per file. Parse-success rate per language is itself the tier signal
   (Python/Java clean, Rust symbol-level, Scala fails) — tier verdicts fall out
   of data, not opinion, and unparseable-but-present beats silently-absent.
2. **`buildmap`** — the universe-of-mess tamer. Detect every build file, extract
   modules, source roots, dependency declarations, toolchain versions. Replaces
   extension-guessing as ground truth — a `pom.xml` with `kotlin-maven-plugin`
   says more than a thousand `.kt` files.
3. **`cimapper`** — CI/CD discovery: pipelines, what they build/test/publish,
   artifact destinations, environment names. Yields test commands (grounding?),
   environment topology (maps to IAP environments), deployment targets (future
   knowledge sources).
4. **`depscan`** — dependency inventory from lockfiles and build declarations,
   cross-checked against the existing cdxgen SBOM sidecar. Declared-vs-locked
   contradictions are themselves findings.
5. **`hygscan`** (read-only, redacted) — secret-shaped strings, private registry
   URLs, hardcoded hosts. Report-only, values redacted — warns the operator
   *before* ingestion copies them into platform state.
6. **`sizer`** — LOC/service, generated-code detection, vendor-dir
   identification, binary-blob inventory. Feeds quota sizing and
   index-exclusion lists.

Each sniffer emits `tool/version/input-SHA/output-schema-version` — reproducible
and diffable. They are platform assets, not agent accessories: `buildmap`
source roots feed profile generation, `sizer` feeds quotas, `depscan` feeds the
SBOM story.

**Verified baseline (2026-10-22):** build-file detection exists only as
fragments — `integrations/profile_gen/detect.py` scores a handful of build
files (`pom.xml`, `build.gradle[.kts]`, `go.mod`, `Cargo.toml`), `github.py`
maps a few to toolchains, `routers/profiles.py` lists three filenames. No
Ant/Make/Bazel/sbt coverage, zero CI detection. "We don't have a sniffer yet"
is accurate for anything unified.

**Taming the build-system universe — tier, don't boil:**

- **Tier 1 — parse deeply** (Maven, Gradle, npm/pnpm, pip/poetry, Cargo, Go
  modules): full module graph, deps, source roots, toolchain versions. Covers
  the overwhelming majority of estates.
- **Tier 2 — detect and sketch** (Ant, Make, CMake, Bazel, sbt/mill, MSBuild):
  identify the system, extract coarse structure (targets, modules, Scala version
  from `build.sbt` alone answers "Scala 2 or 3 and what that implies"), surface
  raw files for the LLM pass. Ant `build.xml` is often the highest-value Tier-2
  target precisely because it signals legacy estates with the most onboarding
  risk.
- **Tier 3 — unknown/custom** (hand-rolled shell pipelines, exotic CI): flag,
  excerpt, hand to the LLM with "what builds what here?" — the long tail humans
  hate reading is where token budget earns its keep.

The known-build-file signature registry is versioned and extensible (same
pattern as the store registry). An unrecognized build system yields a
registry-gap finding, never a silent skip. Enumerable mess is manageable mess.

**Agent reasoning = cross-signal synthesis:** corroborate (Gradle+Kotlin plugin
+ 60/30 java/kt census → mixed JVM estate, one coherent verdict); contradict
(`package.json` claims TS but no `tsconfig.json`, 90% `.js` → downgrade with
reasons); trace consequences ("`payments-py → auth-scala` is a blind handoff;
recommend contract-test harvesting at that boundary as compensating coverage");
guide the operator (ordered remediation: pin moved branch SHAs, exclude
`vendor/`, rotate committed keys, add missing Maven wrapper); scope the
enhancement (sbt detection: small; Scala symbol pipeline via generic
tree-sitter tier: medium; Scala dataflow: large, blocked on call-graph
research — each with sizing evidence).

**Sequencing:** sniffer contracts (schemas) first — cheap, and the report format
gets designed against real shapes; then `census` + Tier-1 `buildmap` (kills the
biggest blind spots, upgrades `provision_tenant.py` guessing); then agent
synthesis over those two outputs with first Coverage Reports on our own repos as
fixtures; then CI mapper + remaining sniffers, Tier-2 driven by whichever
legacy systems early customers actually have (let reality prioritize, not
completeness instinct); ongoing nightly self-run against a polyglot fixture
estate so new blind spots arrive as failing tests.

**Open questions (Entry 3):** report consumer (operator doc only, or structured
artifact stored against the onboarding request for future gating — recommend
both)? Remediation authority (manual apply first, design for audited auto-apply
later)? First Tier-2 target — sbt/Scala (possibly real customer) or Ant/Make
(legacy-estate risk)? Is the Scala customer real or standing in for "the
unknown"?

---

## 2026-10-22 — Entry 4: The Support Registry + bridge discovery (anti-boil-the-ocean device)

**Scenario under discussion (hypothetical):** IAP supports Gradle but not Maven
for Java. The agent discovers a tool that converts Maven `pom.xml` files to
Gradle build files — and IAP is suddenly in business on a Maven estate without
building Maven support. What does this imply for agentic onboarding behavior?
The agent verifies against checklists and checks states.

**The Support Registry is the boundary that keeps the ocean unboiled.** A
versioned registry of everything IAP claims to handle, where every entry names:
capability, handling strategy, evidence requirements, owning team, acceptance
checklist, and current trust state. Handling strategies form a closed
vocabulary:

- **NATIVE** — first-class platform support (Gradle/Java, Python/pip).
- **GENERIC** — handled through a universal mechanism (tree-sitter symbol tier,
  CycloneDX sidecar), with named limits.
- **BRIDGE** — handled through a discovered adapter/converter, with a named
  bridge, version pin, and trust state.
- **UNSUPPORTED** — explicitly out of scope, with the reason and the shape of
  what would change that.

Anything not in the registry is UNSUPPORTED-by-default: fail closed, but
explained — never silently absent. The registry is versioned, and coverage
reports pin the registry version they were evaluated against (same discipline
as `catalog_version` on request rows). Admission, deprecation, and trust
promotion are governed events with an evidence bar, not edits.

**Bridge discovery is the agentic behavior that makes the registry elastic
without making it unbounded.** When the estate needs X and the registry has no
NATIVE/GENERIC path, the agent searches the bridge catalog before declaring
UNSUPPORTED:

- **Official converters first.** The Maven→Gradle hypothetical has a real
  anchor: `gradle init` converts Maven builds to Gradle. The general lesson:
  ecosystems often ship their own escape hatches (Gradle init, `2to3`-style
  codemods, `compile_commands.json` exporters). Prefer vendor-blessed bridges;
  distrust clever ones.
- **Interchange formats as universal adapters.** The N×M support problem
  collapses to N+M wherever an interchange format exists: CycloneDX for
  dependencies (cdxgen already emits it from `pom.xml` — meaning dependency
  inventory on a Maven estate needs *no conversion at all*, only the module
  structure does), SARIF for findings, compilation databases for the C family,
  OAS for API surfaces (Noir sidecar already). The registry should record, per
  ecosystem, which interchange formats it can emit and which IAP consumes.
  Every interchange edge is support without customization.
- **Shims and wrappers** (generated config, adapter configs) — recorded with
  exact generator, version, and inputs.
- **LLM-assisted translation** of build files — permitted only as
  lowest-preference, always PROVISIONAL, always human-reviewed. A translated
  build file the agent cannot independently verify is a liability, not support.

**A discovered bridge is untrusted until verified — verification checklists are
the other half of the agentic behavior.** Concretely, for the Maven→Gradle
walk-through:

1. `buildmap` finds multi-module `pom.xml`, no Gradle files. Registry:
   Maven = Tier-2-sketch, Gradle = Tier-1-NATIVE.
2. Agent consults the bridge catalog: `gradle init` (official converter) for
   structure; cdxgen/CycloneDX (already running) for dependency inventory —
   correctly splitting the problem so no conversion is attempted where none is
   needed.
3. The bridge executes in a **sandbox on the pinned snapshot**: run the pinned
   `gradle init` version, then compare its module list against three
   independent signals — `<modules>` in the POMs, census directory structure,
   cdxgen component list. Full 1:1:1 match → output marked
   TRUSTED-PROVISIONAL; any divergence → flagged with the diff, never silently
   adopted. Determinism check: run twice, byte-compare.
4. The agent states the caveats as findings, not footnotes: converted build is
   *analysis input only*, never written back to the customer repo; custom
   plugins/profiles may not translate (unconverted sections listed by name);
   the conversion is rehearsal-scoped unless the customer adopts Gradle.
5. Coverage report records: *"payments-java: full Java support via
   Gradle-bridge (provisional, verified 1:1 module match, tool pinned) +
   native dependency inventory via CycloneDX. Bridge owner: platform.
   Re-verify on every estate change."*

**Graduated trust, enforced by state:** bridge outputs carry a trust state —
`UNVERIFIED → PROVISIONAL → TRUSTED` — promoted by verification evidence or
human sign-off, demoted automatically on estate change, tool-version change, or
verification failure. A PROVISIONAL bridge can gate planning but never silently
become ground truth. This is "check the states" applied to the agent's own
inputs.

**Three verification loops (the agent's standing job):**

1. **Estate-vs-registry conformance:** does this customer estate fit inside the
   registered support surface, via which strategy per component, and what is
   the trust state of each edge?
2. **Run-vs-plan progress:** read the control-store state (read-only) and check
   it — request advanced DRAFT→VALIDATING? dispatch accepted? workflow
   running? readiness fresh? If a stage is stuck past its bound, diagnose from
   evidence (lease expired? worker silent? Temporal outage? stale policy?) and
   guide the operator to the exact remediation. The agent as read-only watchdog
   with diagnosis.
3. **Bridge-vs-checklist correctness:** every bridge re-verified on each use;
   checklist results appended to the run record, never overwritten.

**Supply-chain honesty (must be designed in, not discovered later):** running
`gradle init` — or any converter — executes third-party code against customer
sources. The sandbox needs pinned tool versions, no/default-deny network,
resource bounds, and an explicit policy for which bridges may execute vs.
which are data-only (CycloneDX *reading* a POM is data; a converter *running
a build* is execution). The paranoid default: data-only bridges preferred;
executing bridges require sandbox + pin + audit, recorded per run.

**Sequencing:** registry schema + closed strategy vocabulary now (cheap, shapes
everything downstream); bridge catalog seeded with the conversions we already
know (`gradle init`, CycloneDX-per-ecosystem matrix, compilation databases);
verification-checklist template per registry entry; agent bridge-seeking and
state-watchdog behaviors after the sniffer suite lands; promotion criteria
(PROVISIONAL→TRUSTED thresholds) set with Security before any bridge output
gates activation.

**Open questions (Entry 4):** who approves new registry entries and bridge
trust promotions — per-item Security review or batched platform review? Are
executing bridges acceptable in the onboarding sandbox at all, or data-only
until a sandbox hardening milestone? Can a TRUSTED bridge ever gate activation,
or do bridges cap out at planning-grade confidence? Should the bridge catalog
live in-repo (versioned with the platform) or as a separately governed data
set?

---

## 2026-10-22 — Entry 5: Where the LLM (Muse) actually earns its place in onboarding

**Thesis:** The LLM is the judgment layer inside a deterministic harness — it
never measures, never mutates, never approves its own output. Every LLM touchpoint
below sits between a deterministic input (sniffer JSON, control-store state,
registry entries) and a deterministic check (schema validation, evidence-pointer
resolution, checklist verification). Token budget is unconstrained at launch, so
the question is never "can we afford to ask" but "is this a judgment call or a
measurement?" Measurements belong in sniffers; judgment calls belong to the LLM.

### 1. Estate understanding (pre-ingest, highest value per token)

- **Ambiguous-signal resolution.** Deterministic detectors classify the known
  universe; the LLM adjudicates the rest — an unfamiliar framework from import
  shapes, a custom build layout, a Tier-3 build system no signature matches.
  Output is always a ranked hypothesis with the evidence cited, never a bare
  label.
- **Repo archeology.** READMEs, runbooks, ADRs, and design docs summarized into
  (a) operator briefing ("how this estate is actually built and run") and (b)
  seed material for the knowledge corpus, with provenance per claim. Humans
  don't read 400-page wiki exports before a demo; the LLM does, and says what
  mattered.
- **Dependency risk narration.** A CVE list is data; "log4j 2.14 reachable from
  the public-facing `ingest` endpoint through `payments-py`, while the X11
  finding is in a dev-only tool — fix order is A, then B, ignore C" is
  judgment. The LLM writes the prioritized narrative from depscan + reachability
  evidence.
- **Polyglot boundary analysis.** Where static analysis stops (the
  Python→Scala handoff), the LLM traces the seam from both sides — API
  contracts, serialization formats, queue topics — and names exactly what
  investigation loses at the crossing plus the cheapest compensating coverage
  (contract tests, log correlation keys).

### 2. Coverage synthesis (the Coverage Report author)

- **Findings → dispositions with reasons.** Sniffer outputs say what is there;
  the LLM argues what it means against the capability catalog: per-service
  SUPPORTED/PARTIAL/UNSUPPORTED verdicts, each with cited evidence and named
  limits ("generic tier: symbol search works, dataflow does not, because no
  call-graph facts exist for Rust in this platform version").
- **Two-tone narrative.** The same verdicts rendered twice: an operator brief
  (precise, remediation-ordered, with commands) and a customer-safe summary
  (honest about D and E, framed around the path to support them, zero internal
  jargon). Tone control is pure LLM territory.
- **Adversarial self-review.** A second pass with the explicit brief "what did
  the census miss?" — unparseable dirs, skipped symlinks, excluded paths that
  look suspiciously load-bearing, languages near the recognition threshold.
  Cheap insurance against the invisible-Scala class, and it gets better the
  more failure exhibits accumulate.
- **Enhancement scoping.** Drafting the sized backlog items from Entry 2:
  what transfers from existing tiers, what is genuinely new research, what
  evidence sizes it. A human prices; the LLM does the homework.

### 3. Guidance generation (operator assistance)

- **Ordered remediation with commands.** "Pin `repo@sha` (branch moved twice
  during sniffing)" beats "repository unstable." Every recommendation carries
  the exact command, the expected outcome, and how to verify it worked.
- **Checklist authoring.** Drafting the per-registry-entry acceptance
  checklists from Entry 4 (what must be true for "Gradle Java estate:
  onboardable"), which humans then ratify. Checklists written once by judgment,
  executed forever by code.
- **Stuck-run diagnosis (the watchdog brain).** Given control-store state +
  evidence (lease ages, worker heartbeats, Temporal history, policy versions),
  rank probable causes with the test that distinguishes each: "lease expired
  twice with no worker heartbeat → worker pool is down, not slow; check
  <runbook link> before re-queuing." Diagnosis is the highest-leverage LLM
  minute in the whole pipeline — it converts a dead dashboard into a next
  action.

### 4. Bridge assistance (Entry 4's long tail)

- **Interpreting the exotic.** Custom build setups and Tier-3 CI YAML, read
  closely and explained: what builds what, in which order, producing which
  artifacts. The output feeds `buildmap` gaps as structured hypotheses a human
  confirms.
- **Drafting translations, lowest-preference.** Build-file translation drafts
  (Maven→Gradle where `gradle init` falls short, Ant target mapping) marked
  PROVISIONAL, diffed against independent signals, human-reviewed. The LLM
  proposes; verification disposes.
- **Bridge-output narration.** Beyond byte-compare: "the converted build
  preserves all 14 modules but drops 3 Maven profiles (`prod-signing`,
  `legacy-jdk8`, `docs-only`) — first two need disposition decisions, third is
  safe to ignore." Semantic diff summaries humans will actually read.

### 5. Conversational interface (operator Q&A over the estate)

- **Evidence-backed answers, not chat.** "Why is D unsupported?" → the verdict,
  the three strongest evidence pointers, and what would change it. Every answer
  carries its citations; a question the evidence can't answer gets "unknown,
  here's the probe that would resolve it" instead of a guess.
- **Demo narration support.** Operator asks "what do I show first?" → the
  verified green path through the estate, ordered by evidentiary strength, with
  fallback branches if a live query misbehaves. A runbook that talks back.
- **Customer-explanation drafting.** Turning internal verdicts into
  customer-safe language on demand, without leaking internals (adapter names,
  thresholds, staging details stay inside).

### 6. Continuous value (after day one)

- **Release re-evaluation.** On every IAP language/tooling release, re-run
  verdicts against stored sniffer outputs: "Rust dataflow shipped — services D
  and F move GENERIC→NATIVE-equivalent; here is the updated report." Expansion
  revenue with receipts.
- **Drift narration.** Estate changed since onboarding → "three new Go
  services, one new Maven module in payments-java (bridge re-verification
  passed), Scala service unchanged." Words over diffs.
- **Correction harvesting (careful).** Operator corrections ("that verdict
  was wrong because X") stored as labeled feedback records attached to the
  report — improving future prompts and checklists. Explicitly *not* model
  training on customer code; feedback records stay in-platform, scoped per
  tenant, and human-approved before reuse anywhere.

### 7. Guardrails that make all of the above safe

- **Evidence-pointer discipline:** every factual claim in every LLM output
  resolves to a sniffer output line, a registry entry, or a control-store
  record — enforced by a report validator, not by instruction-following hope.
- **Calibrated confidence:** verdicts carry high/medium/low plus "what would
  raise this" (the missing probe, named). Medium-confidence verdicts gate
  planning; only high-confidence ones may gate activation — and that threshold
  is policy, not vibes.
- **Redaction at the boundary:** `hygscan` redacts before anything reaches a
  prompt. The LLM never sees raw secrets, and prompts are constructed from
  sniffer JSON, never raw repo text, except through explicit, audited
  excerpt windows (bounded lines, named files, logged).
- **Deterministic pre/post checks:** schema-validate every structured output;
  re-run the checklist the LLM claims to have satisfied; reject-and-retry (or
  escalate to human) on validation failure. The harness treats the LLM as an
  unreliable-but-brilliant subcontractor.
- **Model versioning and caching:** model name/version recorded in every
  report (reproducibility); content-addressed caching (same input SHA → cached
  analysis) so today's unconstrained budget doesn't become tomorrow's
  cost crisis at scale. Cache invalidation keys: input SHA + model version +
  catalog/registry versions.
- **Negative scope (hard):** no direct tool execution with side effects; no
  writes to repos, pipes, or the control store; no self-approval of its own
  outputs; no invented evidence; no checklist bypasses. If a future proposal
  needs the LLM to *do* rather than *judge*, it comes back to this document
  first.

### Touchpoint map (where the LLM sits in the pipeline)

| Stage | LLM role | Deterministic guard |
|---|---|---|
| Sniffing | Interpret Tier-3 unknowns, narrate anomalies | Census/buildmap/sidecar JSON in; schemas out |
| Coverage synthesis | Verdicts + two-tone narrative + self-review | Catalog dispositions; evidence-pointer validation |
| Guidance | Remediation with commands; checklist drafts | Operator applies; checklists human-ratified |
| Bridge verification | Semantic diff narration; translation drafts | Byte-compare + independent-signal match first |
| Watchdog | Stuck-run diagnosis ranked by evidence | State reads only; no remediation actions taken |
| Demo prep | Green-path planning; Q&A; fallback branches | All claims trace to verified gates |
| Post-launch | Re-evaluation; drift narration; correction records | Version pins; per-tenant feedback scope |

**Open questions (Entry 5):** model choice locked to one vendor or
model-agnostic harness with recorded versions (recommend the latter — avoids
single-vendor coupling and makes evals portable)? Human-ratification bar for
checklists the LLM drafts (per-entry sign-off vs. batched review)? Confidence
threshold for activation-gating verdicts — who sets it, and is it per-capability
or global? Are correction records tenant-scoped forever, or is there a
sanitized cross-tenant learning path (recommend tenant-scoped; revisit only
with explicit data-governance approval)?

---

## 2026-10-22 — Entry 6: Degraded estates — low tests, thin logs, no runtime DB access (the guessed answer, done honestly)

**Scenario:** The operator's codebase has low unit-test coverage, no detailed
structured logging, and legal rules forbid runtime database access. The agent
must still produce an initial account of what IAP can achieve and what it
cannot — at least a guessed answer. This is not an edge case; in enterprise
landscapes it is the default starting position. The design must treat degraded
inputs as the norm and full inputs as the upgrade.

**Foundational move: separate the evidence tiers and name the floor.** IAP's
investigation power draws on two fundamentally different input classes:

- **Static (repo-derived, always available):** source code, build files,
  dependency declarations, migrations/DDL, docs, CI definitions. Nobody's
  legal team blocks reading a repo snapshot.
- **Dynamic (runtime-derived, often blocked or thin):** structured logs,
  traces, metrics, live DB state, APM, test suites as oracles.

The "at least" answer is the **static floor**: codebase navigation and symbol
search, static call graphs and dataflow (Python/Java deep tier), SBOM and
vulnerability inventory, endpoint/API catalog, deployment-topology sketch from
build+CI, documentation-grounded answers. That floor must be stated explicitly
in every degraded-estate report — it is real, demoable value ("we mapped your
estate and its dependency risks on day one with zero runtime access"), and it
sets the baseline the ladder climbs from.

**The guessed answer is a confidence-graded projection, not a shrug.** Formalize
three guess tiers, each labeled, each falsifiable, each naming its cheapest
confirming probe:

- **VERIFIED** — evidence in hand (parsed code, SBOM match, probe success).
- **PROJECTED** — strong inference from proxy signals ("the codebase imports
  the OTel SDK and configures an exporter; structured traces are *likely*
  available once you point us at the collector — confirm with one exporter
  endpoint check").
- **SPECULATIVE** — weak signals, honestly marked ("e-commerce checkout flow
  suggests payment-provider webhooks exist; no webhook handlers found in
  scanned services — could live in the unscanned Scala service; confirm by
  pointing the census at it").

A guess without a named confirming probe is just an opinion; with one, it is a
work order. The report's most-read section will be the probe list, not the
verdicts — design for that.

**The upgrade ladder (the real product of the degraded-estate report).** Every
gap converts to a named rung: the exact input that unlocks the next capability,
specified precisely enough to hand to the customer's engineers:

- *Logging rung:* "Your services emit free-text logs. Adding five fields —
  correlation ID, tenant ID, UTC timestamp, outcome code, service version
  (mirroring IAP's own telemetry contract) — unlocks timeline reconstruction
  and cross-service trace stitching." The agent can go further and generate the
  logging-schema diff: logger-config patches, OTel mapping, per-service field
  checklist. Specify-first, patch-on-request.
- *Test rung:* low coverage means IAP loses tests as verification oracles.
  The agent identifies highest-value test gaps (entry points, payment paths,
  auth flows) — guiding customer test investment that simultaneously improves
  their engineering *and* IAP grounding. Frame it as shared interest, which it
  is.
- *Data-access rung (legal-sensitive):* full row access blocked does not mean
  zero data-model visibility. Migrations and ORM models in the repo yield the
  schema statically — table structure, relationships, constraints — without
  touching a live row. What stays blocked is row-level state investigation
  ("what does customer 4821's record look like right now"). The unlock proposal
  should be pre-shaped for legal review: read-only role, named tables only,
  PII columns masked at the role level, every query audited, no exports —
  essentially the `iap_registry_admin` pattern (dedicated role, no bypass,
  audited) applied to customer data. Bring legal a bounded, auditable ask, not
  "give us the database."
- *Each rung states its unlock:* "rung 2 (structured logs) upgrades timeline
  reconstruction VERIFIED and unlocks cross-service stitching; cost to
  customer: ~2 days of logger-config work, agent supplies the diff."

**Legal-constrained patterns to establish early:**

- *Static-first data doctrine:* schema from migrations/models is always fair
  game; rows require the audited role. The report must show its work on which
  side of that line every data claim sits.
- *Minimization by construction:* sniffers extract structure, never row dumps;
  `hygscan`-style redaction applies to anything row-shaped encountered
  incidentally.
- *Access ledger:* everything IAP touched during onboarding (files, schemas,
  endpoints probed) listed for legal comfort. The same evidence-pointer
  discipline that contains hallucination also satisfies auditors — one
  mechanism, two audiences.
- *Hermetic-analysis question (parked):* for estates where even snapshots can't
  leave the customer boundary, analysis must run inside it. That is a
  deployment-model decision with large consequences — see open questions. Do
  not improvise it per customer.

**Progressive enhancement loop:** the degraded report is version 0 by design.
Customer adds structured logging → re-sniff → report v1 with raised
confidences and newly unlocked capabilities. This is Entry 5's drift loop with
its first concrete use case, and it gives the commercial relationship a
heartbeat: every customer improvement visibly lights up new IAP capability,
narrated in the updated report. The ladder is also the expansion roadmap.

**Failure modes to forbid explicitly:**

- Never present static-only findings with the confidence language of runtime
  findings ("no anomalous queries detected" when you never saw a query log is
  a lie by omission — the report must say "query behavior: unknown, rung 3
  unlocks it").
- Never let a SPECULATIVE guess harden into a VERIFIED claim across report
  versions without its confirming probe firing. Version diffs must show
  confidence changes with reasons.
- Never recommend weakening a legal boundary for convenience ("just give us
  prod read access for the demo"). The bounded-role ask is the product; the
  shortcut is a liability.

**Demo angle:** this scenario *is* the enterprise demo. "You have thin logs,
  12% coverage, and locked-down databases — typical. Here's what works today
  (static floor, demoed live), here's the three-rung ladder with exact diffs,
  here's rung 1 lighting up timeline reconstruction." That pitch wins more
  deals than a flawless green-field demo, because every buyer in the room
  recognizes their own estate.

**Open questions (Entry 6):** is the read-only masked replica the standard
legal ask, or do we need a hermetic/on-prem analysis story for some customers
(recommend: bounded-role ask first; hermetic only if a real customer demands
it)? What test-coverage threshold counts as "grounding-grade," and is it
per-service or global? Should the agent generate logging-schema patches or
specify-only (recommend specify-first, patch on explicit request)? Who signs
off that a SPECULATIVE→VERIFIED promotion was earned — the operator, or an
automated confirming probe (recommend: probe evidence required, human
acknowledges)?

---

## 2026-10-22 — Entry 7: Meta Global AI Developer Hackathon ($1M, Oct 26–Nov 4) as showcase vehicle

**Event (from https://dev.meta.ai/events/global-hackathon):** public, virtual,
worldwide hackathon. Ten build days (Oct 26–Nov 4), applications close Oct 25
on a rolling 20,000 cap. $1M prize pool across two tracks: **indie developer**
(individuals/students; judged on creativity, technical ambition, whether it
works) and **startup** (teams/founders; judged on technical execution and
production-readiness). Two themes: **Build for Your World** (Meta models in
your domain, problems you know firsthand) and **Push the Frontier** (new
machine capabilities, science/engineering acceleration). Projects must use
Meta's models (Muse Spark series, Image, SAM) through Meta Model API, plus
Muse Code as the agentic coding agent. Application scored 50/50 on project-idea
clarity (+ theme fit) and demonstrated technical skill, on information in the
form alone. **Eligibility gates to verify before applying:** Meta employees,
contractors, and interns are excluded, along with sanctioned-country residents
and others named in the official rules — confirm status against the published
rules first; everything below assumes a clear path.

**Why the fit is strong (verified against the tree):**

1. **IAP already speaks Muse.** The LLM gateway lists `muse` as a first-class
   provider (`SUPPORTED_PROVIDERS` includes it; `IAP_LLM_PROVIDER=muse` +
   `IAP_MUSE_ENDPOINT`; model policies; transport compat; chat router
   acceptance). Running the reasoner fleet on Muse Spark models is a config
   change, not a build. Verify the `muse` provider path against the actual
   Meta Model API early — config acceptance exists, but prompts and model
   policies were tuned around other entries; smoke-test one investigation and
   re-run a battery slice, budgeting a day for deltas.
2. **One-command demo + built-in scoreboard.** `scripts/start-platform.sh`
   (compose → migrate → API + worker); `scripts/live_battery.py` (scripted
   investigations, fixed rubric). Judge-ready loop: run the battery live, show
   the scoreboard, open a trace showing every reasoning call on the Muse
   endpoint. Most demos assert intelligence; this one measures it on stage.
3. **The whiteboard hands over a hackathon-sized build.** The onboarding
   coverage advisor (Entries 2–5) is read-only, needs no control store, and
   runs snapshot-in/report-out — buildable standalone in days, and it shows
   exactly what Muse Spark is for: judgment over deterministic evidence (tier
   verdicts with citations, bridge discovery, remediation guidance, estate
   Q&A).

**Recommended shape: enter the advisor, stage the fleet.** The hackathon entry
is the standalone coverage advisor — repo snapshot in, coverage report out,
powered by Muse Spark via Model API. The IAP investigation platform is the
production context it was born from and the demo stage (seeded tenant,
scripted defect → autonomous investigation → cited verdict, all reasoning on
Muse). This creates a clean IP boundary: the entry exposes no proprietary
platform code, and if it wins, IAP inherits a proven component.

**Track/theme positioning:** advisor as **indie-track** entry (novel,
ambitious, demoable) — or startup track if entered as the team behind a real
product, where multi-tenancy, audit, and eval battery are exactly what judges
score. Theme: Build for Your World (onboarding pain known firsthand) with
Push-the-Frontier flavor. Application narrative: "an agent that audits a
codebase and tells you exactly what an AI investigation platform can and
cannot cover, with evidence" — clear, demo-shaped, skill-evidenced by shipped
platform work. Apply early (rolling review).

**10-day build sketch:** days 1–2 verify Muse provider path + one-command demo
rehearsal; days 3–7 advisor slice (census + tier mapping + coverage report +
evidence-backed Q&A, read-only); days 8–9 polyglot demo estates including one
designed to surface the invisible-language blind spot + recorded golden-run
fallback (never let live infra be the single point of demo failure); day 10
submission polish. Scope discipline: advisor stays snapshot-scoped; any
intake/route wiring is post-hackathon, Phase-gated work.

**Risks:** endpoint access/reproducibility outside the primary machine;
reasoning-behavior deltas on Muse models vs. tuned policies; scope creep into
platform wiring under time pressure.

**Open questions (Entry 7):** win/place vs. participate/learn (changes scoping
aggressiveness)? IP posture for a public showing — is a standalone advisor
tool built from these ideas clear to show, or does even the concept need
approval (determines whether the entry is the tool or a sanitized demo)?
Confirm Model API compatibility of the existing `muse` provider path in code,
not just config.

**Addendum — model access costs (researched 2026-10-22, from the public event
pages):** Confirmed free inference credits exist but are scoped to
open-weights models via partners — Baseten ($100 + 2M TPM), Modal ($100),
Fireworks ($25) — each "to select participants," not guaranteed to all.
No explicit free-credit statement exists for Muse Spark via Meta Model API;
"access to our newest models" is promised but the mechanism (provisioned keys,
credit grants, free-tier limits, or BYOK) is unpublished — Official Rules land
Oct 26, practical details typically at kickoff/Discord office hours. Working
assumptions: provisioned access is likely but unconfirmed, so (a) ask the
credits/keys question in the first office hours and get it in writing, (b)
keep the build endpoint-portable (IAP's `muse` provider is already an
OpenAI-compatible endpoint flip — same build runs on Model API, Fireworks,
Baseten, or Modal), (c) hold open-weights partner credits as fallback for bulk
work, reserving Spark for high-judgment calls, and (d) keep a rough token
budget for the advisor slice anyway (snapshot-in/report-out is inherently
bounded and cacheable). In the application, describe the advisor as
model-portable rather than single-model-committed.

---

## 2026-10-22 — Entry 8: Integration timing — what must be concrete before which phase

**Question:** looking at the current onboarding implementation (Phases 0/1A
committed, Phase 0R gate next, then 1B–6), before which phase must the
onboarding-agent ideas be concrete so implementation needs no rework and
integrates smoothly?

**Headline answer: lock the agent's contracts during Phase 0R design — before
the 0R exit review.** Phase 0R owns exactly the three things the agent
attaches to: the scoped-grant model, the database roles, and the additive
migration wave. Everything after 1B gets expensive to change (canonical intake
hashes, pinned workflow definitions, activation CAS tables). Concretely, the
0R exit review should carry an explicit agent-integration checklist sign-off;
without it, 1B/2/3 will bake in assumptions the agent later breaks.

**Why late changes hurt (the rework mechanics):**

- *After 1B:* the intake canonical hash is frozen. If coverage linkage isn't
  decided, 1B's acceptance transaction, idempotency binding, and audit linkage
  get designed around an intake shape that later grows appendages.
- *After Phase 2:* workflow definitions pin activity contracts. Retrofitting a
  coverage-analysis activity means a workflow version bump plus reconciler and
  step-ledger changes.
- *After Phase 3:* activation CAS checks a fixed set of readiness evidence.
  Adding coverage as a gate later means changing the transition table — a
  correctness-critical edit under load.

**Phase-by-phase insertion map:**

*BEFORE 0R EXIT (hardest deadline — design inputs to 0R itself):*

1. **Agent identity + grant scopes.** The `onboarding_grants` table design
   must include read-only analysis scopes from day one (e.g. a coverage/analyze
   capability over tenant/app/environment/operation, validity-bounded like all
   grants). Recommendation: the agent runs as its own principal with read-only
   grants — separated from `iap_worker`'s claim privileges by least privilege.
   Retrofitting a scope into the grant schema + resolver after 0R lands is
   schema + code rework.
2. **Registry + coverage tables ride the same additive migration wave.**
   `support_registry` (capability, strategy NATIVE/GENERIC/BRIDGE/UNSUPPORTED,
   trust state, evidence requirements, version, owner) and `coverage_reports`
   (request/revision link, report version, content hash, verdicts). Additive
   either way, but one reviewed wave beats piecemeal DDL churn — and 0R already
   opens that wave for fence/epoch, lease fencing, generations, and the
   `background_jobs(tenant_id, id)` composite unique.
3. **Agent reads need no cross-tenant privilege — confirm by design.** The
   agent works per-request tenant scope (intake snapshot), so it never needs
   the worker's cross-tenant dispatch policy or the registry-admin path. State
   this explicitly in 0R so nobody "helpfully" grants it later.
4. **F-11 closed dispositions + D2.1 enum final.** 1B validates intake against
   them; Phase 3 policy evaluation consumes them. The vocabulary must be frozen
   before 1B schemas reference it.

*BEFORE 1B (intake schemas):*

5. **Link, don't embed.** The coverage report is platform-derived evidence, not
   operator-supplied intake: it must NOT go inside `intake_json`. Link it by
   `(request_id, revision)` with its own content hash, referenced separately
   by readiness/activation decisions. The request `content_hash` covers intake
   only. Decide this before 1B's canonical-hash and idempotency design, or the
   acceptance transaction bakes in the wrong shape.
6. **Redaction rules align with `SecretReference`.** What `hygscan` must redact
   before anything reaches a prompt is defined by the same typed-secret
   shapes 1B introduces. Lock them together.

*BEFORE Phase 2 (workflows):*

7. **Reserve the analysis activity slot.** Workflow definitions include a
   coverage-analysis activity with a stable idempotency key
   (`request_id + revision + generation`), consuming sniffer outputs and filing
   the report. Specify the slot (inputs, key, failure semantics) even if the
   activity body ships later.
8. **Watchdog read scopes** on dispatch/job/projection tables for the agent
   principal come from the 0R grant design (item 1) — verify, don't reinvent.

*BEFORE Phase 3/4:*

9. **Activation-gating policy.** Does `ACTIVE` require a coverage report at
   some trust level, or is coverage advisory? Decide before Phase 3's
   activation CAS enumerates its evidence set. Changing the transition table
   afterward is a correctness-critical edit.
10. **Report API surface** (`GET` coverage) can defer to Phase 4/6 — additive
    routes are cheap — but response schemas must reference the report schema
    locked in item 2.

*CAN DEFER with no rework risk:* sniffer implementations (schemas locked early,
bodies anytime); LLM prompts and Q&A surface; demo bundle; bridge-catalog
contents (data, provided the trust-state enum is locked); enhancement-backlog
export; drift narration (Phase 6/ops).

**Session-phase mapping (current plan):** Session Phase C (intake schemas) must
take items 4–6 as inputs — it cannot start from the catalog alone. Session
Phase E (dispatcher skeleton) must not pour concrete around item 7's slot
until the slot is specified; interfaces only. The 0R-exit agent checklist:
grant scopes present, registry/coverage tables in the migration wave,
link-not-embed recorded, disposition enum frozen, activation-gating question
answered-or-explicitly-parked.

**Open questions (Entry 8):** separate agent principal vs. reusing a read-only
worker credential (recommend separate principal — cleaner audit, least
privilege)? Is coverage advisory-only at activation for v0.1 with gating
deferred to a named follow-up, or a mandatory gate from day one (recommend
advisory-first to avoid blocking activation on a brand-new subsystem)? Do
sniffer output schemas need the same versioned-catalog treatment as the
support registry (recommend yes — same pattern, one mechanism)?

---

## Appendix — Cross-references (no new decisions)

- Onboarding control store, outbox dispatch, scoped grants, Phase 0R gate:
  `docs/IAP-TENANT-ONBOARDING-DESIGN-Luna.md`,
  `docs/IAP-TENANT-ONBOARDING-IMPL-PLAN-luna.md`.
- Security/concurrency, workflow-correctness, deployment/deletion contracts:
  `IAP-TENANT-ONBOARDING-SECURITY-CONCURRENCY.md`,
  `IAP-TENANT-ONBOARDING-WORKFLOW-CORRECTNESS.md`,
  `IAP-TENANT-ONBOARDING-DEPLOYMENT-DELETION-ASSURANCE.md`.
- Existing primitives: `scripts/ingest_github.py`, `scripts/provision_tenant.py`,
  `scripts/seed_profile.py`, `scripts/iap-operator.py`,
  `src/investigation_agent_platform/application/analysis/` (python/java fact
  pipelines), `src/investigation_agent_platform/infrastructure/evidence/code/`
  (generic tree-sitter tier), `integrations/profile_gen/` (embryonic
  build-file detection).
