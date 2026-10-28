# Design: Knowledge Bank conversion component

Component specification for the generic raw-information → knowledge conversion layer:
every investigation's accessed stream (banked evidence rows, code facts, doc chunks,
tool outputs) is normalized, entity-resolved, interpreted, and served as one organized
bank. Adapters are provider-specific; everything downstream is generic.

Status: **PROPOSED — awaiting owner mark.** Sections tagged `PROPOSED` record a
recommended default for an owner-open question (see §9 of the 2026-10-28 session record);
the implementer treats PROPOSED items as decided only after the mark.

## Context

Investigations bank raw provider output (evidence rows per `EvidenceType`, Java code facts,
tool results) and reason over those piles directly. A knowledge write path exists
(`KnowledgeCaptureService.capture_for_investigation`, post-conclusion, never failing the
investigation) distilling `error_signature`, `evidence_summary`, and `premises` (VERIFIED
hypotheses), served back via `retrieve_for_reasoning` into `KnowledgeContext`. What does
not exist is the conversion in between: nothing turns the accessed stream into organized
understanding. Findings/conclusions are not distilled (`finding_repo` is plumbed but
unused); code-linked knowledge has no invalidation (TTL only); per-provider silos mean
the same error seen in logs, code, and docs is learned three times or not at all.

This design adds the missing middle as a generic component in `application/knowledge/`
(the understanding side), following the repo's ports-and-adapters convention: `Protocol`
ports in `ports/knowledge/`, Pydantic domain models with `Field` length bounds, worker
activities for async work, Temporal for dispatched jobs.

No `docs/dev/architecture.md` exists in the project; architecture changes are recorded
against `docs/IAP-architecture-v1.0.md` (see Documentation Plan).

## References

- `src/investigation_agent_platform/application/knowledge/capture.py` — Existing post-conclusion distillation (error signatures, evidence summaries, premises); the bank becomes its upstream input in v2.
- `src/investigation_agent_platform/application/knowledge/retrieve.py` — `retrieve_for_reasoning` envelope gates, confidence floor, attribution join, and `KnowledgeContext`; the bank is served as a new context section under the same gates.
- `src/investigation_agent_platform/application/knowledge/janitor.py` — TTL/expiry sweep pattern reused for bank-entry lifecycle.
- `src/investigation_agent_platform/domain/knowledge/models.py` — `KnowledgeArtifact`, `RefreshPolicy`, `ArtifactVisibility`, `ArtifactView`, `KnowledgeContext` shapes this design mirrors.
- `src/investigation_agent_platform/domain/evidence/models.py` — `EvidenceType` (15 values) and `EvidenceRelationshipType` (`CORROBORATES`/`CONTRADICTS`/`DERIVED_FROM`/…) reused for adapter input and interpretation links.
- `src/investigation_agent_platform/ports/knowledge/ports.py` — `Protocol` port convention (`ArtifactRepository`, `KnowledgeStorePort`, …) the bank ports follow.
- `src/investigation_agent_platform/api/v1/routers/integrations.py` — GitHub push webhook (HMAC, changed-file union, Temporal dispatch) whose changed-file set drives bank staleness marking.
- `src/investigation_agent_platform/application/analysis/java/type_facts.py` — Precedent for old-vs-new parity harnesses gating a switchover, applied here to adapter fidelity.

## Goals / Non-Goals

**Goals:**

- One generic conversion pipeline: any raw item in, normalized bank entries out
- Thin per-provider adapters; provider-specific code lives only in adapters
- Entity resolution across providers (same error/service/file → one entry, many attestations)
- Interpretation links reusing `EvidenceRelationshipType` against open questions
- Bank-first reasoning with raw drill-down (auditability preserved)
- Staleness marking from the webhook changed-file stream; TTL backstop via janitor
- Additive-only rollout: raw rows stay the source of truth; the bank is derived and rebuildable

**Non-Goals:**

- Replacing the evidence store or banked rows (the bank derives from them, never instead of them)
- Cross-investigation reuse queries in v1 (per-investigation working bank first; reuse via capture in v2)
- New user-facing surfaces (no REST/CLI; served through existing `retrieve_for_reasoning`)
- LLM-only interpretation with no deterministic path (deterministic fallback is mandatory)
- Persisting intermediate reasoning traces (explicitly excluded from v1 inputs)

## Decisions

### D1: Placement in `application/knowledge/bank/`

**Decision:** The component lives in a new `application/knowledge/bank/` package with three
submodules: `adapters/` (one module per raw family), `core/` (normalize/resolve/interpret
orchestration), and `serve.py` (rendering served views). Ports live in
`ports/knowledge/ports.py` next to the existing knowledge ports.

```python
# docs layout (package skeleton, no behavior until D-gated phases land)
# src/investigation_agent_platform/application/knowledge/bank/__init__.py
# src/investigation_agent_platform/application/knowledge/bank/adapters/__init__.py
# src/investigation_agent_platform/application/knowledge/bank/adapters/base.py
# src/investigation_agent_platform/application/knowledge/bank/adapters/java_facts.py
# src/investigation_agent_platform/application/knowledge/bank/adapters/runtime_logs.py
# src/investigation_agent_platform/application/knowledge/bank/core/__init__.py
# src/investigation_agent_platform/application/knowledge/bank/core/service.py
# src/investigation_agent_platform/application/knowledge/bank/core/identity.py
# src/investigation_agent_platform/application/knowledge/bank/serve.py
```

**Alternative considered:** `application/evidence/` (gateway-adjacent, closer to the raw rows).
Rejected — `evidence/` is the provider side; conversion to understanding belongs with
`capture`/`retrieve`/`janitor` on the knowledge side, where the envelope-gate, visibility,
and lifecycle conventions already live.

---

### D2: Uniform bank-entry shape

**Decision:** Every adapter emits drafts in one frozen shape; the core promotes drafts to
entries by attaching identity and interpretation. Length bounds mirror
`domain/knowledge/models.py` conventions.

```python
# src/investigation_agent_platform/application/knowledge/bank/core/models.py
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class BankSubjectKind(StrEnum):
    ERROR = "ERROR"
    CODE_SITE = "CODE_SITE"
    SERVICE = "SERVICE"
    DOCUMENT = "DOCUMENT"
    COMMIT = "COMMIT"
    METRIC_SERIES = "METRIC_SERIES"
    GENERIC = "GENERIC"


class BankEntryDraft(BaseModel):
    """Adapter output: normalized but unresolved and uninterpreted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    subject_kind: BankSubjectKind = Field(description="Coarse subject family for routing.")
    subject_hint: str = Field(
        min_length=1, max_length=512, description="Adapter-supplied identity hint (template, symbol, path)."
    )
    content: str = Field(
        min_length=1, max_length=4096, description="Normalized statement of what was learned."
    )
    provider: str = Field(min_length=1, max_length=64, description="Producing provider or adapter name.")
    evidence_type: str = Field(min_length=1, max_length=64, description="Source EvidenceType value.")
    source_refs: list[str] = Field(
        default_factory=list, max_length=50, description="Pointers back to raw rows (drill-down)."
    )
    accessed_at: datetime = Field(description="When the investigation accessed the raw item.")
    turn: int = Field(ge=0, description="Investigation turn that accessed it.")
    confidence: float = Field(
        ge=0.0, le=1.0, description="Adapter-assessed confidence, never inflated downstream."
    )


class BankAttestation(BaseModel):
    """One raw sighting behind an entry. Entries accumulate attestations; they are never edited."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1, max_length=64)
    evidence_type: str = Field(min_length=1, max_length=64)
    source_ref: str = Field(min_length=1, max_length=1024)
    accessed_at: datetime
    turn: int = Field(ge=0)
    raw_excerpt: str = Field(max_length=1024, description="Short verbatim excerpt for audit display.")


class BankEntry(BaseModel):
    """Core output: identity-resolved entry with accumulated attestations and interpretation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID = Field(default_factory=uuid4)
    tenant_id: str = Field(min_length=1, max_length=128)
    application_id: str = Field(min_length=1, max_length=128)
    investigation_id: UUID = Field(description="Owning investigation (v1 scope).")
    identity_kind: str = Field(min_length=1, max_length=64)
    identity_key: str = Field(min_length=1, max_length=512)
    subject_kind: BankSubjectKind
    content: str = Field(min_length=1, max_length=4096)
    attestations: list[BankAttestation] = Field(default_factory=list, max_length=50)
    question_links: list[str] = Field(
        default_factory=list, max_length=20, description="Open-question IDs this entry bears on."
    )
    interpretation: str = Field(
        default="", max_length=2048, description="What the entry means for linked questions; empty until interpreted."
    )
    interpreted: bool = Field(default=False)
    conflict: bool = Field(default=False, description="True when attestations disagree (D6).")
    stale: bool = Field(default=False, description="True when invalidated by code change or TTL (D10).")
    confidence: float = Field(ge=0.0, le=1.0)
    created_at: datetime
    updated_at: datetime
```

**Alternative considered:** Reusing `KnowledgeArtifact` directly as the bank shape. Rejected —
artifacts are conclusion-side envelopes with sharing/visibility semantics; bank entries are
working understanding scoped to one investigation. Conflating them would force tenant-share
decisions onto every intermediate observation. The bank down-converts to artifacts at capture
time (v2), not before.

---

### D3: Adapter port and pilot pair — PROPOSED

**Decision (PROPOSED — owner Q on adapter granularity; recommended: one adapter per raw
family):** Adapters implement a narrow `Protocol`. The pilot ships two adapters: Java code
facts (already bank-shaped via `subject_identity` — easiest) and runtime logs (TemplateMiner
template seam exists). Remaining `EvidenceType` families land as follow-up adapters without
core changes.

```python
# src/investigation_agent_platform/ports/knowledge/ports.py (addition)
from typing import Any, Protocol


class BankAdapter(Protocol):
    """Provider-specific normalizer. The only provider-specific code in the component."""

    @property
    def adapter_name(self) -> str: ...

    def supports(self, evidence_type: str) -> bool:
        """True when this adapter normalizes the given EvidenceType value."""

    def normalize(self, raw: Any, *, turn: int) -> list["BankEntryDraft"]:
        """Convert one raw item to zero or more drafts. Never raises: unconvertible
        input yields [] (adapter degrades to unbanked, raw rows remain readable)."""
```

**Alternative considered:** A single generic normalizer with per-type field maps. Rejected —
field maps degrade into untyped stringly lookups and push provider knowledge into the core,
defeating the generic-core guarantee. One module per family keeps the blast radius of a new
provider to one file plus registration.

---

### D4: Identity reuses existing seams — PROPOSED

**Decision (PROPOSED — owner Q on identity scheme; recommended: reuse):** No new global
identity scheme. Each adapter derives `(identity_kind, identity_key)` from its established
seam, recorded in a derivation table owned by `core/identity.py`:

- Java code facts → `("code_symbol", subject_identity)` (e.g. `com.sample.Foo#bar/1`)
- Runtime logs → `("log_template", template_miner_signature)` (provider + template hash)
- Code references → `("code_site", path + 1-based line range)`
- Docs → `("document", indexed path)`
- Fallback → `("generic", provider + evidence_type + source)` (resolves only with itself)

**Alternative considered:** One bank-level identity scheme from day one. Rejected — it would
re-derive what TemplateMiner, `subject_identity`, and fingerprints already do, and block the
pilot on a cross-cutting design with no consumer yet. Revisit only when two adapters claim
the same seam.

---

### D5: Hybrid conversion, deterministic fallback mandatory — PROPOSED

**Decision (PROPOSED — owner Q on converter; recommended: hybrid):** Normalize and resolve
are deterministic. Interpretation (the `interpretation` text + `question_links`) runs through
a `BankInterpreter` port with two implementations: an LLM interpreter and a deterministic
fallback (keyword/question-overlap heuristics). An entry is valid uninterpreted
(`interpreted=False`); interpretation upgrades it, never gates it. Deterministic-only mode
must keep the full suite green.

```python
# src/investigation_agent_platform/ports/knowledge/ports.py (addition)
class BankInterpreter(Protocol):
    """Assigns meaning: what does this entry say about the open questions?"""

    async def interpret(
        self,
        entry_content: str,
        *,
        question_texts: dict[str, str],
        attestations: int,
    ) -> "BankInterpretation": ...
```

```python
# src/investigation_agent_platform/application/knowledge/bank/core/models.py (addition)
class BankInterpretation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    relation: str = Field(
        min_length=1,
        max_length=32,
        description="One of EvidenceRelationshipType: CORROBORATES, CONTRADICTS, DERIVED_FROM, RELATES_TO.",
    )
    question_ids: list[str] = Field(default_factory=list, max_length=20)
    note: str = Field(max_length=2048, description="One-to-two sentences of meaning, not raw restatement.")
    confidence: float = Field(ge=0.0, le=1.0)


class DeterministicBankInterpreter:
    """Fallback: overlap-based links, empty note, entry stays interpreted=False.

    Full service code on implementation; contract: never raises, never invents
    question IDs, confidence <= min(entry confidences).
    """
```

**Alternative considered:** LLM-only interpretation. Rejected — interpretation would become a
liveness and spend dependency of every reasoning turn, and offline/test paths could not
exercise the bank at all.

---

### D6: Contradiction keeps both, flags conflict — PROPOSED

**Decision (PROPOSED — owner contradiction question; recommended for the bank side):** When a
new draft resolves to an existing entry but disagrees (contradictory content above a
similarity floor, or an explicit `CONTRADICTS` interpretation), the core appends the
attestation, sets `conflict=True`, and keeps both wordings in attestations — it never merges
contradictory content into one statement and never deletes. Verdict-side policy (capture
artifacts) is unaffected by this decision.

**Alternative considered:** Supersede-with-lineage (newer/better replaces older). Rejected for
the bank: the bank is understanding, not verdict — a contradiction between providers is
itself information (clock skew, stale replica, partial rollout) that reasoning needs to see.
Supersession belongs to conclusions, which already have their own lifecycle.

---

### D7: Bank-first reading with raw drill-down — PROPOSED

**Decision (PROPOSED — owner Q on reading surface; recommended: bank-first):** The reasoning
loop reads served bank views first and drills to `source_refs` raw rows on demand.
`KnowledgeContext` gains a `bank_entries` section assembled under the same envelope gates,
confidence floor, and attribution join as `verified_facts`. Raw rows stay the source of
truth; the bank is derived and rebuildable.

```python
# domain/knowledge/models.py (addition, mirrors ArtifactView pattern)
class BankView(BaseModel):
    entry_id: UUID = Field(description="Bank entry identity.")
    identity_kind: str = Field(max_length=64)
    identity_key: str = Field(max_length=512)
    subject_kind: str = Field(max_length=64)
    content: str = Field(max_length=4096)
    interpretation: str = Field(default="", max_length=2048)
    interpreted: bool = Field(default=False)
    conflict: bool = Field(default=False)
    stale: bool = Field(default=False)
    attestation_count: int = Field(ge=0)
    providers: list[str] = Field(default_factory=list, max_length=20)
    question_ids: list[str] = Field(default_factory=list, max_length=20)
    source_refs: list[str] = Field(
        default_factory=list, max_length=50, description="Raw drill-down pointers."
    )
    confidence: float = Field(ge=0.0, le=1.0)
    attribution: dict[str, dict[str, str]] = Field(default_factory=dict)
```

**Alternative considered:** Full replacement of raw rows as the loop's read surface. Rejected —
conclusions must cite verifiable raw rows (the R3 convention: conclusions re-validate citations
from live rows, never from cached context), and removing the raw path destroys auditability.

---

### D8: Scope phasing — per-investigation bank first

**Decision:** v1 builds a per-investigation working bank (`investigation_id`-scoped records,
crash-safe so conclusions can cite them). Cross-investigation reuse is v2 and rides the
existing capture path (capture consumes bank entries instead of raw rows). No cross-tenant
reads in v1 beyond what visibility rules already allow (D9).

**Alternative considered:** Cross-investigation bank from day one. Rejected — it front-loads
retention, dedupe-across-time, and sharing review before the conversion itself is proven
inside one investigation. The pilot's value (collapse 200 accessed rows into 14 learned
things) is fully realizable per-investigation.

---

### D9: Visibility follows artifact rules

**Decision:** Bank entries inherit the `KnowledgeArtifact` visibility discipline by
construction: tenant-private default; shared only under a code-issue fingerprint with the
same fail-closed membership proof `retrieve_for_reasoning` already enforces. No new sharing
primitive is introduced.

**Alternative considered:** Bank-private-always (never shared). Rejected — it would fork the
visibility model and force a second review for the v2 reuse phase. Following the artifact
rules keeps one policy reviewed in one place.

---

### D10: Staleness from the webhook stream, TTL backstop — PROPOSED

**Decision (PROPOSED — depends on the model-store track's decisions):** Code-linked entries
(`CODE_SITE` subjects and entries whose attestations are all `SOURCE_CODE`/`COMMIT_HISTORY`)
are marked `stale=True` when the GitHub push webhook's changed-file set touches their paths.
TTL expiry via the janitor remains the backstop for everything else. Stale entries are still
served (flagged, ranked down, excluded from `min_confidence` fast paths) — staleness is
handled by flagging plus the reverify flow, never by deletion here, mirroring the premises
convention.

**Alternative considered:** Rebuild-affected entries eagerly on push. Rejected — it couples
bank freshness to updater latency and burns parse spend on entries no open investigation
needs. Lazy flagging plus rebuild-on-access is cheaper and sufficient because evidence is
revision-pinned anyway.

---

### D11: Interpretation reuses `EvidenceRelationshipType`

**Decision:** Entry↔question links use the existing `EvidenceRelationshipType` vocabulary
(`CORROBORATES`, `CONTRADICTS`, `DERIVED_FROM`, `RELATES_TO`). No new relation enum.

**Alternative considered:** A bank-specific relation enum. Rejected — the domain vocabulary
already covers the needed relations, and reuse keeps cross-component queries (divergence
records, association links) joinable without translation tables.

---

### D12: v1 input boundary — PROPOSED

**Decision (PROPOSED — owner Q on "accessed" definition; recommended):** v1 converts banked
evidence rows, code facts, doc chunks, and tool outputs. Intermediate reasoning traces
(model chatter, discarded hypotheses, scratchpad) are explicitly excluded — they are
process, not information accessed, and banking them would fossilize wrong turns into
"knowledge."

**Alternative considered:** Bank everything accessed including reasoning traces. Rejected —
it converts the bank from understanding into surveillance and poisons future recall with
superseded intermediate states.

---

## Data Storage

Bank records persist through a `BankEntryRepository` port following the `ArtifactRepository`
pattern; the infrastructure backend mirrors the artifact store (same database, new
record family — no new infrastructure primitive). Records are investigation-scoped in v1;
cross-investigation indexes land with v2 reuse (columns reserved now, unpopulated until then
— structural placeholders, explicitly not written in v1).

```python
# domain/knowledge/models.py (addition — persisted shape; v1 writes investigation-scoped rows only)
class BankEntryRecord(BaseModel):
    """Persisted bank entry. Structural placeholder for v2 reuse columns (marked below)."""

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    tenant_id: str = Field(min_length=1, max_length=128)
    application_id: str = Field(min_length=1, max_length=128)
    investigation_id: UUID
    identity_kind: str = Field(min_length=1, max_length=64)
    identity_key: str = Field(min_length=1, max_length=512)
    subject_kind: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=4096)
    attestations_json: str = Field(
        max_length=65536, description="Serialized list[BankAttestation]; append-only in practice."
    )
    question_ids_json: str = Field(default="[]", max_length=4096)
    interpretation: str = Field(default="", max_length=2048)
    interpreted: bool = Field(default=False)
    conflict: bool = Field(default=False)
    stale: bool = Field(default=False)
    stale_reason: str = Field(default="", max_length=512)
    confidence: float = Field(ge=0.0, le=1.0)
    # Structural placeholders for v2 cross-investigation reuse. Not populated in v1.
    reuse_count: int = Field(default=0, description="v2: times served to later investigations.")
    superseded_by: str = Field(default="", max_length=128, description="v2: promotion lineage.")
    created_at: datetime
    updated_at: datetime
```

```python
# ports/knowledge/ports.py (addition)
class BankEntryRepository(Protocol):
    """Investigation-scoped bank persistence. Mirrors ArtifactRepository conventions."""

    async def save(self, tenant_id: str, entry: "BankEntry") -> None: ...
    async def append_attestation(
        self, tenant_id: str, entry_id: UUID, attestation: "BankAttestation", *, conflict: bool
    ) -> None: ...
    async def list_for_investigation(
        self, tenant_id: str, investigation_id: UUID
    ) -> list["BankEntry"]: ...
    async def mark_stale(
        self, tenant_id: str, application_id: str, paths: list[str], *, reason: str
    ) -> int: ...
```

## Data Structures

Input and output schemas are separated: adapters emit `BankEntryDraft` (input, D2); the core
emits `BankEntry` (internal); serving renders `BankView` (output, D7). The interpreter
consumes entry content plus question texts and returns `BankInterpretation` (D5). The worker
boundary uses explicitly separated submit/report shapes:

```python
# application/knowledge/bank/core/models.py (addition)
class BankUpdateSubmit(BaseModel):
    """Worker input: what was accessed since the last update."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str = Field(min_length=1, max_length=128)
    application_id: str = Field(min_length=1, max_length=128)
    investigation_id: UUID
    turn: int = Field(ge=0)
    raw_item_refs: list[str] = Field(
        default_factory=list, max_length=200, description="Pointers to newly banked raw rows."
    )
    question_texts: dict[str, str] = Field(
        default_factory=dict, max_length=20, description="Open-question ID → text snapshot."
    )


class BankUpdateReport(BaseModel):
    """Worker output: what the update changed. Never carries entry contents (IDs only)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    investigation_id: UUID
    turn: int = Field(ge=0)
    entries_created: int = Field(ge=0)
    entries_attested: int = Field(ge=0)
    conflicts_flagged: int = Field(ge=0)
    interpreted: int = Field(ge=0)
    entry_ids: list[UUID] = Field(default_factory=list, max_length=200)
```

## Interfaces

No new user-facing surfaces. The component is internal: ports for code, one worker activity
for async updates, and a `KnowledgeContext` extension for serving. Error behavior: adapter
failures degrade to unbanked (raw rows remain); core failures degrade to empty bank section
(retrieve continues, mirroring the existing "degraded to empty context" convention); worker
failures retry via Temporal like other knowledge activities.

| Interface | Shape | Description |
| --------- | ----- | ----------- |
| `BankAdapter` port | `Protocol`: `adapter_name`, `supports(evidence_type)`, `normalize(raw, turn)` | Per-family normalizer; only provider-specific code |
| `BankInterpreter` port | `Protocol`: `interpret(entry_content, question_texts, attestations)` | Meaning assignment; LLM + deterministic-fallback impls |
| `BankEntryRepository` port | `Protocol`: `save`, `append_attestation`, `list_for_investigation`, `mark_stale` | Investigation-scoped persistence |
| `BankService` | `build_turn(submit) -> BankUpdateReport`; `render(tenant_id, investigation_id) -> list[BankView]`; `mark_stale(application_id, paths, reason) -> int` | Core orchestration; signatures normative, bodies implementer-owned within D-decision invariants |
| Worker activity | `bank_update_activity(submit: BankUpdateSubmit) -> BankUpdateReport` | Async per-turn (or per-batch) update; Temporal retries; idempotent on `(investigation_id, turn)` |
| `KnowledgeContext` | gains `bank_entries: list[BankView] = []` | Served under existing envelope gates + confidence floor + attribution join |
| CLI `--json` / TUI / REST | none added | Explicitly out of scope (Non-Goals) |

## Implementation Detail

Service code structure (contracts normative; method bodies implementer-owned within the
invariants stated in Decisions):

- `BankService.build_turn(submit)`: (1) fetch raw rows by `raw_item_refs`; (2) route each
  row to the first `supports()` adapter, collecting drafts (adapter exceptions → skip item,
  count degraded); (3) resolve drafts via `core/identity.py` derivation table —
  existing `identity_key` hit appends an attestation (D6 conflict check), miss creates an
  entry; (4) optionally interpret new/changed entries through `BankInterpreter`
  (deterministic fallback always available); (5) persist via `BankEntryRepository`;
  (6) return counts + IDs only. Invariants: never raises on adapter content; never edits an
  attestation; never merges contradictory content; confidence only decreases downstream.
- `BankService.render(tenant_id, investigation_id)`: list entries → drop stale-gated ones per
  the same floor rules as artifacts → render `BankView`s → attribution join (reuse the
  existing `_join_attribution` path). Served ordering: interpreted first, then by attestation
  count, conflicts surfaced not buried.
- `BankService.mark_stale(application_id, paths, reason)`: called from the webhook consumer
  path with the push changed-file set; delegates to `mark_stale` on the repository; returns
  the count. Pure flagging, no rebuild (D10).
- `serve.py`: the `KnowledgeContext.bank_entries` assembly used by `retrieve_for_reasoning`,
  placed after `verified_facts` assembly under identical gating.
- Worker wiring: `bank_update_activity` registered alongside `retrieve_knowledge_activity`
  and capture in `application/worker/activities/knowledge.py`, dispatched per-turn-batch
  (not per-row) to bound Temporal churn; `(investigation_id, turn)` idempotency key.
- Janitor extension: TTL expiry transitions for bank records mirror artifact sweeps; stale
  flags are set by `mark_stale`, cleared only by re-attestation from fresh raw rows.
- v2 capture switch (explicitly later phase): `capture.py::_distill` gains a bank-reading
  branch emitting conclusion/artifact inputs from entries; raw-row distillation stays until
  parity is proven, then retires per the same harness-gated pattern used for resolver
  migrations.

## Migrations

Creation order with dependencies; each step is independently shippable and additive:

1. Models + ports (`BankEntryDraft`, `BankEntry`, `BankAttestation`, `BankView`,
   `BankInterpretation`, `BankUpdateSubmit/Report`, `BankEntryRecord`, three ports). No
   behavior; unblocks everything.
2. Pilot adapters (Java facts, runtime logs) + core normalize/resolve behind the service,
   exercised by unit tests only — not wired to the loop. Compatibility: zero production
   reads or writes.
3. Worker activity + per-turn-batch dispatch; loop reads bank-first with drill-down (D7);
   `KnowledgeContext.bank_entries` served under existing gates. Raw rows remain the source
   of truth throughout.
4. Working-bank persistence (`BankEntryRepository` backend mirroring the artifact store);
   conclusions may cite bank entries alongside raw rows.
5. Webhook staleness consumer (`mark_stale` path; depends on the model-store track's
   changed-file contract, shared event, separate consumer).
6. v2: capture input switch + cross-investigation reuse queries (populates the reserved
   `reuse_count`/`superseded_by` columns). Requires its own owner GO; not implied by v1.

## Testing Philosophy

### Adapter fidelity

For each pilot adapter, fixture pairs of raw rows and expected drafts asserting lossless
normalization: every convertible field lands in the draft, unconvertible input yields `[]`
rather than raising, and confidence never exceeds the raw item's. New adapters ship with
their fixture pairs; the core never changes for a new provider.

### Deterministic resolve and dedupe

Same entity across providers collapses to one entry with N attestations; distinct entities
never merge (near-miss fixtures: same file different lines, same error different services).
Resolution is byte-deterministic: same inputs, same `identity_key`s, no ordering dependence.

### Conflict behavior

Contradictory drafts set `conflict=True`, preserve both wordings in attestations, and never
merge content. Tests cover contradiction arrival order-independence (A-then-B equals
B-then-A) and conflict surfacing in served views.

### Staleness marking

Changed-file sets mark exactly the code-linked entries (path-prefix matching incl. renames
as remove+add); non-code entries untouched; `mark_stale` returns the count and is
idempotent on redelivery.

### Retrieve integration

Bank sections obey the same envelope gates, confidence floor, and attribution join as
`verified_facts`; stale entries rank down and exclude from fast paths; empty/degraded bank
never fails reasoning. Cached contexts include the bank section in the cache key.

### Interpreter fallback

Deterministic-only mode keeps the full suite green with zero LLM calls; LLM interpreter
tests assert contract bounds (no invented question IDs, confidence ceiling, note length)
rather than wording.

### Performance bounds

Pilot throughput test: normalize + resolve over a synthetic 10k-row mixed-provider window
completes within the turn budget on CI hardware; per-turn-batch dispatch (not per-row)
keeps Temporal activity counts flat as evidence grows.

## Documentation Plan

### `docs/IAP-knowledge-bank-component-spec-v1.0.md` (this file)

**Audience:** Implementers and reviewers.

Normative contracts (models, ports, activity shapes), decision records with alternatives,
phased migration order, and the PROPOSED-marked owner questions. Implementers work from
this file alone for contracts; bodies follow the stated invariants.

### `docs/IAP-architecture-v1.0.md` changes

**Audience:** Architects and future maintainers.

No `docs/dev/architecture.md` exists; the project architecture document is
`docs/IAP-architecture-v1.0.md`. On implementation, add a knowledge-bank subsection to the
Part 6 knowledge area describing the conversion pipeline (adapters → core → serve), the
`KnowledgeContext.bank_entries` extension, and the webhook changed-file consumer shared with
the model-store track; update the knowledge data-flow diagram with the bank between raw
banked rows and capture. Exact anchor section to be confirmed against the document's current
numbering at implementation time.

### Adapter author runbook (new file under `docs/`, created at implementation)

**Audience:** Engineers adding provider adapters.

How to implement `BankAdapter` for a new family: seam selection for identity derivation,
fixture-pair requirements, confidence guidance, and the registration checklist. One page;
written when the third adapter lands so it documents reality, not intention.

### `docs/IAP_Arch_Eng_Recommendations.log`

**Audience:** Session continuity.

Ledger entries for spec acceptance, each phase landing, and the v2 GO — one line each per
the log's format rule.

## Risks / Trade-offs

### Over-dedupe merges distinct events

**Risk:** Aggressive resolution collapses genuinely distinct occurrences (same template,
different incident) into one entry, hiding multiplicity from reasoning.

**Mitigation:** Identity keys include discriminating dimensions (service, window, turn
ranges); attestations preserve per-sighting metadata so multiplicity is countable even when
grouped; near-miss fixtures guard the boundary permanently.

### LLM interpretation cost and latency per turn

**Risk:** Interpreting every new entry through an LLM adds spend and turn latency to all
investigations, including ones that never read the bank.

**Mitigation:** Interpretation runs in the async worker path, not the reasoning turn;
deterministic fallback is always available and entries are valid uninterpreted; batching per
turn-bounds calls; deterministic-only mode stays green so the LLM can be degraded silently.

### Stale entries mislead reasoning

**Risk:** A bank entry learned from code that has since changed reads as current
understanding and poisons conclusions.

**Mitigation:** Webhook-driven `stale` flags plus TTL backstop; stale entries rank down and
are visually flagged in views; conclusions cite live raw rows, never bank content alone;
re-attestation from fresh rows clears staleness.

### The bank diverges into a second source of truth

**Risk:** Once reasoning reads the bank, fixes and features target bank content instead of
raw rows, and the two silently diverge.

**Mitigation:** Structural, not cultural: the bank is append/flag-only (attestations never
edited, entries never deleted except by janitor TTL), fully rebuildable from raw rows at any
time, and rebuild-equivalence is a permanent test (rebuild from raw equals live bank modulo
interpretation wording).

### Scope creep into replacing the evidence store

**Risk:** Each new consumer asks the bank for one more raw-store capability until the
layering collapses.

**Mitigation:** The Non-Goals are load-bearing: no raw replacement, no new surfaces, no
trace banking. Adapter additions are cheap by design so provider pressure vents into
adapters, not core changes.

## Accessibility

No user-facing surface exists in this component: no REST endpoints, no CLI commands, no TUI
screens, no GUI components. All interfaces are internal code contracts (ports, service
signatures, worker activities) and one data extension to an existing served context. The
accessibility checklist is therefore not applicable; if a future phase adds a human-readable
bank surface (e.g. an investigation "what was learned" view), that phase must include the
full accessibility treatment (keyboard access, contrast, screen-reader announcements,
reduced-motion) as a condition of its own GO.
