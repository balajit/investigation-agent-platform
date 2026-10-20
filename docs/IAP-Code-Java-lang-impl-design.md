# Design: IAP Code Java Language Implementation

**Status:** Phases 0–8 landed in the working tree (uncommitted) per this design; the gates below were the acceptance record. Phases remain candidate-gated for any rework: no metric may improve by weakening a verifier gate.

## Context

The project has a working deterministic Java field-value path on top of the existing investigation pipeline. The baseline establishes one narrow semantic contract and proves it end to end. The design below is proposed work, not a description of implemented capabilities.

What exists today, verified against the working tree:

- `src/investigation_agent_platform/application/analysis/java_field_facts.py` (tracked at review) exposes `resolve_java_field_fact(evidence, field_name, declaring_type=None)`. It reads no files and follows no references. Its input is one revision-pinned `GET_CODE`/`GET_SOURCE`/`READ_FILE` range-read `Evidence` with numbered `NNN| ...` lines. It parses the window with `tree-sitter-language-pack` Java grammar, finds exactly one ordinary-class `field_declaration`/`variable_declarator` matching `field_name`, normalizes a conservative literal subset via `_literal_value`, applies Java field defaults when no initializer is present, and returns `CodeFactResolution` with `RESOLVED`, `PARTIAL` (unsupported initializer), or `UNRESOLVED` (fail-closed limitation codes). It does not validate contiguous line numbering or Java literal grammar fully; those are confirmed gaps below.
- `src/investigation_agent_platform/domain/code/models.py` defines `CodeFactStatus` (`RESOLVED`/`PARTIAL`/`UNRESOLVED`), `CodeValueSemantics` (`EXPLICIT_INITIALIZER`, `LANGUAGE_DEFAULT`, `POST_CONSTRUCTION`, `LATER_MUTABLE`, `RUNTIME_EFFECTIVE`), and `CodeFactResolution` as a frozen ephemeral model carried with evidence, never a persistence aggregate. `RESOLVED` requires `resolved_value` plus immutable revision and path provenance.
- `src/investigation_agent_platform/application/worker/activities.py` (`execute_action_activity`, GET_CODE branch) calls `resolve_java_field_fact` when `field_name` is present in action parameters, stores the dumped resolution in both `merged_attributes["code_fact_resolution"]` and `outcome_detail["code_fact_resolution"]`, then persists the evidence row.
- `src/investigation_agent_platform/domain/evidence/models.py` validates `attributes["code_fact_resolution"]` against `CodeFactResolution` shape on `Evidence` validation.
- `src/investigation_agent_platform/application/investigation/verification.py` adds `claims_field_value`, `_resolution_from_evidence`, `code_value_proof`, and `validate_code_value_claim`. Proof requires a `RESOLVED` resolution bound to cited evidence, matching `value_semantics`, exact `claimed_value == resolved_value`, field-name and declaring-type match, bound evidence-ID intersection, same revision and path, a range read whose window contains the declaration range and claimed line. `VerificationResult.code_value_validated` marks the tier.
- `src/investigation_agent_platform/domain/analysis/question_shape.py` routes source-value questions (`default value of`, `field initializer`, `initialized to`, `what value does`, and similar) to `code` shape unless a runtime marker (`in production`, `at runtime`, `effective runtime`, and similar) is present.
- `src/investigation_agent_platform/domain/hypothesis/models.py` (`ClaimTarget.declaring_type/value_semantics/claimed_value`, `HypothesisEvidenceAssessment.value_semantics/claimed_value`) and `src/investigation_agent_platform/ports/reasoning/reasoner.py` (`DecisionClaim.value_semantics/claimed_value`) carry the typed claim through reason to workflow to `FORMULATE`.
- `tests/unit/test_java_field_facts.py` (tracked at review) covers explicit literals and defaults, multi-declarator shared types, signs and bases, local-variable rejection, partial on method-call initializer, ambiguity, wrong declaring type, non-Java path, path-only rejection, and missing-revision fail-closed behavior.

Beyond field initializers and primitive/reference field defaults, no local-variable, parameter, constructor-chain, control-flow, data-flow, method-dispatch, exception-path, generics, lambda, stream, annotation, reflection, DI, or runtime-observation resolution exists in the current Java analyzer. The current `CodeFactResolution` is field-shaped (`field_name`, `field_type`, `field_category`) and cannot express a local, a call target, a branch, or a possible effect without extension. The verifier only proves `EXPLICIT_INITIALIZER` and `LANGUAGE_DEFAULT` field-value facts.

This design generalizes the field baseline across the full practical anatomy of Java while preserving the Evidence Gateway, code-intelligence providers, context compiler, verifier, and persistence paths. No new agent framework, no new infrastructure, no schema migration unless a concrete consumer demands it.

## Review Verdict and Confirmed Gaps

The current field-fact path is a useful narrow baseline, not a general Java semantic analyzer. The highest-risk design gaps found in review are: stale as-built statements that call tracked files uncommitted; incorrect compatibility claims for strict Pydantic readers; Java initialization stages being collapsed into one order; a call-target example that chooses by arity rather than Java overload applicability; Python literal parsing proposed for Java literals (and already allowing at least one non-Java spelling in the current analyzer); proof rules that do not say what evidence establishes each fact category; and a roadmap that did not make the prompt's full A–K scope auditable. Additional confirmed correctness gaps include non-contiguous source-line handling, weak declaring-type matching, invalid Java literal acceptance, and missing authoritative Java source-level provenance. Phases below remain candidates and need go/no-go review before implementation.

| Finding | Status | Required design correction |
| ------- | ------ | -------------------------- |
| Baseline source/tests are described as untracked although Git tracks them. | Confirmed | Corrected in Context; code and tests are tracked at review. |
| The initial draft referenced `docs/JAV_PROMPT.md` but lacked a direct A–K status matrix and did not reflect all mandatory rules. | Confirmed | Added the matrix below and explicitly mapped each area to candidate phases. |
| New aliases do not make new serialized shapes readable by current `extra="forbid"` model; emitted aliases and current field names also need an explicit rollout contract. | Confirmed | Specified legacy-read compatibility separately from forward compatibility and reader-first rollout/versioned payloads. |
| Static class initialization, instance construction, and constructor delegation are not one textual sequence. | Confirmed | Separated class initialization from per-instance construction and encoded superclass/`this(...)` delegation as distinct traces. |
| The call-target sketch selects a unique overload using only method name and arity. | Confirmed | Require Java invocation applicability/resolution, including conversions and varargs, or return ambiguous/unresolved. |
| Python `ast.literal_eval` and Python numeric prefixes are not Java lexical/constant-expression semantics. | Confirmed | Replaced Python parser sketch with a Java-AST/JLS contract; fail closed on unsupported forms. |
| Fact category alone does not describe the evidence needed to establish the fact or its verdict ceiling. | Confirmed | Replaced placeholder proof table with explicit evidence, scope, status, and verdict requirements. |
| Current claim target/assessment types do not carry `fact_category`, despite the proposed text implying they do. | Confirmed | Specified the field as a proposed contract change and required old assessment replay tests. |
| Current `_literal_value` accepts Python-style `0o` prefixes as octal, although Java integer literals use the Java lexical grammar and do not have that prefix. | Confirmed | Require rejecting non-Java spellings and pin negative fixtures for Python-only spellings. |
| The proposed `target_identity` and line-only `SourceLocation` do not guarantee stable symbol identity or distinguish multiple declarations on the same line. | Confirmed | Added a canonical qualified symbol identity and exact source spans; require same-line and cross-provider identity fixtures. |
| The source-line adapter permits skipped, duplicate, or out-of-order line numbers, then the resolver concatenates text and computes declaration lines by offset. | Confirmed | Require contiguous, ordered, unique source lines or treat gaps as parse barriers; never compute offsets across missing lines. |
| `declaring_type` matching uses string `endswith`, so a requested simple name can match an unrelated longer type name; package identity is not included in the extracted class name. | Confirmed | Use segment-aware qualified-name matching and explicit package/nesting identity; ambiguity or unavailable package scope must abstain. |
| Current integer normalization does not enforce Java literal grammar/type ranges and accepts octal `0o`; Python float/string/char parsing does not preserve Java lexical semantics. | Confirmed | Resolve parsed Java literal nodes with source-level-aware grammar, type/range checks, and Java escape/overflow rules; include invalid/out-of-range counterexamples. |
| The sketch lists `language_version` among GET_CODE action parameters without establishing who is authoritative for it. | Confirmed | Select source level and preview flags from trusted repository/build evidence; treat action/model-supplied levels as untrusted hints. |
| The proposed generalized model still requires field name/type and declaration location for call, flow, and runtime fact categories. | Confirmed | Made legacy field/declaration fields optional, added subject identity and structured fact payload, multiple exact spans, and separate runtime observation identity/time. |

### Java coverage matrix (as of 2026-10-20 review)

The labels describe the present Java-specific analyzer, not the proposed phases: **partial** means a narrow supported subset exists; **missing** means no Java fact resolver for the area; **unverified** means current generic infrastructure may provide related evidence but the Java semantic capability was not established in this review.

| Prompt area | Current status | Present subset / boundary | Candidate phase |
| ----------- | -------------- | ------------------------- | --------------- |
| A. Declarations and initialization | Partial | Ordinary class fields, explicit supported literals, and primitive/reference defaults; locals, parameters, blank finals, order, constructors, inheritance, enums, and records missing. The present analyzer does not reject gaps/duplicate/out-of-order numbered source lines before deriving ranges. | 2–3 |
| B. Types and type relationships | Partial | Field type text and nested ordinary class name are extracted; package-qualified identity, type checking, inheritance, visibility, generics, modules, conversions, and dispatch missing. Declaring-type matching uses suffix matching rather than exact segment identity. | 2, 4, 6 |
| C. Expressions and value computation | Partial | Conservative literals only; numeric grammar/type-range validation, Java escape/text-block semantics, operators, compile-time constants, overflow, and evaluation order missing. The existing normalization has Python-specific parsing behavior. | 2 |
| D. Methods and dispatch | Missing | No Java invocation resolver; static call graph evidence is a separate structural provider and is not Java semantic proof. | 4 |
| E. Control flow and data flow | Missing | No Java branch/path/reaching-definition resolver. | 4–5 |
| F. Exceptions and failure propagation | Missing | No Java exception-selection/propagation resolver; generic exception evidence does not prove Java source behavior. | 5 |
| G. Collections, arrays, and object state | Missing | No alias, mutation, collection-content, or stream semantics. | 4–7 |
| H. Lambdas and asynchronous execution | Missing | No capture, scheduling, happens-before, or observed-execution resolver. | 6–7 |
| I. Annotations, reflection, and code generation | Missing | No Java framework/generated-source resolver. | 6–7 |
| J. Resources, modules, and external interactions | Missing | No Java-specific resource/transaction/configuration/external-effect proof. | 5, 7 |
| K. Build and language-version semantics | Unverified | No demonstrated resolver from build metadata to source level, preview flags, generated source, or deployed artifact identity. | 6–8 |

The existing investigation path already provides candidate integration points: question classification in `domain/analysis/question_shape.py`; `GET_CODE` and the current field hook in `application/worker/activities.py`; evidence shape validation in `domain/evidence/models.py`; typed claims in `domain/hypothesis/models.py` and `ports/reasoning/reasoner.py`; code packet construction in `application/investigation/context_compiler.py`; and verification in `application/investigation/verification.py`. Reuse these; each phase must verify the current call path and authorization/revision behavior before changing it.

### Fact and claim compatibility

The prompt taxonomy can be represented as a `fact_category`, but existing `ClaimKind` describes a different axis (DEFINITION, CALL_EDGE, BEHAVIOR, ABSENCE, CAUSAL). Keep these independent: retain `ClaimKind` for claim/evidence binding, and add optional validated `fact_category` to both `ClaimTarget` and `HypothesisEvidenceAssessment` plus `DecisionClaim`. Do not infer one from the other or from prose. Stored assessments with no category remain legacy/untyped and cannot gain a new Java proof rule by default. A fact category is not by itself a verification result: each rule also requires source/runtime evidence, investigation scope, exact revision/path/range binding, permissible status, and a final-verdict ceiling.

**Smallest next safe increment:** Phase 0, limited to reconciling this design with tracked source/tests and defining a versioned compatible resolution contract. Proceed to Phase 1 only after counterexamples prove that every new category is rejected by default until its own evidence rule is implemented. Do not implement broader Java resolvers as part of this review.

## References

- [docs/JAV_PROMPT.md](./JAV_PROMPT.md) — mandatory practical Java areas A–K, taxonomy, contract, verifier requirements, candidate phases, evaluation strategy, and required review output. The coverage matrix and phase plan below map to it; phases remain proposals, not authorization to implement.
- [docs/IAP-architecture-v1.0.md](./IAP-architecture-v1.0.md) — As-built architecture: Evidence Gateway, code-intelligence and Git providers, context compiler, reasoning loop, verification gates, worker activities and workflows. Primary source of truth for call paths this design reuses.
- [docs/DS_REVIEW_v0.1.md](./DS_REVIEW_v0.1.md) — Code-grounding target, revision, evidence, and verification contracts plus phased file-level plan this design extends to Java semantics.
- [Tree-sitter Java grammar](https://github.com/tree-sitter/tree-sitter-java) — Node types (`field_declaration`, `variable_declarator`, `local_variable_declaration`, `method_declaration`, `method_invocation`, `object_creation_expression`, and similar) informing the AST extraction layer.
- [Java Language Specification, Java SE 21](https://docs.oracle.com/javase/specs/jls/se21/html/index.html), especially [§3 lexical structure](https://docs.oracle.com/javase/specs/jls/se21/html/jls-3.html), [§4.12.5 initial values](https://docs.oracle.com/javase/specs/jls/se21/html/jls-4.html#jls-4.12.5), [§12.4 class initialization](https://docs.oracle.com/javase/specs/jls/se21/html/jls-12.html#jls-12.4), [§12.5 instance creation](https://docs.oracle.com/javase/specs/jls/se21/html/jls-12.html#jls-12.5), [§15.12 method invocation](https://docs.oracle.com/javase/specs/jls/se21/html/jls-15.html#jls-15.12), and [§16 definite assignment](https://docs.oracle.com/javase/specs/jls/se21/html/jls-16.html) — normative basis for defaults, lexical literals, initialization, overload resolution, and definite assignment. Confirm the supported source level per repository before applying these Java SE 21 rules.
- [Pydantic v2 model validation](https://docs.pydantic.dev/latest/) — Informs the frozen ephemeral `CodeFactResolution` extension pattern and `Evidence.attributes` shape validation.

## Goals / Non-Goals

**Goals:**

- Smallest safely rolled-out `CodeFactResolution` extension that expresses declarations, source values, language semantics, control flow, data flow, call targets, possible effects, configuration bindings, and runtime observations with provenance and fail-closed statuses; causal claims remain verifier conclusions rather than analyzer outputs.
- Layered deterministic analyzer package under `src/investigation_agent_platform/application/analysis/java/` reusing the field baseline: source acquisition, identity normalization, AST extraction, local semantic evaluation, bounded relationship analysis, resolution assembly, claim verification, conclusion enforcement.
- Coverage matrix for mandatory Java areas A through K with per-area status (implemented, partial, missing, or unverified) and candidate phase acceptance gates mapped to `docs/JAV_PROMPT.md` phases 0 through 8.
- Verifier integrity: no location-only promotion, no initializer-as-runtime promotion, no possible-as-observed promotion, revision-mismatch fail-closed, exact value comparison, no brittle substring proof.
- Revision-pinned synthetic Java fixtures with positive, negative, ambiguous, inherited, shadowed, and dynamically unresolved cases plus counterexamples where the location is valid but the interpretation is false.
- No new persistence tables or REST/CLI surfaces are proposed; resolutions remain ephemeral on existing Evidence unless a concrete consumer and compatibility analysis justify change.

**Non-Goals:**

- General symbolic execution, arbitrary application execution, unsandboxed project builds, or full-program whole-repo points-to analysis.
- New agent framework, generic plugin system, separate reasoning engine, gateway rebuild, verifier duplicate, or broad platform rewrite.
- Runtime tracing, log-derived execution proof, debugger attachment, or production-state observation beyond declaring the `RUNTIME_OBSERVATION_FACT` boundary and its required evidence shape.
- Framework-specific deep integrations (DI container startup, persistence provider internals, message-broker semantics) beyond configuration-binding facts and explicit unresolved boundaries in Phase 7.
- Java version upgrade, bytecode analysis, annotation-processor execution, or build-system changes. Analysis consumes trusted repository/build metadata for source level; if that metadata is absent or contradictory, it abstains rather than trusting a requested/model-supplied level.

## Proposed Decisions

### D1: Keep resolution ephemeral and extend the contract with a safe rollout

**Decision:** Keep `CodeFactResolution` frozen and ephemeral, attached to existing evidence. The design proposes a versioned contract expansion, not transparent bidirectional compatibility. The current model uses `extra="forbid"`, requires `field_category`, and validates the old shape; a new payload with `symbol_name`, `fact_category`, or other extra fields will not validate in the current process. Pydantic aliases alone cannot make an old reader understand fields it does not define. Before emitting a generalized payload, update all readers and validators in a reader-first rollout, or write it under a distinct versioned key while continuing to emit the legacy `code_fact_resolution` shape. For in-place migration, keep `field_name` and `field_type` as canonical serialized fields and add optional general fields; if canonical names change, explicitly support both input spellings with `validation_alias=AliasChoices(...)`, serialize a documented canonical spelling, and reject conflicting duplicate spellings. Do not claim old-reader compatibility for generalized payloads.

The expanded contract has `fact_category`, `semantic_stage`, `analysis_method`, nullable `language_version`, `assumptions`, and `conflicting_evidence_ids`; optional identity/relationship fields include a canonical qualified `subject_identity`, `node_id` when available, `target_identity`, `structured_fact`, multiple exact source spans with start/end line and column (or byte offsets), `control_flow_refs`, `data_flow_refs`, and `call_target`. Legacy `field_name`/`field_type` remain accepted during migration but are not mandatory for non-field facts. Declaration locations are optional and source-location collections may be empty only for non-source facts. Unknown repository, revision, path, or range must remain explicitly unknown with a limitation code; unresolved output must not fabricate provenance merely to satisfy a schema. Every known provenance component and investigation/evidence binding is retained. A `RESOLVED` source fact requires immutable revision, path, exact span, and supporting evidence IDs. Runtime observations require their own runtime-source identity and observation time and cannot be represented as source-only resolutions. A causal claim is a verifier conclusion over evidence, not a deterministic source-analysis resolution.

```python
# src/investigation_agent_platform/domain/code/models.py
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from investigation_agent_platform.domain.provenance.models import SourceLocation


class CodeFactStatus(StrEnum):
    RESOLVED = "RESOLVED"
    PARTIAL = "PARTIAL"
    UNRESOLVED = "UNRESOLVED"


class CodeValueSemantics(StrEnum):
    EXPLICIT_INITIALIZER = "EXPLICIT_INITIALIZER"
    LANGUAGE_DEFAULT = "LANGUAGE_DEFAULT"
    POST_CONSTRUCTION = "POST_CONSTRUCTION"
    LATER_MUTABLE = "LATER_MUTABLE"
    RUNTIME_EFFECTIVE = "RUNTIME_EFFECTIVE"


class JavaFactCategory(StrEnum):
    DECLARATION_FACT = "DECLARATION_FACT"
    SOURCE_VALUE_FACT = "SOURCE_VALUE_FACT"
    LANGUAGE_SEMANTIC_FACT = "LANGUAGE_SEMANTIC_FACT"
    CONTROL_FLOW_FACT = "CONTROL_FLOW_FACT"
    DATA_FLOW_FACT = "DATA_FLOW_FACT"
    CALL_TARGET_FACT = "CALL_TARGET_FACT"
    POSSIBLE_EFFECT_FACT = "POSSIBLE_EFFECT_FACT"
    CONFIGURATION_FACT = "CONFIGURATION_FACT"
    RUNTIME_OBSERVATION_FACT = "RUNTIME_OBSERVATION_FACT"
    CAUSAL_CLAIM = "CAUSAL_CLAIM"


class SemanticStage(StrEnum):
    DECLARATION = "DECLARATION"
    CLASS_PREPARATION = "CLASS_PREPARATION"
    CLASS_INITIALIZATION = "CLASS_INITIALIZATION"
    INSTANCE_DEFAULTING = "INSTANCE_DEFAULTING"
    INSTANCE_INITIALIZATION = "INSTANCE_INITIALIZATION"
    CONSTRUCTOR_BODY = "CONSTRUCTOR_BODY"
    POST_CONSTRUCTION = "POST_CONSTRUCTION"
    LATER_MUTATION = "LATER_MUTATION"
    RUNTIME_EFFECTIVE = "RUNTIME_EFFECTIVE"


class CodeFactResolution(BaseModel):
    """Ephemeral deterministic source-level fact.

    Carried with existing evidence; never a persistence aggregate.
    A RESOLVED value describes the named (category, semantics, stage) only.
    """

    # Proposed shape only. Keep field_name/field_type as serialized keys during
    # migration; do not claim old strict readers accept new fields.
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: CodeFactStatus
    repository: str | None = Field(default=None, max_length=256)
    revision: str | None = Field(default=None, max_length=128)
    fact_category: JavaFactCategory
    semantic_stage: SemanticStage
    declaring_type: str | None = Field(default=None, max_length=512)
    field_name: str | None = Field(default=None, min_length=1, max_length=256)  # legacy
    field_type: str | None = Field(default=None, min_length=1, max_length=256)  # legacy
    subject_identity: str | None = Field(default=None, max_length=1024)
    field_category: Literal["INSTANCE_FIELD", "STATIC_FIELD"] | None = Field(default=None)
    value_semantics: CodeValueSemantics
    analysis_method: str = Field(default="tree-sitter-range-v1", max_length=128)
    language_version: str | None = Field(default=None, max_length=32)
    raw_initializer: str | None = Field(default=None, max_length=2048)
    resolved_value: str | None = Field(default=None, max_length=2048)
    structured_fact: dict[str, Any] | None = None
    node_id: UUID | None = None
    target_identity: str | None = Field(default=None, max_length=1024)
    source_locations: tuple[SourceLocation, ...] = ()
    source_spans: tuple[tuple[int, int, int, int], ...] = ()  # start/end line and column
    runtime_source_identity: str | None = Field(default=None, max_length=1024)
    runtime_observed_at: datetime | None = None
    control_flow_refs: tuple[str, ...] = ()
    data_flow_refs: tuple[str, ...] = ()
    call_target: str | None = Field(default=None, max_length=1024)
    source_evidence_ids: tuple[UUID, ...] = ()
    conflicting_evidence_ids: tuple[UUID, ...] = ()
    declaration_location: SourceLocation | None = None  # legacy single-declaration view
    limitation_codes: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_resolution(self) -> "CodeFactResolution":
        # Category-specific invariants are specified in the verifier contract;
        # they must not be reduced to a generic non-None value check.
        return self
```

This sketch is not a migration implementation. Phase 0 must add round-trip tests for legacy banked dictionaries, new payload validation, serialization spelling, current evidence validation, and downgrade behavior before selecting in-place expansion versus a versioned key. The verifier—not this general model validator—enforces category-specific proof rules. `CAUSAL_CLAIM` remains outside deterministic resolution.

**Alternative considered:** New model per fact category (ten models). Rejected — multiplies verifier branches, evidence validation, and worker plumbing for no consumer benefit; one discriminated model with `fact_category` covers the matrix.

---

### D2: Layer the analyzer as a package, keep the field baseline as layer 1

**Decision:** Move `java_field_facts.py` logic into `src/investigation_agent_platform/application/analysis/java/` as thin re-export shims plus new modules, one per prompt layer. Each module is pure (typed evidence object in, resolution out), reads no files, follows no network references, and returns fail-closed `UNRESOLVED`/`PARTIAL` with machine-readable `limitation_codes`. Shared provenance gating and Tree-sitter parsing live in `_provenance.py` and `_parse.py` so every phase inherits revision pinning.

```python
# src/investigation_agent_platform/application/analysis/java/__init__.py
"""Deterministic Java fact resolution layers."""

from investigation_agent_platform.application.analysis.java.field_facts import (
    resolve_java_field_fact,
)
from investigation_agent_platform.application.analysis.java.local_facts import (
    resolve_java_local_fact,
)
from investigation_agent_platform.application.analysis.java.method_facts import (
    resolve_java_call_target,
)
from investigation_agent_platform.application.analysis.java.flow_facts import (
    resolve_java_control_flow,
)

__all__ = [
    "resolve_java_field_fact",
    "resolve_java_local_fact",
    "resolve_java_call_target",
    "resolve_java_control_flow",
]
```

```python
# src/investigation_agent_platform/application/analysis/java/_provenance.py
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from investigation_agent_platform.domain.evidence.models import EvidenceType


@dataclass(frozen=True)
class ParsedLine:
    number: int
    text: str


def java_source_lines(evidence: Any) -> tuple[list[ParsedLine], str | None]:
    """Return verified numbered source lines or a fail-closed reason."""
    provenance = getattr(evidence, "provenance", None)
    fingerprint = getattr(provenance, "query_fingerprint", None)
    location = getattr(provenance, "source_location", None)
    attrs = getattr(evidence, "attributes", None) or {}
    operation = str(getattr(fingerprint, "operation", "") or "").upper()
    revision = str(getattr(location, "revision", "") or "")
    start = getattr(location, "line_start", None)
    end = getattr(location, "line_end", None)
    if (
        str(getattr(getattr(evidence, "evidence_type", None), "value", "")).upper()
        != EvidenceType.SOURCE_CODE.value
    ):
        return [], "not_source_code_evidence"
    if operation not in {"GET_CODE", "GET_SOURCE", "READ_FILE"}:
        return [], "not_a_source_read"
    if not isinstance(attrs, dict) or attrs.get("read_mode") not in (None, "range"):
        return [], "source_read_not_range_pinned"
    if attrs.get("discovery_only"):
        return [], "source_read_is_discovery_only"
    if not revision or revision in {"HEAD", "UNPINNED"} or not getattr(location, "file_path", None):
        return [], "missing_immutable_source_provenance"
    if not isinstance(start, int) or not isinstance(end, int) or start < 1 or end < start:
        return [], "missing_source_line_window"
    if attrs.get("truncated"):
        return [], "source_range_truncated"
    parsed: list[ParsedLine] = []
    for raw in str(getattr(evidence, "content_snippet", "") or "").splitlines():
        match = re.match(r"^\s*(\d+)\| ?(.*)$", raw)
        if not match:
            return [], "source_range_missing_numbered_lines"
        parsed.append(ParsedLine(number=int(match.group(1)), text=match.group(2)))
    if not parsed or parsed[0].number < start or parsed[-1].number > end:
        return [], "source_lines_outside_provenance_window"
    return parsed, None


def repository_of(identifier: str, source_path: str) -> str:
    if source_path and identifier.endswith("/" + source_path):
        return identifier[: -(len(source_path) + 1)]
    if identifier == source_path:
        return ""
    return identifier
```

The existing `application/analysis/java_field_facts.py` becomes a deprecated re-export so the worker hook and tests keep importing during migration. New code imports from the package. The move is gated on import-cycle checks and unchanged worker/evidence validation behavior.

**Alternative considered:** One monolithic `java_facts.py`. Rejected — the coverage matrix has eleven areas with different Tree-sitter node types and different verifier rules; a single module becomes unreviewable by Phase 3.

---

### D3: Resolve Java declarations and safe expressions before anything else

**Decision:** Phase 2 implements locals, parameters, `var` inference anchors, JLS compile-time constant variables, and a conservative expression evaluator. Only explicitly supported side-effect-free expressions evaluate: literals, parenthesized literals, unary and binary operators over constants, string concatenation of constants, ternary over constant conditions, and casts under Java conversion rules. A local being effectively final does not make it a compile-time constant. Anything with a method call, `new`, lambda, field access outside the window, array access with non-constant index, or unknown identifier returns `PARTIAL` with `unsupported_initializer_expression`. This generalizes the current `_literal_value`, not Python expression semantics.

```python
# src/investigation_agent_platform/application/analysis/java/expressions.py
from __future__ import annotations

from typing import Any


def evaluate_java_constant(node: Any, source_level: int | None) -> str | None:
    """Evaluate only a whitelisted Java AST expression under JLS semantics.

    Parse literal nodes after Java lexical translation; preserve Java numeric
    widths, unary signs, overflow, promotion, escapes, text blocks, and
    conditional/evaluation rules. Unknown source level or unsupported syntax
    returns None. Never parse Java with Python's ast or execute snippets.
    """
    raise NotImplementedError
```

This is an interface sketch, not executable implementation. Java octal integer literals use a leading `0` (not Python's `0o` spelling), signs are unary operators rather than part of integer-literal tokens, underscores are permitted only in specified positions, floating-point values include infinities/NaN through supported operations, and Java char/string escapes, Unicode escapes, and text blocks are not Python string-literal syntax. Parse Java AST literal kinds, then evaluate only explicitly supported nodes with JLS type-promotion, narrowing, overflow, and evaluation-order behavior. Compile-time constant variables require the JLS definition and compile-time-constant expression rules, not merely `final` or effectively-final. Unknown source level or parse/lexical uncertainty abstains.

**Alternative considered:** Reusing a JVM or executing snippets. Rejected — violates the no-arbitrary-execution boundary and breaks replay determinism and tenant isolation.

---

### D4: Treat initialization and construction as bounded relationship analysis

**Decision:** Phase 3 distinguishes class initialization from creation of each instance. For a class, static field initializers and static initializer blocks execute in textual order when the class is initialized, subject to superclass/interface initialization rules and active-use triggers. For an object, instance fields and instance initializer blocks run exactly once on the constructor path after the superclass constructor returns and before the body of the non-delegating constructor. A `this(...)` constructor delegates first; after the delegated constructor (including instance initialization and its body) returns, the delegating constructor body continues. These are separate semantic traces, not one interleaved static/instance/constructor list. A source window may establish declared order or possible writes, but cannot by itself prove class initialization was triggered, a particular constructor ran, or which instance is queried. Report `POST_CONSTRUCTION` only when the analyzed constructor path and all relevant writes are established; otherwise use `PARTIAL`/`LATER_MUTABLE` with a limitation. Cross-file superclass chains, DI-constructed instances, and generated constructors require additional evidence or remain partial. Enum constants, records, and compact constructors remain partial until dedicated rules and fixtures land.

```python
# src/investigation_agent_platform/application/analysis/java/init_facts.py
from __future__ import annotations

from typing import Any


def constructor_chain_targets(constructor_node: Any, source: bytes) -> list[str]:
    """Return explicit this(...)/super(...) targets in textual order."""
    targets: list[str] = []
    for child in constructor_node.children:
        if child.type in {"explicit_constructor_invocation"}:
            text = source[child.start_byte : child.end_byte].decode("utf-8", errors="replace")
            targets.append(" ".join(text.split()))
    return targets


def initialization_order_kind(declaration_node: Any) -> str:
    """Classify by initialization domain; never mix class and object traces."""
    node_type = declaration_node.type
    if node_type == "static_initializer":
        return "class_initializer"
    if node_type == "block":
        return "instance_initializer"
    if node_type == "constructor_declaration":
        return "constructor_body"
    if node_type == "field_declaration":
        return "field_initializer"  # inspect modifiers to classify static/instance
    return "unsupported_initialization_node"
```

No value computed in Phase 3 is labeled `RUNTIME_EFFECTIVE`. Source order alone does not establish execution or the final state of an instance. A post-construction write proven in-source yields `POST_CONSTRUCTION` only for a bounded, fully modeled constructor path; any write or alias outside the scope yields `LATER_MUTABLE` at best.

**Alternative considered:** Whole-repo constructor graph. Rejected — needs cross-file revision-consistent reads and a call-graph join the gateway does not yet guarantee; bounded single-window analysis delivers the common single-class cases first.

---

### D5: Resolve call targets statically, never claim execution

**Decision:** Phase 4 implements Java invocation applicability/resolution only when receiver and argument types, accessible candidates, and relevant declarations are sufficiently known. Resolution must account for strict/loose invocation conversions, primitive widening, boxing/unboxing, generic inference where supported, fixed-arity versus variable-arity phases, and most-specific selection; arity and type-name string matching is insufficient. If dependencies, imports, receiver type, or candidates are incomplete, return `PARTIAL`/`UNRESOLVED` with a limitation rather than selecting a unique-looking candidate. A `CALL_TARGET_FACT` names the statically resolved target and its declaration location. It never asserts the call executes. Instance virtual dispatch may identify a statically known declared target set, but selecting the runtime override requires receiver runtime type evidence. Reflection, dynamic proxies, lambdas passed across method boundaries, and cross-module calls remain unresolved absent supporting metadata.

```python
# src/investigation_agent_platform/application/analysis/java/method_facts.py
from __future__ import annotations

from typing import Any


def overload_key(name: str, arity: int, type_names: list[str]) -> str:
    return f"{name}/{arity}({','.join(type_names)})"


def static_call_target(invocation_node: Any, source: bytes, type_environment: Any) -> Any:
    """Resolve only when Java applicability and most-specific rules are proven."""
    # Return typed resolution (target or ambiguity/limitation); do not choose
    # by arity alone and do not claim that the invocation executes.
    raise NotImplementedError
```

Data-flow facts record `data_flow_refs` as structured definition-use references including repository, revision, path, and exact line/column ranges inside the window; a bare `file:line` string is insufficient to validate evidence binding. Anything requiring interprocedural aliasing, stream laziness, or concurrent visibility is `PARTIAL`.

**Alternative considered:** Points-to analysis. Rejected — unbounded, version-sensitive, and untestable against the current fixture strategy; bounded reaching-definitions covers the investigation-relevant local cases.

---

### D6: Keep control flow, exceptions, and concurrency conservative

**Decision:** Phases 5 and 6 implement `CONTROL_FLOW_FACT` (a statically established branch, return, or exception path) and `POSSIBLE_EFFECT_FACT` (a potential mutation, exception, or side effect) with mandatory abstention defaults. A catch handler is never reported as executed because it exists. A `finally` block is reported as a possible cleanup path, not a proven outcome. `synchronized`, `volatile`, atomics, `CompletableFuture`, executors, and reactive chains always produce `PARTIAL` or `UNRESOLVED` with `nondeterministic_scheduling`, `requires_runtime_observation`, or `async_boundary`. Pattern matching, switch expressions, records, sealed hierarchies, and generics resolve only when every branch target is inside the window and type erasure does not change the answer; otherwise they abstain with explicit codes.

Control-flow references use stable `file:start-end` strings so the verifier can check window containment without re-parsing.

**Alternative considered:** Exhaustive path enumeration. Rejected — path explosion on loops and recursion makes verification untestable; bounded branch and exception-path facts with explicit limits match the prompt requirement.

---

### D7: Enforce verifier integrity for every new fact category

**Decision:** Generalize the existing `code_value_proof` pattern to a per-category proof table. Each category declares required provenance, permissible statuses, comparison operator, and abstention conditions. The verifier loads only well-formed resolutions bound to cited evidence, checks revision and path identity, checks window containment, applies exact comparison (no substring matching), and maps `PARTIAL`/`UNRESOLVED` to typed limitations plus `next_check`, never to `VERIFIED`. `RUNTIME_OBSERVATION_FACT` and `CAUSAL_CLAIM` can never verify from source evidence alone.

```python
# src/investigation_agent_platform/application/investigation/verification.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from investigation_agent_platform.domain.code.models import JavaFactCategory

@dataclass(frozen=True)
class CategoryProofRule:
    required_evidence_families: frozenset[str]
    requires_immutable_source_or_observation_identity: bool
    requires_exact_scope_match: bool
    requires_source_window_for_each_location: bool
    requires_structured_fact_match: bool
    source_only_verifiable: bool
    max_verdict: Literal["INSUFFICIENT", "LIKELY", "VERIFIED"]


_VERIFIED_SOURCE = {
    "requires_immutable_source_or_observation_identity": True,
    "requires_exact_scope_match": True,
    "requires_source_window_for_each_location": True,
    "requires_structured_fact_match": True,
    "source_only_verifiable": True,
    "max_verdict": "VERIFIED",
}
_LIKELY_SOURCE = {**_VERIFIED_SOURCE, "max_verdict": "LIKELY"}

CATEGORY_PROOF_RULES: dict[JavaFactCategory, CategoryProofRule] = {
    JavaFactCategory.DECLARATION_FACT: CategoryProofRule(frozenset({"SOURCE_RANGE"}), **_VERIFIED_SOURCE),
    JavaFactCategory.SOURCE_VALUE_FACT: CategoryProofRule(frozenset({"SOURCE_RANGE", "ANALYZER_RESOLUTION"}), **_VERIFIED_SOURCE),
    JavaFactCategory.LANGUAGE_SEMANTIC_FACT: CategoryProofRule(frozenset({"SOURCE_RANGE", "LANGUAGE_RULE_VERSION", "ANALYZER_RESOLUTION"}), **_VERIFIED_SOURCE),
    JavaFactCategory.CONTROL_FLOW_FACT: CategoryProofRule(frozenset({"SOURCE_RANGE", "ANALYZER_RESOLUTION"}), **_LIKELY_SOURCE),
    JavaFactCategory.DATA_FLOW_FACT: CategoryProofRule(frozenset({"SOURCE_RANGE", "ANALYZER_RESOLUTION"}), **_LIKELY_SOURCE),
    JavaFactCategory.CALL_TARGET_FACT: CategoryProofRule(frozenset({"CALL_SITE_RANGE", "TARGET_DECLARATION_RANGE", "ANALYZER_RESOLUTION"}), **_LIKELY_SOURCE),
    JavaFactCategory.POSSIBLE_EFFECT_FACT: CategoryProofRule(frozenset({"SOURCE_RANGE", "ANALYZER_RESOLUTION"}), **_LIKELY_SOURCE),
    JavaFactCategory.CONFIGURATION_FACT: CategoryProofRule(frozenset({"SOURCE_RANGE", "CONFIGURATION_SOURCE"}), **_LIKELY_SOURCE),
    JavaFactCategory.RUNTIME_OBSERVATION_FACT: CategoryProofRule(
        frozenset({"RUNTIME_OBSERVATION"}), True, True, False, True, False, "LIKELY"
    ),
    JavaFactCategory.CAUSAL_CLAIM: CategoryProofRule(
        frozenset({"SOURCE_RANGE", "RUNTIME_OBSERVATION", "CAUSAL_STEP_BINDINGS"}),
        True, True, True, True, False, "LIKELY"
    ),
}


def category_proof_rule(category: JavaFactCategory) -> CategoryProofRule | None:
    return CATEGORY_PROOF_RULES.get(category)
```

Treat this as a policy outline. `VERIFIED` is available only where deterministic source proof is complete and the existing final verifier permits it; potential behavior and static call targets do not establish execution. Configuration declaration does not establish applied configuration. Runtime facts require correctly scoped runtime observations and cannot be sourced from Java analysis. Causal claims require each typed causal step to bind its own supporting evidence; source locations alone never prove causation. Unknown categories or categories without an implemented rule fail closed. Existing field-value compatibility is tested against the current verifier; it is not presumed by this table.

The existing `claims_field_value` detector is generalized only as a routing aid; it must not supply a structured target, category, or value. Extend `ClaimTarget` and `HypothesisEvidenceAssessment` with optional `fact_category`, preserve the independent `ClaimKind` axis, and validate category/claim-kind compatibility. Verify the exact relation requested: typed value equality for values; a shown call site and applicable target declaration for calls; relevant definitions and control-flow ranges for data-flow/branch claims; separate runtime evidence for observations and causal claims. Legacy claims without category use only existing proof paths.

**Alternative considered:** One generic proof for all categories. Rejected — it would let a possible effect or runtime observation verify on source location alone, which is exactly the false-acceptance hole the prompt closes.

---

### D8: Wire the pipeline without new infrastructure

**Decision:** Reuse the current call path end to end. `question_shape.classify_question_shape` gains Java-specific source-value, call-target, and control-flow patterns, without routing runtime questions away from required runtime evidence. The reasoner emits typed `DecisionClaim` values with `fact_category`, `value_semantics`, and `claimed_value`. The worker `execute_action_activity` GET_CODE branch dispatches only validated parameters to a resolver matching the declared category, then stores the resulting contract on the evidence row and outcome detail using the Phase 0 payload strategy. The context compiler surfaces code-fact provenance in code packets. No new provider, queue, or endpoint is proposed.

```text
GET_CODE source-range evidence
  -> validate category-specific action parameters and immutable provenance
  -> dispatch to the resolver for that category (or do not resolve)
  -> serialize using the Phase 0 versioned/reader-first payload contract
  -> attach resolution to the copied Evidence and activity outcome detail
  -> persist evidence through the existing evidence repository
```

Phase 7 framework boundaries (DI, config binding, generated sources, persistence, messaging, async execution) reuse the same hook: the resolver returns `CONFIGURATION_FACT` with the declared binding or `PARTIAL` naming the missing runtime evidence, and the verifier enforces that a declared binding never verifies as an applied configuration.

**Alternative considered:** Separate Java analysis service or queue. Rejected — no evidence the existing activity, evidence, and verifier abstractions cannot carry the load; a new service adds failure modes without new capability.

---

## Data Storage

No new persistence structures are justified by this design. A schema migration is not currently proposed, but payload/key versioning and reader rollout must be decided before changing the evidence shape.

The resolution remains ephemeral and attached to existing Evidence attributes; the current activity also mirrors it in `outcome_detail`. The current Evidence validator parses the attribute through a strict `CodeFactResolution` model, so any generalized format must be accepted by that validator and every consumer before it is emitted under the current key. `InvestigationConclusion.next_check` (migration 022) can carry abstention guidance; it does not carry Java fact payloads.

Future persistence, if a concrete consumer ever needs cross-run caching of resolutions, would add a content-addressed sidecar keyed on `(repository, revision, file_path, symbol_identity, fact_category)` with the resolution JSON and a TTL tied to revision immutability. That consumer does not exist in this design, so the table is not created. This section records the decision explicitly so a later phase does not invent storage ad hoc.

## Data Structures

Input schemas are the existing `Evidence` (with `EvidenceProvenance`, `QueryFingerprint`, `SourceLocation`) plus validated action parameters. Output schema is the proposed extended `CodeFactResolution` in D1 plus `JavaFactCategory` and `SemanticStage`. Claim inputs reuse `ClaimTarget` (`declaring_type`, `value_semantics`, `claimed_value`) and `DecisionClaim` (`value_semantics`, `claimed_value`); both `ClaimTarget` and `HypothesisEvidenceAssessment` need an optional validated `fact_category` in addition to `DecisionClaim`. Existing `ClaimKind` remains a separate evidence-binding dimension.

```python
# src/investigation_agent_platform/ports/reasoning/reasoner.py
from pydantic import BaseModel, Field

from investigation_agent_platform.domain.hypothesis.models import ClaimKind, ClaimTarget


class DecisionClaim(BaseModel):
    model_config = {"extra": "forbid"}

    statement: str = Field(..., max_length=2048)
    claim_kind: ClaimKind = ClaimKind.CAUSAL
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)
    target: ClaimTarget | None = None
    fact_category: JavaFactCategory | None = None
    value_semantics: str | None = Field(default=None, max_length=64)
    claimed_value: str | None = Field(default=None, max_length=512)
```

```python
# src/investigation_agent_platform/domain/analysis/question_shape.py
_SOURCE_VALUE_PATTERNS = (
    "default value of",
    "default value for",
    "field initializer",
    "initializer for",
    "initializes to",
    "initialized to",
    "what value does",
    "declared default",
)
```

Known resolution provenance (`repository`, `revision`, `source_evidence_ids`, source locations/spans, limitation codes, assumptions, conflicting evidence IDs, analysis method, language version) must be carried on every status. Unknown identity must be represented as unknown, not fabricated; use a typed limitation for missing repository, revision, path, or range. `RESOLVED` source facts require immutable provenance and sufficient exact source spans; runtime observations require runtime source identity/time and runtime evidence IDs. Analyzer output remains bound to tenant and investigation through existing Evidence ownership and verifier checks; it must not be detached from those boundaries.

## Proposed Interfaces

No new REST endpoints, CLI commands, MCP tools, TUI screens, or GUI components. All interfaces below are internal function contracts already present in the tree, extended with optional parameters only.

| Interface | Signature | Description |
| --------- | --------- | ----------- |
| `resolve_java_field_fact` | `(evidence, field_name, declaring_type=None) -> CodeFactResolution` | Existing field baseline; preserved as shim. |
| `resolve_java_local_fact` | `(evidence, symbol_name, method_scope=None, language_version=None) -> CodeFactResolution` | Phase 2 locals, params, `var` anchors; returns typed limitations for out-of-scope or unknown type. |
| `resolve_java_call_target` | `(evidence, invocation, type_environment) -> CodeFactResolution` | Phase 4 Java applicability resolution; target is not execution proof. |
| `resolve_java_control_flow` | `(evidence, target, branch_hint=None) -> CodeFactResolution` | Phase 5 bounded branch, return, and exception-path facts. |
| `VerificationEngine.java_fact_proof` | `(hypothesis, evidence_items) -> tuple[bool, str]` | Per-category proof dispatch with typed category policy and evidence-family requirements. |
| `VerificationEngine.validate_java_fact_claim` | `(hypothesis, evidence_items, contradictions) -> VerificationResult \| None` | Generalization of `validate_code_value_claim`, preserving legacy path and final conclusion gates. |

Action parameters accepted by the GET_CODE branch may gain optional `symbol_name`, `fact_category`, and a language-level hint after verifying action validation and authorization paths. Absence preserves current field-only behavior. The analyzer selects source/release level only from trusted repository/build metadata; model/action-supplied level is not authoritative. Even though action parameters are free-form, validate names, allowlisted categories, source span, and scope at the activity boundary; unknown categories fail closed. No OpenAPI change is expected unless these parameters become public API contract.

Accessibility: no user-facing interface changes in this design, so no new color, keyboard, screen-reader, motion, or form requirements arise. If a later phase adds surfaced fact explanations to API responses or UI, that phase must include the standard accessibility section.

## Implementation Detail

If implementation is authorized, proceed through these candidate phases. Each phase lists objective, exact source paths, contract delta, and gate. Numbering maps to `docs/JAV_PROMPT.md` section 8; these are candidate gates, not implementation authorization.

### Phase 0 — Baseline and contract audit

Objective: prove the field baseline is real at HEAD and lock the extension contract. Non-goal: no new Java semantics.

- Confirm current source and tests against `docs/JAV_PROMPT.md`, architecture §58, and the ledger; distinguish confirmed code paths from historical claims.
- Add counterexamples for non-Java literal spellings, Java escape/text-block semantics, source levels, and constructor/class-initializer distinction before broadening expression support.
- Specify and test legacy payload validation, new payload serialization, `Evidence` validation, assessment replay, mixed-version reader behavior, downgrade behavior, and default-deny unknown categories. Select versioned-key versus reader-first migration before adding fields.
- Gate: focused tests pass; contract preserves existing field tests; each unknown category/status remains non-verifying. Full-suite status is reported when run, not assumed.

### Phase 1 — Verifier integrity

Objective: close every promotion hole before new facts can flow. Non-goal: no new resolvers.

- Generalize `claims_field_value` to `claims_java_fact`, add `java_fact_proof` dispatch on `fact_category`, enforce revision and path identity, window containment, exact value comparison, and `RUNTIME_OBSERVATION_FACT`/`CAUSAL_CLAIM` never-verifiable-from-source.
- Wire `PARTIAL`/`UNRESOLVED` to typed limitations and `next_check` via existing `next_check_for` priority (`rival > relation > unmapped > provider`).
- Exact files: `application/investigation/verification.py`, `application/investigation/validator.py`, `application/investigation/reasoning.py`, `tests/unit/test_evidence_bound_verification.py`, `tests/unit/test_phase2_remaining.py`.
- Gate: counterexample tests where location is valid but value, semantics, category, declaring type, revision, evidence family, or window is wrong all fail closed; unknown-category and legacy-claim behavior is explicit; no substring-only proof remains.

### Phase 2 — Java declarations and expressions

Objective: fields, locals, parameters, defaults, constants, literals, safe expressions. Non-goal: no constructors, no cross-file scope.

- Ship `java/_provenance.py`, `java/_parse.py`, `java/expressions.py`, `java/field_facts.py` (moved baseline), `java/local_facts.py`.
- Tree-sitter nodes: `field_declaration`, `variable_declarator`, `local_variable_declaration`, `formal_parameter`, `modifiers`, `type_identifier`, `generic_type`, `array_type`; preserve contiguous numbered-line mapping and exact node spans through parsing.
- Java rules: primitive and reference defaults, declaration without initializer, `final` and blank `final`, definite assignment, effectively-final, shadowed and inherited names (ambiguity fails closed), `var` recorded as declared type anchor only.
- Gate: parametrized literal/default tests cover Java lexical and value semantics; invalid Java literals, type overflow/out-of-range values, gaps/duplicate lines, and same-line declaration spans have counterexamples; locals/parameters obey scope and definite assignment; method-call, `new`, lambda, unknown language level, and out-of-window references abstain with stable limitation codes.

### Phase 3 — Initialization and object construction

Objective: static and instance initialization, constructor chaining, inheritance within one window, field hiding. Non-goal: no cross-file superclass, no DI, no Lombok execution.

- Ship `java/init_facts.py`; nodes `static_initializer`, `block`, `constructor_declaration`, `explicit_constructor_invocation`.
- Model class initialization and per-instance construction separately, including superclass and same-class constructor delegation. Source order alone never proves the class initialized or a particular instance followed the path. Later in-window writes yield `POST_CONSTRUCTION` only for a fully bounded supported path; otherwise use `PARTIAL`/`LATER_MUTABLE`; enum, record, and compact-constructor cases yield explicit `PARTIAL` until supported.
- Gate: JLS-driven chaining (`this(...)`/`super(...)`), hiding, class initialization triggers, instance construction, and static-versus-instance isolation fixtures pass; cross-file and generated-source cases assert named limitations.

### Phase 4 — Methods, overrides, and data flow

Objective: overload resolution, static dispatch naming, bounded local flow. Non-goal: no reflection, no proxies, no interprocedural aliasing.

- Ship `java/method_facts.py`, `java/dataflow.py`; nodes `method_declaration`, `method_invocation`, `object_creation_expression`, `return_statement`.
- Resolve only using Java invocation applicability and most-specific rules (including conversion and varargs phases) over a complete candidate set; otherwise return `ambiguous_overload` or an unknown-candidate limitation. Reaching definitions and return-path enumeration stay inside the window; `data_flow_refs` bind exact definition-use ranges.
- Gate: overloads differing by widening, boxing, and varargs; overrides/default methods; early returns; and definite assignment have correct-target and wrong-target counterexamples. Reflection, proxy, lambda-escape, incomplete candidates, and cross-module cases abstain. A target fact never proves invocation execution.

### Phase 5 — Control flow and exceptions

Objective: branch conditions, possible paths, exception propagation, resource handling. Non-goal: no executed-branch claims, no transaction-outcome claims.

- Ship `java/flow_facts.py`; nodes `if_statement`, `switch_expression`, `try_statement`, `catch_clause`, `finally_clause`, `throw_statement`, `try_with_resources`.
- Catch existence never implies execution; `finally` is possible cleanup; wrapping and cause chains are named but not proven; try-with-resources close is possible, not observed.
- Gate: branch, catch-selection, wrapping, and suppression fixtures assert possible-path language and window containment; execution-wording assertions fail closed.

### Phase 6 — Types and advanced language features

Objective: generics, lambdas, records, sealed types, pattern matching, version-gated features. Non-goal: no type-erasure-transcending claims, no async ordering claims.

- Nodes `type_parameters`, `wildcard`, `lambda_expression`, `method_reference`, `record_declaration`, `sealed` modifiers, `pattern` and `guard` nodes.
- Every feature checks `language_version` from build metadata when available; unknown version plus version-gated syntax yields `unsupported_language_version` rather than a guessed parse.
- Gate: generic, lambda-capture, record, sealed, and pattern fixtures green within supported subset; erasure-sensitive and version-unknown cases abstain.

### Phase 7 — Frameworks and external boundaries

Objective: DI and config bindings as declared facts, generated-code and async boundaries as explicit unresolved. Non-goal: no container startup, no broker semantics, no observed-production claims.

- `CONFIGURATION_FACT` carries the declared mapping and its source location; applied-configuration claims require runtime evidence and otherwise fail closed. Reflection, proxies, annotation processing, Lombok, build-generated classes, JDBC, transactions, HTTP clients, brokers, caches, serialization, time, locale, and randomness each have a named limitation code and required-evidence note.
- Gate: binding-declaration fixtures verify; applied-behavior, transaction-outcome, and concurrent-ordering assertions abstain with the documented code.

### Phase 8 — End-to-end investigation evaluation

Objective: validate question routing, retrieval, resolution, verification, and conclusion semantics on pinned synthetic fixtures. Non-goal: no live LLM or production evaluation in this design.

- Synthetic Java fixtures pinned to exact commits covering positive, negative, ambiguous, inherited, shadowed, and dynamically unresolved examples per prompt section 9, plus counterexamples where source evidence is valid but the asserted interpretation is false.
- Invariants enforced as tests: no verification from location alone, no mixed-revision facts, no possible-as-observed wording, no initializer-as-runtime promotion, explicit partial on unsupported constructs, tenant and application boundaries, evidence-ID and range survival, limitation preservation in conclusions.
- Gate: precision, in-subset recall, false-verification rate, abstention correctness, provenance correctness, and repeatability reported separately; coverage never buys weaker verification.

### `docs/IAP-architecture-v1.0.md` changes

No `docs/dev/architecture.md` exists in this project; the architecture source of truth is `docs/IAP-architecture-v1.0.md`. Its §58 already documents the field-fact baseline. Any implementation phase that changes the as-built path must update that section alongside this design, before removing its recommendation-ledger item. Phase 4 should document only the call-target/data-flow behavior actually shipped, including evidence and verdict limits; Phase 8 should append fixture pinning and measured metrics only after the gates pass.

## Proposed Migrations and Compatibility

No database migration is currently proposed, but compatibility is not automatic. The current `Evidence` validator invokes the strict existing `CodeFactResolution` model, and persisted assessment JSON has legacy shapes. First choose a reader-first migration or versioned resolution key, implement dual-shape validation with explicit conflict handling, and test persisted evidence/assessment replay and downgrade behavior. Add optional `fact_category` fields to both assessment/target models and `DecisionClaim`; preserve missing values on legacy rows. Migration 022 (`InvestigationConclusion.next_check`) remains available for abstention guidance but does not itself carry the new fact contract.

Compatibility notes: legacy banked rows with only field-shaped keys must continue validating and receive conservative legacy defaults, including mapping the old generic initialization stage to a narrower stage only when the claim and evidence support that interpretation. Generalized rows are not readable by old strict readers unless they are emitted under a separate key/format or all readers are upgraded first. Rollback must retain the legacy shape/key until old readers are out of service; unknown future category values must fail closed, not coerce to source-value proof. A downgrade/replay test is a Phase 0 gate.

## Testing Philosophy

### Field baseline lock

Keep `tests/unit/test_java_field_facts.py` green unmodified through Phase 1. It is the contract anchor: literals, defaults, multi-declarator types, signs and bases, local rejection, partial on calls, ambiguity, declaring-type mismatch, non-Java path, path-only rejection, and missing-revision fail-closed. Any model change that breaks these tests is a compatibility break, not a feature.

### Contract and verifier tests

Test model invariants by category: resolved value facts require structured values; every resolved source fact requires immutable path/range/revision and bound evidence; causal claims cannot be analyzer-resolved. Each category has positive tests only for its declared evidence families and verdict ceiling, and negative tests for wrong fact/value, wrong semantics/target, unbound evidence ID, revision/scope mismatch, path mismatch, missing/truncated window, or incompatible evidence families. Runtime and causal categories never verify from source evidence regardless of location quality; unsupported categories never default to source-value proof.

### Per-phase fixture tests

Each phase ships its own test module mirroring the phase name (`test_java_locals.py`, `test_java_init.py`, `test_java_methods.py`, `test_java_flow.py`, `test_java_types.py`, `test_java_boundaries.py`). Fixtures are inline Java windows in the style of the field tests for unit speed, plus a small set of pinned synthetic-repo fixtures for end-to-end revision and windowing checks. Every supported feature has a correct-claim test and an incorrect-claim counterexample; every unsupported construct has a partial or unresolved test naming the limitation code.

### Integration and pipeline tests

Cover the worker hook (resolution banked on the evidence row and mirrored in outcome detail), the evidence shape validator (malformed resolutions rejected), question-shape routing (source-value questions prioritize code; runtime-marked questions retain the required code-plus-runtime evidence path), and conclusion mapping (limitations and conflicting evidence survive to the final answer). Keep deterministic unit tests separate from database and integration tests and from any live LLM evaluation; name environment prerequisites explicitly and report blockers instead of skipping silently.

### Evaluation and metrics

Phase 8 fixtures live under `tests/fixtures/java/` with a manifest pinning each file to a commit SHA. The battery grader reports precision, in-subset recall, false-verification rate, abstention correctness, provenance correctness, and repeatability as separate numbers. Partials fail the battery exit code per the existing Phase 4 convention. No metric may improve by weakening a verifier gate; gate changes require new counterexample tests first.

## Documentation Plan

### `docs/IAP-Code-Java-lang-impl-design.md`

**Audience:** Implementers and reviewers

This file carries the full proposed contract, layer map, per-phase file lists, proof table, and candidate gates. Update it when a phase lands: mark the phase as-built, record deviations, and link current tests. Read it alongside the architecture file and original prompt.

### `docs/IAP-architecture-v1.0.md`

**Audience:** Platform engineers and operators

The existing architecture already documents the field-fact baseline in §58. Any implementation phase that changes the as-built path must update that section alongside this design, before removing its recommendation-ledger item. Phase 4 should document only the call-target/data-flow behavior actually shipped, including evidence and verdict limits; Phase 8 should append fixture pinning and measured metrics only after the gates pass.

### `docs/IAP_Arch_Eng_Recommendations.log`

**Audience:** Architecture and engineering stewards

Keep the review item OPEN until the design gaps are incorporated and reviewed. Then move it to PLANNED; move to IMPLEMENTED only as each phase gate passes, with current test evidence and the updated architecture/doc references. Declined scope (symbolic execution, whole-repo points-to, container startup) is recorded as DECLINED with reason rather than silently dropped.

### `tests/fixtures/java/README.md`

**Audience:** Test authors

Document the fixture manifest format (file, commit SHA, language version, covered prompt area, positive and negative claims, expected status and limitation codes). Explain how to add a fixture without breaking pinning, and how the battery consumes the manifest. This file is created with Phase 2 and extended through Phase 8.

## Risks / Trade-offs

### Window too small to see the truth

**Risk:** Range reads sized for the snippet budget can exclude the decisive write, override, or branch while still citing a valid nearby location, letting a plausible but wrong value verify.

**Mitigation:** Window-containment checks on declaration ranges and claimed lines, `truncated` and `next_window` disclosure from the provider, compiler packet budgets that shrink within the packet rather than silently, and `PARTIAL`/`LATER_MUTABLE` whenever scope does not prove the complete write set. `LATER_MUTABLE` is not itself evidence that a later write exists; it denotes that effective value is not established.

### Tree-sitter grammar drift from the repository Java version

**Risk:** Newer syntax (records, sealed types, pattern matching, string templates) parses differently or errors under the bundled grammar, producing false unresolved or false resolved results.

**Mitigation:** `language_version` travels with every resolution, version-gated syntax without a known version abstains, grammar errors fail closed to `java_syntax_error_or_incomplete_source_window`, and Phase 6 fixtures pin one language level per file.

### Overload and override misresolution

**Risk:** Name-only matching picks the wrong overload or the wrong hierarchy member, especially with boxing, varargs, generics, and default interface methods.

**Mitigation:** Java invocation applicability and most-specific selection over a complete candidate set; otherwise abstain with an ambiguity/incomplete-scope limitation. The verifier binds the call site, receiver/argument types, candidate target declaration, repository/revision/path, and evidence IDs. A resolved static target is never an execution claim.

### Possible-versus-observed confusion

**Risk:** Static possibilities (a catch that exists, a branch that parses, a config that is declared) leak into conclusions as observed behavior or root causes.

**Mitigation:** Category proof table caps possible effects at likely, bars runtime and causal categories from source-only verification, and conclusion mapping demotes prose-only or single-row causal sentences. Counterexample tests guard each promotion path.

### Scope creep into execution and frameworks

**Risk:** Pressure to prove runtime-effective values pulls in container startup, build execution, or production reads inside the analyzer, breaking determinism, tenant isolation, and replay safety.

**Mitigation:** Hard phase boundary at Phase 7: the analyzer names the required runtime evidence and abstains. No file reads, no network calls, no subprocess builds inside resolvers. Framework behavior is a declared binding or an explicit unresolved, never an inferred runtime fact.

### Ephemeral-only storage limits debugging

**Risk:** Without persisted resolutions, operators cannot audit what the analyzer claimed on a past run beyond the banked evidence attributes.

**Mitigation:** Resolutions are already banked on evidence rows and mirrored in outcome detail with stable IDs, revisions, and limitation codes, which is sufficient for audit and replay. A content-addressed sidecar is deferred until a concrete consumer demonstrates the need.
