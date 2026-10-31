# Design: Java/Python static analysis, Spring configuration and range banking (review group J)

- Source review: `docs/REVIEW-Sonnet-v0.1-part3.md` section 8 (findings J5, J6, J8, J9, J10, J11, J12, J13), plus four earlier ledger entries and the section 8.4 untested list.
- Baseline: git HEAD `7860fb4`, current shipped loop version `lv7`. Every claim below was re-verified against that tree on 2026-10-31 (read-only probes on synthetic sources; nothing was written, committed, restarted or run live).
- Loop-version placeholder: `LV_J` (the next free number after `lv7`; never hardcoded here, assigned when the owner approves the J10 tranche).

## Context

Group J is the Java/Python static-analysis layer (`application/analysis/java/`, `application/analysis/python/`), the Spring configuration importers (`integrations/profile_gen/`, `integrations/spring_config/`), and the range-banking edges around them (`infrastructure/evidence/code/git.py`, `application/investigation/context_compiler.py`, `domain/collection/frontier.py`, `application/worker/workflows/forcing.py`).

J1-J4 and J7 are already implemented (the ledger records them as `IMPLEMENTED`; the code confirms the J1 scope walk, the J2 anonymous scope and conflicting-field guard, the J3 `control_flow_not_modeled` abstention, the J4 point-based declaration match and the J7 explicit `read_mode == "range"` guard). What remains has one shape: the analyzers are sound on what they model, but they stay silent about what they skip, and several identity and boundary assumptions are weaker than they look.

Three properties of the current code shape the whole design and are easy to miss:

1. **Analyzer output is data, not loop semantics.** Window enumeration runs at verify time over banked rows and is never persisted (`_with_window_backfill` copies rows in memory). Selector resolvers write `CodeFactResolutionV2` payloads into evidence attributes. Neither path changes what the loop dispatches. Soundness fixes here (J1-J4 precedent) carry no loop version; the stamps that change are the parser identity, the `analysis_method`/`column_unit` markers and the limitation-code vocabulary.
2. **The collect frontier is advisory today.** `rank_advisory_orders` and `advisory_orders` are written onto `PostReasonCascadeState` and nothing in `run_investigation.py` reads them back (verified with a repository-wide search; only `forcing.py` and tests touch them). So the review's J10 consequence ("the frontier does not escalate when dataflow bottoms out") is true but incomplete: even a perfect `EvidenceState` would not change dispatch until a consumer exists. The design still fixes the derivation (it is the prerequisite), pins it to `LV_J`, and asks the owner whether the ranking should ever drive dispatch.
3. **Several review premises no longer hold or were stated too strongly.** See the verdict table below.

### Item verdicts at HEAD

| Item | Verdict | One-line evidence |
|---|---|---|
| J5 | **Open** (partly mis-described) | Probe: a 1,200-character string constant makes the whole window enumerate to `[]` with no signal (pydantic `ValidationError` swallowed); enum constants, interface constants (`constant_declaration` nodes), switch returns are skipped silently; an enum method is attributed to the outer class; else-if records carry no negated outer guard. The review's "`resolved[0]` picks `pos`" is **not reproducible at the verifier gate** (two guard records conflict and fail closed), but it makes every multi-guard method unverifiable. |
| J6 | **Open** | `parser_version()` returns the constant `tree-sitter-java`; pins are `>=`; the pack fetches grammars on first use (documented download model, verified through Context7); parser and `Query` are rebuilt per call. |
| J8 | **Open, severity raised for one sub-defect** | `splitlines()` shifts line numbers (probe: 7 vs 5); the 25-record cap silently starves a trailing field; a method before the first in-window class header gets a **bare** subject and a claim naming `WrongClass.helper` is VERIFIED against it (probe); columns are bytes (probe: 18 vs 17 characters). |
| J9 | **Open, one new finding** | `external_classifier_for_roots([])` returns "external" while the comment promises the opposite; the profile generator emits `source_roots: ["."]` and both the classifier and the validator treat `"."` as matching nothing. |
| J10 | **Open** (lower real impact than rated) | `EvidenceState` comes from a banked-row count only; no producer of `STALE_MATCH` or `LOCAL_COMPLETE`; no consumer of the ranking. |
| J11 | **GATED**; premise **not reproducible as stated** | The importer reads only exact basenames (`application.yml`, `bootstrap.yml`, ...), so `application-dev.yml` is never read; the real defect is flat first-wins across modules, test resources and bootstrap-versus-application, and only `spring.application.name` reaches the draft profile, so "blocker" is not supported. |
| J12 | **GATED** | All six sub-claims reproduce; `merge_configs` repo-defaults inversion is latent (no caller passes defaults). |
| J13 | **GATED** | All five sub-claims reproduce. |
| Parser adapter drops recoverable symbols | **Open** | `parse_symbols` returns `[]` for one broken method among three (probe). |
| Node identity audit | **Not reproducible as a live defect**; two latent `is` comparisons | No `id()` on nodes remains; `==` is value-based (verified); `is` on fresh wrappers at two sites is always true. |
| Claim text/value mismatch | **Partly addressed** | The "omitted `claimed_value`" half is fixed; a method-return statement asserting a contradictory literal still VERIFIES (probe). |
| `LANGUAGE_SEMANTIC_FACT` ceiling | **Open (decision record)** | Ceiling is `VERIFIED`; recommendation is keep and document. |

## References

- `docs/REVIEW-Sonnet-v0.1.md` (part 1, remediation table) and `docs/REVIEW-Sonnet-v0.1-part3.md` section 8 (findings, 8.3 illustration, 8.4 untested list).
- `docs/IAP_Arch_Eng_Recommendations.log` entries dated 2026-10-20 (Node identity, ceiling, claim/value), 2026-10-30 (parser adapter, J5-J13). J1-J4 and J7 are `IMPLEMENTED`.
- `docs/IAP-architecture-v1.0.md` sections 66-71 (house style for loop-version sections), `docs/IAP-Code-Java-lang-impl-design.md`, `docs/design/README.md` and `docs/design/DES-S7-handoff-v1.0.md` (conventions: no legacy branches, version-pinned semantics only).
- `src/investigation_agent_platform/domain/investigation/loop_version.py` (`loop_version_at_least`, `LOOP_VERSION_CURRENT = lv7`).
- External API verification (Context7, 2026-10-31): py-tree-sitter (`Language.abi_version`, `Parser(language)`, `Query(language, source)`, `QueryCursor(query).captures(node)`); tree-sitter-language-pack (`get_parser`, `get_language`, `download`, `prefetch`, `init(PackConfig)`, `configure`, `cache_dir`, `downloaded_languages`; documented download model: first call fetches the parser binary from release assets into `<cache>/tree-sitter-language-pack/v<version>/libs/`; pre-download at build time is the documented offline recipe); javaproperties (`loads`, Java `.properties` semantics).
- Installed versions at HEAD (read from the environment): `tree-sitter 0.26.0` (`LANGUAGE_VERSION 15`), `tree-sitter-language-pack 1.20.0`, Java grammar ABI 14.

## Goals / Non-Goals

**Goals**

- Every skipped or failed analysis produces a typed, class-tagged limitation code instead of silence (J5), and the verifier and frontier can read it (J10).
- The parser identity stamped on facts names the pack version, core version and grammar ABI, and a cold or air-gapped worker fails loudly and once instead of per row (J6; dependency pinning is a proposal needing owner approval).
- Cited line numbers equal git and IDE line numbers; window enumeration cannot starve a later subject; no fact is attributed to a class the analyzer cannot name (J8).
- Cross-boundary classification distinguishes "external", "in tenant" and "unknown", is repo-qualified, and never invents a handoff from a transient store error (J9).
- `EvidenceState` distinguishes "found nothing" from "gave up" (J10).
- Spring configuration items (J11-J13) are designed in full but stay GATED.

**Non-Goals**

- No change to the dispatch behavior of any loop rule other than the pinned J10 state derivation. Wiring the advisory ranking into dispatch is explicitly out of scope and left as an owner decision.
- No change to the request validator's security semantics (J9 proposes aligning it; that proposal requires owner approval).
- No new analyzer capability beyond what is needed to name a limitation (switch returns, enum-constant values and lambda returns stay unmodeled and are reported as such).
- No live runs, tenant cleans, restarts, migrations or dependency changes in this document's tranche. Where a design proposes one, the item says "requires owner approval".
- Nothing here references any analyzed repository, organization, host or class; fixtures use synthetic names (`OrderServiceHandler`, `SampleOrderFilter`, "Spring service", "Oracle sample DB").

Standing rules restated: one tranche per owner message; tests first and shown to fail before the fix; no commits without the owner; no scratch-directory or operator-directory references in any artifact; no legacy branches (IAP is not live) and no env-switched behavior (only recorded version pins).

## Decisions

Decisions are numbered globally. D1-D4 are cross-cutting and used by several items; the rest live inside the item that owns them.

### D1: One typed limitation registry with a class per code

Every limitation code an analyzer can emit is registered once with a coarse class. The class is what the frontier derivation (J10) and the verifier explanation (J5) read; it replaces substring sniffing such as `"runtime" in codes_text.lower()` in `_java_partial_limitation`.

```python
# src/investigation_agent_platform/application/analysis/limitations.py
"""Typed vocabulary for analyzer limitation codes (single home)."""

from __future__ import annotations

from enum import StrEnum


class LimitationClass(StrEnum):
    WINDOW = "window"            # a wider or different source read can fix it
    RUNTIME = "runtime"          # needs runtime evidence; no source read helps
    UNSUPPORTED = "unsupported"  # construct not modeled by this analyzer version
    FAILURE = "failure"          # analyzer or infrastructure fault
    PROVENANCE = "provenance"    # the row was not an admissible source read


_W, _R, _U, _F, _P = (
    LimitationClass.WINDOW,
    LimitationClass.RUNTIME,
    LimitationClass.UNSUPPORTED,
    LimitationClass.FAILURE,
    LimitationClass.PROVENANCE,
)

_REGISTRY: dict[str, LimitationClass] = {
    # window-bound
    "out_of_window_reference": _W,
    "java_syntax_error_or_incomplete_source_window": _W,
    "source_range_truncated": _P,
    "symbol_not_in_window": _W,
    "branch_target_outside_window": _W,
    "lambda_capture_outside_window": _W,
    "method_reference_target_outside_window": _W,
    "incomplete_candidate_set": _W,
    "no_binding_declaration_in_window": _W,
    "interprocedural_aliasing": _W,
    "declaring_type_not_in_window": _W,
    "declaring_type_mismatch_in_window": _W,
    "window_records_truncated": _W,
    "method_not_found": _W,
    "field_not_found": _W,
    # runtime-bound
    "concurrent_visibility": _R,
    "nondeterministic_scheduling": _R,
    "requires_runtime_observation": _R,
    "applied_behavior_requires_runtime_evidence": _R,
    "async_boundary": _R,
    "lombok_generated_code": _R,
    "spel_expression": _R,
    # unsupported construct
    "control_flow_not_modeled": _U,
    "unsupported_language_version": _U,
    "unsupported_initializer_expression": _U,
    "unsupported_default_type": _U,
    "type_erasure_changes_answer": _U,
    "stream_laziness": _U,
    "enum_constant_not_modeled": _U,
    "switch_return_not_modeled": _U,
    "ambiguous_guard_branch": _U,
    "literal_too_long": _U,
    "receiver_mutation_possible": _U,
    "alias_escape": _U,
    "lambda_capture_not_modeled": _U,
    "ambiguous_constructor": _U,
    # (abridged: the implementation registers every code the completeness scan finds,
    #  including the existing missing_*_name, *_not_found, ambiguous_*, invalid_java_literal,
    #  use_before_definition and *_hint_not_found codes)
    # failure
    "analysis_failed": _F,
    "grammar_unavailable": _F,
    # provenance
    "not_a_source_read": _P,
    "source_read_not_range_pinned": _P,
    "source_read_is_discovery_only": _P,
    "missing_immutable_source_provenance": _P,
    "missing_source_line_window": _P,
    "source_range_missing_numbered_lines": _P,
    "source_lines_outside_provenance_window": _P,
    "source_lines_noncontiguous": _P,
    "not_source_code_evidence": _P,
    "not_a_java_source_file": _P,
    "unpinned_revision": _P,
    "repository_identity_unavailable": _P,
}


def limitation_class(code: str) -> LimitationClass:
    """Class of a code; an unregistered code reads as UNSUPPORTED, never WINDOW or RUNTIME.

    A trailing ``:detail`` suffix is ignored. The registry-completeness test
    fails the build when an analyzer emits a code that is not registered, so
    the fallback is a safety net, not a feature.
    """
    return _REGISTRY.get(str(code).split(":", 1)[0], LimitationClass.UNSUPPORTED)
```

**Alternative considered:** an enum of every code with the class as a member attribute. Rejected: the existing resolvers emit plain strings through ten `unresolved(...)` helpers and the persisted payloads carry plain strings; an enum would force a payload migration for no gain. A registry keyed by the persisted string changes no stored shape.

**Rationale and trade-offs.** One table is greppable and testable. The cost is that a new code needs a registry line; the completeness test (JGF section) makes forgetting it a red build rather than a silent `UNSUPPORTED`.

---

### D2: `WindowAnalysis`, the typed result of window enumeration

`enumerate_java_window_facts` returned `list[CodeFactResolutionV2]` and collapsed refusal, failure and "nothing to enumerate" into the same empty list. It now returns a value that says which it was.

```python
# src/investigation_agent_platform/application/analysis/java/window_result.py
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from investigation_agent_platform.domain.code.models import CodeFactResolutionV2


class WindowStatus(StrEnum):
    OK = "ok"              # analyzed; records (possibly none) are complete for the window
    DEGRADED = "degraded"  # analyzed; records are sound but known to be incomplete
    REFUSED = "refused"    # the row is not an admissible range read (a provenance gate)
    FAILED = "failed"      # the analyzer or grammar raised; nothing can be said


@dataclass(frozen=True)
class WindowAnalysis:
    status: WindowStatus
    records: tuple[CodeFactResolutionV2, ...] = ()
    limitations: tuple[str, ...] = ()
    failure_type: str | None = None   # exception class name only, never a message
    omitted: int = 0                  # records dropped by the budget

    def to_attribute(self) -> dict[str, Any]:
        """JSON form spliced onto the in-memory verify-time copy of the row."""
        return {
            "status": self.status.value,
            "limitations": list(self.limitations),
            "failure_type": self.failure_type,
            "omitted": self.omitted,
        }
```

```python
# src/investigation_agent_platform/domain/code/models.py  (additive constant)
WINDOW_ANALYSIS_KEY = "window_analysis"
```

**Alternative considered:** raise a typed exception and let the backfill catch it. Rejected: the backfill's job is to keep verification pure and total; a typed value composes with the existing "splice into the envelope" flow and needs no new `except` clauses.

**Rationale.** The attribute lives only on the in-memory verify-time copy (`_with_window_backfill` never mutates banked rows), so no stored shape changes and no migration exists.

---

### D3: One line-splitting rule shared by providers and analyzers

Python's `str.splitlines()` also splits on form feed, `\x1c-\x1e`, `\x85`, U+2028, U+2029 and a lone `\r`. Git, IDEs and tree-sitter count rows by `\n` only. The splitter lives in the domain so both the infrastructure provider and the analyzers import the same rule.

```python
# src/investigation_agent_platform/domain/code/lines.py
"""Source line splitting that matches git, IDEs and tree-sitter row counting."""

from __future__ import annotations


def split_source_lines(text: str) -> list[str]:
    """Split on ``\\n`` only; fold a trailing ``\\r`` (CRLF); drop the final empty segment.

    ``""`` has no lines; ``"a"`` and ``"a\\n"`` both have one. Form feed,
    U+2028/2029, ``\\x85`` and a lone ``\\r`` inside a line stay inside it.
    """
    if not text:
        return []
    parts = text.split("\n")
    if parts[-1] == "":
        parts.pop()
    return [part[:-1] if part.endswith("\r") else part for part in parts]
```

**Alternative considered:** `str.splitlines(keepends=False)` with a pre-pass that replaces the exotic separators. Rejected: it hides the intent and still disagrees with git for a lone `\r`.

---

### D4: Version policy for the group

| Item | Loop version | What changes instead |
|---|---|---|
| J5 | none | New `window_analysis` verify-time attribute; new limitation codes (D1); `JavaModel.schema_version` `iap/java-model/v1` to `v2` (the model is in-memory only). |
| J6 | none | `parser_version` string now `tree-sitter-<lang>;pack=<v>;ts=<v>;abi=<n>`; nothing branches on it (verified: no verifier code reads `parser_version`). Dependency pins: **requires owner approval**. |
| J8 | none | `Span` columns renamed (`byte_col_*`), dataflow facts add `column_unit: "code_point"`; subjects no longer bare. |
| J9 | none (prompt-shape only) | `REASONER_PROMPT_VERSION` suffix bump for the new `boundary` keys; no forcing rule reads dormant templates (verified). |
| J10 | **`LV_J`** for the forcing-side state derivation; data-only for the history stamp and the pure derivation | `ActionOutcome.analysis` and the history `analysis` key are additive and read only at `LV_J`+. |
| J11-J13 | none (profile-generation and analyzer data) | `analysis_method` unchanged; new candidate-list shape is a generation-time artifact (GATED). |
| Related | none | Fail-closed verifier tightening follows the J2 precedent (no pin). |

Recorded versions, never environment switches, route anything pinned. Unmarked rows resolve to current semantics.

---

## Items

### Item J5: Newer Java constructs are skipped with no limitation signal

**Status:** Open. Severity should-fix (verifier-visible effect: wrong or missing explanation, one misattribution that can verify a claim about the wrong class). Dependencies: D1, D2 (and D14 for owner resolution). Gating: none; no loop version (analyzer data, D4).

#### Verified problem

All probes ran offline on synthetic sources through `build_java_model` and `enumerate_java_window_facts`.

| Claim in the review | Verdict at HEAD | Evidence |
|---|---|---|
| Enum constants skipped silently | Confirmed | `enum Mode { FAST, SLOW; ... }` produces no record and no code. `enum_constant` nodes are never visited by `model.py` `visit`. |
| Interface constants skipped silently | Confirmed, cause refined | tree-sitter-java parses interface fields as `constant_declaration`, not `field_declaration` (node dump), so `visit` never reaches them; `window_facts` additionally filters `declared_in != "class_declaration"`. |
| Switch-expression returns skipped | Confirmed | `String sw(int v) { return switch (v) { ... }; }` yields nothing; old-style `switch_statement` returns are the same. |
| Nested-type methods mis-attributed (`nearest_class` tracks only `class_declaration`) | Confirmed, and worse for enums | Probe: `enum Mode` inside `OrderServiceHandler` with method `label()` is emitted as `com.sample.orders.OrderServiceHandler.label` (the enum name is lost). A nested class method is emitted as `com.sample.orders.Inner.get` (the outer class is lost and the package is prepended to a bare name), while a field of the same nested class is `...OrderServiceHandler.Inner.n`. The subject forms disagree. |
| else-if guards lack the negated outer guard | Confirmed | `if (v > 0) ... else if (v < 0) ...` yields guard texts `(v > 0)` and `(v < 0)`; the second record does not say "and not (v > 0)". The same hole exists for a nested `if` (the outer positive guard is missing). |
| `resolved[0]` picks `pos` | **Not reproducible at the verifier gate** | `java_fact_proof` collects all RESOLVED candidates for the subject and returns `conflicting matching analyzer resolutions` when their content differs (verified by direct call). A claim for either literal ends `INCONCLUSIVE`. The practical effect is the opposite of the review's: every method with two or more guarded literals is unverifiable through window enumeration. |
| `window_facts.py` swallows exceptions as `[]` | Confirmed, with a decisive probe | A 1,200-character string constant makes `build_java_model` raise a pydantic `ValidationError` (`FieldDef.raw_initializer` is capped at 1,024 and `ReturnSite.value_text` likewise) and `enumerate_java_window_facts` returns `[]` for the whole window, including the unrelated clean field and method. No signal reaches the verifier. |
| `python/dispatch.py` swallows exceptions as `continue` | Confirmed | The resolver chains `except Exception: continue`; if every attempt raises, the function returns `(None, None)` and the activity records nothing. |
| An `analysis_failed` stamp exists | **Exists but is malformed** | `execution.py` (`_run_java_fact`) writes `{"status": "UNRESOLVED", "limitation_codes": ["analysis_failed"]}`. That payload is not a valid `CodeFactResolutionV2` (`fact_category` is required; probe raised `ValidationError`), so `_v2_candidates_from_evidence` either skips it (subject filter) or flags the row `malformed`; and because the `code_fact_java.*` key prefix alone adds the `ANALYZER_RESOLUTION` family, a failed analyzer currently makes the evidence look *more* analyzed. |
| Failure mislabeled | Confirmed | Every resolver maps any exception to `java_parser_unavailable:<ExceptionName>`, so a model-validation bug reads as a missing grammar. |

#### Design

**D5: Model fixes: attribute every nested type, carry path conditions, model interface constants, record skipped constructs.**

```python
# src/investigation_agent_platform/application/analysis/java/model.py  (additions)
class PathCondition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(default="", max_length=4096)
    holds: bool  # True: guard true on this path (consequence); False: false (else branch)


class SkippedConstruct(BaseModel):
    """A construct the analyzer saw and deliberately did not model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=1, max_length=128)  # registered in limitations.py
    owner: str = Field(default="", max_length=1024)  # qualified enclosing type
    name: str = Field(default="", max_length=512)    # member or constant name
    span: Span


class GuardBranch(BaseModel):
    ...  # existing fields unchanged
    path_conditions: list[PathCondition] = Field(default_factory=list)


class JavaModel(BaseModel):
    schema_version: Literal["iap/java-model/v2"] = SCHEMA_VERSION  # was v1
    ...
    skipped: list[SkippedConstruct] = Field(default_factory=list)
```

```python
def _contains(outer: Any, inner: Any) -> bool:
    return outer.start_byte <= inner.start_byte and inner.end_byte <= outer.end_byte


def _path_conditions(node: Any, source: bytes) -> list[PathCondition]:
    """Enclosing if-conditions of ``node`` inside its method, outermost first.

    Climbs to the nearest scope boundary (method, lambda, type body), recording
    for each enclosing ``if`` whether we arrived through its consequence (the
    guard holds) or its alternative (the guard is false: the else-if case).
    """
    conditions: list[PathCondition] = []
    child, parent = node, node.parent
    while parent is not None and parent.type not in _SCOPE_BOUNDARY_TYPES:
        if parent.type == "if_statement":
            cond = parent.child_by_field_name("condition")
            text = _normalize_guard(_p.node_text(cond, source)) if cond is not None else ""
            consequence = parent.child_by_field_name("consequence")
            alternative = parent.child_by_field_name("alternative")
            if consequence is not None and _contains(consequence, child):
                conditions.append(PathCondition(text=text, holds=True))
            elif alternative is not None and _contains(alternative, child):
                conditions.append(PathCondition(text=text, holds=False))
        child, parent = parent, parent.parent
    conditions.reverse()
    return conditions
```

In `visit`: (a) `class_only.append(name)` for every named type kind (not just `class_declaration`), so `nearest_class` of an enum method is the enum; (b) the field branch accepts `{"field_declaration", "constant_declaration"}` and treats a `constant_declaration` as static (interface fields are implicitly `public static final`); (c) when a type is an `enum_declaration`, every `enum_constant` child appends `SkippedConstruct(code="enum_constant_not_modeled", ...)`; (d) after the guard scan of a method, `any(sub.type in {"switch_expression", "switch_statement"} for sub in _scope_walk(node))` appends `SkippedConstruct(code="switch_return_not_modeled", owner=<method owner>, name=<method name>)`; (e) each `GuardBranch` is built with `path_conditions=_path_conditions(sub, source)`.

```python
# model.py: the enum branch inside visit(), after class_stack.append(qualified)
if node_type == "enum_declaration":
    for body in (c for c in node.children if c.type == "enum_body"):
        for constant in (c for c in body.children if c.type == "enum_constant"):
            ident = constant.child_by_field_name("name") or next(
                (c for c in constant.children if c.type == "identifier"), None
            )
            if ident is not None:
                skipped.append(
                    SkippedConstruct(
                        code="enum_constant_not_modeled",
                        owner=qualified,
                        name=_p.node_text(ident, source).strip(),
                        span=_span(constant, file_path),
                    )
                )
```

The model text fields are aligned to the evidence snippet cap so a long literal is a value, not a crash: `FieldDef.raw_initializer` and `ReturnSite.value_text` rise from 1,024 to 4,096 (the evidence `content_snippet` is capped at 4,096 characters, so no declaration can exceed it). The audit step of the implementation walks every `max_length` in `model.py` against that cap and the long-literal test pins four positions (field, local, return, argument).

**Alternative considered:** keep `nearest_class` as "nearest `class_declaration`" and add a second field for enums. Rejected: `method_return_facts` and `window_facts` both match declaring types by trailing segments of `nearest_class`; a stale "outer class" for an enum method is exactly the misattribution (a claim `OrderServiceHandler.label` verifies against `Mode.label`). One consistent rule is smaller than two.

**D6: The enumerator returns `WindowAnalysis` and contains failure per declaration.**

```python
# src/investigation_agent_platform/application/analysis/java/window_facts.py
MAX_WINDOW_RECORDS = 100  # see D13; the evidence cap bounds any window to a few hundred declarations


@dataclass(frozen=True)
class _Candidate:
    kind: str           # "guard" | "field" | "marker"
    subject: str
    record: CodeFactResolutionV2


def _marker(subject: str, code: str, **common: Any) -> _Candidate:
    return _Candidate(
        "marker",
        subject,
        CodeFactResolutionV2(
            status=CodeFactStatus.UNRESOLVED,
            fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
            subject_identity=subject or None,
            value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
            limitation_codes=(code,),
            parser_version=_p.parser_version(),
            **common,
        ),
    )


def _contained(build: Callable[[], _Candidate], subject: str, **common: Any) -> _Candidate:
    """Build one candidate; a bad declaration becomes a typed marker, never a lost window."""
    try:
        return build()
    except ValidationError:
        return _marker(subject, "literal_too_long", **common)


@dataclass(frozen=True)
class _Context:
    model: JavaModel
    tree: Any
    location: Any
    first_line: int
    evidence: Any


def enumerate_java_window_facts(
    evidence: Any, *, language_version: object = None, subject_hint: str | None = None
) -> WindowAnalysis:
    """Enumerate RESOLVED source-value facts and typed skip markers for one banked read (pure)."""
    lines, reason = java_source_lines(evidence)
    if reason:
        return WindowAnalysis(WindowStatus.REFUSED, limitations=(reason,))
    location = getattr(getattr(evidence, "provenance", None), "source_location", None)
    source_path = str(getattr(location, "file_path", "") or "")
    revision = str(getattr(location, "revision", "") or "")
    if not source_path.lower().endswith(".java"):
        return WindowAnalysis(WindowStatus.REFUSED, limitations=("not_a_java_source_file",))
    if not revision or revision in {"HEAD", "UNPINNED"}:
        return WindowAnalysis(WindowStatus.REFUSED, limitations=("unpinned_revision",))
    source_bytes = "\n".join(line.text for line in lines).encode("utf-8")
    try:
        tree = _p.parse_java(source_bytes)
        code_model = _model.build_java_model(
            source_bytes,
            file_path=source_path,
            repository=repository_of(str(getattr(location, "identifier", "") or ""), source_path)
            or "unknown",
            revision=revision,
            language_version=language_version,
        )
    except Exception as exc:  # contained and typed; never a silent []
        return WindowAnalysis(
            WindowStatus.FAILED,
            limitations=(failure_code(exc),),
            failure_type=type(exc).__name__,
        )
    ctx = _Context(code_model, tree, location, lines[0].number, evidence)
    candidates = [*_guard_candidates(ctx), *_field_candidates(ctx), *_marker_candidates(ctx)]
    chosen, omitted = _allocate(candidates, subject_hint, MAX_WINDOW_RECORDS)
    limitations: list[str] = []
    if code_model.has_syntax_error:
        # Head reads run into the provider byte cap and end mid-declaration; records come
        # only from declarations whose own subtree is clean (J4), so they stay sound.
        limitations.append("java_syntax_error_or_incomplete_source_window")
    if omitted:
        limitations.append("window_records_truncated")
    return WindowAnalysis(
        WindowStatus.DEGRADED if limitations else WindowStatus.OK,
        records=tuple(c.record for c in chosen),
        limitations=tuple(limitations),
        omitted=omitted,
    )
```

The guard candidate gains the facts the review asked for; `structured_fact` carries them without using the `symbol`/`method`/`target` keys that `java_fact_proof` treats as subject identifiers:

```python
structured_fact={
    "guard_text": branch.guard_text,
    "path_conditions": [pc.model_dump() for pc in branch.path_conditions],
},
```

`_marker_candidates` converts each `code_model.skipped` entry into `_marker(subject=<owner>.<name>, code=skipped.code, declaration_location=...)`. A marker's subject is `<qualified owner>.<constant or method>`, so a claim naming `Mode.FAST` matches its marker through the existing dot-boundary rule.

**D7: One failure helper replaces every silent `except` and the malformed stamp.**

```python
# src/investigation_agent_platform/application/analysis/failure.py
from __future__ import annotations

from typing import Any

from investigation_agent_platform.domain.code.models import (
    CodeFactResolutionV2,
    CodeFactStatus,
    CodeValueSemantics,
    JavaFactCategory,
)


class GrammarUnavailable(Exception):
    """The grammar library could not be loaded (cold cache, no network, bad binary)."""


def failure_code(exc: BaseException) -> str:
    return "grammar_unavailable" if isinstance(exc, GrammarUnavailable) else "analysis_failed"


def analysis_failed_resolution(
    evidence: Any,
    *,
    category: JavaFactCategory,
    subject: str | None,
    exc: BaseException,
    parser_version: str,
) -> CodeFactResolutionV2:
    """A valid UNRESOLVED payload naming the failure; the message is never recorded."""
    location = getattr(getattr(evidence, "provenance", None), "source_location", None)
    evidence_id = getattr(evidence, "evidence_id", None)
    return CodeFactResolutionV2(
        status=CodeFactStatus.UNRESOLVED,
        revision=str(getattr(location, "revision", "") or "") or None,
        fact_category=category,
        subject_identity=subject or None,
        value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
        source_evidence_ids=(evidence_id,) if evidence_id is not None else (),
        limitation_codes=(failure_code(exc),),
        assumptions=(f"exception_type={type(exc).__name__}",),
        parser_version=parser_version,
    )
```

Call sites: the ten resolvers replace `f"java_parser_unavailable:{type(exc).__name__}"` with `failure_code(exc)` (the exception name moves to `assumptions`); `_run_java_fact` in `execution.py` builds the failure payload with `analysis_failed_resolution(..., category=_SELECTOR_CATEGORY[selector_name])` where the table is `call_target_name` to `CALL_TARGET_FACT`, `local_name` to `SOURCE_VALUE_FACT`, `init_name` and `type_symbol` to `LANGUAGE_SEMANTIC_FACT`, `dataflow_symbol` to `DATA_FLOW_FACT`, `control_symbol` to `CONTROL_FLOW_FACT`, `effect_symbol` to `POSSIBLE_EFFECT_FACT`, `boundary_symbol` to `CONFIGURATION_FACT`; the Java method-return and Python field/method `except Exception: pass` blocks stamp the same helper; and `python/dispatch.py` collects exceptions instead of `continue`-ing:

```python
# src/investigation_agent_platform/application/analysis/python/dispatch.py (field chain)
failures: list[BaseException] = []
for attempt in attempts:
    try:
        candidate = attempt()
    except Exception as exc:
        failures.append(exc)
        continue
    resolution = _prefer(resolution, candidate)
    if _is_resolved(resolution):
        break
if resolution is None and failures:
    resolution = analysis_failed_resolution(
        evidence,
        category=JavaFactCategory.SOURCE_VALUE_FACT,
        subject=field_name,
        exc=failures[0],
        parser_version=_p.parser_version(),
    )
```

**D8: The verifier reads `window_analysis`, so "nothing enumerated" is never silent.**

```python
# src/investigation_agent_platform/application/investigation/verification.py
@staticmethod
def _window_analysis_of(evidence_items: list[Any]) -> dict[str, Any] | None:
    for item in evidence_items or []:
        attrs = getattr(item, "attributes", None)
        raw = attrs.get(WINDOW_ANALYSIS_KEY) if isinstance(attrs, dict) else None
        if isinstance(raw, dict) and raw.get("status") in {"failed", "degraded", "refused"}:
            return raw
    return None
```

In `_with_window_backfill` the copy always carries `attrs[WINDOW_ANALYSIS_KEY] = analysis.to_attribute()` when the status is not `ok`, even when no record was produced, and the function takes the optional `subject` so D13 can order by it. In `java_fact_proof`, the final "no resolved CodeFactResolutionV2 is bound" refusal becomes `window analysis <status>: <codes>` when a window analysis explains the absence. In `java_partial_result`, `v2_any is None` with a failed or refused window analysis returns `INCONCLUSIVE` with a limitation built by `_java_partial_limitation(None, reason)` (class-driven: a `RUNTIME` code maps to `NO_RUNTIME_OBSERVATION`, everything else to `RELATION_UNDEMONSTRATED`, replacing the substring test).

**Alternative considered:** make the model robust and leave the verifier alone. Rejected: robustness removes one crash class but not the next; the verifier must be able to say why nothing was enumerated.

**Resulting verifier behavior:** the verdict is unchanged (`INCONCLUSIVE` before and after, because a typed category with no verifying candidate already returns `INCONCLUSIVE`); the *explanation and limitation* change from the untyped "no resolved CodeFactResolutionV2" to a typed reason the next-check logic can use.

**Trade-offs.** Markers add records to the envelope (bounded by D13). Multi-guard methods stay unverifiable by design in this tranche; selecting a record by claimed value or by a claimed guard is a verifier change and is listed as an open owner decision (it has soundness stakes: "the method returns X" is a weaker claim than "the method always returns X").

#### Version pinning / migration

No loop version. The change is analyzer output and verifier explanation (D4): new `window_analysis` attribute on the in-memory verify-time copy; new limitation codes; `iap/java-model/v2` (in-memory model, never persisted); `declaring_type` subjects become correctly nested. No persisted stored shape changes, so there is no migration. IAP is not live, so the old `java_parser_unavailable:*` strings are replaced outright, with no alias.

#### Test cases

Tests are written first and must be shown failing before the fix. Synthetic sources use `OrderServiceHandler` and nested types; no analyzed repository is referenced.

| ID | Name | Level | Setup | Action | Expected | Notes |
|---|---|---|---|---|---|---|
| J5-T01 | else-if carries the negated outer guard | unit | `kind(int v)` with `if (v > 0) return "pos"; else if (v < 0) return "neg";` in `OrderServiceHandler` | Enumerate; read `structured_fact` of the `neg` record | `path_conditions == [{text:"(v > 0)", holds:False}]` and `guard_text == "(v < 0)"` | Negative control: stop recording the `alternative` branch in `_path_conditions` and the test fails. |
| J5-T02 | nested if carries the positive outer guard | unit | `if (a) { if (b) { return "x"; } }` | Enumerate | `path_conditions == [{"(a)", True}]` on the inner record | Edge: three-deep nesting yields outermost-first order. |
| J5-T03 | enum method attributed to the enum | unit | `class OrderServiceHandler { enum Mode { FAST; String label() { if (this == FAST) return "f"; return "s"; } } }` | Enumerate | subject is `...OrderServiceHandler.Mode.label`, never `...OrderServiceHandler.label` | Negative control: restore `if node_type == "class_declaration": class_only.append(name)` and the test fails. Regression pin for the probe above. |
| J5-T04 | nested class method keeps the outer class | unit | `class Inner { String get() { if (n > 3) return "nine"; return "zero"; } }` inside `OrderServiceHandler` | Enumerate | subject `...OrderServiceHandler.Inner.get` (no package prepended to a bare name) | Cross-check: the field `n` of the same class has the matching prefix. |
| J5-T05 | interface constant resolves | unit | `interface Limits { int LIMIT = 5; String NAME = "n"; }` | Enumerate | RESOLVED records `Limits.LIMIT=5`, `Limits.NAME=n`, `field_category == "STATIC_FIELD"` | Pins the `constant_declaration` node handling. |
| J5-T06 | enum constant yields a typed marker | integration | `enum Mode { FAST("a"), SLOW }`; claim `Mode.FAST` is `a` (SOURCE_VALUE_FACT) | Backfill the row, then run `validate_java_fact_claim` and `java_partial_result` | marker with `enum_constant_not_modeled`; claim `INCONCLUSIVE` with that code in the explanation | Edge: 60 constants hit the budget (see J8). Negative control: drop the marker emission and the explanation reverts to the untyped text. |
| J5-T07 | switch return yields a typed marker | unit | `String sw(int v) { return switch (v) { case 1 -> "one"; default -> "o"; }; }` and an old-style `switch` method | Enumerate | marker `switch_return_not_modeled` for each method; guard records of other methods unaffected | Boundary: a switch inside a lambda inside the method is not credited to the method (scope walk). |
| J5-T08 | long literals no longer crash the window | unit | 1,200-char string field, 1,200-char guarded return, 3,000-char return, 1,200-char argument, plus a clean field and a clean method | Enumerate | status `ok` or `degraded`; the clean field and method are present; the 3,000-char return yields a `literal_too_long` marker for that subject only | Fail-open scope: containment is per declaration. Negative control: lower `ReturnSite.value_text` back to 1,024 and the 1,200-char case fails. |
| J5-T09 | builder exception is typed, not swallowed | unit | Monkeypatch `build_java_model` to raise `RuntimeError("secret-looking text")` | Enumerate | status `failed`, limitations `("analysis_failed",)`, `failure_type == "RuntimeError"`; the message text appears nowhere in the result | Security: no message leakage. |
| J5-T10 | grammar failure is typed distinctly | unit | Monkeypatch `parse_java` to raise `GrammarUnavailable` | Enumerate | status `failed`, limitations `("grammar_unavailable",)` | Distinguishes a cold cache from a code bug (J6). |
| J5-T11 | provenance refusal is not a failure | unit | Unnumbered snippet; unpinned revision; non-Java path | Enumerate | status `refused` with the provenance reason; never `analysis_failed` | Fail-closed, expected path. |
| J5-T12 | verifier names the reason when a window analysis failed | integration | Evidence whose analysis is monkeypatched to fail; typed SOURCE_VALUE claim | `validate_java_fact_claim` then `java_partial_result` | `INCONCLUSIVE`; explanation contains `window analysis failed: analysis_failed`; limitation class RELATION_UNDEMONSTRATED | Pins D8. Negative control: remove the `_window_analysis_of` lookup and the explanation reverts. |
| J5-T13 | execution failure payload is a valid resolution | unit | Call `analysis_failed_resolution` for each selector category | `CodeFactResolutionV2.model_validate(payload)` | validates; `status == UNRESOLVED`; `limitation_codes == ("analysis_failed",)`; `assumptions` names the exception type only | Regression pin for the malformed stamp. |
| J5-T14 | failed analyzer no longer adds the analyzed family | unit | Row carrying only a failure payload | `_java_evidence_families` | `ANALYZER_RESOLUTION` absent unless a valid record exists | Edge: today the `code_fact_java.*` key prefix alone adds the family. |
| J5-T15 | Python field chain reports failure | unit | Monkeypatch every attempt in the scope chain to raise | `resolve_python_field_fact` | returns an UNRESOLVED `analysis_failed` resolution, not `(None, None)` | Negative control: restore `continue` with no collection. |
| J5-T16 | multi-guard methods stay fail-closed (pin) | integration | `kind(int v)` with two guarded literals | `java_fact_proof` for claim `pos` and for `neg` | both `(False, "conflicting matching analyzer resolutions")` | Documents the intentional precision cost; flips only when the owner approves value-based selection. |
| J5-T17 | J1/J2 regressions hold | unit | Lambda with an `if`, anonymous class with an `if` | Enumerate | no record for either; outer method unaffected | Regression pin; negative control: remove `lambda_expression` from `_SCOPE_BOUNDARY_TYPES`. |

#### Rollout, risks and open owner decisions

Rollout is one tranche: tests first (every J5 case above red), then D5-D8, then the full java and python analysis unit files. No restart or live run is needed or requested.

Risks: (1) markers inflate the envelope (bounded by D13); (2) qualifying nested subjects changes which claims verify: claims naming only the outer class for a nested member stop verifying, which is the intended soundness gain and fails closed; (3) renaming the failure code touches ten resolvers, so the diff is wide but mechanical.

Owner decisions: (a) whether to allow selecting among multiple guarded literals by claimed value or claimed guard (a verifier-semantics change); (b) whether enum-constant argument values should ever be modeled (this document only reports them as unmodeled).

---

### Item J6: Grammar identity is a constant and grammars load at runtime

**Status:** Open. Severity should-fix (operational and audit; no wrong-verdict path). Dependencies: D1, D7 (`GrammarUnavailable`). Gating: **dependency pins and image/seed changes require owner approval**; everything else is code-only and no loop version applies (D4).

#### Verified problem

| Claim | Verdict | Evidence |
|---|---|---|
| Identity is a constant | Confirmed | `java/_parse.py` `_PARSER_VERSION = "tree-sitter-java"`; `python/_parse.py` `"tree-sitter-python"`; the docstring promises "grammar-drift auditing". Nothing branches on it (no verifier code reads `parser_version`), so it is audit-only. Four tests pin the constant and change with this item. The topology path already records `tree-sitter-language-pack==<version>` (`infrastructure/topology/payload.py`); the analyzers do not. |
| Pins are lower bounds | Confirmed | `pyproject.toml`: `tree-sitter>=0.26.0`, `tree-sitter-language-pack>=1.20.0`; `uv.lock` resolves `0.26.0` and `1.20.0`. |
| Grammars load at runtime into a user cache | Confirmed by the documented download model (Context7) and by the installed package | First `get_parser`/`get_language` call checks the cache, fetches a manifest and the parser binary from release assets, writes `<cache>/tree-sitter-language-pack/v<version>/libs/`, then `dlopen`s it. The documented offline recipe is to call `download([...])` at build time. This workstation's cache already holds both grammars. **Cold-cache behavior was not executed** (it would reach the network); the claim rests on the documentation plus the package's `DownloadError`, `DynamicLoadError`, `LanguageNotFoundError` exception types (verified importable, all subclasses of the pack's `Error`). |
| A cold worker raises per row or returns `[]` | Confirmed by reading | Resolvers map any exception to `java_parser_unavailable:<Name>`; `window_facts` returns `[]`. Each row retries the download. |
| Parser and `Query` are rebuilt per call | Confirmed; cost is small | Measured: `get_parser("java")` returns a new object each call (`is` is False); 200 `get_parser`+`parse` calls take about 1.5 ms; 200 `Query(...)` builds take about 47 ms (about 0.24 ms each). Caching is a determinism and failure-locality fix, not a performance fix. |
| Language ABI is available | Confirmed | `Language.abi_version` exists (`14` for Java here); `tree_sitter.LANGUAGE_VERSION == 15`. `Language` has no usable `version` or `semantic_version` (`None`). |

Secondary observation: `TreeSitterParser` (`infrastructure/evidence/code/parser.py`) caches one `Parser` per language on the instance and calls it from `asyncio.to_thread`, so one `Parser` can be used from several threads. py-tree-sitter does not document `Parser` as thread-safe. This is outside J6's scope; it is recorded in the ledger section.

#### Design

**D9: Identity names the pack, the core and the ABI.**

```python
# src/investigation_agent_platform/application/analysis/grammar.py
"""Grammar loading, caching and identity for the Java and Python analyzers (single home)."""

from __future__ import annotations

import threading
import time
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from investigation_agent_platform.application.analysis.failure import GrammarUnavailable

SUPPORTED = frozenset({"java", "python"})
_RETRY_AFTER_S = 60.0

_languages: dict[str, Any] = {}
_abi: dict[str, int] = {}
_failed_at: dict[str, float] = {}
_local = threading.local()


@lru_cache(maxsize=None)
def _package_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "unknown"


def language(name: str) -> Any:
    """The process-wide ``tree_sitter.Language``; loads once, fails typed and fast."""
    if name not in SUPPORTED:
        raise GrammarUnavailable(f"unsupported_language:{name}")
    cached = _languages.get(name)
    if cached is not None:
        return cached
    failed = _failed_at.get(name)
    if failed is not None and time.monotonic() - failed < _RETRY_AFTER_S:
        # A cold or air-gapped worker must not retry a download on every row.
        raise GrammarUnavailable("negative_cache")
    try:
        from tree_sitter_language_pack import get_language

        loaded = get_language(name)
    except Exception as exc:  # pack Error subclasses, ImportError, OSError
        _failed_at[name] = time.monotonic()
        raise GrammarUnavailable(type(exc).__name__) from exc
    _languages[name] = loaded
    _failed_at.pop(name, None)
    return loaded


def parser(name: str) -> Any:
    """One ``Parser`` per thread: parsers are not documented as thread-safe."""
    cache = getattr(_local, "parsers", None)
    if cache is None:
        cache = _local.parsers = {}
    cached = cache.get(name)
    if cached is None:
        from tree_sitter import Parser

        cached = cache[name] = Parser(language(name))
    return cached


def query(name: str, pattern: str) -> Any:
    """One compiled ``Query`` per thread and pattern."""
    cache = getattr(_local, "queries", None)
    if cache is None:
        cache = _local.queries = {}
    key = (name, pattern)
    cached = cache.get(key)
    if cached is None:
        from tree_sitter import Query

        cached = cache[key] = Query(language(name), pattern)
    return cached


def identity(name: str) -> str:
    """``tree-sitter-java;pack=1.20.0;ts=0.26.0;abi=14`` (46 characters; field cap is 64).

    Never raises: an unloadable grammar reports ``abi=unavailable`` so failure
    records can still be built.
    """
    abi = _abi.get(name)
    if abi is None:
        try:
            abi = _abi[name] = int(language(name).abi_version)
        except GrammarUnavailable:
            abi = None
    return (
        f"tree-sitter-{name};pack={_package_version('tree-sitter-language-pack')};"
        f"ts={_package_version('tree-sitter')};abi={abi if abi is not None else 'unavailable'}"
    )
```

**D10: The analyzers' `_parse.py` modules delegate to it, and a grammar fault is its own typed code.**

```python
# src/investigation_agent_platform/application/analysis/java/_parse.py  (replaces the constant and loaders)
from investigation_agent_platform.application.analysis import grammar as _grammar


def parse_java(source: bytes) -> Any:
    return _grammar.parser("java").parse(source)


def parser_version() -> str:
    return _grammar.identity("java")


def query_captures(root: Any, source: bytes, pattern: str) -> dict[str, list[Any]]:
    from tree_sitter import QueryCursor

    return dict(QueryCursor(_grammar.query("java", pattern)).captures(root))
```

The Python `_parse.py` is symmetric (`"python"`). `QueryCursor` stays per call (it carries match state). Every resolver's `except Exception as exc` now reports `failure_code(exc)` (D7): `grammar_unavailable` only for `GrammarUnavailable`, `analysis_failed` for everything else, with the exception name in `assumptions`, so a pydantic bug no longer reads as a missing grammar. `TreeSitterParser` and `payload._parser_version()` adopt `grammar.identity` for the recorded version string in a follow-up edit within the same tranche so the topology and analyzer stamps agree.

**Alternative considered:** `lru_cache` on a loader. Rejected: it would cache a failure only if it returned a sentinel, and an exception is never cached, so a cold worker would still hit the network on every row. The explicit negative cache with a 60-second window bounds that cost and heals once the cache is seeded.

**D11: Preflight, seeding and pinning (proposals; owner approval required).**

```python
# src/investigation_agent_platform/bootstrap/worker.py  (call once before registering activities)
def preflight_grammars() -> list[str]:
    """Names of required grammars that cannot be loaded. Logs once; never raises."""
    from investigation_agent_platform.application.analysis import grammar

    missing: list[str] = []
    for name in sorted(grammar.SUPPORTED):
        try:
            grammar.parser(name).parse(b"")
        except GrammarUnavailable as exc:
            missing.append(name)
            logger.error("grammar unavailable at startup: %s (%s)", name, exc)
    return missing
```

Whether a non-empty result stops the worker is an owner decision (recommended: log at error and expose in worker health so the failure appears once at start-up instead of once per row; hard-fail is stricter but blocks a worker that can still serve non-code runs). Seeding follows the documented recipe, run at deployment build time and **requiring owner approval because it performs a network fetch**:

```text
python -c "from tree_sitter_language_pack import download; download(['java', 'python'])"
```

A deployment that bakes a fixed cache path calls `configure(PackConfig(cache_dir="<base>"))` before first use (the pack appends `tree-sitter-language-pack/v<version>/libs` to the base). Pinning, **requires owner approval (dependency change)**:

```text
# pyproject.toml
"tree-sitter==0.26.0",
"tree-sitter-language-pack==1.20.0",
```

`uv.lock` already resolves exactly these versions, so the lock changes only its recorded specifier lines. Because the cache path embeds the pack version, an exact pin also pins the grammar binaries the cache serves.

#### Version pinning / migration

No loop version. Stamp change: `parser_version` becomes the identity string on every new fact; the four tests that pin the constant are updated in the same change. Old persisted facts keep `tree-sitter-java` (audit-only; nothing reads it). No migration. IAP is not live, so no alias for the old constant.

#### Test cases

| ID | Name | Level | Setup | Action | Expected | Notes |
|---|---|---|---|---|---|---|
| J6-T01 | identity format | unit | Installed grammars | `identity("java")` and `identity("python")` | matches `^tree-sitter-(java\|python);pack=\d+\.\d+\.\d+;ts=\d+\.\d+\.\d+;abi=\d+$`; length at most 64 | Negative control: return the old constant and the regex fails. |
| J6-T02 | identity tracks versions | unit | Monkeypatch `_package_version` to return other values | `identity("java")` | string changes with pack and core versions | Clears the `lru_cache` between cases. |
| J6-T03 | identity never raises | unit | Monkeypatch `language` to raise `GrammarUnavailable` | `identity("java")` | `abi=unavailable`, no exception | Fail-open for stamping only. |
| J6-T04 | loader failure is typed | unit | Monkeypatch `get_language` to raise the pack's `DownloadError` | `parse_java(b"class A {}")` | raises `GrammarUnavailable` with `__cause__` of the original type | Unsupported language name also raises it. |
| J6-T05 | negative cache bounds retries | unit | Counting fake `get_language` that always raises; fake `time.monotonic` | call 5 times within 10 s | loader invoked once; later calls raise `negative_cache` | Negative control: remove the `_failed_at` check and the count reaches 5. |
| J6-T06 | recovery | unit | Loader fails then succeeds; advance clock 61 s | call again | loads, `_failed_at` cleared, parse works | Proves a seeded cache heals without restart. |
| J6-T07 | parsers are per thread, languages shared | unit | Two threads each call `parser("java")` twice | compare identities | same object within a thread, different across threads; `language("java")` is the same object | Pins the thread-safety choice. |
| J6-T08 | queries are cached | unit | Count `Query` constructions | run `query_captures` 20 times with one pattern | one construction per thread | Regression pin for the per-call rebuild. |
| J6-T09 | query parity | unit | Existing query-substrate fixtures | run the 12 existing `query_captures` cases | results identical to the pre-change snapshot | Behavior-preserving refactor guard. |
| J6-T10 | resolvers distinguish faults | unit | One resolver per family; monkeypatch `parse_java` to raise `GrammarUnavailable` once and `ValueError` once | resolve | codes are `grammar_unavailable` and `analysis_failed`; neither equals the old `java_parser_unavailable:*` | Registry completeness: both registered. |
| J6-T11 | preflight reports missing | unit | Fake `grammar.parser` raising for `java` | `preflight_grammars()` | returns `["java"]`; one error log line; no exception; no network | Level unit with all I/O faked. |
| J6-T12 | pins are exact (approval-gated) | unit | Parse `pyproject.toml` | assert both packages use `==` | strict `xfail` until the owner approves the pin, then green | **Requires owner approval**; the xfail records intent without changing dependencies. |
| J6-T13 | cold-cache end to end | live | Fresh cache directory configured, network blocked | run one Java range read | one `grammar_unavailable` outcome, then fast failures; no crash | **Requires owner approval; not run in this tranche.** |

#### Rollout, risks and open owner decisions

Code rollout is one tranche (tests T01-T11 red first). The pin (T12), seeding and the live cold-cache check (T13) wait for owner approval. Risks: (1) an exact pin delays security updates of the pack; mitigated by a scheduled bump that re-runs the analysis suites; (2) the 60-second negative cache delays recovery after seeding by up to a minute; acceptable and explicit; (3) thread-local queries cost a few extra compilations per thread, negligible. Open decisions: hard-fail versus log-and-continue on a failed preflight; whether to bake the cache path via `PackConfig`.

---

### Item J8: Window-enumeration edges

**Status:** Open. Severity should-fix; the bare-subject sub-defect is **raised** because a false `VERIFIED` is reproducible. Dependencies: D2, D3, D5. Gating: none; no loop version (D4).

#### Verified problem

| Sub-claim | Verdict | Evidence |
|---|---|---|
| `splitlines()` shifts line numbers (`git.py`, `_provenance.py`) | Confirmed | `"class A {\n\x0c\n  int a = 1;\n  String s = \"x\u2028y\";\n  int b = 2;\n}\n".splitlines()` puts `int b` on line 7; `split("\n")` (git and IDE numbering) puts it on line 5. `git.py` (`raw_text.splitlines()`) numbers the snippet after splitting, so the cited numbers are internally consistent but 2 ahead of git and IDE. `_provenance.py` fails closed for the same input, but with the wrong reason (`source_range_missing_numbered_lines`). The same call appears in `topology/payload.py` (slices lines by tree-sitter rows), `pipeline.py` (reports SQL hit line numbers), and seven snippet readers (see D12). |
| `MAX_WINDOW_RECORDS = 25` truncates silently and starves later fields | Confirmed | 30 guarded methods then `private String tail = "LATE";` yields exactly 25 guard records and no field record, with no marker. |
| File-stem fallback misattributes secondary and nested classes | Confirmed, and worse than described | Window starting before a class header with a secondary class later in the window: the leading method gets the **bare** subject `helper` (the fallback only applies when the window names no class at all). The verifier's dot-boundary rule lets a qualified request match a bare candidate, so claims naming `WrongClass.helper`, `OrderServiceHandler.helper` and `SecondaryOne.helper` all returned `VERIFIED` against it (probe through `validate_java_fact_claim`). A method-only window from inside a secondary class is attributed to the file-stem class. |
| Spans carry byte columns under `col_*` | Confirmed | `private String s = "é";` reports `col_end == 18`; the line has 17 characters (`é` is two bytes). `_declaration_subtree_clean` relies on byte points (correct); the dataflow facts expose the same numbers to readers as if they were character columns. |

#### Design

**D12: One splitter everywhere a line number is minted or consumed.**

`git.py` uses `split_source_lines(raw_text)` (D3). Because the provider now preserves form feed and U+2028 inside lines, **every consumer of a numbered snippet must also stop using `splitlines()`**, or a legitimate numbered line containing such a character would break. The atomic list (each replaced by `split_source_lines` imported from `domain/code/lines.py`): `java/_provenance.py` and `python/_provenance.py`, `application/investigation/verification.py` (three snippet readers), `domain/hypothesis/templates.py` (two), `application/investigation/context_compiler.py` (two), `application/worker/activities/execution.py` (attribute scan), and the two line-indexing sites `infrastructure/topology/payload.py` and `infrastructure/evidence/code/pipeline.py`. A lint test pins the list (the stray-`splitlines` guard in the table below).

```python
# src/investigation_agent_platform/infrastructure/evidence/code/git.py  (get_source)
from investigation_agent_platform.domain.code.lines import split_source_lines

lines = split_source_lines(raw_text)   # was raw_text.splitlines()
```

**Alternative considered:** leave the provider alone and only harden the analyzers. Rejected: the cited numbers are the evidence; if they differ from git and IDE numbering, every citation is off by the same shift, and the analyzer fix would merely agree with the wrong number.

**D13: A budget that cannot starve a later subject and says when it truncated.**

```python
# src/investigation_agent_platform/application/analysis/java/window_facts.py
from collections import deque


from investigation_agent_platform.application.analysis.subjects import subject_identifies


def _identifies(hint: str | None, subject: str) -> bool:
    return bool(hint) and subject_identifies(hint, {subject})


def _allocate(
    candidates: list[_Candidate], hint: str | None, cap: int
) -> tuple[list[_Candidate], int]:
    """Subject-first, then fair share across kinds, then document order; reports the omitted count."""
    first = [c for c in candidates if _identifies(hint, c.subject)]
    taken = {id(c) for c in first}
    queues = {
        kind: deque(c for c in candidates if c.kind == kind and id(c) not in taken)
        for kind in ("field", "guard", "marker")
    }
    chosen = list(first[:cap])
    while len(chosen) < cap and any(queues.values()):
        for kind in ("field", "guard", "marker"):
            if queues[kind] and len(chosen) < cap:
                chosen.append(queues[kind].popleft())
    return chosen, len(candidates) - len(chosen)
```

`VerificationEngine._subject_identifies` lives in the verification module and the analyzer must not import it (layering), so the matcher moves verbatim to `application/analysis/subjects.py` and `VerificationEngine._subject_identifies` delegates to it:

```python
# src/investigation_agent_platform/application/analysis/subjects.py
def subject_identifies(requested: str | None, candidates: set[str]) -> bool:
    """Exact match, else dot-boundary suffix in either direction (never substring).

    A bare requested name (no dot) never suffix-matches a qualified candidate.
    """
    req = str(requested or "").strip()
    if not req:
        return False
    for candidate in candidates:
        cand = str(candidate or "").strip()
        if not cand:
            continue
        if cand == req or ("." in req and cand.endswith("." + req)) or req.endswith("." + cand):
            return True
    return False
```

Moving it verbatim is behavior-preserving; the existing subject tests pin that. Tightening the `req.endswith("." + cand)` branch for bare candidates is deliberately **not** done here: D14 removes bare candidates at the source instead, which fixes the misattribution without changing the matcher the live claim flow depends on. `_with_window_backfill(evidence_items, subject=None)` receives the claim subject from its two callers (both already compute it) so the requested subject is enumerated first. `MAX_WINDOW_RECORDS` rises to 100 (the 4,096-character evidence cap bounds a window to a few hundred declarations, so truncation is rare and always reported).

**D14: Never emit a bare subject; infer the owner from the file name only with a stamp.**

```python
def _owner_of(enclosing: str | None, package: str, stem: str) -> tuple[str | None, bool]:
    """(qualified owner, inferred). ``enclosing_class`` already carries package and nesting."""
    if enclosing:
        return enclosing, False
    if stem:
        return (f"{package}.{stem}" if package else stem), True
    return None, False
```

A declaration whose owner is `None` becomes a `declaring_type_not_in_window` marker instead of a bare-named record. An inferred owner is emitted with `assumptions=("declaring class inferred from the file name; a nested or secondary class is not excluded",)` and `structured_fact["class_attribution"] = "file_stem_inferred"`. The method-only window fallback that live runs depend on is preserved; what changes is that it is labeled and that a window with a leading orphan method no longer produces a bare name that any qualified claim matches. Requiring corroboration (a second window containing the class header) before an inferred owner may verify is a verifier-semantics change and is an owner decision, not part of this tranche.

**D15: Name what the columns are.**

```python
class Span(BaseModel):
    file_path: str = Field(min_length=1, max_length=1024)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    byte_col_start: int = Field(default=0, ge=0)   # tree-sitter byte column
    byte_col_end: int = Field(default=0, ge=0)


def code_point_column(line_text: str, byte_col: int) -> int:
    """Convert a UTF-8 byte offset within ``line_text`` to a code-point column."""
    return len(line_text.encode("utf-8")[:byte_col].decode("utf-8", errors="ignore"))
```

`dataflow.py` converts to code-point columns when it emits definitions, uses and `data_flow_refs` (it already holds `lines`), and adds `"column_unit": "code_point"` to `structured_fact`. `window_facts._declaration_subtree_clean` keeps matching on byte points via `byte_col_*`; the `model.py` dataflow dedupe keys and `dataflow.py` sort keys switch to the renamed fields in the same change. No reader compares columns across rows, so old byte-column rows and new code-point rows never meet in a comparison.

#### Version pinning / migration

No loop version (D4): the changes are provider numbering, analyzer data and subject identity. The line-number change affects only newly banked reads; previously banked rows keep their old numbering and their own provenance windows (verification binds each record to its own row), so nothing is re-interpreted. No migration. Stamp changes: `column_unit` marker on dataflow facts; `class_attribution` on inferred records; limitation codes `window_records_truncated`, `declaring_type_not_in_window`.

#### Test cases

| ID | Name | Level | Setup | Action | Expected | Notes |
|---|---|---|---|---|---|---|
| J8-T01 | splitter table | unit | Inputs: `""`, `"a"`, `"a\n"`, `"\n"`, `"a\r\nb"`, `"a\x0cb\nc"`, `"a\u2028b\n"`, `"a\rb\n"` | `split_source_lines` | `[]`, `["a"]`, `["a"]`, `[""]`, `["a","b"]`, `["a\x0cb","c"]`, `["a\u2028b"]`, `["a\rb"]` | Negative control: swap in `str.splitlines` and the form-feed, U+2028 and lone-CR rows fail. |
| J8-T02 | provider numbering equals git numbering | integration | Temporary pygit2 repo in `tmp_path`, one commit of a file with a form-feed line and a U+2028 inside a string | `get_source` with `start_line=5, end_line=5` | the snippet is `5\|   int b = 2;` | Synthetic repo only; no network. |
| J8-T03 | analyzers accept exotic separators inside a line | unit | Numbered snippet `2\| <FF>` and `2\|   String s = "x<U+2028>y";` | `java_source_lines` and the Python twin | parsed with the correct numbers, no refusal | Edge and regression: today both fail closed with the wrong reason. |
| J8-T04 | topology line indexing | unit | File with a form feed before a method; payload builder | compare symbol rows with sliced bodies | `lines[start-1:end]` begins at the method | Covers the line-indexing site. |
| J8-T05 | no stray `splitlines` on snippet paths | unit | The named module list in D12 | regex scan | zero `.splitlines()` occurrences in those modules | Guard test; negative control: reintroduce one and it fails. |
| J8-T06 | budget is subject-first | unit | 30 guarded methods then field `tail`; `subject_hint="OrderServiceHandler.tail"`, cap patched to 10 | enumerate | the `tail` record is present; `omitted == 21`; limitation `window_records_truncated` | Negative control: ignore the hint and `tail` is absent. |
| J8-T07 | budget is fair without a hint | unit | Same source, no hint, cap 10 | enumerate | at least one field record among the 10 | Round-robin pin. |
| J8-T08 | no bare subjects | unit | Window starting with `helper()` before a closing brace and a secondary class | enumerate | no record whose `subject_identity` lacks a dot; `helper` is `...OrderServiceHandler.helper` with `class_attribution == "file_stem_inferred"` | Negative control: restore the `class_fallback` condition and a bare subject returns. |
| J8-T09 | wrong-class claim no longer verifies | integration | Same window; claims `WrongClass.helper` and `SecondaryOne.helper` (value `h`) | `validate_java_fact_claim` | both `None` / `INCONCLUSIVE`; `OrderServiceHandler.helper` still verifies | Fail-closed regression pin for the probe. |
| J8-T10 | unknown owner becomes a marker | unit | Method window with an unnamed path stem | enumerate | `declaring_type_not_in_window` marker, no record | Edge. |
| J8-T11 | inferred owner is labeled | unit | Method-only window inside a nested class, path stem `OrderServiceHandler` | enumerate | assumption text present and `structured_fact.class_attribution` set | Documents the residual risk instead of hiding it. |
| J8-T12 | columns are named and converted | unit | `private String s = "é";` | `build_java_model` and `resolve_java_data_flow` | `byte_col_end == 18`; dataflow ref column is 17 and `structured_fact.column_unit == "code_point"` | Negative control: emit byte columns and the 17 assertion fails. |
| J8-T13 | declaration locations match git numbering | integration | Banked row built from a file with a form feed | enumerate | record `declaration_location.line_start` equals the git/IDE line of the declaration | End-to-end pin. |
| J8-T14 | CRLF equals LF | unit | The same source with `\r\n` and `\n` | enumerate both | identical records (values, subjects, lines) | Regression. |

#### Rollout, risks and open owner decisions

One tranche; the D12 file list is atomic (partial application breaks numbered-line parsing). Risks: (1) a missed `splitlines` consumer breaks on exotic characters (mitigated by the stray-`splitlines` guard); (2) claims about nested members that relied on the bare or stem subject stop verifying (intended, fail-closed); (3) the verifier delegation to `subject_identifies` must be behavior-identical (pinned by the existing subject tests). Open decisions: corroboration requirement for inferred owners; whether to extend the splitter to the remaining infrastructure `splitlines` sites outside numbered-snippet paths.

---

### Item J9: Cross-boundary classification is a path-prefix test

**Status:** Open. Severity should-fix (prompt steering, not a verdict path); one new finding (the `"."` root) is security-adjacent. Dependencies: D1. Gating: the validator alignment in D18 **requires owner approval**; the rest has no loop version (D4).

#### Verified problem

| Sub-claim | Verdict | Evidence |
|---|---|---|
| Empty `source_roots` makes every site external; the comment says the opposite | Confirmed | `external_classifier_for_roots([])` returns `lambda _file_path: True`. `reasoning.py` says "a profile-store failure leaves roots empty, so every site stays in-tenant (conservative default, never a handoff)". The unit test named `test_in_tenant_default_when_no_roots` asserts `external is True` and a rendered handoff, so the test name, the comment and the behavior disagree. |
| A transient profile-store error hands off every template "at seed time" | Confirmed, scope corrected | `except Exception: profile_roots = []` is silent. The effect is in `compile_dormant_templates` (the model-facing `dormant_templates` block): every template renders as a cross-boundary handoff with no discriminating fetch. "Seed time" is the prompt compile; `seed_from_dormant_templates` has no production caller and no workflow rule reads dormant templates or handoffs (verified by search). Impact is prompt steering. |
| Roots are not repo-qualified | Confirmed | `ApplicationProfile.all_source_roots()` flattens `source_roots` and `paths` across services and repos; the classifier receives only `file_path`, although `classify_banked_sites` already derives a per-row `repository`. A path under another repository that shares `src/main/java` counts as in-tenant. The request validator uses the same flattened list. |
| `cross_module_scope` conflates other-file with other-window | Confirmed | `method_facts.py` emits it whenever a requested `declaring_type` matches no method in the window (other file, other window, nested class, typo). |
| New: whole-repository root `"."` matches nothing | **Confirmed by pure-function probe** | `assemble_profile` emits `source_roots: ["."]` for single-module repos (and a unit test pins it). `external_classifier_for_roots(["."])("src/A.java")` is `True` (external) and `_is_within_roots("src/A.java", ["."])` is `False` (the validator's GET_CODE path check), because `"/."` is a literal prefix. Roots `""` and `"./"` behave the same. Not confirmed end to end against a live profile; the validator half is security-adjacent. |

#### Design

**D16: Three-state, repo-qualified classification; an unknown boundary never invents a handoff.**

```python
# src/investigation_agent_platform/domain/profile/scope.py
from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class BoundaryClass(StrEnum):
    IN_TENANT = "in_tenant"
    EXTERNAL = "external"
    UNKNOWN = "unknown"      # roots not declared or not loadable: say so, do not guess


class SourceScope(BaseModel):
    model_config = ConfigDict(frozen=True)

    repos: tuple[str, ...] = ()   # org-qualified locators of one service
    roots: tuple[str, ...] = ()   # normalized; "" means the whole repository


def normalize_source_root(root: object) -> str | None:
    """``"."``, ``"./"``, ``""`` and ``"/"`` mean the whole repository (``""``); ``..`` is invalid."""
    text = str(root or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    text = text.strip("/")
    if ".." in text.split("/") or "\x00" in text:
        return None
    return "" if text in {"", "."} else text


def classify(scopes: list[SourceScope], repository: str, file_path: str) -> BoundaryClass:
    if not any(scope.repos or scope.roots for scope in scopes):
        return BoundaryClass.UNKNOWN
    path = f"/{str(file_path or '').lstrip('/')}"
    repo_known = bool(repository) and repository not in {"unknown-repo", "Git"}
    for scope in scopes:
        if repo_known and scope.repos and repository not in scope.repos:
            continue  # another service's repository: its roots do not apply here
        if not scope.roots or "" in scope.roots:
            return BoundaryClass.IN_TENANT   # repos declared, no narrower root
        if any(path == f"/{r}" or path.startswith(f"/{r}/") for r in scope.roots):
            return BoundaryClass.IN_TENANT
    return BoundaryClass.EXTERNAL
```

```python
# src/investigation_agent_platform/domain/profile/models.py  (ApplicationProfile)
def source_scopes(self) -> list[SourceScope]:
    scopes: list[SourceScope] = []
    for svc in self.services:
        code = svc.code
        roots = dict.fromkeys(
            r for r in (normalize_source_root(x) for x in [*code.source_roots, *code.paths])
            if r is not None
        )
        repos = dict.fromkeys(
            loc for loc in (repo_locator(code.org or "", repo) for repo in code.repos) if loc
        )
        scopes.append(SourceScope(repos=tuple(repos), roots=tuple(roots)))
    return scopes

def classify_source(self, repository: str, file_path: str) -> BoundaryClass:
    return classify(self.source_scopes(), repository, file_path)
```

`repo_locator(org, repo)` is the org-qualification rule now private to `PyGit2Adapter._split_repo_entry`; it moves to `domain/profile/scope.py` and the adapter delegates to it, so the classifier and the checkout resolver agree on locator spelling. `all_source_roots()` stays for the validator until D18 is approved.

The compile path distinguishes why roots are missing and renders accordingly:

```python
# src/investigation_agent_platform/application/investigation/reasoning.py
profile = None
roots_status = "unavailable"
try:
    if self._profile_repo is not None:
        profile = await self._profile_repo.get_by_application_id(
            tenant_id, state.investigation.application_id
        )
        roots_status = "known" if profile is not None else "unavailable"
except Exception as exc:
    logger.warning("boundary roots unavailable: %s", type(exc).__name__)  # type only
compiled_context = compile_reasoning_context(
    state, classify=boundary_classifier(profile), roots_status=roots_status
)
```

```python
# src/investigation_agent_platform/application/investigation/context_compiler.py
def boundary_classifier(profile: Any | None) -> Callable[[str, str], BoundaryClass]:
    if profile is None:
        return lambda _repo, _path: BoundaryClass.UNKNOWN
    return profile.classify_source
```

`classify_banked_sites(rows, classify=...)` passes `(repository, file_path)` per row. Only `EXTERNAL` sets `cross_boundary` and builds a handoff; `UNKNOWN` leaves the site in-tenant (the discriminating fetch is kept, as the original comment promised) and sets `boundary_unknown=True` on the `SuspectSite`, which the renderer exposes as `"boundary": "unknown"` and the block carries `"roots_status"`. The request validator remains the authority at dispatch time: if roots are truly absent it denies the fetch, which is the correct place for that decision. The existing test is renamed and its assertion corrected.

**Alternative considered:** make the empty-roots case in-tenant (a literal reading of the comment) without a third state. Rejected: an unknown boundary should be visible to the model and to telemetry, and "in tenant" asserts something the system does not know.

**D17: Split `cross_module_scope` into two honest codes.**

```python
# src/investigation_agent_platform/application/analysis/java/method_facts.py
if declaring_type:
    matched = [c for c in candidates if _p.matches_declaring_type(c.qualified_type, declaring_type)]
    if not matched:
        declared = {m.enclosing_class for m in code_model.methods if m.enclosing_class}
        code = (
            "declaring_type_mismatch_in_window"  # the window declares other types
            if declared or code_model.classes
            else "declaring_type_not_in_window"  # the window names no type at all
        )
        return _unresolved(repository, revision, subject, code, evidence_ids, level_text)
    candidates = matched
```

Both are `WINDOW`-class codes (D1): a different read can fix them. Neither claims "another module".

**D18: `"."` normalization and validator alignment (proposal; requires owner approval).** The classifier already uses `normalize_source_root` (D16). `_is_within_roots` in `validator.py` should call the same function so a generated single-module profile (`source_roots: ["."]`) is not denied for every `GET_CODE`. This **widens** what the validator permits (from "nothing" to "the whole repository") for that profile shape, so it is security-adjacent and is not applied until the owner approves and a security review confirms that a `"."` root is meant to be whole-repository. Until then the validator is unchanged and a strict `xfail` records the intended behavior.

#### Version pinning / migration

No loop version (D4). `REASONER_PROMPT_VERSION` takes the next suffix because the compiled `dormant_templates` block gains `roots_status` and per-template `boundary` keys. Nothing branches on them. No migration. IAP is not live, so `external_classifier_for_roots` is replaced, not kept beside the new classifier.

#### Test cases

| ID | Name | Level | Setup | Action | Expected | Notes |
|---|---|---|---|---|---|---|
| J9-T01 | unknown roots do not invent a handoff | unit | Banked range row with an external-call site; `profile=None` | `compile_dormant_templates` via `boundary_classifier(None)` | template keeps `discriminating_fetch`, `handoff is None`, entry `boundary == "unknown"` | Negative control: make `UNKNOWN` map to `EXTERNAL` and the test fails. |
| J9-T02 | store error is surfaced, not silent | unit | `_profile_repo.get_by_application_id` raises `RuntimeError("secret")` | run the compile path | `roots_status == "unavailable"`; one warning containing `RuntimeError` and not the message | Security: error type only. |
| J9-T03 | declared roots still classify external | unit | Profile `source_roots=["svc/src/main/java"]`; row path `upstream/lib/a.py` | classify | `EXTERNAL`; handoff rendered as today | Pins existing behavior. |
| J9-T04 | roots are repo-qualified | unit | Two services; service A repo `org/a` root `src/main/java`; row from repo `org/b` with the same prefix | `classify_source("org/b", "src/main/java/X.java")` | `EXTERNAL` | Negative control: ignore `repository` and it returns `IN_TENANT`. |
| J9-T05 | repos without roots mean the whole repo | unit | Service with `repos=["org/a"]`, no roots | classify a file in `org/a` and one in `org/z` | `IN_TENANT`, `EXTERNAL` | Edge. |
| J9-T06 | whole-repository root | unit | `source_roots=["."]` | classify `src/A.java` | `IN_TENANT` | Regression pin for the probe. |
| J9-T07 | root normalization table | unit | `"."`, `"./"`, `""`, `"/"`, `"/svc/"`, `"svc/../x"`, `"a\\b"` | `normalize_source_root` | `""`, `""`, `""`, `""`, `"svc"`, `None`, `"a/b"` | Edge and malformed input. |
| J9-T08 | undeterminable repository falls back to roots only | unit | Row repository `unknown-repo` | classify | root match decides; no exception | Fail-open on the repo dimension only. |
| J9-T09 | codes are honest | unit | Window declaring another class; window declaring none | `resolve_java_call_target` with a `declaring_type` | `declaring_type_mismatch_in_window` and `declaring_type_not_in_window`; `cross_module_scope` is gone | Update the existing `test_cross_module_scope_abstains`. |
| J9-T10 | renamed legacy test | unit | Replace `test_in_tenant_default_when_no_roots` | assert the new UNKNOWN semantics | passes | Removes the misleading test name. |
| J9-T11 | prompt shape stamp | unit | Compile a context | inspect | `prompt_version` suffix bumped; `roots_status` present | Negative control: revert the bump. |
| J9-T12 | validator alignment (approval-gated) | unit | `_is_within_roots("src/A.java", ["."])` | assert `True` | strict `xfail` until owner and security approval | **Requires owner approval.** |

#### Rollout, risks and open owner decisions

One tranche, tests first. Risks: (1) a profile with only `paths` and no repos now classifies by roots alone, as before; (2) models see `"boundary": "unknown"` and may ignore it; accepted, it is information not a rule; (3) delegating `_split_repo_entry` touches the git adapter, so its tests run with this change. Owner decisions: approve D18; confirm that `"."` means the whole repository; confirm that an unknown boundary should keep the discriminating fetch.

---

### Item J10: Dataflow stopping points and the frontier's blind spot

**Status:** Open. Severity should-fix, **lower real impact than rated** because the ranking has no dispatch consumer (see below). Dependencies: D1, D2. Gating: the forcing-side derivation is pinned to **`LV_J`**; everything else is data-only (D4). Wiring the ranking into dispatch is an explicit owner decision outside this tranche.

#### Verified problem

| Sub-claim | Verdict | Evidence |
|---|---|---|
| `EvidenceState` comes from a row count only | Confirmed | `forcing.rank_advisory_orders`: `NO_ROWS if banked <= 0 else ROWS_WITHOUT_MATCH`. |
| Nothing produces `STALE_MATCH` or `LOCAL_COMPLETE` | Confirmed | Both enum members exist in `domain/collection/frontier.py`; only tests construct them. |
| Analyzer refusals and clean unrelated rows both map to `ASSUMPTION_SPREAD` | Confirmed, and the input is not even available | A history entry carries `outcome_status`, `evidence_added`, `miss_reason`, `retrieval_attempt`; it carries **no analyzer status**. A GET_CODE that banked one row whose dataflow was `PARTIAL: interprocedural_aliasing` and one that banked a clean unrelated row are indistinguishable to the cascade. |
| Dataflow stopping points are only partly typed | Confirmed (probe) | Typed: passed-as-argument (`interprocedural_aliasing`), concurrency markers, stream markers, out-of-window reference, conditional definition (`control_flow_not_modeled`, J3). **Not typed, all return `RESOLVED` with `definite_assignment: established`:** receiver mutation (`x.add("a")`), field store through the variable (`x.count = 4`), array element store (`x[0] = 5`), aliasing store (`this.cache = x`), lambda and anonymous-class capture without a call. The cause: `model.py` records no event for an identifier that is the receiver of a call, so these uses are invisible rather than refused. |
| The frontier does not escalate when dataflow bottoms out | True but incomplete | `rank_advisory_orders` and `advisory_orders` are written to `PostReasonCascadeState` and **never read** by `run_investigation.py`; `seed_from_dormant_templates` has no production caller. The frontier cannot escalate because nothing consumes it, in addition to being unable to see the stop. |

#### Design

**D19: Type the remaining dataflow stopping points (data-only).** New event kinds in `_build_dataflow_records`: `receiver_call` (identifier is the `object` of a `method_invocation`), `member_store` (identifier is the object of an assignment's `field_access` or `array_access` lvalue), `alias_store` (identifier on the right of an assignment whose lvalue is not a plain identifier), `lambda_capture` (identifier inside a lambda or anonymous class body bound outside it). The resolver abstains with three new registered codes:

```python
# src/investigation_agent_platform/application/analysis/java/dataflow.py  (after the interprocedural check)
_IMMUTABLE_RECEIVERS = frozenset(
    {"String", "Integer", "Long", "Double", "Float", "Short", "Byte", "Boolean", "Character",
     "BigDecimal", "BigInteger", "LocalDate", "LocalDateTime", "Instant", "UUID"}
)
kinds = {event.kind for event in method_events}
declared_type = binding.var_type.split("<", 1)[0].strip()
if "member_store" in kinds or (
    "receiver_call" in kinds and declared_type not in _IMMUTABLE_RECEIVERS
):
    return _unresolved(repository, revision, subject, "receiver_mutation_possible",
                       evidence_ids, level_text, partial=True)
if "alias_store" in kinds:
    return _unresolved(repository, revision, subject, "alias_escape",
                       evidence_ids, level_text, partial=True)
if "lambda_capture" in kinds:
    return _unresolved(repository, revision, subject, "lambda_capture_not_modeled",
                       evidence_ids, level_text, partial=True)
```

All three are `UNSUPPORTED`-class in the registry (the fact is not modeled; a wider read does not fix it). `var_type` comes from the binding (`unknown` for `var`, which is therefore treated as mutable).

**D20: Put the analyzer outcome on the history entry (data-only).** A pure summarizer reduces any number of analyzer payloads to scalar counts by status and by limitation class. It records no values, messages or identifiers, so it is safe in Temporal history and replay-stable.

```python
# src/investigation_agent_platform/application/analysis/summary.py
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

from investigation_agent_platform.application.analysis.limitations import limitation_class


def summarize_analysis(payloads: Iterable[Mapping[str, Any]]) -> dict[str, int] | None:
    """Counts by status and limitation class over analyzer payloads; None when none ran."""
    status: Counter[str] = Counter()
    classes: Counter[str] = Counter()
    for raw in payloads:
        state = str(raw.get("status", "")).upper()
        if state not in {"RESOLVED", "PARTIAL", "UNRESOLVED"}:
            continue
        status[state.lower()] += 1
        for code in raw.get("limitation_codes") or ():
            classes[limitation_class(str(code)).value] += 1
    if not status:
        return None
    return {k: status.get(k, 0) for k in ("resolved", "partial", "unresolved")} | {
        k: classes.get(k, 0)
        for k in ("window", "runtime", "unsupported", "failure", "provenance")
    }
```

`ActionOutcome` gains `analysis: dict[str, int] | None = None` (frozen model, optional, default `None`); `execution.py` fills it from the payloads it already merged (`code_fact_resolution_v2`, the `code_fact_resolutions` envelope and the `code_fact_java.*` records); `run_investigation.py` copies `_outcome.get("analysis")` into the history entry beside `evidence_added`. Replay-safe: the key is computed from recorded activity results and is simply absent on old entries.

**D21: Derive the state from analyzer status (pure domain).**

```python
# src/investigation_agent_platform/domain/collection/frontier.py  (enum and mapping)
class EvidenceState(StrEnum):
    NO_ROWS = "NO_ROWS"
    ROWS_WITHOUT_MATCH = "ROWS_WITHOUT_MATCH"
    STALE_MATCH = "STALE_MATCH"
    LOCAL_COMPLETE = "LOCAL_COMPLETE"
    ANALYSIS_INCOMPLETE = "ANALYSIS_INCOMPLETE"  # rows banked; the analyzer gave up, a wider or other read can help
    ANALYSIS_BLOCKED = "ANALYSIS_BLOCKED"        # rows banked; the analyzer gave up, source reads cannot help

# FailureCharacter.spread_dimension, after the ROWS_WITHOUT_MATCH branch:
if evidence_state == EvidenceState.ANALYSIS_INCOMPLETE:
    return FrontierDimension.ASSUMPTION_SPREAD
if evidence_state == EvidenceState.ANALYSIS_BLOCKED:
    return FrontierDimension.LAYER_SPREAD
```

```python
# src/investigation_agent_platform/domain/collection/evidence_state.py
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, fields

from investigation_agent_platform.domain.collection.frontier import EvidenceState

RECENT_ANALYZED_TURNS = 2  # a widen-and-retry must be able to supersede an earlier refusal


@dataclass(frozen=True)
class AnalysisStamp:
    resolved: int = 0
    partial: int = 0
    unresolved: int = 0
    window: int = 0
    runtime: int = 0
    unsupported: int = 0
    failure: int = 0
    provenance: int = 0

    @classmethod
    def parse(cls, raw: object) -> AnalysisStamp | None:
        if not isinstance(raw, dict):
            return None
        try:
            return cls(**{f.name: int(raw.get(f.name, 0) or 0) for f in fields(cls)})
        except (TypeError, ValueError):
            return None


def derive_evidence_state(
    *,
    banked_total: int,
    stamps: Sequence[AnalysisStamp],
    claims_verified: bool | None = None,
    match_predates_onset: bool | None = None,
) -> EvidenceState:
    """Pure. ``stamps`` are in turn order; only the most recent ones count.

    ``claims_verified`` and ``match_predates_onset`` are producers for LOCAL_COMPLETE
    and STALE_MATCH that need claim-level and onset-anchor facts the cascade does not
    hold today; production passes ``None`` for both until the owner schedules that
    wiring, so neither state is produced yet (an analyzer-RESOLVED row for one
    selector cannot show that the claim needs nothing further).
    """
    if banked_total <= 0:
        return EvidenceState.NO_ROWS
    recent = list(stamps)[-RECENT_ANALYZED_TURNS:]
    if not recent:
        return EvidenceState.ROWS_WITHOUT_MATCH
    gave_up = [s for s in recent if s.partial or s.unresolved]
    if any(s.runtime or s.failure for s in gave_up):
        return EvidenceState.ANALYSIS_BLOCKED
    if gave_up:
        return EvidenceState.ANALYSIS_INCOMPLETE
    if match_predates_onset:
        return EvidenceState.STALE_MATCH
    if claims_verified:
        return EvidenceState.LOCAL_COMPLETE
    return EvidenceState.ROWS_WITHOUT_MATCH
```

**D22: The forcing-side change, pinned to `LV_J`.**

```python
# src/investigation_agent_platform/application/worker/workflows/forcing.py
def _frontier_state_derived(loop_version: Any) -> bool:
    """Whether the run's pin derives EvidenceState from analyzer status (LV_J+), fail-safe."""
    try:
        return bool(loop_version_at_least({"loop_version": loop_version}, LOOP_VERSION_LV_J))
    except Exception:
        return False


def _evidence_state_for(
    history: list[Any] | None, banked_total: int, loop_version: Any
) -> FrontierEvidenceState:
    if not _frontier_state_derived(loop_version):
        return (
            FrontierEvidenceState.NO_ROWS
            if banked_total <= 0
            else FrontierEvidenceState.ROWS_WITHOUT_MATCH
        )
    stamps = [
        stamp
        for entry in history or []
        if isinstance(entry, dict) and int(entry.get("evidence_added", 0) or 0) > 0
        for stamp in [AnalysisStamp.parse(entry.get("analysis"))]
        if stamp is not None
    ]
    return derive_evidence_state(banked_total=banked_total, stamps=stamps)
```

`rank_advisory_orders(orders, history, banked_total, *, loop_version=LOOP_VERSION_CURRENT)` replaces its inline `evidence_state = ...` with `_evidence_state_for(...)` and adds `"gave_up": state in {ANALYSIS_INCOMPLETE, ANALYSIS_BLOCKED}` to its returned dict; the call in `apply_post_reason_cascade` passes `state.loop_version`. In `loop_version.py`, `LOOP_VERSION_LV_J` (the number is assigned at implementation) joins `KNOWN_LOOP_VERSIONS`, the order table and `LOOP_VERSION_CURRENT`, with a docstring entry: "from LV_J the advisory frontier state is derived from analyzer status; it only relabels an advisory recommendation and cannot publish a verdict".

**Alternative considered:** compute the state inside the activity and pass a ready-made state to the workflow. Rejected: the workflow must stay deterministic and replayable from recorded results, and a derived enum written into history would freeze today's mapping into stored rows. Storing the raw counts and deriving in pure code keeps the mapping changeable behind a version pin.

**Alternative considered (and recommended as a follow-up, not done here):** make the smallest-fetch rule consult `gave_up`, widening a window when the state is `ANALYSIS_INCOMPLETE`. That is a new loop rule and needs its own `RULES` entry, stand-down contract and owner decision.

#### Version pinning / migration

The history `analysis` key, `ActionOutcome.analysis`, the summarizer, the pure derivation, the two enum members and the D19 dataflow codes are **data-only**: older pins ignore the key and no rule reads it. The only loop-behavior change is `_evidence_state_for`, pinned with `loop_version_at_least(metadata, LV_J)`; rows pinned to `lv7` or earlier keep the row-count derivation exactly. Today that change is observable only in the recorded advisory ranking, because the workflow does not read it; the pin exists so that when a consumer arrives, runs have deterministic semantics. No environment switch; unmarked rows resolve to current. No migration (the JSONB history and handoff columns are untouched).

#### Test cases

| ID | Name | Level | Setup | Action | Expected | Notes |
|---|---|---|---|---|---|---|
| J10-T01 | no rows | unit | `banked_total=0`, any stamps | `derive_evidence_state` | `NO_ROWS` | |
| J10-T02 | rows, no analyzer signal | unit | `banked_total=3`, `stamps=[]` | derive | `ROWS_WITHOUT_MATCH` | Preserves today's value where no signal exists. |
| J10-T03 | window-class refusal | unit | Latest stamp `partial=1, window=1` | derive | `ANALYSIS_INCOMPLETE` | Negative control: ignore `partial` and it reads `ROWS_WITHOUT_MATCH`. |
| J10-T04 | runtime or failure refusal | unit | Latest stamp `unresolved=1, runtime=1`; another with `failure=1` | derive | `ANALYSIS_BLOCKED` both times | |
| J10-T05 | widen-and-retry supersedes | unit | Stamps `[partial window, resolved only]` | derive | `ROWS_WITHOUT_MATCH` | Edge: only the last two analyzed turns count. |
| J10-T06 | clean unrelated row is not "gave up" | unit | Stamp `resolved=1` only | derive | `ROWS_WITHOUT_MATCH` | The conflation the review named, now separated. |
| J10-T07 | producers need claim-level facts | unit | Stamp resolved-only; `claims_verified=True`; separately `match_predates_onset=True` | derive | `LOCAL_COMPLETE`; `STALE_MATCH` | Exists and is tested; production passes `None` for both. |
| J10-T08 | dimension mapping | unit | `FailureCharacter` with and without a rival | `spread_dimension` for the two new states | `ASSUMPTION_SPREAD`, `LAYER_SPREAD`; a rival still returns `RIVAL_SPREAD` | Precedence pin. |
| J10-T09 | summarizer shape | unit | Payloads: resolved, partial with `interprocedural_aliasing`, unresolved with `analysis_failed` | `summarize_analysis` | counts by status and class; no code text, value or message anywhere | Security: counts only. |
| J10-T10 | summarizer is total | unit | Empty input; malformed entries; unknown status | call | `None` for none; malformed skipped; no exception | Fail-open for a diagnostic. |
| J10-T11 | pre-`LV_J` pin keeps the row count | unit | `loop_version="lv7"`, history with a failure stamp | `rank_advisory_orders` | `evidence_state == "ROWS_WITHOUT_MATCH"` | Replay-stability pin. |
| J10-T12 | `LV_J` derives from analyzer status | unit | `loop_version=LV_J`, history with a window-class refusal on a banked turn | `rank_advisory_orders` | `evidence_state == "ANALYSIS_INCOMPLETE"`, `gave_up == True`, dimension `ASSUMPTION_SPREAD` | Negative control: hard-code the old derivation and it fails. |
| J10-T13 | history stamp is written | workflow-harness | Execute one GET_CODE turn with a stubbed activity returning `analysis` counts | inspect `action_history[-1]` | contains the `analysis` dict; old entries without it parse to `None` | Temporal-replay safe. |
| J10-T14 | receiver mutation is typed | unit | `List<String> x = new ArrayList<>(); x.add("a"); return x.size();` | `resolve_java_data_flow(x)` | `PARTIAL`, `receiver_mutation_possible` | Probe previously showed `RESOLVED`. |
| J10-T15 | immutable receiver stays resolved | unit | `String x = "a"; return x.trim();` | resolve | `RESOLVED` | Guards against over-abstention. |
| J10-T16 | field and array store | unit | `x.count = 4;` and `x[0] = 5;` | resolve | `receiver_mutation_possible` | |
| J10-T17 | alias store and lambda capture | unit | `this.cache = x;` and `IntSupplier q = () -> x;` | resolve | `alias_escape` and `lambda_capture_not_modeled` | |
| J10-T18 | registry covers the new codes | unit | Registry | `limitation_class` for each new code | `UNSUPPORTED` for the three dataflow codes | Completeness. |

#### Rollout, risks and open owner decisions

One tranche: data-only parts first, then the `LV_J` constant and the pinned derivation, tests first throughout. Risks: (1) over-abstention from D19 (mitigated by the immutable-type list and the immutable-receiver test); (2) a counts-only stamp cannot say which selector failed (acceptable; the cascade needs the character, not the cause); (3) a new loop version for a currently advisory output (accepted so a later consumer is deterministic). Owner decisions: (a) should the advisory ranking drive dispatch (a separate F-series tranche), (b) should `claims_verified` and `match_predates_onset` be wired (needs verify-turn outcomes and onset anchors in the cascade state), (c) the `ANALYSIS_*` to dimension mapping defaults.

---

### Item J11: Spring config merge is profile-blind first-wins (GATED)

**GATED:** Spring items J11-J13 are excluded by a standing owner rule and the J13 in/out decision is unresolved. This section is a complete design for when the gate lifts. Nothing here is implemented, scheduled or assumed approved. Reviewers rated J11 a blocker; the evidence below does not support that rating.

**Status:** Gated, severity should-fix (re-rated, see below). Dependencies: J12 (parsers) should land first. Gating: standing Spring exclusion; adding profile-file reading widens what the importer ingests, so the owner should also confirm that scope.

#### Verified problem

| Claim | Verdict | Evidence |
|---|---|---|
| `setdefault` across every `application*`/`bootstrap*` file, sorted, so `application-dev` precedes `application` | **Not reproducible as stated** | `parse_checkout` filters on `path.name in _SPRING_BASENAMES`, an exact-name set (`application.yml`, `application.yaml`, `application.properties`, `bootstrap.*`). `application-dev.yml` is never read, so a dev value cannot be recorded. Probe on a synthetic checkout confirmed that only exact names are parsed. |
| Profile-blind | Confirmed (differently) | Profile-specific files and profile-gated YAML documents are ignored entirely, so the importer cannot say what any profile sets. |
| First-wins across modules, test resources and bootstrap | Confirmed | Probe: `app-a/.../application.yml` (`service.timeout: 30`) beat `app-b/.../application.yml` (99) and `src/test/resources/application.yml` (1) because `list_files` sorts and `setdefault` keeps the first. `bootstrap.yml` sorts before `application.yml` in the same directory and so wins the same key. Config in `config/` sorts ahead of `src/` and wins by accident of spelling. |
| "Blocker" | **Not supported** | The assembler reads one key from this map, `spring.application.name` (`_cfg("spring.application.name", ...)`); the rest only feeds the `overridden_keys` note. The output is a DRAFT profile that an operator promotes. Severity is should-fix: a wrong service name in a draft, flagged for review. |
| Silent loss | Confirmed (J12 overlap) | A multi-document YAML file parses to `{}` (see J12), so profile-gated documents vanish without a trace. |

#### Design

**D23: Collect entries with provenance; resolve a default view; never pick across modules.**

```python
# src/investigation_agent_platform/integrations/profile_gen/config_files.py
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_NAME = re.compile(
    r"^(?P<layer>application|bootstrap)(?:-(?P<profile>[A-Za-z0-9_.\-]+))?"
    r"\.(?P<ext>ya?ml|properties)$"
)


@dataclass(frozen=True)
class ConfigEntry:
    key: str
    value: Any
    file: str
    module: str            # path prefix before src/main/resources (or config/); "" at the root
    layer: str             # "application" | "bootstrap"
    profile: str | None    # None: the default document of the file
    ext: str               # "properties" outranks "yaml" in one location (Boot reference)


def classify_config_path(rel: str) -> tuple[str, str, str | None, str] | None:
    """(module, layer, profile, ext) for a Spring config file, else None.

    Test resources (a ``src/test`` path) are excluded: they configure tests, not the service.
    """
    parts = rel.split("/")
    if any(parts[i : i + 2] == ["src", "test"] for i in range(len(parts) - 1)):
        return None
    match = _NAME.match(parts[-1])
    if match is None:
        return None
    cut = next((i for i in range(len(parts) - 1) if parts[i : i + 2] == ["src", "main"]), None)
    if cut is None and "config" in parts[:-1]:
        cut = parts.index("config")
    module = "/".join(parts[:cut]) if cut else ""
    return module, match["layer"], match["profile"], "properties" if match["ext"] == "properties" else "yaml"


def default_view(entries: list[ConfigEntry]) -> tuple[dict[str, dict[str, Any]], dict[str, list[ConfigEntry]]]:
    """Per-module effective default-profile values and, per key, the entries that disagree.

    Within one module and layer the default document wins over nothing, and ``.properties`` wins
    over YAML in one location. Across modules nothing is merged: a key with different values in
    different modules is reported as a candidate list and gets no effective value.
    """
    by_module: dict[str, dict[str, ConfigEntry]] = {}
    for e in sorted(entries, key=lambda e: (e.ext != "properties",)):   # properties first, setdefault keeps it
        if e.profile is None and e.layer == "application":
            by_module.setdefault(e.module, {}).setdefault(e.key, e)
    effective = {m: {k: e.value for k, e in kv.items()} for m, kv in by_module.items()}
    conflicts: dict[str, list[ConfigEntry]] = {}
    for module, kv in by_module.items():
        for key, e in kv.items():
            conflicts.setdefault(key, []).append(e)
    conflicts = {k: v for k, v in conflicts.items() if len({str(x.value) for x in v}) > 1}
    return effective, conflicts
```

`parse_checkout` returns `config_entries` (a list) instead of the flat `spring_files`; the new `_SPRING_BASENAMES` check is `classify_config_path(rel) is not None`. The two callers (`api/v1/routers/profiles.py`, `cli/onboard.py`) obtain the assembler's flat `config` from the single module that holds the service (the module of the first detected module, or the only module); when several modules declare different `spring.application.name` values no value is chosen and the report carries `needs_review: ["spring.application.name differs across modules"]`. The derivation report records, for each conflicting key, only the **key name, count and source file and profile**, never values (matching the existing rule that values are never persisted in reports).

**Alternative considered:** key the flat dict by `(file, profile)` and let the assembler pick. Rejected: it moves the ambiguity downstream without resolving it; `default_view` is pure and testable.

**Alternative considered:** apply a supplied active profile and produce one effective map. Deferred: the repository alone cannot say which profile is active; the Config Server path supplies one (`binding.profile`) and J12 fixes how it is used. Repo-derived values stay candidate lists.

#### Version pinning / migration

No loop version: profile generation output is a DRAFT artifact, not loop semantics. The report gains a `config_candidates` section (key, count, sources); existing drafts are unaffected and not rewritten. Generation-time artifacts carry `derivation.notes` naming the resolver version (`config-files-v1`). No migration; IAP is not live, so the flat `spring_files` shape is replaced outright.

#### Test cases

| ID | Name | Level | Setup | Action | Expected | Notes |
|---|---|---|---|---|---|---|
| J11-T01 | profile files are collected, not merged into the default | unit | Synthetic tree: `application.yml` (timeout 30), `application-dev.yml` (5), `application-prod.yml` (60) | `collect` then `default_view` | effective timeout 30; `application-dev` and `application-prod` entries present with their profile tags and absent from the default view | Negative control: treat `profile` as `None` for every file and the effective value changes. |
| J11-T02 | cross-module conflict is a candidate list | unit | `app-a` name `a-svc`, `app-b` name `b-svc` | `default_view` | no effective cross-module value; conflicts list names both files; report has no values | Security: no values in the report. |
| J11-T03 | test resources excluded | unit | `src/test/resources/application.yml` | `classify_config_path` | `None` | |
| J11-T04 | bootstrap is its own layer | unit | `bootstrap.yml` and `application.yml` both set one key | `default_view` | only the `application` layer supplies the effective value | Today `bootstrap.yml` wins by sort order. |
| J11-T05 | properties outrank YAML in one location | unit | `application.yml` and `application.properties` in one module | `default_view` | the properties value wins | Rule verified against the Boot reference (Context7, 2026-10-31). |
| J11-T06 | draft name chosen only when unambiguous | integration | `assemble_profile` fed the single-module and the two-module trees | assert `services[0].name` and `needs_review` | single module uses its name; two modules fall back to `app_id` with the review note | |
| J11-T07 | `config/` is not accidentally first | unit | `config/application.yml` and `src/main/resources/application.yml` in one module | `default_view` | both map to the same module; no sort-order dependence (shuffle input order) | Determinism pin. |
| J11-T08 | negative control for the sort claim | unit | Shuffle `files` order 20 times | `default_view` | identical output every time | If any run differs, the sort dependence is back. |

#### Rollout, risks and open owner decisions

Not scheduled (GATED). When the gate lifts: tests first, then D23 with J12's parsers. Risk: reading profile files increases ingested volume (bounded by the existing 5,000-file and 1,000,000-character caps). Owner decisions: lift the gate; confirm that report candidate lists may name files and profiles (never values).

---

### Item J12: Spring config secondary defects (GATED)

**GATED:** same standing exclusion as J11. Complete design for when the gate lifts; the `javaproperties` dependency in D26 **requires owner approval** on top of the gate.

**Status:** Gated, severity should-fix. Dependencies: none for D24-D25 and D27-D29. Gating: standing Spring exclusion; dependency approval for D26.

#### Verified problem

| Sub-claim | Verdict | Evidence |
|---|---|---|
| `merge_configs` iterates `(repo_defaults, files)` first-wins, so repo defaults beat file values | Confirmed, **latent** | Probe: `merge_configs({}, {"k": "file"}, {"k": "default"})` returns `"default"`. The docstring says "repo-derived defaults fill gaps". No caller passes `repo_defaults` (`cli/onboard.py` and `profiles.py` pass only server and files), so no live effect. |
| `parse_spring_yaml` uses `safe_load`; multi-document profile YAML returns `{}`; `a:` is dropped | Confirmed | Probe: a two-document file returns `{}`; `a:` followed by `b: 2` returns `{"b": 2}`. The bare `except Exception: return {}` makes "unparseable" and "empty" identical. |
| Properties parser misses indexed keys, whitespace separators, continuations | Confirmed | Probe: `routes[0].uri=...` and `svc.name my-svc` are dropped; `long=a\` yields `a\` (continuation not joined). The key regex excludes `[` and `]`. |
| Config Server client flattens per key and mixes lists across sources | Confirmed | Probe: sources `[hi: topics[0]=A]` and `[lo: topics[0]=B, topics[1]=C]` merge to `{topics[0]: A, topics[1]: C}`. Spring replaces the whole list from the highest source that defines any index (Boot reference, Context7: "a dev profile override that replaces the entire list"). Only source **names** are kept, in a flat tuple, with no per-key origin. |
| Derivation defaults a missing profile to `"production"` and does not split comma lists | Confirmed | Probe: no `spring.profiles.active` gives `environment == "production"` even when the binding asked for `dev`; `"dev,prod"` becomes the environment string `dev,prod`; `"prod,metrics"` becomes `production` with `environments == ["prod,metrics"]`. |
| `fnmatch` treats `[*]` as a character class so `routes[0].uri` is rejected | Confirmed | Probe: `is_allowed_property("spring.cloud.gateway.routes[0].uri", defaults)` is `False`; the pattern `routes[*]` only matches the literal text `routes*`. Consequence: gateway-route dependency derivation never receives a route key today. Also, `take_any("...routes")` treats **every** route property value (id, predicates, uri) as a dependency name, which the allowlist currently hides. |

#### Design

**D24: Lower sources fill gaps; they never win.**

```python
# src/investigation_agent_platform/integrations/profile_gen/precedence.py
for source in (files or {}, repo_defaults or {}):   # was (repo_defaults, files)
    ...
```

Order is server, then files, then repo defaults. The `except` fallback (`update` order) is reordered the same way. Pinned by a test that passes a default and a file for one key.

**D25: Multi-document YAML with profile activation, and failure that is distinguishable from empty.**

```python
# src/investigation_agent_platform/integrations/profile_gen/parsers.py
@dataclass(frozen=True)
class YamlDocument:
    profiles: tuple[str, ...] | None   # None: applies to every profile
    expression: bool                   # on-profile is an expression (!, &, |, parentheses) we do not evaluate
    flat: dict[str, Any]


@dataclass(frozen=True)
class YamlParse:
    documents: tuple[YamlDocument, ...]
    error: str | None                  # exception class name; documents are empty when set


def parse_spring_yaml_documents(text: str) -> YamlParse:
    try:
        raw_docs = [d for d in yaml.safe_load_all(str(text or "")) if d is not None]
    except yaml.YAMLError as exc:
        return YamlParse((), type(exc).__name__)
    out: list[YamlDocument] = []
    for doc in raw_docs:
        flat = _flatten(doc, "")
        raw = flat.pop("spring.config.activate.on-profile", None)
        if raw is None:
            raw = flat.pop("spring.profiles", None)   # pre-2.4 key
        tokens = [t.strip() for t in str(raw).split(",") if t.strip()] if raw is not None else []
        expression = any(c in str(raw or "") for c in "!&|()")
        out.append(YamlDocument(tuple(tokens) or None, expression, flat))
    return YamlParse(tuple(out), None)
```

A document whose `on-profile` is an expression is kept, flagged `expression=True`, and excluded from any default view with a `needs_review` note, never evaluated. A key with a `null` value (`a:`) is retained with the value `""` instead of dropped. Parse errors surface as `YamlParse.error`, and `parse_checkout` records a per-file diagnostic instead of treating the file as empty.

**D26: A real `.properties` parser (dependency proposal; requires owner approval).**

```python
# src/investigation_agent_platform/integrations/profile_gen/parsers.py
import re

import javaproperties  # requires owner approval; no in-house parser is written

_DOC_SEP = re.compile(r"(?m)^#---\s*$")


def parse_properties_documents(text: str) -> list[dict[str, str]] | None:
    """One dict per ``#---`` document (Boot multi-document properties); None on a parse error."""
    try:
        return [javaproperties.loads(chunk) for chunk in _DOC_SEP.split(str(text or ""))]
    except ValueError:
        return None
```

`javaproperties.loads` implements Java `.properties` semantics (verified through Context7): `=`, `:` or whitespace separators, backslash line continuation, `\uXXXX` escapes, last value wins within one file. Indexed keys such as `routes[0].uri` pass through unchanged. **Alternative considered:** a ~40-line in-house parser. Rejected under the "prefer existing libraries" rule; if the owner declines the dependency, the in-house parser must cover continuation, escapes and the three separators with the same tests.

**D27: Config Server merge honors list replacement and records per-key origin.**

```python
# src/investigation_agent_platform/integrations/spring_config/client.py
_INDEXED = re.compile(r"^(?P<root>.+?)\[\d+\]")


def merge_property_sources(
    sources: list[tuple[str, dict[str, Any]]],
) -> tuple[dict[str, Any], dict[str, str]]:
    """Highest-precedence source first. Returns (merged, origin by key); names only, no values."""
    merged: dict[str, Any] = {}
    origin: dict[str, str] = {}
    claimed: dict[str, str] = {}          # list root -> the source that owns the whole list
    for name, values in sources:
        owned_here: set[str] = set()
        for key, value in values.items():
            match = _INDEXED.match(key)
            if match is not None:
                root = match["root"]
                if claimed.get(root, name) != name:
                    continue              # a higher source already replaced this list
                owned_here.add(root)
            elif key in merged:
                continue
            merged.setdefault(key, value)
            origin.setdefault(key, name)
        for root in owned_here:
            claimed.setdefault(root, name)
    return merged, origin
```

`ConfigResult` gains `property_origin: dict[str, str]` (key to source name). `SpringConfigClient.fetch` calls this instead of the inline `setdefault` loop.

**D28: The allowlist understands `[*]`.**

```python
# src/investigation_agent_platform/integrations/spring_config/security.py
@lru_cache(maxsize=256)
def _compile(pattern: str) -> re.Pattern[str]:
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("[*]", i):
            out.append(r"\[\d+\]")
            i += 3
        elif pattern[i] == "*":
            out.append(".*")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def is_allowed_property(key: str, patterns: tuple[str, ...]) -> bool:
    return any(_compile(pattern).match(key) for pattern in patterns)
```

The default patterns become `spring.cloud.gateway.routes[*].uri` and `spring.cloud.gateway.routes[*].id`, and the derivation reads only keys ending `.uri` (taking the host of `lb://name` or `http://name`) instead of treating every route value as a dependency. `looks_sensitive` still runs after the allowlist.

**D29: Derive the environment from what was asked, split lists, never default to production.**

```python
# src/investigation_agent_platform/integrations/spring_config/derivation.py
def resolve_active_profiles(props: dict[str, Any], binding_profile: str) -> tuple[list[str], str]:
    raw = props.get("spring.profiles.active")
    tokens = (
        [str(t).strip() for t in raw]
        if isinstance(raw, list)
        else [t.strip() for t in raw.split(",")] if isinstance(raw, str) else []
    )
    tokens = [t for t in tokens if t]
    if tokens:
        return tokens, "spring.profiles.active"
    tokens = [t.strip() for t in str(binding_profile or "").split(",") if t.strip()]
    return tokens, "binding.profile"
```

The environment comes from the first token (prefix rules unchanged); the rest go to `environments`; two or more tokens add `needs_review: "N active profiles; environment taken from the first"`. When no token exists at all, `environment` is `"unknown"` with a `needs_review` entry. The `"production"` default is deleted.

#### Version pinning / migration

No loop version (D4): profile-generation and import artifacts only. The Config Server result shape gains `property_origin`; no stored shape depends on it. IAP is not live, so every old behavior above is replaced rather than flagged.

#### Test cases

| ID | Name | Level | Setup | Action | Expected | Notes |
|---|---|---|---|---|---|---|
| J12-T01 | defaults fill gaps only | unit | `files={"k":"file"}`, `repo_defaults={"k":"default","g":"gap"}` | `merge_configs({}, files, defaults)` | `k == "file"`, `g == "gap"` | Negative control: restore the old order and `k` reads `default`. |
| J12-T02 | server still wins | unit | Same plus `server={"k":"srv"}` | merge | `k == "srv"`, `overridden == ["k"]` | Regression pin. |
| J12-T03 | multi-document YAML | unit | Three documents: default, `on-profile: dev`, legacy `spring.profiles: prod` | `parse_spring_yaml_documents` | three documents; profiles `None`, `("dev",)`, `("prod",)`; the activation keys are not in `flat` | |
| J12-T04 | parse error is not empty | unit | `"a: [unclosed"` | parse | `error == "ParserError"`, no documents | Distinguishes malformed from empty. |
| J12-T05 | null value retained; expression flagged | unit | `a:` and `on-profile: "!prod"` | parse | `a == ""`; document flagged `expression=True` | |
| J12-T06 | properties semantics | unit | `routes[0].uri=http://x`, `svc.name my-svc`, a continued line, `k : v`, a `#---` section | `parse_properties_documents` | all keys present, continuation joined, two documents | **Requires the owner-approved dependency.** |
| J12-T07 | list replacement | unit | Sources `[hi: topics[0]=A]`, `[lo: topics[0]=B, topics[1]=C]` | `merge_property_sources` | merged is `{topics[0]: A}`; `topics[1]` absent | Negative control: use per-key `setdefault` and `topics[1]` reappears. |
| J12-T08 | per-key origin, no values | unit | Same | inspect `property_origin` | keys map to source names; no value anywhere in the origin or logs | Security. |
| J12-T09 | indexed allowlist | unit | `routes[0].uri`, `routes[0].id`, `routes[0].predicates[0]`, `routesX` | `is_allowed_property` | allowed, allowed, rejected, rejected | Edge. |
| J12-T10 | gateway derivation reads only `.uri` | unit | Properties with `id`, `uri` (`lb://order-svc`), `predicates[0]` | `derive_profile` | one dependency, `order-svc`, `critical=False` | |
| J12-T11 | environment derivation | unit | No active profile with binding `dev`; `"dev,prod"`; `"prod,metrics"`; nothing at all | `derive_profile` | `dev`; first token plus review note; `production` plus `["metrics"]`; `unknown` plus review note | Negative control: restore the `"production"` default. |
| J12-T12 | sensitivity unchanged | unit | `spring.datasource.password` and `routes[0].token` | derive | both dropped as sensitive | Regression pin. |

#### Rollout, risks and open owner decisions

Not scheduled (GATED). Risks: (1) the properties dependency (approval); (2) `unknown` environment must be accepted by promotion checks (it is a free string of at most 64 characters today); (3) list replacement can drop entries operators expected to merge, which is Spring's behavior and documented. Owner decisions: lift the gate; approve `javaproperties` or require the in-house parser; confirm `unknown` as the no-evidence environment.

---

### Item J13: Boundary annotation parsing loses the value that matters (GATED)

**GATED:** same standing exclusion, and the owner's J13 in/out decision is unresolved. Complete design only.

**Status:** Gated, severity should-fix. Dependencies: D1. Gating: standing Spring exclusion; no loop version (analyzer data, D4).

#### Verified problem

All five sub-claims reproduced on synthetic sources through `resolve_java_framework_boundary`.

| Sub-claim | Verdict | Evidence |
|---|---|---|
| `@Profile` value dropped | Confirmed | `@Profile("prod") @Bean Client client()` gives `[qualifiers: Profile]` with no `prod`. `_declared_bindings` builds `qualifiers` (with values) but returns `qualifier_names` (names only). |
| Fully-qualified annotations rejected | Confirmed | `@org.springframework.beans.factory.annotation.Value("${app.timeout:30}")` is `UNRESOLVED: no_binding_declaration_in_window`. `_annotations` reads only `identifier` children; a qualified name is a `scoped_identifier`. |
| `@Value("#{...}")` mislabeled `lombok_generated_code` | Confirmed | A SpEL value gives `PARTIAL` with `lombok_generated_code`, because `_boundary_codes` treats every `@Value` without `${` as Lombok. |
| `_string_arg` spans first to last quote | Confirmed | `@GetMapping(value = "/a", produces = "application/json")` renders the route as `/a", produces = "application/json`. |
| `${key:default}` not split | Confirmed | The binding says `config key ${app.timeout:30}` with the key and default fused. |
| Constructors unresolvable | Confirmed | `W(R r)` with `@Autowired` is `UNRESOLVED: ambiguous_symbol` (the class and its constructor share the name). |

#### Design

**D30: Parse annotation arguments from tree nodes.** Annotation arguments are `element_value_pair` nodes (a key identifier and a value) or one bare element value (the implicit `value`). Values are string literals, concatenations, arrays (`element_value_array_initializer`), identifiers or field accesses.

```python
# src/investigation_agent_platform/application/analysis/java/boundary_facts.py
@dataclass(frozen=True)
class AnnotationArg:
    name: str                  # "value" when implicit
    values: tuple[str, ...]    # literal strings only; arrays flatten
    non_literal: bool          # identifier or constant reference that was not resolved


def _annotation_name(modifier: Any, source: bytes) -> str:
    node = modifier.child_by_field_name("name")
    if node is None:
        return ""
    text = _p.node_text(node, source).strip()
    return text.rsplit(".", 1)[-1]          # scoped_identifier: keep the simple name


def _string_values(node: Any, source: bytes) -> tuple[list[str], bool]:
    if node.type == "string_literal":
        return [_unquote(_p.node_text(node, source))], False
    if node.type == "element_value_array_initializer":
        out: list[str] = []
        non_literal = False
        for child in (c for c in node.children if c.is_named):
            values, nl = _string_values(child, source)
            out.extend(values)
            non_literal = non_literal or nl
        return out, non_literal
    if node.type == "binary_expression":    # "a" + "b"
        parts = [_string_values(c, source) for c in node.children if c.is_named]
        if parts and all(not nl and len(v) == 1 for v, nl in parts):
            return ["".join(v[0] for v, _ in parts)], False
    return [], True


def _annotation_args(modifier: Any, source: bytes) -> list[AnnotationArg]:
    args = next((c for c in modifier.children if c.type == "annotation_argument_list"), None)
    if args is None:
        return []
    out: list[AnnotationArg] = []
    for child in (c for c in args.children if c.is_named):
        if child.type == "element_value_pair":
            key = _p.node_text(child.child_by_field_name("key"), source).strip()
            value_node = child.child_by_field_name("value")
        else:
            key, value_node = "value", child
        if value_node is None:
            continue
        values, non_literal = _string_values(value_node, source)
        out.append(AnnotationArg(key, tuple(values), non_literal))
    return out
```

`_declared_bindings` builds each detail from the arg it needs (`value`/`path` for routes, `name` or `value` for beans and components, `prefix` for configuration properties) instead of `_string_arg`. `@Profile` and the other qualifiers emit **values** into the binding as `profiles=[prod]`; the qualifier tuple keeps names and values together.

**D31: Split placeholders and classify `@Value` honestly.**

```python
def split_placeholder(text: str) -> tuple[str, str | None] | None:
    """``${key:default}`` to (key, default); nested placeholders are balanced; None if not one."""
    if not (text.startswith("${") and text.endswith("}")):
        return None
    inner, depth, cut = text[2:-1], 0, None
    for i, ch in enumerate(inner):
        if inner.startswith("${", i):
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == ":" and depth == 0 and cut is None:
            cut = i
    return (inner, None) if cut is None else (inner[:cut], inner[cut + 1 :])
```

A `${...}` value becomes `config_key` with `key` and `default` as separate fields; a `#{...}` value becomes `spel_expression` (registered `RUNTIME`, a PARTIAL boundary code, **not** `lombok_generated_code`); a plain literal becomes `literal_value`. The Lombok test in `_boundary_codes` keeps only the annotation-name and `lombok.` checks (the extra `Value without ${` clause is deleted).

**D32: Name the constructor.** `_find_symbol` accepts `Type#<init>` and `Type#<init>(N)`; a bare `Type` selects the type declaration (not its constructors), removing the class-versus-constructor ambiguity; an overloaded constructor without an arity returns `ambiguous_constructor` (registered `UNSUPPORTED`).

**Alternative considered:** regex over the argument text (the current approach, with the quoting bugs patched). Rejected: it fails on nested parentheses, arrays and concatenation, and it is the root of three of the five defects.

#### Version pinning / migration

No loop version (D4). The structured fact's `bindings` entries gain fields (`profiles`, `key`, `default`) and a `spel_expression` kind; `analysis_method` is unchanged; banked rows keep their old shape and nothing compares them. No migration; the old fused strings are replaced, not aliased.

#### Test cases

| ID | Name | Level | Setup | Action | Expected | Notes |
|---|---|---|---|---|---|---|
| J13-T01 | profile value kept | unit | `@Profile("prod") @Bean Client client()` | `resolve_java_framework_boundary(client)` | binding detail names `profiles=[prod]` | Negative control: return names only and it fails. |
| J13-T02 | profile array | unit | `@Profile({"dev", "test"})` | resolve | `profiles=[dev, test]` | |
| J13-T03 | fully qualified annotation | unit | `@org.example.Value("${app.timeout:30}") int t;` | resolve | `RESOLVED`, key `app.timeout`, default `30` | Regression pin for `scoped_identifier`. |
| J13-T04 | SpEL is not Lombok | unit | `@Value("#{systemProperties['user.home']}")` | resolve | `PARTIAL` with `spel_expression`; `lombok_generated_code` absent | Negative control: restore the clause and the Lombok code returns. |
| J13-T05 | multi-argument route | unit | `@GetMapping(value = "/a", produces = "application/json")` | resolve | route detail is `/a` | |
| J13-T06 | array and concatenated values | unit | `@RequestMapping(path = {"/a", "/b"})` and `@RequestMapping("/api" + "/v1")` | resolve | `/a, /b` and `/api/v1` | Edge. |
| J13-T07 | non-literal argument | unit | `@RequestMapping(Paths.ORDERS)` | resolve | binding present with `non_literal` noted, no fabricated value | Fail-closed on unresolvable constants. |
| J13-T08 | placeholder split | unit | `${a:${b:1}}`, `${a}`, `${a:}`, `plain` | `split_placeholder` | `("a", "${b:1}")`, `("a", None)`, `("a", "")`, `None` | Boundary cases. |
| J13-T09 | constructor selection | unit | `@Autowired W(R r)` and two constructors | resolve `W#<init>(1)` and `W#<init>` | resolved; `ambiguous_constructor` for the overload without arity | |
| J13-T10 | wording guard | unit | Existing banned-wording list | assert no authored word appears in any new detail | passes | Reuses `_AUTHORED_WORDS_BANNED`. |

#### Rollout, risks and open owner decisions

Not scheduled (GATED). Risk: the new fields change what a downstream reader sees in `bindings`; the verifier reads the CONFIGURATION_FACT only structurally (ceiling `LIKELY`), so no verdict path depends on the detail text. Owner decisions: lift the gate; decide whether J13 is in or out of scope at all.

---

### Item JPS: The parser adapter silently drops recoverable AST symbols

**Status:** Open (ledger 2026-10-30, still open at HEAD). Severity should-fix. Dependencies: D9 (shared identity). Gating: none; no loop version (data, D4).

#### Verified problem

`TreeSitterParser._parse_symbols_sync` returns `[]` whenever `root_node.has_error`. Probe: a Java class with three methods where the second has `int b( {` returns `[]`; the same class with the typo fixed returns the class and all three methods. Both callers (`topology/payload.py`, `evidence/code/pipeline.py`) treat `[]` as "no symbols" with no diagnostic, so one typo removes every symbol of a file (up to 512 KB) from the topology graph with nothing recorded.

#### Design

**D25: Return an outcome, keep error-free declarations, say what was skipped.**

```python
# src/investigation_agent_platform/infrastructure/evidence/code/parser.py
@dataclass(frozen=True)
class ParseOutcome:
    symbols: list[CodeSymbol]
    has_errors: bool
    skipped_error_nodes: int


def _parse_symbols_sync(self, code_bytes: bytes, language: str) -> ParseOutcome:
    parser = self._get_parser(language)
    tree = parser.parse(code_bytes)
    has_errors = bool(getattr(tree.root_node, "has_error", False))
    skipped = 0
    ...  # the existing cursor walk, with these two changes:
    #   if node.type in target_types:
    #       ...compute symbol_name and signature as today...
    #       if getattr(node, "has_error", False):
    #           skipped += 1                      # keep descending: its clean children are recoverable
    #       else:
    #           symbols.append(CodeSymbol(...))
    #       parent_stack.append((symbol_name, depth))   # children keep a parent name either way
    return ParseOutcome(symbols, has_errors, skipped)
```

A node that itself contains an error is skipped, but its error-free descendants are kept (a class with one broken method still yields its other methods). `payload.py` records `ParseDiagnostic(file_path=rel, message=f"syntax errors; {n} declaration(s) skipped; recoverable symbols kept", severity="WARNING")`; `pipeline.py` logs the same at warning level. `parse_symbols` returns the outcome; the two callers and `test_infra_coverage` are updated directly (no list-returning shim).

**Alternative considered:** keep the early return and add a diagnostic only. Rejected: the diagnostic would say a file was lost while the clean 95 percent was still recoverable, and the soundness rule used elsewhere (J4: a declaration is trusted only when its own subtree is error-free) already defines what "recoverable" means.

#### Version pinning / migration

No loop version. The topology payload's `parser_version` already carries the pack version; it adopts `grammar.identity` (D9). Previously ingested topologies for broken files stay as they were; a re-ingest picks up the recovered symbols. No migration.

#### Test cases

| ID | Name | Level | Setup | Action | Expected | Notes |
|---|---|---|---|---|---|---|
| JPS-T01 | recoverable symbols survive | unit | Class `OrderServiceHandler` with methods `a`, broken `b`, `c` | `parse_symbols` | symbols include `a` and `c` and exclude `b` (the enclosing class node carries the error, so it is skipped too); `has_errors` is true and `skipped_error_nodes >= 1` | Negative control: restore the early `return []`. |
| JPS-T02 | clean file unchanged | unit | The corrected class | parse | class plus three methods, `has_errors False`, `skipped 0` | Regression. |
| JPS-T03 | parent linkage survives a skipped container | unit | Broken outer class, clean inner method | parse | inner method has `parent_symbol` equal to the outer class name | |
| JPS-T04 | diagnostic reaches the payload | integration | Checkout of one broken and one clean file | `build_payload_for_repo` | one `ParseDiagnostic` WARNING for the broken file; its clean symbols are in `ast_nodes` | |
| JPS-T05 | size guard unchanged | unit | Source above 524,288 bytes | `parse_symbols` | raises `ExecutionError` | Fail-closed regression. |

#### Rollout, risks and open owner decisions

One tranche. Risk: partial symbol sets enter the graph for broken files; mitigated by skipping every erroring node and surfacing a warning. Owner decision: none beyond approving the signature change.

---

### Item JNI: Audit tree-sitter Node identity across `java/` analyzers

**Status:** **Not reproducible as a live defect**; two latent sites. Severity consider. Gating: none; no loop version.

#### Verified problem

py-tree-sitter hands out a fresh `Node` wrapper per access: probe `t.root_node is t.root_node` is `False` while `t.root_node == t.root_node` is `True` and the hashes are equal. So `==`, `in` and set membership are value-based and correct; `is` is always false across accesses. Search over `analysis/java/` and `analysis/python/`: no `id(...)` comparison remains (the `type_facts` fix is the only mention). `==` on nodes is used at `python/framework_facts.py`, `python/control.py` and `python/class_facts.py` and is correct. Two `is` comparisons across separately obtained wrappers exist:

- `type_facts._permits_names`: `descendant.parent is not node` is always true (the intent, excluding the keyword node itself, never held).
- `boundary_facts._with_field_context`: `child is not declaration` is always true, so the declaration's own text is scanned twice when the declaration is a field (harmless duplication today).

`model.py` `bound_node is bound_nodes[0]` compares objects drawn from one list and is correct.

#### Design

**D26: Compare by span, and add a guard.** Both sites call the existing `_p.same_node(left, right)` (type plus byte span). A lint-style unit test scans `analysis/java/` and `analysis/python/` for `id(` calls and for `is`/`is not` between two non-`None` expressions whose names contain `node`, `decl`, `child` or `parent`, with an explicit allowlist for the one list-identity site.

```python
# type_facts._permits_names
if descendant.type == "type_identifier" and not _p.same_node(descendant.parent, node):
# boundary_facts._with_field_context
if child.type == "field_declaration" and not _p.same_node(child, declaration)
```

#### Version pinning / migration

None; identical analyzer output except the removal of duplicate text from a boundary scan. No migration.

#### Test cases

| ID | Name | Level | Setup | Action | Expected | Notes |
|---|---|---|---|---|---|---|
| JNI-T01 | wrapper identity premise | unit | Parse one window | compare `root_node` twice | `is` False, `==` True | Documents the premise the rule rests on. |
| JNI-T02 | sibling-field scan excludes the declaration | unit | Class with fields `RestTemplate rest;` and `int n;`, resolve boundary for `rest` | inspect the analyzed text | the declaration text appears once | Negative control: restore `is not` and it appears twice. |
| JNI-T03 | permits scan | unit | `sealed interface S permits A, B {}` | `_permits_names` | `["A", "B"]` | Regression. |
| JNI-T04 | lint guard | unit | Source scan of the two packages | regex | no `id(` and no unallowlisted node `is` comparison | Negative control: add one and it fails. |

#### Rollout, risks and open owner decisions

Trivial; one tranche. No owner decision. Recommendation: close the ledger entry as "audited, two latent sites fixed".

---

### Item JCV: Java fact claim text/value mismatch

**Status:** Partly addressed. The "omitted `claimed_value`" half is fixed (ledger 2026-10-20 IMPLEMENTED; the code returns `source-value claim has no structured claimed_value`). The "statement asserts a contradictory literal" half is open for method-return phrasing. Severity should-fix. Gating: none; fail-closed tightening of a verifier gate, no loop version (J2 precedent, D4).

#### Verified problem

`java_fact_proof` reconciles the statement with the resolved value only through `_claimed_literal_from_statement`, whose triggers are field phrasing (`default value`, `initial value`, `defaults to`, `assigned to`, ...). Probe: resolved `STANDARD`, structured `claimed_value="STANDARD"`, statement `The one method returns "OTHER" when s is null.` returns `VERIFIED`. The model can therefore ship prose that contradicts the proven value.

#### Design

**D27: Reconcile every literal the statement asserts as a return value.**

```python
# src/investigation_agent_platform/application/investigation/verification.py
_RETURN_LITERAL = re.compile(
    r"(?i)\breturns?\s+(?:the\s+(?:string|value|literal)\s+)?"
    r"(['\"][^'\"]+['\"]|[-+]?\d+(?:\.\d+)?(?:[fFdDlL])?|true|false|null)"
)


@staticmethod
def _asserted_return_literals(statement: str) -> list[str]:
    values = []
    for match in _RETURN_LITERAL.finditer(statement or ""):
        text = match.group(1).strip()
        values.append(text[1:-1] if len(text) >= 2 and text[0] in "'\"" else text)
    return values

# in java_fact_proof, SOURCE_VALUE_FACT branch, after the existing statement check:
if resolution.value_semantics == CodeValueSemantics.GUARDED_RETURN_LITERAL:
    asserted = VerificationEngine._asserted_return_literals(
        str(getattr(hypothesis, "statement", "") or "")
    )
    if any(value != str(resolution.resolved_value) for value in asserted):
        return (False, "claim statement asserts a returned value that conflicts with the resolved value")
```

A statement that names two different returned literals (one per branch) while `claimed_value` names one now fails closed with `INCONCLUSIVE`. That is a deliberate precision cost: such a claim is two claims, and the claim-coverage layer already types one claim per statement. Both sides are compared bare (`_bare_literal`) as today.

**Alternative considered:** reject only when the statement has exactly one literal. Rejected: a two-literal statement is exactly where a contradictory prose value hides.

#### Version pinning / migration

No pin: the change only turns a former `VERIFIED` into `INCONCLUSIVE` for claims whose own prose contradicts the proof; it never flips a verdict toward true. In-flight hypotheses re-verify under the stricter gate at their next verify turn, as with J2. If the owner prefers a pin for verifier tightening, route it with `loop_version_at_least(meta, LV_J)` in the verification activity, as H11 did with lv5; the default recommendation is no pin.

#### Test cases

| ID | Name | Level | Setup | Action | Expected | Notes |
|---|---|---|---|---|---|---|
| JCV-T01 | contradictory returned literal | unit | Method resolving `STANDARD`; statement says it returns `"OTHER"`; `claimed_value="STANDARD"` | `java_fact_proof` | `(False, "...conflicts with the resolved value")` | Negative control: delete the new branch and it returns `(True, "")`. |
| JCV-T02 | consistent statement verifies | unit | Statement returns `"STANDARD"` | proof | `(True, "")` | |
| JCV-T03 | two-literal statement fails closed | unit | Statement returns `"STANDARD"` for null and `"X"` otherwise | proof | `(False, ...)` | Documents the precision cost. |
| JCV-T04 | omitted `claimed_value` still fails | unit | `claimed_value=None` | proof | `(False, "source-value claim has no structured claimed_value")` | Regression for the fixed half. |
| JCV-T05 | field phrasing unchanged | unit | `The default value of x is 999` against a field resolving `3` | proof | `(False, ...)` through the existing check | Regression. |
| JCV-T06 | non-return statements unaffected | unit | Statement with no `returns` phrase | proof | decided by the existing checks only | Edge. |

#### Rollout, risks and open owner decisions

One tranche. Risk: slightly more `INCONCLUSIVE` for multi-literal prose; observable and fail-closed. Owner decision: confirm "no pin" or route under `LV_J`.

---

### Item JLS: `LANGUAGE_SEMANTIC_FACT` verifies at `VERIFIED` while flow, effect and config cap at `LIKELY`

**Status:** Open as a decision record (ledger 2026-10-20). Severity consider. Gating: owner confirmation; code change only if the owner disagrees.

#### Verified problem

`CATEGORY_PROOF_RULES` caps `CONTROL_FLOW_FACT`, `DATA_FLOW_FACT`, `CALL_TARGET_FACT`, `POSSIBLE_EFFECT_FACT` and `CONFIGURATION_FACT` at `LIKELY`, and leaves `DECLARATION_FACT`, `SOURCE_VALUE_FACT` and `LANGUAGE_SEMANTIC_FACT` at `VERIFIED`. Three producers emit `LANGUAGE_SEMANTIC_FACT`: `type_facts` (declared type shapes; its own assumptions say "declared shapes only; no shape is established as taken, run, or observed"), `init_facts` (initialization order from in-window declarations) and Python `mro` (C3 linearization when every base is in the window). Each returns `PARTIAL` when anything it needs is outside the window or version-gated, so a `RESOLVED` record means every input was in the window. These are rules of the language applied to declarations, not claims about what a run did.

#### Design

**D28: Keep the `VERIFIED` ceiling and make the reason executable.** The recommendation is that the ceiling is intended: the category is "what the language rules say about declared source", which source plus the rule version proves; the `LIKELY` categories describe possible behavior or applied configuration, which source alone cannot prove. The change is documentation and a guard, not a ceiling change: a docstring on `CategoryProofRule` states the rationale, and a test pins that every `RESOLVED` `LANGUAGE_SEMANTIC_FACT` record carries `assumptions` that disclaim execution (the existing banned-wording test list) and that `PARTIAL` is used for out-of-window inputs. If the owner prefers a stricter posture, the alternative is `LIKELY` for `init_facts` only (initialization order is the closest of the three to runtime behavior), a one-line change in the rule table plus a producer-specific tier; it is not recommended without evidence of a false `VERIFIED`.

#### Version pinning / migration

None for the recommended path. A ceiling change would be a verifier tightening and follows the JCV routing decision.

#### Test cases

| ID | Name | Level | Setup | Action | Expected | Notes |
|---|---|---|---|---|---|---|
| JLS-T01 | ceiling table pinned | unit | `CATEGORY_PROOF_RULES` | read ceilings | language-semantic `VERIFIED`; control, data, call, effect, configuration `LIKELY` | Read-only pin of the decision. |
| JLS-T02 | resolved records disclaim execution | unit | One resolver output per producer (type, init, mro) | scan assumptions and details | no banned execution wording; `assumptions` non-empty | Negative control: add banned wording to one producer and it fails. |
| JLS-T03 | out-of-window input is partial | unit | Class whose superclass is outside the window | `resolve_java_init_fact` | `PARTIAL`, never `RESOLVED` | Why `VERIFIED` is safe for `RESOLVED`. |

#### Rollout, risks and open owner decisions

Owner decision: confirm the ceiling is intended (recommended) or choose the stricter `init_facts` cap.

---

## Review untested list as golden-fixture tests (review 8.4)

Each bullet of review section 8.4 becomes golden fixtures with **synthetic** Java and YAML only. Java fixtures extend `tests/fixtures/java/` (new files plus manifest entries; a new `resolver: "window"` kind drives `enumerate_java_window_facts`); Spring fixtures live in a new `tests/fixtures/spring_config/` directory as plain files. No fixture names or derives from any analyzed repository. Some bullets are already covered since the review (verified by search); those are listed so the gap list is current, not duplicated.

| Review 8.4 bullet | Status at HEAD | Where it is covered |
|---|---|---|
| No lambda, anonymous-class or nested-enum host attribution in `test_java_window_facts.py` | Lambda, anonymous and local class **covered** (J1); nested enum **open** | `test_tranche4_java_soundness.py` covers the first three; the nested enum is covered by item J5 (enum method attributed to the enum). |
| No same-line declarations | **Covered** (J4) | `test_tranche2_java_guards.py`. |
| No 25-record cap, interface/enum constants, non-ASCII/CRLF/form-feed windows | **Open** | Items J5 and J8 plus the fixtures below. |
| No definite assignment under a conditional or loop | **Covered** (J3) | `test_tranche4_java_soundness.py` (conditional, loop, try). |
| No fixture with a syntax error plus Java 17/21 syntax | **Open** | The first two fixtures below. |
| `parse_spring_yaml`, `parse_properties_file`, `merge_configs`, `parse_value_keys` have no test; no multi-profile or multi-module test; no `@Profile` value test | **Open (GATED items)** | The four Spring fixtures below (staged; assertions wait for the Spring gate). |
| No `source_roots=[]` test for the classifier | **Covered but pinning the contradicted behavior** | `test_s2_wiring.py` asserts the old semantics; replaced by the J9 unknown-roots and renamed-test cases. |
| No test that a path-only head read is rejected by `_is_range_read` | **Covered** (J7) | `test_tranche2_java_guards.py`. |

| ID | Name | Level | Setup | Action | Expected | Notes |
|---|---|---|---|---|---|---|
| JGF-T01 | records, sealed types and patterns with a syntax error | unit | `Shapes17.java`: `record Point(int x, int y)`, `sealed interface Shape permits Circle, Square`, a pattern `instanceof`, a `switch` with `yield`, and one deliberately broken trailing method | `enumerate` and `resolve_java_type_fact` | clean declarations enumerate; the broken method yields nothing; status `degraded` with `java_syntax_error_or_incomplete_source_window`; no `analysis_failed` | Negative control: remove the broken method and the status becomes `ok`. |
| JGF-T02 | Java 21 constructs, unknown language version | unit | Same file with the level unset | resolve type facts | `unsupported_language_version` for the gated constructs, never a guessed parse | |
| JGF-T03 | nested enum, interface constants, enum constants | unit | `Nested.java`: outer `OrderServiceHandler` with `enum Mode`, `interface Limits { int LIMIT = 5; }` and a nested class | `window` resolver | subjects carry the full nesting; interface constant resolves; enum constants yield `enum_constant_not_modeled` markers | Mirrors the J5 nested-subject, interface-constant and enum-marker cases as a manifest case. |
| JGF-T04 | budget and truncation | unit | `Wide.java`: 120 small guarded methods and a trailing field | `window` resolver with `subject_hint` on the field | the field is present; `omitted` is reported | Mirrors the J8 subject-first budget case. |
| JGF-T05 | line separators | unit | `Separators.java` written with CRLF, a form feed and a U+2028 inside a string | `window` resolver and a provider-style numbering helper | records match the LF baseline; cited lines equal the editor lines | Mirrors the J8 git-numbering and CRLF-equals-LF cases. |
| JGF-T06 | Spring multi-profile YAML | unit | `multi_profile/application.yml` with a default document and two `on-profile` documents, plus `application-dev.yml` | `parse_spring_yaml_documents` and `default_view` | three documents; default view uses the default document only | **GATED**; fixture staged, assertion enabled when the gate lifts. |
| JGF-T07 | Spring multi-module precedence | unit | `multi_module/app-a/...`, `app-b/...`, `src/test/resources/...` | `default_view` | conflicts listed by key, no values, test resources excluded | **GATED**. |
| JGF-T08 | Spring properties edge cases | unit | `edge.properties`: indexed keys, whitespace separators, continuation, `#---` sections | properties parser | all keys parsed | **GATED**; needs the owner-approved dependency. |
| JGF-T09 | annotation values from nodes | unit | `Boundaries17.java`: `@Profile`, fully qualified `@Value`, SpEL, multi-argument route, constructor injection | `resolve_java_framework_boundary` | every sub-claim of J13 resolved or typed | **GATED**. |
| JGF-T10 | registry completeness | unit | Source scan of `analysis/java` and `analysis/python` | collect string codes passed to `unresolved(...)`, `limitation_codes=(...)` and `_unresolved(...)` | every code is registered in `limitations.py` | The guard that keeps D1 honest; negative control: add an unregistered code and it fails. |

## Data Storage

No new tables, columns, migrations or stored-shape requirements.

- `ActionOutcome.analysis` and the history `analysis` key (J10) are additive JSON scalars in structures that are already JSONB (workflow history and `retrieval`/outcome payloads); absent on old entries.
- The `window_analysis` attribute (J5) exists only on the in-memory verify-time copy of a banked row; banked rows are never mutated.
- Analyzer payloads keep `CodeFactResolutionV2` unchanged; `structured_fact` gains optional keys (`guard_text`, `path_conditions`, `class_attribution`, `column_unit`); `parser_version` carries a longer identity string (46 characters; the field cap is 64).

## Migrations

None. IAP is not live, so replaced shapes (`java_parser_unavailable:*`, `cross_module_scope`, flat `spring_files`, byte `col_*` names) are replaced outright with no alias, shim or dual reader. Loop-version routing exists for one change only (J10, `LV_J`).

## Testing Philosophy

**Tests first, shown to fail.** Every test case above is written and run red against HEAD before its fix, recorded in the tranche report; the negative control named in each row is the line or condition to break to prove the test can fail. One tranche per owner message.

**Analyzer soundness (J5, J8, J10 dataflow, J13).** Tests use the real tree-sitter grammar on tiny synthetic sources, because the defects live in node shapes (`constant_declaration`, `enum_constant`, `scoped_identifier`, `element_value_pair`) that a mocked tree would not reproduce. Assertions are on typed outputs (subject, status, limitation code, structured fact), never on prose. Fail-open versus fail-closed is stated per case: refusal and failure never become `RESOLVED`, a failure is never an empty list, and an unknown boundary never becomes a handoff.

**Verifier interaction (J5, J8, JCV).** Integration tests drive `validate_java_fact_claim`, `java_fact_proof` and `java_partial_result` over backfilled evidence, because the false-`VERIFIED` defects only exist through the whole path (subject matching plus enumeration). Each fix is pinned both ways: the false claim stops verifying, and the true claim still verifies.

**Grammar and environment (J6).** Loader behavior is tested with fakes (a counting loader, a fake clock) so no test touches the network or the user cache. The cold-cache end-to-end case is a live-level test that requires owner approval and is not part of this tranche. Pin tests are strict `xfail` until the owner approves the dependency change, so intent is recorded without changing dependencies.

**Provider numbering (J8).** One integration test builds a temporary pygit2 repository in `tmp_path` and reads a range through the real provider; everything else is pure. The lint test for stray `splitlines()` makes the atomic file list enforceable.

**Frontier and forcing (J10).** The derivation is pure and tested exhaustively at the table level; the `LV_J` pin is tested both ways (an `lv7` row keeps the row-count state, an `LV_J` row derives from analyzer status), and a workflow-harness test confirms the history stamp survives replay.

**Spring (J11-J13, GATED).** Fixtures and tests are specified now and enabled when the gate lifts. Values never appear in reports or logs, and tests assert that.

**Completeness guards.** The limitation-registry scan, the `splitlines` lint and the node-identity lint turn three classes of silent regression into red builds.

## Documentation Plan

### docs/IAP-Code-Java-lang-impl-design.md

**Audience:** engineers extending the Java analyzers.

Add the typed skip-marker contract (enum constants, switch returns), nested-subject rules, path conditions on guard records, and the stem-attribution assumption; replace any mention of `java_parser_unavailable` and `cross_module_scope`.

### docs/IAP-Code-python-lang-impl-design.md

**Audience:** engineers extending the Python analyzers.

Document the failure helper (`analysis_failed` and `grammar_unavailable`), the shared `grammar` module and the identity string.

### docs/design/DES-collect-frontier-v1.0.md

**Audience:** engineers working on the frontier and the forcing cascade.

Add the two analyzer-derived `EvidenceState` members, the dimension mapping, the statement that the advisory ranking has no dispatch consumer yet, and the owner decisions about `LOCAL_COMPLETE` and `STALE_MATCH` producers.

### docs/design/ (new, GATED) DES-spring-config-precedence-v1.0.md

**Audience:** engineers working on profile generation and the Config Server importer.

Written when the Spring gate lifts: candidate lists, per-module default view, list-replacement semantics, allowlist glob, environment derivation. Not created in this tranche.

### Operations note (requires owner approval to publish as a procedure)

**Audience:** operators deploying workers.

The grammar pre-seed recipe (`download([...])` at build time), the optional `configure(PackConfig(cache_dir=...))` base path, and the startup preflight result.

### docs/IAP-architecture-v1.0.md changes

**Audience:** engineers and reviewers maintaining the analysis layer, verifier and loop.

Add section 72 (next after 71). Section 72, "Analyzer limitations are typed, windows say what they skipped, and the frontier sees the stop (review J5/J6/J8/J9/J10, 2026-10-31)". Body: verdict table; the analyzer, identity and boundary changes that are not loop semantics versus the one that is (`LV_J`); the explicit sentence that the advisory ranking is not consumed by the workflow; the table of new limitation codes with their classes; and the list of tests that pin each guarantee. The loop-version docstring in `domain/investigation/loop_version.py` gains the `LV_J` entry in the same change. The section states plainly which parts are data-only and which are pinned.

## Risks / Trade-offs

### Risk: Stricter subjects reduce verified claims

**Risk:** Qualifying nested members, removing bare subjects and tightening the statement/value check turn some previously `VERIFIED` claims into `INCONCLUSIVE`, which can lower the live pass rate on cases that relied on the loose behavior.

**Mitigation:** Every such change fails closed and was found by a probe that produced a false `VERIFIED`. The tranche report lists each affected claim shape, and any battery comparison remains owner-scheduled (the standing Tranche-6 gate is deferred and is not run here).

### Risk: A wide mechanical diff

**Risk:** Replacing the failure code in ten resolvers, the atomic `splitlines` list and the `Span` rename touch many files; a missed site would surface late.

**Mitigation:** The registry-completeness scan, the `splitlines` lint and the node-identity lint make a miss a red build; the diff is mechanical and reviewed per decision.

### Risk: Dependency and deployment changes

**Risk:** Exact pins, the grammar seed step and the `javaproperties` dependency alter the environment.

**Mitigation:** All three are proposals marked "requires owner approval"; the code paths work without them (the negative cache and typed failure make a cold worker visible without any seeding).

### Risk: A loop version for an advisory output

**Risk:** `LV_J` adds a version for a derivation nothing consumes yet.

**Mitigation:** The pin is what makes a future consumer deterministic; the change is behavior-neutral for dispatch today and only relabels a recorded recommendation. If the owner prefers, the derivation can ship unpinned until a consumer exists, at the cost of a later pin.

### Risk: Prompt shape change for J9

**Risk:** New `boundary` and `roots_status` keys alter the compiled reasoning context and its prompt version.

**Mitigation:** The keys are informational, nothing in the workflow reads dormant templates, and the version suffix bump keeps A/B attribution honest.

### Risk: Gated work drifting

**Risk:** J11-J13 are designed but unscheduled, and J13's in/out status is unresolved.

**Mitigation:** Each item is headed GATED with its reason; nothing in this tranche touches Spring code; the staged fixtures cost nothing until enabled.

## Ledger entries to append

The research brief forbids editing any file except this document, so these entries are proposed for the next session to append to `docs/IAP_Arch_Eng_Recommendations.log` (one line each, dated 2026-10-31):

```text
[2026-10-31] (OPEN) Window enumeration returns an untyped [] for failure — a 1,200-character string constant makes build_java_model raise a pydantic ValidationError and enumerate_java_window_facts return [] for the whole window; execution.py's analysis_failed stamp is not a valid CodeFactResolutionV2 and adds the ANALYZER_RESOLUTION family. Designed in docs/sonnet/Sonnet_J_items_detailed_design.md (J5). Source: group J design.
[2026-10-31] (OPEN) Bare window-enumeration subjects verify wrong-class claims — a method before the first in-window class header gets a bare subject and claims naming WrongClass.helper or SecondaryOne.helper return VERIFIED (offline probe). Raised severity within J8. Source: group J design.
[2026-10-31] (OPEN) Advisory frontier ranking has no consumer — rank_advisory_orders/advisory_orders are written to PostReasonCascadeState and never read by run_investigation.py; seed_from_dormant_templates has no production caller, so J10's frontier consequence is unobservable until a consumer exists. Owner decision: wire or leave. Source: group J design.
[2026-10-31] (OPEN) source_roots ["."] matches nothing — assemble_profile emits ["."] for single-module repos; external_classifier_for_roots and validator._is_within_roots treat "." as a literal prefix (pure-function probe). Security-adjacent; validator change needs owner approval. Source: group J design (J9).
[2026-10-31] (OPEN) TreeSitterParser shares one Parser across asyncio.to_thread workers — py-tree-sitter does not document Parser as thread-safe; parser.py caches one per language per instance. Source: group J design (J6).
[2026-10-31] (OPEN) Method-return statement/value mismatch — a method-return statement asserting a contradictory literal still VERIFIES when claimed_value matches the resolution (field phrasing is checked, return phrasing is not). Source: group J design (JCV).
[2026-10-31] (OPEN) J11 premise corrected — the importer reads only exact basenames, so application-dev.yml is never read; the real defects are flat first-wins across modules/test resources/bootstrap and silent loss of multi-document YAML; only spring.application.name reaches the draft profile, so the blocker rating is not supported. Gated by the Spring exclusion. Source: group J design.
```

## Open Questions for the Owner

1. J10: should the advisory frontier ranking ever drive dispatch (a separate F-series tranche), and should `claims_verified` and `match_predates_onset` be wired so `LOCAL_COMPLETE` and `STALE_MATCH` can be produced?
2. J6: approve the exact pins and the grammar pre-seed step; hard-fail or log-and-continue when the startup preflight finds a missing grammar?
3. J9: approve aligning the validator with the whole-repository `"."` root (security-adjacent), and confirm `"."` means the whole repository.
4. J8: should an owner inferred from the file name require corroboration (a window containing the class header) before it may verify?
5. J5: may multi-guard methods be disambiguated by claimed value or claimed guard (a verifier-semantics change)?
6. JCV: no pin (recommended, J2 precedent) or route under `LV_J`?
7. JLS: confirm the `VERIFIED` ceiling for `LANGUAGE_SEMANTIC_FACT`.
8. Spring: lift the J11-J12 gate? Is J13 in or out? Approve `javaproperties` or require an in-house parser?
