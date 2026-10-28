# Java Semantic Attribution Design v1.0

Status: DESIGN. This document defines the architecture for adding compiler-backed Java semantic attribution to the existing Tree-sitter Java query stack. It does not authorize deployment, dependency changes, or replacement of the current syntax model.

## Executive Decision

Use a JVM semantic-analysis sidecar, with **Eclipse JDT Core** as the preferred engine. Retain the Python Tree-sitter model as the fast, tolerant, source-preserving syntax layer. Expose semantic attribution through a language-neutral protocol and join results back to syntax records using revision-pinned source spans and a compilation-context digest.

Use **javac APIs** as the alternative when exact behavior must match the project’s Java compiler or JDK. Do not use JavaParser Symbol Solver, Spoon, Checker Framework, TypeTools, or generics-resolver as the primary semantic authority for the full generic-attribution problem. They remain viable for narrower or future use cases.

## Problem

The current Java model intentionally preserves syntax and abstains from semantic attribution. It can represent:

- Generic declarations and bounds as source syntax.
- Parameterized type uses.
- Nested generic segments.
- Wildcard forms.
- Diamond syntax.
- Method, field, local, and parameter source contexts.

It cannot safely determine:

- Which declaration an imported or unqualified name denotes.
- Whether a name is a type variable, declared type, or raw generic use.
- Whether type arguments satisfy declaration bounds.
- How enclosing generic types substitute into member signatures.
- The captured type represented by a wildcard use.
- The erased type of a parameterized or type-variable use.
- The inferred arguments of diamond or generic method calls.
- Whether overload resolution selects a generic method based on target typing.

These are compiler-semantic questions, not Tree-sitter syntax questions.

## Goals

- Resolve Java declarations and types against an explicit compilation context.
- Preserve source syntax as the authoritative source record; semantic facts are additive.
- Attribute generic declarations, parameterized types, wildcards, type variables, nested types, arrays, erasure, capture, and substituted members.
- Return explicit diagnostics and incomplete-context states rather than guesses.
- Join semantic results to existing Python model records by source identity, not display names.
- Keep the system usable when attribution is unavailable: syntax queries continue to work.

## Non-goals

- Replacing Tree-sitter as the syntax parser.
- Treating compiler output as runtime behavior.
- Hiding compiler errors or unresolved dependencies.
- Implementing a complete Java build system inside the investigation service.
- Making semantic attribution available for unpinned or ambiguous revisions.
- Adding generic method inference and overload integration in the first milestone.

## Authoritative Semantic Boundaries

The design follows the Java Language Specification:

- Type variables and bounds: [JLS §4.4](https://docs.oracle.com/javase/specs/jls/se25/html/jls-4.html#jls-4.4).
- Parameterized types and arguments: [JLS §§4.5–4.5.1](https://docs.oracle.com/javase/specs/jls/se25/html/jls-4.html#jls-4.5.1).
- Type erasure: [JLS §4.6](https://docs.oracle.com/javase/specs/jls/se25/html/jls-4.html#jls-4.6).
- Raw types: [JLS §4.8](https://docs.oracle.com/javase/specs/jls/se25/html/jls-4.html#jls-4.8).
- Capture conversion: [JLS §5.1.10](https://docs.oracle.com/javase/specs/jls/se25/html/jls-5.html#jls-5.1.10).
- Generic class creation and diamond: [JLS §15.9](https://docs.oracle.com/javase/specs/jls/se25/html/jls-15.html#jls-15.9).
- Expression/type attribution: [JLS §15.3](https://docs.oracle.com/javase/specs/jls/se25/html/jls-15.html#jls-15.3).

The source model follows the pinned Tree-sitter Java grammar. Tree-sitter remains a concrete syntax parser and is not treated as a type checker.

## Library Evaluation

### Eclipse JDT Core: recommended

[Eclipse JDT Core](https://github.com/eclipse-jdt/eclipse.jdt.core) provides a source-oriented AST and compiler-style bindings. Its `ITypeBinding` API distinguishes:

- Generic declarations.
- Parameterized types.
- Raw types.
- Type variables.
- Wildcards.
- Capture bindings.
- Type bounds.
- Erasures.
- Declaration bindings.
- Substituted member and executable signatures.

It provides source offsets, headless operation, classpath/sourcepath configuration, and IDE-style recovery for incomplete source. This matches the project’s need for semantic attribution while preserving source-oriented joins.

License: Eclipse Public License 2.0, with the project’s stated secondary-license terms. License review is required before distribution of a packaged sidecar.

References:

- [JDT Core guide](https://help.eclipse.org/latest/topic/org.eclipse.jdt.doc.isv/guide/jdt_int_core.htm)
- [`ITypeBinding`](https://help.eclipse.org/latest/topic/org.eclipse.jdt.doc.isv/reference/api/org/eclipse/jdt/core/dom/ITypeBinding.html)
- [`ASTNode`](https://help.eclipse.org/latest/topic/org.eclipse.jdt.doc.isv/reference/api/org/eclipse/jdt/core/dom/ASTNode.html)
- [JDT license](https://github.com/eclipse-jdt/eclipse.jdt.core/blob/master/LICENSE)

### javac APIs: compiler-fidelity alternative

The JDK compiler API provides `JavaCompiler`, `JavacTask`, `Trees`, `Elements`, `Types`, and `TypeMirror` implementations. The [`Types`](https://docs.oracle.com/en/java/javase/25/docs/api/java.compiler/javax/lang/model/util/Types.html) API directly supports:

- `capture`.
- `erasure`.
- `asMemberOf`.
- `isSubtype`.
- `isAssignable`.
- `getDeclaredType`.
- `getWildcardType`.

javac is preferred when results must match the selected JDK compiler exactly. It is less convenient as a long-running source-analysis service, and useful functionality may require implementation-specific `com.sun.tools.javac.*` APIs and module exports.

References:

- [OpenJDK compiler overview](https://openjdk.org/groups/compiler/doc/package-overview/index.html)
- [`TypeMirror`](https://docs.oracle.com/en/java/javase/25/docs/api/java.compiler/javax/lang/model/type/TypeMirror.html)
- [`DeclaredType`](https://docs.oracle.com/en/java/javase/25/docs/api/java.compiler/javax/lang/model/type/DeclaredType.html)
- [`WildcardType`](https://docs.oracle.com/en/java/javase/25/docs/api/java.compiler/javax/lang/model/type/WildcardType.html)
- [`Types`](https://docs.oracle.com/en/java/javase/25/docs/api/java.compiler/javax/lang/model/util/Types.html)

### Other options

| Library | Position in this architecture | Reason |
| --- | --- | --- |
| JavaParser + Symbol Solver | Optional lower-authority resolver | Easy Java integration, source ranges, imports/JAR solvers, and useful common-case resolution; less authoritative for capture, complex inference, and substituted members. [Project](https://github.com/javaparser/javaparser) |
| Spoon | Optional source transformation/metamodel tool | Excellent source model, shadow types, and source positions; not the preferred compiler-semantic authority for capture and full member substitution. [Project](https://github.com/INRIA/spoon) |
| Checker Framework | Add-on only | Extends javac with pluggable qualifier systems; not the primary declaration resolver. [Project](https://github.com/typetools/checker-framework) |
| TypeTools | Not suitable | Runtime reflection utility, no source spans or source attribution. [Project](https://github.com/jhalterman/typetools) |
| generics-resolver | Not suitable | Runtime hierarchy generic resolution, not source semantic analysis. [Project](https://github.com/xvik/generics-resolver) |

## Architecture

```text
Python investigation service
        |
        | JSON Lines RPC over stdin/stdout or local HTTP
        v
JVM semantic attribution sidecar
  - JDT Core preferred
  - explicit source roots
  - explicit generated-source roots
  - explicit classpath/module path
  - explicit Java release and compiler options
        |
        +--> declaration bindings
        +--> attributed type graph
        +--> generic substitutions
        +--> capture and erasure views
        +--> diagnostics and source spans

Python Tree-sitter model remains available independently for syntax queries.
```

### Why a sidecar

- The application is Python-based; embedding a JVM compiler directly is not practical.
- A sidecar isolates JDT/JDK lifecycle, heap usage, module exports, and licensing.
- The protocol can support JDT first and javac later without changing Python query consumers.
- Sidecar failures degrade semantic attribution, not syntax parsing.

## Compilation Context

Semantic results are valid only relative to an explicit context.

```text
CompilationContext:
  repository
  revision
  source_roots[]
  source_files[]
  generated_source_roots[]
  classpath_entries[]
  module_path_entries[]
  java_release
  source_level
  preview_enabled
  compiler_options[]
  sidecar_engine
  sidecar_engine_version
  context_digest
```

### Context rules

- `repository` and `revision` are mandatory and revision-pinned.
- Paths are normalized and ordered before hashing.
- Classpath/module entries include path plus content or artifact digest where available.
- Java release and preview settings are part of the digest.
- Generated sources must be explicit; the sidecar must not discover arbitrary files outside the manifest.
- Missing dependencies produce `INCOMPLETE_CONTEXT`, not best-effort resolution.
- Results from a different context digest are incompatible even when file paths and source spans match.

## Sidecar Protocol

Use JSON Lines initially for simple process supervision and deterministic replay. Each request and response carries a protocol version.

### Request

```json
{
  "protocol_version": "iap/java-attribution/v1",
  "request_id": "uuid",
  "context": {
    "repository": "repo",
    "revision": "commit",
    "source_roots": ["src/main/java"],
    "classpath_entries": [],
    "java_release": 17,
    "context_digest": "sha256:..."
  },
  "queries": [
    {
      "file_path": "src/Box.java",
      "start_offset": 42,
      "end_offset": 43,
      "operation": "attribute_type"
    }
  ]
}
```

### Response

```json
{
  "protocol_version": "iap/java-attribution/v1",
  "request_id": "uuid",
  "engine": "jdt",
  "engine_version": "...",
  "context_digest": "sha256:...",
  "results": [
    {
      "status": "RESOLVED",
      "source": {"file_path": "src/Box.java", "start_offset": 42, "end_offset": 43},
      "type": {
        "kind": "TYPE_VARIABLE",
        "display_text": "T",
        "declaration_key": "...",
        "upper_bounds": [],
        "lower_bound": null,
        "erased_type": "java.lang.Number"
      },
      "diagnostics": []
    }
  ]
}
```

### Failure states

Use a closed result status:

- `RESOLVED`: semantic identity/type established in the specified context.
- `PARTIAL`: some semantic data exists, but an unresolved dependency, recovered AST, or unsupported construct limits completeness.
- `UNRESOLVED`: the requested symbol/type has no safe semantic answer.
- `INCOMPLETE_CONTEXT`: attribution was blocked by missing source/dependency/build context.
- `COMPILER_ERROR`: the compiler produced an error relevant to the requested result.
- `SIDECAR_FAILURE`: the helper failed operationally; preserve the error class without converting it into absence.

No failure state may be serialized as a successful resolved type.

## Semantic Type Model

The semantic model is additive to the existing syntax records.

```text
SemanticType:
  kind: DECLARED | TYPE_VARIABLE | WILDCARD | ARRAY | PRIMITIVE |
        VOID | NULL | INTERSECTION | ERROR | UNKNOWN
  display_text
  qualified_name
  declaration_key
  type_arguments[]
  enclosing_type
  upper_bounds[]
  lower_bound
  erased_type
  captured_from
  source_span
  diagnostics[]
```

### Generic semantics

- `DECLARED` distinguishes generic declaration identity from a particular parameterization.
- `type_arguments` recursively preserve explicit arguments.
- `WILDCARD` preserves upper/lower bounds and source origin.
- `TYPE_VARIABLE` identifies its declaring type/method parameter and effective bounds.
- `captured_from` identifies compiler-generated capture derived from a wildcard; it never replaces the source wildcard record.
- `erased_type` is derived and separate from the attributed source type.
- `enclosing_type` preserves substitutions for nested classes such as `Outer<String>.Inner<Integer>`.
- Raw types are semantic classification only; no syntax-level record may claim rawness.

## Query Operations

First semantic milestone:

- `attribute_declaration_at(file, offset)`.
- `attribute_type_at(file, offset)`.
- `attribute_generic_use(use_identity)`.
- `attribute_type_parameter(parameter_identity)`.
- `attribute_member_as_type(containing_type, member_identity)`.

Later, separately authorized:

- `capture_type(type_identity)`.
- `erase_type(type_identity)`.
- `infer_generic_invocation(invocation_identity)`.
- `resolve_overload_with_generic_inference(invocation_identity)`.

## Mapping Back to Python Records

Join semantic results to syntax records using:

1. Repository and revision equality.
2. Context-digest equality.
3. Normalized file path equality.
4. Exact or bounded source offset/range match.
5. Syntax record identity as a final local key.

Never join by simple name alone. Multiple source records may legitimately share names due to overloads, nested scopes, or shadowed type parameters.

## Caching

Cache keys must include:

```text
(repository, revision, context_digest, file_path, start_offset, end_offset, operation)
```

The sidecar may cache parsed/attributed compilation contexts internally, but cache invalidation must occur when any source, classpath, module-path, compiler option, Java release, or sidecar engine version changes.

## Rollout Plan

### Phase 0: Context and sidecar proof

- Build a minimal JDT sidecar.
- Attribute one synthetic source unit.
- Return declaration identity, type kind, source range, diagnostics, and context digest.
- Test missing dependency and malformed source behavior.

Exit: deterministic protocol fixtures and no false success on incomplete context.

### Phase 1: Declaration and generic-use attribution

- Attribute class/method type parameters and bounds.
- Attribute parameterized uses and wildcards.
- Attribute nested enclosing types and arrays.
- Preserve existing syntax records unchanged.

Exit: fixture parity for declaration identity, arguments, wildcard bounds, and source ranges.

### Phase 2: Derived semantic views

- Add raw-type classification.
- Add erasure.
- Add effective type-variable bounds.
- Add substituted member signatures using `asMemberOf` or JDT equivalents.

Exit: positive, invalid, missing-classpath, nested, and wildcard fixtures.

### Phase 3: Capture conversion

- Add compiler-generated capture records.
- Preserve wildcard origin.
- Test `List<?>`, bounded wildcard, nested wildcard, and member access.

Exit: no capture identity is confused with a declared type variable or source wildcard.

### Phase 4: Generic invocation inference

Defer until a concrete product query requires it. This phase crosses generic inference, target typing, overload resolution, lambdas, method references, and constructor inference.

## Library Selection Record

### Preferred: Eclipse JDT Core

JDT is preferred for source-oriented binding APIs, generic capture/raw/parameterized distinctions, source recovery, and headless operation.

- [JDT Core guide](https://help.eclipse.org/latest/topic/org.eclipse.jdt.doc.isv/guide/jdt_int_core.htm)
- [`ITypeBinding`](https://help.eclipse.org/latest/topic/org.eclipse.jdt.doc.isv/reference/api/org/eclipse/jdt/core/dom/ITypeBinding.html)
- [`ASTNode`](https://help.eclipse.org/latest/topic/org.eclipse.jdt.doc.isv/reference/api/org/eclipse/jdt/core/dom/ASTNode.html)
- [JDT license](https://github.com/eclipse-jdt/eclipse.jdt.core/blob/master/LICENSE)

### Alternative: javac

Use javac when exact alignment with the selected JDK compiler is the primary requirement. It provides authoritative `TypeMirror` and `Types` operations but is more sensitive to JDK modules, compiler lifecycle, and implementation details.

- [OpenJDK compiler overview](https://openjdk.org/groups/compiler/doc/package-overview/index.html)
- [`Types`](https://docs.oracle.com/en/java/javase/25/docs/api/java.compiler/javax/lang/model/util/Types.html)
- [`DeclaredType`](https://docs.oracle.com/en/java/javase/25/docs/api/java.compiler/javax/lang/model/type/DeclaredType.html)

### Not primary authorities

- [JavaParser Symbol Solver](https://github.com/javaparser/javaparser): practical common-case resolution, but lower authority for capture and complex generic substitution.
- [Spoon](https://github.com/INRIA/spoon): excellent source metamodel and transformation framework, but not the selected compiler-semantic authority.
- [Checker Framework](https://github.com/typetools/checker-framework): use for pluggable qualifier checking, not base attribution.
- [TypeTools](https://github.com/jhalterman/typetools) and [generics-resolver](https://github.com/xvik/generics-resolver): runtime reflection utilities, not source attribution.

## Risks

- Wrong or incomplete classpath can produce plausible but false results; context digest and explicit incomplete status are mandatory.
- JDT and javac may differ in recovery and diagnostic behavior; engine identity must be persisted.
- Tree-sitter and compiler source offsets can diverge around Unicode, comments, and recovered syntax; add offset-mapping fixtures.
- Annotation processors and generated sources can change the attributed world; they must be explicit context inputs.
- Raw, wildcard, capture, erasure, and substitution are different concepts and must remain different result fields.
- Generic method inference is coupled to overload resolution and target typing; keep it out of the first semantic milestone.

## Acceptance

The semantic attribution design is ready for implementation when:

- The sidecar engine and license choice are approved.
- Compilation-context construction is specified for the supported build systems.
- Protocol fixtures cover resolved, partial, unresolved, incomplete-context, compiler-error, and sidecar-failure outcomes.
- Generic fixtures cover nested types, bounds, wildcards, raw uses, capture candidates, erasure, and missing dependencies.
- Syntax records remain unchanged and semantic records are additive.
- No semantic result can be accepted without repository, revision, source span, engine version, and context digest.
