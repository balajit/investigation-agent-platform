# Java Generics Query Design v1.0

Status: APPROVED FOR IMPLEMENTATION on `iap-java-query`. This design adds a syntax-preserving generic query layer. It does not implement Java type attribution, substitution, capture conversion, erasure, raw-type classification, or compile-time type checking.

## Decision

Represent generic declarations and generic type-use syntax as typed, revision-pinned model records. Preserve nested type segments, wildcard forms, explicit bounds, arrays, context, owner identity, and source spans. Treat semantic classification as unresolved unless a later compiler-backed layer is explicitly authorized.

Tree-sitter is the parser and matcher substrate. The current lockfile pins tree-sitter 0.26.0 and tree-sitter-language-pack 1.20.0. Query records must carry the existing parser identity and use the grammar's named fields rather than angle-bracket text splitting.

## Goals

- Query class/interface/method/constructor type-parameter declarations.
- Query explicit parameterized type occurrences in supported declaration and expression contexts.
- Preserve nested generic structure such as `Outer<String>.Inner<List<Integer>>`.
- Distinguish `?`, `? extends T`, and `? super T`.
- Preserve arrays inside type arguments and bounds.
- Represent diamond creation as inference syntax, not explicit arguments.
- Keep unresolved names and unsupported semantic questions explicit.

## Non-goals

- Determine whether a bare name is a type variable, declared type, or raw generic use.
- Prove that type arguments satisfy declaration bounds.
- Resolve imports, inheritance, substitutions, member types, or overloads.
- Perform capture conversion or erasure.
- Infer diamond or generic-method type arguments.
- Validate type-use annotation applicability.
- Replace existing `MethodDef.param_types` and `return_type` strings until parity and schema migration are separately approved.

## JLS Boundary

- Type variables and declaration bounds follow JLS §4.4.
- Parameterized types and nested parameterization follow JLS §§4.5–4.5.1.
- Raw types are defined in JLS §4.8 and cannot be identified safely from an unqualified syntax name alone.
- Erasure is JLS §4.6 and is semantic, not a syntax normalization.
- Diamond inference belongs to JLS §15.9 and must remain an explicit inference marker.
- Expression lvalue/value distinctions follow JLS §15.1; this design records type syntax only.

References:

- https://docs.oracle.com/javase/specs/jls/se25/html/jls-4.html#jls-4.4
- https://docs.oracle.com/javase/specs/jls/se25/html/jls-4.html#jls-4.5
- https://docs.oracle.com/javase/specs/jls/se25/html/jls-4.html#jls-4.5.1
- https://docs.oracle.com/javase/specs/jls/se25/html/jls-4.html#jls-4.6
- https://docs.oracle.com/javase/specs/jls/se25/html/jls-4.html#jls-4.8
- https://docs.oracle.com/javase/specs/jls/se25/html/jls-15.html#jls-15.9
- https://github.com/tree-sitter/tree-sitter-java/blob/master/grammar.js

## Model Records

### TypeParameterDecl

One record per formal type parameter.

- `identity`: stable owner-qualified identity plus ordinal, never name alone.
- `name`: source identifier.
- `owner_identity`: class/interface/method/constructor identity.
- `owner_kind`: closed owner category.
- `bounds`: ordered recursive `TypeSyntax` records; empty means no explicit bound.
- `annotations`: source annotations attached to the type parameter, retained as text/spans only.
- `span`: declaration source span.

An omitted bound remains omitted. Do not inject implicit `Object` into the syntax record.

### TypeSyntax

Recursive source syntax for one type position.

- `kind`: closed syntax kind: `named`, `parameterized`, `wildcard`, `array`, `annotated`, `primitive`, `void`, `inferred`, or `unknown`.
- `text`: normalized source text.
- `name`: name text when applicable.
- `segments`: nested `TypeSegment` records for qualified/member type structure.
- `arguments`: recursive `TypeArgument` records.
- `element`: recursive element type for arrays.
- `dimensions`: array dimension count and span/text metadata.
- `annotations`: source annotation records attached to the type position.
- `span`: source span.

### TypeSegment

One segment of a qualified/member type occurrence.

- `name`: segment source name.
- `arguments`: arguments owned by this segment.
- `text`: normalized segment text.
- `span`: source span.

This preserves `Outer<String>.Inner<List<Integer>>` without flattening ownership.

### TypeArgument

One actual type argument.

- `kind`: `explicit_type`, `wildcard_unbounded`, `wildcard_extends`, or `wildcard_super`.
- `type`: recursive `TypeSyntax` for explicit or bounded arguments.
- `bound_kind`: `extends`, `super`, or absent.
- `span`: source span.

Primitive arguments are retained as syntax and marked `unknown` semantically; the query layer does not decide whether the source is legal Java.

### GenericTypeUse

One generic occurrence in a source context.

- `identity`: stable occurrence ID from owner identity and span.
- `owner_identity`: enclosing type/method/constructor identity.
- `context`: closed context: `type_parameter_bound`, `field`, `local`, `parameter`, `receiver`, `return`, `throws`, `extends`, `implements`, `permits`, `cast`, `instance_creation`, `method_type_arguments`, `method_reference_type_arguments`, or `other`.
- `syntax`: recursive `TypeSyntax`.
- `syntax_kind`: `parameterized`, `diamond`, or `array_parameterized`.
- `resolution`: `UNRESOLVED` in this phase; no inferred semantic identity.
- `span`: source span.

Diamond creation uses `syntax_kind=diamond`, `resolution=UNRESOLVED`, and an explicit `inferred_arguments=true` marker. It is not represented as a parameterized type with zero arguments.

## Owner Identity

Use the existing class-qualified method/type identity conventions:

- Top-level type: `package.Type` when package is present.
- Nested type: `package.Outer.Inner`.
- Method: `package.Type.method#overload-key`.
- Constructor: `package.Type.Type#overload-key`.

Type parameter identities append an ordinal/name component, for example:
`package.Type.method#method/1(T@0)`.

## Query API

Add only these initial predicates/accessors:

- `all_type_parameters(models)`
- `type_parameters_for(models, owner_identity)`
- `all_generic_type_uses(models)`
- `generic_type_uses_for(models, owner_identity, context=None)`
- `type_uses_named(models, name, context=None)` using exact syntax-name matching only.
- `type_uses_wildcard(models, kind=None, bound_name=None)`.
- `type_uses_diamond(models)`.

Queries expose source records. They do not imply type resolution, validity, bounds compatibility, rawness, or runtime behavior.

## Parsing Strategy

Use Tree-sitter named fields and recursive conversion:

- `type_parameters` → `TypeParameterDecl`.
- `type_parameter` and `type_bound` → ordered bounds.
- `generic_type` and `type_arguments` → `GenericTypeUse`, `TypeSyntax`, `TypeSegment`, and `TypeArgument`.
- `wildcard` → exact wildcard kind and optional recursive bound.
- `array_type` → recursive element type and dimensions.
- `object_creation_expression` with an empty `type_arguments` node → diamond marker.
- Declaration fields determine `context`; do not infer context from text.

Do not use regular expressions or angle-bracket splitting for nested generic structure. Preserve malformed/incomplete syntax through the existing syntax-error model; do not emit complete records from an erroneous subtree without marking coverage limitations.

## Phases

### Phase 0: Grammar fixtures

Add fixtures for:

- `class Box<T>` and `<T extends Number & Comparable<T>>`.
- Generic methods and constructors with shadowed type-parameter names.
- `List<String>`, `Map<String, List<Integer>>`.
- `Outer<String>.Inner<List<Integer>>`.
- `List<?>`, `List<? extends Number>`, and `List<? super Integer>`.
- Generic arrays, type-variable arrays, and nested arrays.
- Fields, locals, parameters, receivers, returns, casts, throws, and creation contexts.
- `new Box<>()` and explicit `new Box<String>()`.
- Type-use and type-parameter annotations as retained syntax.
- Invalid primitive type arguments, missing arguments, and incomplete windows.

Pin expected record trees, spans, owner identities, and unresolved statuses.

### Phase 1: Typed model records

Add the four record families to `model.py` and corresponding `JavaModel` collections. Populate them from the shared parse during the existing model build. Keep generic declaration text in the current type snapshots for compatibility, but make the new records authoritative for queries.

### Phase 2: Query predicates

Add the query API and documentation entries. Add vocabulary drift tests covering all public generic predicates and all closed enum values.

### Phase 3: Parity and negative coverage

Compare the new records against fixture goldens. Assert that no record claims resolved/raw/type-correct semantics. Existing type resolver tests must remain green, including erasure-sensitive abstention.

### Phase 4: Optional semantic design

Only after syntax coverage is stable, decide whether any question requires compiler attribution. If approved, add a separate semantic record family backed by javac or another compiler service. Keep `TypeSyntax` immutable and lossless; semantic records are additive.

## Risks and Mitigations

- **Shadowed type variables:** owner-qualified identities and ordinals prevent collisions.
- **Nested type arguments:** per-segment argument lists preserve ownership.
- **Raw type ambiguity:** leave `resolution=UNRESOLVED`; never infer rawness from missing arguments.
- **Diamond:** model as inference syntax with no invented arguments.
- **Wildcard semantics:** preserve exact form; no capture conversion in the syntax layer.
- **Arrays/annotations:** retain recursive element/dimension/annotation structure rather than flattening text.
- **Grammar drift:** parser version and fixture goldens gate grammar changes.
- **Invalid source:** syntax-error state prevents complete absence or validity claims.

## Acceptance Gates

- All fixture record goldens pass deterministically.
- All generic query predicates have vocabulary entries and contract tests.
- Existing Java resolver and full unit suites pass unchanged.
- No syntax record claims symbol resolution, bounds validity, rawness, substitution, capture, erasure, or runtime type behavior.
- Ruff, formatting, and `git diff --check` pass.
