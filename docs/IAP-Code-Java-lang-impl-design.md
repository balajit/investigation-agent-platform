# Design: IAP Code Java Language Implementation

## Context

The project has a working deterministic Java field-value path on top of the existing investigation pipeline, currently uncommitted in the working tree. The baseline establishes one narrow semantic contract and proves it end to end.

What exists today, verified against the working tree:

- `src/investigation_agent_platform/application/analysis/java_field_facts.py` (new, untracked, 318 lines) exposes `resolve_java_field_fact(evidence, field_name, declaring_type=None)`. It reads no files and follows no references. Its only input is one revision-pinned `GET_CODE`/`GET_SOURCE`/`READ_FILE` range-read `Evidence` with numbered `NNN| ...` lines. It parses the window with `tree-sitter-language-pack` Java grammar, finds exactly one `field_declaration`/`variable_declarator` matching `field_name`, normalizes a conservative literal subset via `_literal_value`, applies Java language defaults when no initializer is present, and returns `CodeFactResolution` with `RESOLVED`, `PARTIAL` (unsupported initializer), or `UNRESOLVED` (fail-closed limitation codes).
- `src/investigation_agent_platform/domain/code/models.py` defines `CodeFactStatus` (`RESOLVED`/`PARTIAL`/`UNRESOLVED`), `CodeValueSemantics` (`EXPLICIT_INITIALIZER`, `LANGUAGE_DEFAULT`, `POST_CONSTRUCTION`, `LATER_MUTABLE`, `RUNTIME_EFFECTIVE`), and `CodeFactResolution` as a frozen ephemeral model carried with evidence, never a persistence aggregate. `RESOLVED` requires `resolved_value` plus immutable revision and path provenance.
- `src/investigation_agent_platform/application/worker/activities.py` (`execute_action_activity`, GET_CODE branch) calls `resolve_java_field_fact` when `field_name` is present in action parameters, stores the dumped resolution in both `merged_attributes["code_fact_resolution"]` and `outcome_detail["code_fact_resolution"]`, then persists the evidence row.
- `src/investigation_agent_platform/domain/evidence/models.py` validates `attributes["code_fact_resolution"]` against `CodeFactResolution` shape on `Evidence` validation.
- `src/investigation_agent_platform/application/investigation/verification.py` adds `claims_field_value`, `_resolution_from_evidence`, `code_value_proof`, and `validate_code_value_claim`. Proof requires a `RESOLVED` resolution bound to cited evidence, matching `value_semantics`, exact `claimed_value == resolved_value`, field-name and declaring-type match, bound evidence-ID intersection, same revision and path, a range read whose window contains the declaration range and claimed line. `VerificationResult.code_value_validated` marks the tier.
- `src/investigation_agent_platform/domain/analysis/question_shape.py` routes source-value questions (`default value of`, `field initializer`, `initialized to`, `what value does`, and similar) to `code` shape unless a runtime marker (`in production`, `at runtime`, `effective runtime`, and similar) is present.
- `src/investigation_agent_platform/domain/hypothesis/models.py` (`ClaimTarget.declaring_type/value_semantics/claimed_value`, `HypothesisEvidenceAssessment.value_semantics/claimed_value`) and `src/investigation_agent_platform/ports/reasoning/reasoner.py` (`DecisionClaim.value_semantics/claimed_value`) carry the typed claim through reason to workflow to `FORMULATE`.
- `tests/unit/test_java_field_facts.py` (untracked, 182 lines) covers explicit literals and defaults, multi-declarator shared types, signs and bases, local-variable rejection, partial on method-call initializer, ambiguity, wrong declaring type, non-Java path, path-only rejection, and missing-revision fail-closed behavior.

What does not exist: everything in `tmp/JAV_PROMPT.md` sections 3A through 3K beyond field initializers and primitive defaults. No local-variable, parameter, constructor-chain, control-flow, data-flow, method-dispatch, exception-path, generics, lambda, stream, annotation, reflection, DI, or runtime-observation resolution. The current `CodeFactResolution` is field-shaped (`field_name`, `field_type`, `field_category`) and cannot express a local, a call target, a branch, or a possible effect without a compatible extension. The verifier only proves `EXPLICIT_INITIALIZER` and `LANGUAGE_DEFAULT` source-value facts.

This design generalizes the field baseline across the full practical anatomy of Java while preserving the Evidence Gateway, code-intelligence providers, context compiler, verifier, and persistence paths. No new agent framework, no new infrastructure, no schema migration unless a concrete consumer demands it.

## References

- [tmp/JAV_PROMPT.md](../tmp/JAV_PROMPT.md) — Mission, mandatory Java coverage areas A through K, fact taxonomy, contract requirements, layered analysis, verifier rules, phases 0 through 8, and evaluation invariants governing this design.
- [docs/IAP-architecture-v1.0.md](./IAP-architecture-v1.0.md) — As-built architecture: Evidence Gateway, code-intelligence and Git providers, context compiler, reasoning loop, verification gates, worker activities and workflows. Primary source of truth for call paths this design reuses.
- [docs/DS_REVIEW_v0.1.md](./DS_REVIEW_v0.1.md) — Code-grounding target, revision, evidence, and verification contracts plus phased file-level plan this design extends to Java semantics.
- [Tree-sitter Java grammar](https://github.com/tree-sitter/tree-sitter-java) — Node types (`field_declaration`, `variable_declarator`, `local_variable_declaration`, `method_declaration`, `method_invocation`, `object_creation_expression`, and similar) informing the AST extraction layer.
- [Java Language Specification: definite assignment and initialization](https://docs.oracle.com/javase/specs/jls/se21/html/index.html) — Source of truth for defaults, blank finals, initialization order, and constant expressions cited in the coverage matrix.
- [Pydantic v2 model validation](https://docs.pydantic.dev/latest/) — Informs the frozen ephemeral `CodeFactResolution` extension pattern and `Evidence.attributes` shape validation.

## Goals / Non-Goals

**Goals:**

- Smallest compatible `CodeFactResolution` extension that expresses declarations, source values, language semantics, control flow, data flow, call targets, possible effects, configuration bindings, runtime observations, and causal claims with provenance and fail-closed statuses.
- Layered deterministic analyzer package under `src/investigation_agent_platform/application/analysis/java/` reusing the field baseline: source acquisition, identity normalization, AST extraction, local semantic evaluation, bounded relationship analysis, resolution assembly, claim verification, conclusion enforcement.
- Coverage matrix for Java areas A through K with per-area status (implemented, partial, missing) and per-phase acceptance gates matching `tmp/JAV_PROMPT.md` phases 0 through 8.
- Verifier integrity: no location-only promotion, no initializer-as-runtime promotion, no possible-as-observed promotion, revision-mismatch fail-closed, exact value comparison, no brittle substring proof.
- Revision-pinned synthetic Java fixtures with positive, negative, ambiguous, inherited, shadowed, and dynamically unresolved cases plus counterexamples where the location is valid but the interpretation is false.
- Zero new persistence tables and zero new REST or CLI surface in this design; resolutions stay ephemeral and ride on existing `Evidence.attributes`.

**Non-Goals:**

- General symbolic execution, arbitrary application execution, unsandboxed project builds, or full-program whole-repo points-to analysis.
- New agent framework, generic plugin system, separate reasoning engine, gateway rebuild, verifier duplicate, or broad platform rewrite.
- Runtime tracing, log-derived execution proof, debugger attachment, or production-state observation beyond declaring the `RUNTIME_OBSERVATION_FACT` boundary and its required evidence shape.
- Framework-specific deep integrations (DI container startup, persistence provider internals, message-broker semantics) beyond configuration-binding facts and explicit unresolved boundaries in Phase 7.
- Java version upgrade, bytecode analysis, annotation-processor execution, or build-system changes; analysis honors the repository-declared language level and otherwise abstains.

## Decisions

### D1: Keep resolution ephemeral and extend the contract compatibly

**Decision:** `CodeFactResolution` remains a frozen ephemeral Pydantic model carried in `Evidence.attributes["code_fact_resolution"]` and `outcome_detail`. No new table, no new API field. The field-shaped contract is generalized to a symbol-shaped contract with backward-compatible aliases: `field_name` stays as an alias of `symbol_name`, `field_type` stays as an alias of `symbol_type_name`, `field_category` stays optional. New required fields are `fact_category`, `semantic_stage`, `analysis_method`, `language_version`, `assumptions`, and `conflicting_evidence_ids`. New optional fields are `target_identity`, `control_flow_refs`, `data_flow_refs`, and `call_target`. Old field-only readers keep working; new Java analyzers populate the general fields.

```python
# src/investigation_agent_platform/domain/code/models.py
from enum import StrEnum
from typing import Literal
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
    INITIALIZATION = "INITIALIZATION"
    POST_CONSTRUCTION = "POST_CONSTRUCTION"
    LATER_MUTATION = "LATER_MUTATION"
    RUNTIME_EFFECTIVE = "RUNTIME_EFFECTIVE"


class CodeFactResolution(BaseModel):
    """Ephemeral deterministic source-level fact.

    Carried with existing evidence; never a persistence aggregate.
    A RESOLVED value describes the named (category, semantics, stage) only.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    status: CodeFactStatus
    repository: str = Field(..., min_length=1, max_length=256)
    revision: str | None = Field(default=None, max_length=128)
    fact_category: JavaFactCategory = Field(default=JavaFactCategory.SOURCE_VALUE_FACT)
    semantic_stage: SemanticStage = Field(default=SemanticStage.INITIALIZATION)
    declaring_type: str = Field(..., min_length=1, max_length=512)
    symbol_name: str = Field(..., min_length=1, max_length=256, alias="field_name")
    symbol_type_name: str = Field(..., min_length=1, max_length=256, alias="field_type")
    field_category: Literal["INSTANCE_FIELD", "STATIC_FIELD"] | None = Field(default=None)
    value_semantics: CodeValueSemantics
    analysis_method: str = Field(default="tree-sitter-range-v1", max_length=128)
    language_version: str | None = Field(default=None, max_length=32)
    raw_initializer: str | None = Field(default=None, max_length=2048)
    resolved_value: str | None = Field(default=None, max_length=2048)
    target_identity: str | None = Field(default=None, max_length=1024)
    control_flow_refs: tuple[str, ...] = ()
    data_flow_refs: tuple[str, ...] = ()
    call_target: str | None = Field(default=None, max_length=1024)
    source_evidence_ids: tuple[UUID, ...] = ()
    conflicting_evidence_ids: tuple[UUID, ...] = ()
    declaration_location: SourceLocation
    limitation_codes: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()

    @property
    def field_name(self) -> str:
        return self.symbol_name

    @property
    def field_type(self) -> str:
        return self.symbol_type_name

    @model_validator(mode="after")
    def validate_resolution(self) -> "CodeFactResolution":
        if self.status == CodeFactStatus.RESOLVED and self.resolved_value is None:
            if self.fact_category not in {
                JavaFactCategory.DECLARATION_FACT,
                JavaFactCategory.CALL_TARGET_FACT,
                JavaFactCategory.CONTROL_FLOW_FACT,
            }:
                raise ValueError("resolved value facts require a resolved_value")
        if self.status == CodeFactStatus.RESOLVED and (
            not self.revision
            or not self.declaration_location.file_path
            or not self.declaration_location.revision
        ):
            raise ValueError(
                "resolved code facts require source path and immutable revision provenance"
            )
        if self.fact_category == JavaFactCategory.CAUSAL_CLAIM and self.status == CodeFactStatus.RESOLVED:
            raise ValueError("causal claims are never RESOLVED by static analysis alone")
        return self
```

Pydantic aliases preserve wire compatibility with the banked `code_fact_resolution` dicts written by the current worker hook. Existing `Evidence` shape validation keeps passing. The `CAUSAL_CLAIM` guard encodes the prompt rule that a possible mechanism is never promoted merely because its locations exist.

**Alternative considered:** New model per fact category (ten models). Rejected — multiplies verifier branches, evidence validation, and worker plumbing for no consumer benefit; one discriminated model with `fact_category` covers the matrix.

---

### D2: Layer the analyzer as a package, keep the field baseline as layer 1

**Decision:** Move `java_field_facts.py` logic into `src/investigation_agent_platform/application/analysis/java/` as thin re-export shims plus new modules, one per prompt layer. Each module is pure (evidence dict in, resolution out), reads no files, follows no network references, and returns fail-closed `UNRESOLVED`/`PARTIAL` with machine-readable `limitation_codes`. Shared provenance gating and Tree-sitter parsing live in `_provenance.py` and `_parse.py` so every phase inherits revision pinning.

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

The existing `application/analysis/java_field_facts.py` becomes a deprecated re-export so the worker hook and tests keep importing during migration. New code imports from the package.

**Alternative considered:** One monolithic `java_facts.py`. Rejected — the coverage matrix has eleven areas with different Tree-sitter node types and different verifier rules; a single module becomes unreviewable by Phase 3.

---

### D3: Resolve Java declarations and safe expressions before anything else

**Decision:** Phase 2 implements locals, parameters, `var` inference anchors, compile-time constants, and a conservative expression evaluator. Only side-effect-free expressions evaluate: literals, parenthesized literals, unary and binary operators over constants, string concatenation of constants, ternary over constant conditions, casts that narrow within a proven range, and `final`/`effectively-final` constant references inside the same range window. Anything with a method call, `new`, lambda, field access outside the window, array access with non-constant index, or unknown identifier returns `PARTIAL` with `unsupported_initializer_expression`. This is a direct generalization of the current `_literal_value`.

```python
# src/investigation_agent_platform/application/analysis/java/expressions.py
from __future__ import annotations

import ast
import re

_INTEGER_SUFFIX = re.compile(r"(?i)[l]$")
_FLOAT_SUFFIX = re.compile(r"(?i)[fd]$")


def normalize_java_literal(expression: str, field_type: str) -> str | None:
    """Conservative literal normalization; never executes application code."""
    raw = expression.strip()
    if raw.startswith("(") and raw.endswith(")"):
        raw = raw[1:-1].strip()
    if raw in {"true", "false"}:
        return raw if field_type == "boolean" else None
    if raw == "null":
        return (
            "null"
            if field_type
            not in {"boolean", "byte", "short", "int", "long", "char", "float", "double"}
            else None
        )
    if raw.startswith("'") and raw.endswith("'") and field_type == "char":
        try:
            value = ast.literal_eval(raw)
        except (SyntaxError, ValueError):
            return None
        return f"U+{ord(value):04X}" if isinstance(value, str) and len(value) == 1 else None
    if raw.startswith('"') and raw.endswith('"') and field_type in {"String", "java.lang.String"}:
        try:
            value = ast.literal_eval(raw)
        except (SyntaxError, ValueError):
            return None
        return value if isinstance(value, str) else None
    numeric = raw.replace("_", "")
    sign = ""
    if numeric[:1] in {"+", "-"}:
        sign, numeric = numeric[0], numeric[1:]
    if field_type in {"byte", "short", "int", "long", "char"}:
        numeric = _INTEGER_SUFFIX.sub("", numeric)
        try:
            base = (
                0
                if numeric.lower().startswith(("0x", "0b", "0o"))
                else (8 if len(numeric) > 1 and numeric.startswith("0") else 10)
            )
            number = int(numeric, base)
        except ValueError:
            return None
        number = -number if sign == "-" else number
        if field_type == "char":
            return f"U+{number:04X}" if 0 <= number <= 0xFFFF else None
        return str(number)
    if field_type in {"float", "double"}:
        numeric = _FLOAT_SUFFIX.sub("", numeric)
        try:
            return repr(float(sign + numeric))
        except ValueError:
            return None
    return None


_SAFE_BINARY = re.compile(r"^(.+?)([+\-*/%])$")
_FORBIDDEN_TOKENS = re.compile(r"(\bnew\b|->|::|\.\s*\w+\s*\(|\(\s*\w[\w.<>\[\]]*\s*\))")


def is_supported_constant_expression(expression: str) -> bool:
    """Gate for constant folding: literals, arithmetic, concatenation, ternary."""
    text = expression.strip()
    if _FORBIDDEN_TOKENS.search(text):
        return False
    if text.startswith(("\"", "'", "true", "false", "null")) or text[:1].isdigit() or text[:1] in "+-(":
        return True
    return False
```

Full constant folding across binary operators and ternaries is implemented in Phase 2 behind this gate; the gate itself ships first so unsupported expressions stay `PARTIAL` instead of silently unevaluated.

**Alternative considered:** Reusing a JVM or executing snippets. Rejected — violates the no-arbitrary-execution boundary and breaks replay determinism and tenant isolation.

---

### D4: Treat initialization and construction as bounded relationship analysis

**Decision:** Phase 3 resolves static and instance initialization order, constructor chaining (`this(...)`, `super(...)`), field hiding, and shadowing within one file and one revision. The analyzer collects `static_initializer`, `block`, `constructor_declaration`, and `field_declaration` nodes from the same range window, orders them per language rules (static initializers and static fields in textual order, then instance initializers and fields in textual order, then constructor body), and reports `POST_CONSTRUCTION` or `LATER_MUTABLE` semantics when a later write exists in the window. Cross-file superclass chains, DI-constructed instances, and Lombok-generated constructors return `PARTIAL` with `requires_cross_file_scope` or `requires_generated_source`. Enum constants, records, and compact constructors are explicit `PARTIAL` until their dedicated fixtures land.

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
    """Classify a declaration for ordering: static_init, instance_init, constructor."""
    node_type = declaration_node.type
    if node_type == "static_initializer":
        return "static_init"
    if node_type == "block":
        return "instance_init"
    if node_type == "constructor_declaration":
        return "constructor"
    return "field"
```

No value computed in Phase 3 is labeled `RUNTIME_EFFECTIVE`. A post-construction write proven in-source yields `POST_CONSTRUCTION`; any write outside the window yields `LATER_MUTABLE` at best.

**Alternative considered:** Whole-repo constructor graph. Rejected — needs cross-file revision-consistent reads and a call-graph join the gateway does not yet guarantee; bounded single-window analysis delivers the common single-class cases first.

---

### D5: Resolve call targets statically, never claim execution

**Decision:** Phase 4 implements overload resolution over statically known arity and type names, override-aware target naming (`declaring_type.method(signature)`), and bounded local data flow (reaching definitions within the window, early-return enumeration, definite-assignment checks). A `CALL_TARGET_FACT` names the statically resolved target and its declaration location. It never asserts the call executes. Reflection, dynamic proxies, lambdas passed across method boundaries, `var` with unknown inference, and cross-module calls resolve to `POSSIBLE_EFFECT_FACT` or `UNRESOLVED` with codes such as `dynamic_dispatch`, `reflection_boundary`, or `cross_module_scope`.

```python
# src/investigation_agent_platform/application/analysis/java/method_facts.py
from __future__ import annotations

from typing import Any


def overload_key(name: str, arity: int, type_names: list[str]) -> str:
    return f"{name}/{arity}({','.join(type_names)})"


def static_call_target(
    invocation_node: Any, source: bytes, candidates: dict[str, list[str]]
) -> str | None:
    """Return the unique static overload key, or None when ambiguous."""
    name_node = invocation_node.child_by_field_name("name")
    if name_node is None:
        return None
    name = source[name_node.start_byte : name_node.end_byte].decode("utf-8", errors="replace")
    args = invocation_node.child_by_field_name("arguments")
    arity = 0 if args is None else sum(1 for c in args.children if c.is_named)
    options = candidates.get(name, [])
    matches = [key for key in options if key.startswith(f"{name}/{arity}(")]
    return matches[0] if len(matches) == 1 else None
```

Data-flow facts record `data_flow_refs` as `file:line` definition-use pairs inside the window. Anything requiring interprocedural aliasing, stream laziness, or concurrent visibility is `PARTIAL`.

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

from typing import Any

CATEGORY_PROOF_RULES: dict[str, dict[str, Any]] = {
    "DECLARATION_FACT": {
        "allowed_semantics": {"EXPLICIT_INITIALIZER", "LANGUAGE_DEFAULT"},
        "requires_value_match": False,
        "requires_window_containment": True,
    },
    "SOURCE_VALUE_FACT": {
        "allowed_semantics": {"EXPLICIT_INITIALIZER", "LANGUAGE_DEFAULT"},
        "requires_value_match": True,
        "requires_window_containment": True,
    },
    "LANGUAGE_SEMANTIC_FACT": {
        "allowed_semantics": {"EXPLICIT_INITIALIZER", "LANGUAGE_DEFAULT"},
        "requires_value_match": True,
        "requires_window_containment": True,
    },
    "CONTROL_FLOW_FACT": {
        "allowed_semantics": {"EXPLICIT_INITIALIZER"},
        "requires_value_match": False,
        "requires_window_containment": True,
    },
    "DATA_FLOW_FACT": {
        "allowed_semantics": {"EXPLICIT_INITIALIZER"},
        "requires_value_match": False,
        "requires_window_containment": True,
    },
    "CALL_TARGET_FACT": {
        "allowed_semantics": {"EXPLICIT_INITIALIZER"},
        "requires_value_match": False,
        "requires_window_containment": True,
    },
    "POSSIBLE_EFFECT_FACT": {
        "allowed_semantics": {"EXPLICIT_INITIALIZER"},
        "requires_value_match": False,
        "requires_window_containment": True,
        "verdict_cap": "likely",
    },
    "CONFIGURATION_FACT": {
        "allowed_semantics": {"EXPLICIT_INITIALIZER"},
        "requires_value_match": True,
        "requires_window_containment": True,
    },
    "RUNTIME_OBSERVATION_FACT": {"verifiable_from_source": False},
    "CAUSAL_CLAIM": {"verifiable_from_source": False},
}


def category_proof_rule(category: str) -> dict[str, Any] | None:
    return CATEGORY_PROOF_RULES.get((category or "").upper())
```

The existing `claims_field_value` detector is generalized to `claims_java_fact` covering declaration, value, call-target, and branch language, while `code_value_proof` becomes `java_fact_proof` dispatching on `fact_category`. All existing field-value tests keep passing because `SOURCE_VALUE_FACT` with `EXPLICIT_INITIALIZER`/`LANGUAGE_DEFAULT` follows the current proof path byte for byte.

**Alternative considered:** One generic proof for all categories. Rejected — it would let a possible effect or runtime observation verify on source location alone, which is exactly the false-acceptance hole the prompt closes.

---

### D8: Wire the pipeline without new infrastructure

**Decision:** Reuse the current call path end to end. `question_shape.classify_question_shape` gains Java-specific source-value, call-target, and control-flow patterns. The reasoner emits typed `DecisionClaim` values with `fact_category`, `value_semantics`, and `claimed_value`. The worker `execute_action_activity` GET_CODE branch forwards `field_name`/`declaring_type` plus new optional `symbol_name`, `fact_category`, and `language_version` parameters into the matching resolver and banks the dumped resolution on the evidence row. The context compiler surfaces `code_fact_resolution` provenance in code packets. No new provider, no new queue, no new endpoint.

```python
# src/investigation_agent_platform/application/worker/activities.py
async def attach_java_fact(evidence: object, parameters: dict[str, object]) -> dict[str, object]:
    """Bank a deterministic Java fact on a GET_CODE evidence row (sketch)."""
    from investigation_agent_platform.application.analysis.java import resolve_java_field_fact

    outcome_detail: dict[str, object] = {}
    fact_field = str(parameters.get("field_name", "") or parameters.get("symbol_name", "") or "").strip()
    if fact_field:
        resolution = resolve_java_field_fact(
            evidence,
            fact_field,
            declaring_type=(str(parameters.get("declaring_type") or "").strip() or None),
        )
        dumped = resolution.model_dump(mode="json", by_alias=True)
        merged = dict(getattr(evidence, "attributes", {}) or {})
        merged["code_fact_resolution"] = dumped
        outcome_detail["code_fact_resolution"] = dumped
    return outcome_detail
```

Phase 7 framework boundaries (DI, config binding, generated sources, persistence, messaging, async execution) reuse the same hook: the resolver returns `CONFIGURATION_FACT` with the declared binding or `PARTIAL` naming the missing runtime evidence, and the verifier enforces that a declared binding never verifies as an applied configuration.

**Alternative considered:** Separate Java analysis service or queue. Rejected — no evidence the existing activity, evidence, and verifier abstractions cannot carry the load; a new service adds failure modes without new capability.

---

## Data Storage

No new persistence structures. No migration. No new table, index, or column.

The resolution stays ephemeral: it is dumped to JSON in `Evidence.attributes["code_fact_resolution"]` (validated against `CodeFactResolution` shape by `Evidence.validate_redaction_and_payload_invariants`) and mirrored in the activity `outcome_detail`. Existing `Evidence` rows, `InvestigationConclusion.next_check` (migration 022), and hypothesis assessment JSON carry the typed claim without schema changes.

Future persistence, if a concrete consumer ever needs cross-run caching of resolutions, would add a content-addressed sidecar keyed on `(repository, revision, file_path, symbol_identity, fact_category)` with the resolution JSON and a TTL tied to revision immutability. That consumer does not exist in this design, so the table is not created. This section records the decision explicitly so a later phase does not invent storage ad hoc.

## Data Structures

Input schemas are the existing `Evidence` (with `EvidenceProvenance`, `QueryFingerprint`, `SourceLocation`) plus action parameters. Output schema is the extended `CodeFactResolution` in D1 plus the `JavaFactCategory` and `SemanticStage` enums. Claim inputs reuse `ClaimTarget` (`declaring_type`, `value_semantics`, `claimed_value`) and `DecisionClaim` (`value_semantics`, `claimed_value`) with one addition: an optional `fact_category` string so the reasoner can name the intended proof rule.

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
    fact_category: str | None = Field(default=None, max_length=64)
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

Resolution provenance fields (`repository`, `revision`, `source_evidence_ids`, `declaration_location`, `limitation_codes`, `assumptions`, `conflicting_evidence_ids`, `analysis_method`, `language_version`) are mandatory on every status including `UNRESOLVED`, so the verifier and conclusion renderer never receive a bare reason string without identity.

## Interfaces

No new REST endpoints, CLI commands, MCP tools, TUI screens, or GUI components. All interfaces below are internal function contracts already present in the tree, extended with optional parameters only.

| Interface | Signature | Description |
| --------- | --------- | ----------- |
| `resolve_java_field_fact` | `(evidence, field_name, declaring_type=None) -> CodeFactResolution` | Existing field baseline; preserved as shim. |
| `resolve_java_local_fact` | `(evidence, symbol_name, method_scope=None, language_version=None) -> CodeFactResolution` | Phase 2 locals, params, `var` anchors. |
| `resolve_java_call_target` | `(evidence, symbol_name, declaring_type=None) -> CodeFactResolution` | Phase 4 static overload and override target. |
| `resolve_java_control_flow` | `(evidence, symbol_name, branch_hint=None) -> CodeFactResolution` | Phase 5 branch, return, and exception path. |
| `VerificationEngine.java_fact_proof` | `(hypothesis, evidence_items) -> tuple[bool, str]` | Per-category proof dispatch. |
| `VerificationEngine.validate_java_fact_claim` | `(hypothesis, evidence_items, contradictions) -> VerificationResult \| None` | Generalization of `validate_code_value_claim`. |

Action parameters accepted by the GET_CODE branch gain optional `symbol_name`, `fact_category`, and `language_version` keys. All are optional; absence preserves current field-only behavior. No OpenAPI change is required because action parameters are already free-form dicts validated per action.

Accessibility: no user-facing interface changes in this design, so no new color, keyboard, screen-reader, motion, or form requirements arise. If a later phase adds surfaced fact explanations to API responses or UI, that phase must include the standard accessibility section.

## Implementation Detail

Implementation proceeds in the prompt phases. Each phase lists objective, exact source paths, contract delta, and gate. Phase numbering matches `tmp/JAV_PROMPT.md` section 8.

### Phase 0 — Baseline and contract audit

Objective: prove the field baseline is real at HEAD and lock the extension contract. Non-goal: no new Java semantics.

- Confirm `application/analysis/java_field_facts.py`, `domain/code/models.py` (`CodeFactResolution`), `application/investigation/verification.py` (`code_value_proof`), `application/worker/activities.py` (GET_CODE hook), `domain/analysis/question_shape.py` (source-value routing), and `tests/unit/test_java_field_facts.py` behave as described in Context.
- Land D1 model extension with alias compatibility plus `CATEGORY_PROOF_RULES` table behind the existing proof path.
- Gate: full unit suite green, `test_java_field_facts.py` green unmodified, banked `code_fact_resolution` dicts from the old shape still validate.

### Phase 1 — Verifier integrity

Objective: close every promotion hole before new facts can flow. Non-goal: no new resolvers.

- Generalize `claims_field_value` to `claims_java_fact`, add `java_fact_proof` dispatch on `fact_category`, enforce revision and path identity, window containment, exact value comparison, and `RUNTIME_OBSERVATION_FACT`/`CAUSAL_CLAIM` never-verifiable-from-source.
- Wire `PARTIAL`/`UNRESOLVED` to typed limitations and `next_check` via existing `next_check_for` priority (`rival > relation > unmapped > provider`).
- Exact files: `application/investigation/verification.py`, `application/investigation/validator.py`, `application/investigation/reasoning.py`, `tests/unit/test_evidence_bound_verification.py`, `tests/unit/test_phase2_remaining.py`.
- Gate: counterexample tests where location is valid but value, semantics, declaring type, revision, or window is wrong all fail closed; no substring-only proof remains.

### Phase 2 — Java declarations and expressions

Objective: fields, locals, parameters, defaults, constants, literals, safe expressions. Non-goal: no constructors, no cross-file scope.

- Ship `java/_provenance.py`, `java/_parse.py`, `java/expressions.py`, `java/field_facts.py` (moved baseline), `java/local_facts.py`.
- Tree-sitter nodes: `field_declaration`, `variable_declarator`, `local_variable_declaration`, `formal_parameter`, `modifiers`, `type_identifier`, `generic_type`, `array_type`.
- Java rules: primitive and reference defaults, declaration without initializer, `final` and blank `final`, definite assignment, effectively-final, shadowed and inherited names (ambiguity fails closed), `var` recorded as declared type anchor only.
- Gate: parametrized literal and default tests extended to locals and params; method-call, `new`, lambda, and out-of-window references assert `PARTIAL`.

### Phase 3 — Initialization and object construction

Objective: static and instance initialization, constructor chaining, inheritance within one window, field hiding. Non-goal: no cross-file superclass, no DI, no Lombok execution.

- Ship `java/init_facts.py`; nodes `static_initializer`, `block`, `constructor_declaration`, `explicit_constructor_invocation`.
- Order facts per language rules; later in-window writes yield `POST_CONSTRUCTION`; out-of-window writes yield `LATER_MUTABLE`; enum, record, and compact-constructor cases yield explicit `PARTIAL`.
- Gate: chaining (`this(...)`/`super(...)`), hiding, and static-versus-instance order fixtures green; cross-file and generated-source cases assert named limitations.

### Phase 4 — Methods, overrides, and data flow

Objective: overload resolution, static dispatch naming, bounded local flow. Non-goal: no reflection, no proxies, no interprocedural aliasing.

- Ship `java/method_facts.py`, `java/dataflow.py`; nodes `method_declaration`, `method_invocation`, `object_creation_expression`, `return_statement`.
- Overload key `name/arity(types)`; unique match yields `CALL_TARGET_FACT`, otherwise `ambiguous_overload`. Reaching definitions and return-path enumeration stay inside the window; `data_flow_refs` carry definition-use pairs.
- Gate: overloaded, overridden, early-return, and definite-assignment fixtures green; reflection, proxy, lambda-escape, and cross-module cases abstain.

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

No `docs/dev/architecture.md` exists in this project; the architecture source of truth is `docs/IAP-architecture-v1.0.md`. This design requires no edits to that file to land Phases 0 through 2. When Phase 4 lands, append a short subsection under the verification chapter naming `JavaFactCategory`, `SemanticStage`, and the per-category proof table with a pointer to this design. When Phase 8 lands, append the fixture-commit pinning note and the six reported metrics. Until those phases land, the architecture file stays untouched.

## Migrations

No migrations. The `CodeFactResolution` extension is wire-compatible through Pydantic aliases (`field_name`, `field_type` continue to validate). `Evidence.attributes` is a free-form dict already validated against the model shape. Hypothesis assessments and `DecisionClaim` gain optional string fields that default to absent. Migration 022 (`InvestigationConclusion.next_check`) is reused as is.

Compatibility notes: old banked rows with only field-shaped keys validate under the new model with `fact_category` defaulting to `SOURCE_VALUE_FACT` and `semantic_stage` defaulting to `INITIALIZATION`. New rows with general keys are readable by old code only through the `field_name`/`field_type` aliases; rolling back the model after Phase 4 ships would drop category fidelity, so rollback past that point requires re-banking affected rows. No rollback is planned.

## Testing Philosophy

### Field baseline lock

Keep `tests/unit/test_java_field_facts.py` green unmodified through Phase 1. It is the contract anchor: literals, defaults, multi-declarator types, signs and bases, local rejection, partial on calls, ambiguity, declaring-type mismatch, non-Java path, path-only rejection, and missing-revision fail-closed. Any model change that breaks these tests is a compatibility break, not a feature.

### Contract and verifier tests

Test the model validators directly: resolved without value fails, resolved without revision or path fails, causal resolved fails. Test every row of the proof table: each verifiable category proves on exact match and fails on wrong value, wrong semantics, wrong declaring type, unbound evidence ID, revision mismatch, path mismatch, and window non-containment. Test that runtime and causal categories never verify from source evidence regardless of location quality.

### Per-phase fixture tests

Each phase ships its own test module mirroring the phase name (`test_java_locals.py`, `test_java_init.py`, `test_java_methods.py`, `test_java_flow.py`, `test_java_types.py`, `test_java_boundaries.py`). Fixtures are inline Java windows in the style of the field tests for unit speed, plus a small set of pinned synthetic-repo fixtures for end-to-end revision and windowing checks. Every supported feature has a correct-claim test and an incorrect-claim counterexample; every unsupported construct has a partial or unresolved test naming the limitation code.

### Integration and pipeline tests

Cover the worker hook (resolution banked on the evidence row and mirrored in outcome detail), the evidence shape validator (malformed resolutions rejected), question-shape routing (source-value to code, runtime-marked to non-code fast path), and conclusion mapping (limitations and conflicting evidence survive to the final answer). Keep deterministic unit tests separate from database and integration tests and from any live LLM evaluation; name environment prerequisites explicitly and report blockers instead of skipping silently.

### Evaluation and metrics

Phase 8 fixtures live under `tests/fixtures/java/` with a manifest pinning each file to a commit SHA. The battery grader reports precision, in-subset recall, false-verification rate, abstention correctness, provenance correctness, and repeatability as separate numbers. Partials fail the battery exit code per the existing Phase 4 convention. No metric may improve by weakening a verifier gate; gate changes require new counterexample tests first.

## Documentation Plan

### `docs/IAP-Code-Java-lang-impl-design.md`

**Audience:** Implementers and reviewers

This file. It carries the full contract, layer map, per-phase file lists, proof table, and gates. It is updated when a phase lands: move the phase from planned to as-built, record deviations, and link the landing tests. It is the only Java-implementation doc implementers need alongside the architecture file.

### `docs/IAP-architecture-v1.0.md`

**Audience:** Platform engineers and operators

Add the verification-chapter subsection described in Implementation Detail only when Phase 4 lands, and the fixture-metrics note only when Phase 8 lands. No other edits from this design. Each addition names the new enums, the proof table location, and the preserved ephemeral-storage decision.

### `docs/IAP_Arch_Eng_Recommendations.log`

**Audience:** Architecture and engineering stewards

Append one `PLANNED` entry when this design lands and flip it to `IMPLEMENTED` phase by phase as gates pass, per the ledger rule. Each entry cites the phase, the gate evidence (test counts, suite result), and the doc reference. Declined scope (symbolic execution, whole-repo points-to, container startup) is recorded as `DECLINED` with reason rather than silently dropped.

### `tests/fixtures/java/README.md`

**Audience:** Test authors

Document the fixture manifest format (file, commit SHA, language version, covered prompt area, positive and negative claims, expected status and limitation codes). Explain how to add a fixture without breaking pinning, and how the battery consumes the manifest. This file is created with Phase 2 and extended through Phase 8.

## Risks / Trade-offs

### Window too small to see the truth

**Risk:** Range reads sized for the snippet budget can exclude the decisive write, override, or branch while still citing a valid nearby location, letting a plausible but wrong value verify.

**Mitigation:** Window-containment checks on declaration ranges and claimed lines, `truncated` and `next_window` disclosure from the provider, compiler packet budgets that shrink within the packet rather than silently, and `LATER_MUTABLE` as the default semantics whenever a later write could exist outside the window.

### Tree-sitter grammar drift from the repository Java version

**Risk:** Newer syntax (records, sealed types, pattern matching, string templates) parses differently or errors under the bundled grammar, producing false unresolved or false resolved results.

**Mitigation:** `language_version` travels with every resolution, version-gated syntax without a known version abstains, grammar errors fail closed to `java_syntax_error_or_incomplete_source_window`, and Phase 6 fixtures pin one language level per file.

### Overload and override misresolution

**Risk:** Name-only matching picks the wrong overload or the wrong hierarchy member, especially with boxing, varargs, generics, and default interface methods.

**Mitigation:** Arity plus type-name matching, unique-match-or-abstain rule, `ambiguous_overload` limitation, declaring-type conformance in the verifier, and no execution claim from a resolved target.

### Possible-versus-observed confusion

**Risk:** Static possibilities (a catch that exists, a branch that parses, a config that is declared) leak into conclusions as observed behavior or root causes.

**Mitigation:** Category proof table caps possible effects at likely, bars runtime and causal categories from source-only verification, and conclusion mapping demotes prose-only or single-row causal sentences. Counterexample tests guard each promotion path.

### Scope creep into execution and frameworks

**Risk:** Pressure to prove runtime-effective values pulls in container startup, build execution, or production reads inside the analyzer, breaking determinism, tenant isolation, and replay safety.

**Mitigation:** Hard phase boundary at Phase 7: the analyzer names the required runtime evidence and abstains. No file reads, no network calls, no subprocess builds inside resolvers. Framework behavior is a declared binding or an explicit unresolved, never an inferred runtime fact.

### Ephemeral-only storage limits debugging

**Risk:** Without persisted resolutions, operators cannot audit what the analyzer claimed on a past run beyond the banked evidence attributes.

**Mitigation:** Resolutions are already banked on evidence rows and mirrored in outcome detail with stable IDs, revisions, and limitation codes, which is sufficient for audit and replay. A content-addressed sidecar is deferred until a concrete consumer demonstrates the need.
