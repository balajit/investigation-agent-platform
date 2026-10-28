# Design: Java proxy-frame canonicalization

Runtime-generated proxy classes (JDK dynamic proxies, Spring CGLIB, Hibernate lazy-loading)
appear in stack traces under generated names that fragment error identity, defeat call
resolution, and would duplicate knowledge-bank learning on every redeploy. This design adds
intake-time canonicalization: every proxy frame is served in canonical + raw form, canonical
names are matching keys (never proof), resolution through a proxy still abstains, and
downstream identity (error signatures, bank derivation) converges.

Status: **PROPOSED — awaiting owner mark.** Sections tagged PROPOSED record a recommended
default for an owner-open question. Priority relative to the red-tree fixes is also an
owner call (recommended: after the dataflow fail-open and gate recovery).

## Context

Stack traces are parsed by `application/evidence/stackframes.py` into `{file, line, function}`
frames verbatim — `com.sun.proxy.$Proxy42.handler`, `com.foo.Bar$$EnhancerBySpringCGLIB$$1a2b3c`
pass through with generated numbers and hash suffixes. Java call-target resolution abstains
on proxy *creation* markers (`Proxy.newProxyInstance`/`InvocationHandler` → `reflection_boundary`)
but nothing maps a proxy *frame* back to an interface or implementation. Error signatures and
code-issue fingerprints hash verbatim text, so the same failure through `$Proxy42` vs `$Proxy43`
is two different signatures. The knowledge-bank component spec (`IAP-knowledge-bank-component-spec-v1.0.md`)
D4 derives bank identity from frame class names with no proxy rule — every redeploy registers
as new knowledge.

A read-only survey of the local CodeQL repository (`~/workspace/repos/opensource/codeql/codeql`,
`java/` tree) confirmed CodeQL models none of this either: one auto-generated taint step on
`InvocationHandler.invoke`, explicit neutral/no-flow entries for `newProxyInstance`, zero
references to CGLIB/ByteBuddy/Hibernate proxies, no proxy call edges anywhere. This design is
greenfield, not catch-up. Full survey: `docs/IAP-codeql-gap-analysis-v1.0.md`.

No `docs/dev/architecture.md` exists in the project; architecture changes are recorded
against `docs/IAP-architecture-v1.0.md` (see Documentation Plan).

## References

- `src/investigation_agent_platform/application/evidence/stackframes.py` — Java/Python frame parsers; canonicalization lands here so every consumer benefits.
- `src/investigation_agent_platform/application/analysis/java/method_facts.py` — Proxy/reflection abstention site (`reflection_boundary`); canonicalization does not weaken these guards.
- `src/investigation_agent_platform/application/analysis/java/data/framework_boundaries_v1.json` — `dynamic_proxy_boundary` marker catalog; canonical frame kinds align with these codes.
- `src/investigation_agent_platform/domain/knowledge/fingerprint.py` — Code-issue fingerprinting; consumes canonical names under D5.
- `docs/IAP-knowledge-bank-component-spec-v1.0.md` — D4 identity derivation gains the proxy rule (D4 here).
- `docs/IAP-codeql-gap-analysis-v1.0.md` — Why CodeQL offers no prior art to reuse.

## Goals / Non-Goals

**Goals:**

- Canonical + raw frame pairs for JDK dynamic proxies, Spring CGLIB, and Hibernate lazy-loading proxies, computed once at intake.
- Canonical names as matching keys for signatures, fingerprints, and bank identity; raw names preserved for audit and drill-down.
- Resolution through a proxy still abstains (fail-closed); abstention happens once per canonical shape, with a stable limitation code.
- Error-signature convergence across redeploys/restarts (same failure, new `$ProxyN` → same signature).
- Scaffolding frames (`GeneratedMethodAccessor*`, delegating accessors) explicitly dispositioned, not silently dropped or kept.

**Non-Goals:**

- Resolving a proxy call to its target (statically impossible in general; abstention stands).
- Mockito/test-double proxies, Kotlin coroutines/`$default` stubs, Scala closures (out of scope unless a later phase names them).
- Changing the `reflection_boundary` abstention semantics (canonicalization refines matching, never weakens guards).
- Rewriting `stackframes.py` parsing (additive canonical layer only).

## Decisions

### D1: Canonicalize at intake, serve both forms

**Decision:** Canonicalization runs inside `stackframes.py` at parse time. Each frame gains
`canonical_function` (and `canonical_file` where the file token carries generated names);
the raw `function`/`file` fields are untouched. Every downstream consumer (signatures,
fingerprints, bank adapters, display) chooses which form to read, with the rule: canonical
for matching, raw for audit.

```python
# application/evidence/stackframes.py (addition — pure function, no I/O)
import re

_PROXY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("jdk_proxy", re.compile(r"^(?P<ns>(?:[\w$]+\.)*)?\$Proxy\d+$")),
    (
        "spring_cglib",
        re.compile(r"^(?P<base>[\w$.]+?)\$\$EnhancerBySpringCGLIB\$\$(?P<hash>[0-9a-f]+)$"),
    ),
    (
        "cglib",
        re.compile(r"^(?P<base>[\w$.]+?)\$\$EnhancerByCGLIB\$\$(?P<hash>[0-9a-f]+)$"),
    ),
    (
        "hibernate_proxy",
        re.compile(r"^(?P<base>[\w$.]+?)\$\$_javassist_(?:\w+\.)?(?P<id>\d+)$"),
    ),
    (
        "bytebuddy",
        re.compile(r"^(?P<base>[\w$.]+?)\$ByteBuddy\$(?P<id>\w+)$"),
    ),
)


def canonicalize_frame_class(class_name: str) -> tuple[str, str, str]:
    """Return (kind, canonical, raw_remainder).

    kind is one of jdk_proxy, spring_cglib, cglib, hibernate_proxy,
    bytebuddy, or plain. canonical strips the generated suffix; raw_remainder
    preserves the stripped part for audit. Non-matching names return
    ("plain", class_name, "").
    """
    name = (class_name or "").strip()
    for kind, pattern in _PROXY_PATTERNS:
        match = pattern.match(name)
        if match is None:
            continue
        groups = match.groupdict()
        base = groups.get("base") or groups.get("ns") or ""
        remainder = name[len(base) :] if base and name.startswith(base) else name
        if kind == "jdk_proxy":
            return (kind, (base + "$Proxy") if base else "$Proxy", remainder)
        return (kind, base, remainder)
    return ("plain", name, "")
```

**Alternative considered:** Canonicalize per-consumer (each adapter normalizes independently).
Rejected — N consumers reimplement the same table, diverge on edge cases, and signatures
computed before bank intake would never converge.

---

### D2: Canonical forms per family

**Decision:** The canonical table covers exactly three families to begin with (owner direction:
JDK dynamic proxies + Spring CGLIB core, Hibernate lazy-loading next), with ByteBuddy rows
reserved because Spring Boot 3 / Mockito already emit them in traces:

| Family | Raw example | Canonical | Remainder kept |
| --- | --- | --- | --- |
| JDK dynamic proxy | `com.sun.proxy.$Proxy42` | `com.sun.proxy.$Proxy` | `42` |
| Spring CGLIB | `com.foo.Bar$$EnhancerBySpringCGLIB$$1a2b3c` | `com.foo.Bar` | `$$EnhancerBySpringCGLIB$$1a2b3c` |
| Plain CGLIB | `com.foo.Bar$$EnhancerByCGLIB$$9f8e` | `com.foo.Bar` | `$$EnhancerByCGLIB$$9f8e` |
| Hibernate/Javassist | `com.foo.Bar$$_javassist_12` | `com.foo.Bar` | `$$_javassist_12` |
| ByteBuddy | `com.foo.Bar$ByteBuddy$a1b2` | `com.foo.Bar` | `$ByteBuddy$a1b2` |
| Plain class | `com.foo.Bar` | `com.foo.Bar` | (none) |

Scaffolding frames (`jdk.internal.reflect.GeneratedMethodAccessor*`,
`DelegatingMethodAccessorImpl`, `Method.invoke` reflection plumbing) are classified as
`kind="scaffolding"` and flagged, not dropped: consumers may filter them, but the raw trace
stays complete. Exact disposition (drop vs flag) is PROPOSED below.

**Alternative considered:** Normalize to the proxied interface (e.g. `$Proxy42` → the
interface it implements). Rejected — the interface is not recoverable from the frame text
alone; guessing it would manufacture exactly the false precision the vocabulary forbids.

---

### D3: Canonical names match; they never prove — PROPOSED

**Decision (PROPOSED — placement and priority are owner-open; the rule itself is recommended):**
Canonical identity may join, dedupe, and route. It may never establish that code ran, that a
target was invoked, or that a proxy wraps a particular implementation. Concretely:
method-target resolution through a canonical proxy frame still returns the existing
`reflection_boundary` abstention; error correlation may group by canonical shape; conclusions
must still cite raw rows.

**Alternative considered:** Allow canonical frames as resolution evidence ("the proxy must
wrap X"). Rejected — same generated shape routinely wraps different targets across
deployments; treating it as evidence is the fabrication pattern the proof kernel exists
to prevent.

---

### D4: Bank identity uses canonical names

**Decision:** The knowledge-bank D4 derivation table gains one rule: frame-derived identity
keys use the canonical class name; the raw remainder is carried as an attestation note, not
part of the key. Redeploys converge; distinct proxies of the same target still merge only
when all other identity dimensions agree (no weakening of the merge rule).

**Alternative considered:** Bank on raw names and dedupe later. Rejected — "later" never
comes, and the bank fills with one entry per restart, which is the failure mode this design
exists to prevent.

---

### D5: Signatures converge before fingerprinting

**Decision:** Error-signature construction and code-issue fingerprint inputs pass frame class
names through `canonicalize_frame_class` first. Same failure, new `$ProxyN` → same signature.
The raw trace remains attached to the underlying evidence for audit.

**Alternative considered:** Canonicalize only in the bank, leaving signatures verbatim.
Rejected — signatures are the cross-investigation join key; verbatim signatures keep the
fragmentation exactly where it hurts most (incident-to-incident linking).

---

### D6: Phase the families — PROPOSED

**Decision (PROPOSED — owner named the first two; Hibernate order confirmed here):**
Phase 1 ships JDK dynamic proxies + Spring CGLIB (+ plain CGLIB, same pattern). Phase 2 adds
Hibernate/Javassist lazy-loading proxies, informed by Phase 1 field experience. ByteBuddy rows
ship inert in Phase 1 (recognized, no behavior change) and activate in Phase 2 if traces
warrant it.

**Alternative considered:** All families at once. Rejected — each family needs fixture traces
from real incidents to validate the regexes; shipping unvalidated patterns risks
over-canonicalizing legitimate class names containing `$$`.

## Data Storage

No new persistence in this design. Canonical pairs live on the existing frame dicts
(`canonical_function`, `canonical_file`, `frame_kind`, `raw_remainder`) and flow through
existing evidence/bank records. If a later phase persists canonical indexes, it extends the
bank record schema, not this design.

## Data Structures

Input and output are separated: raw frame dicts in, canonical frame views out. The
`canonicalize_frame_class` triple `(kind, canonical, remainder)` is the only new shape;
`kind` is drawn from a closed set (`jdk_proxy`, `spring_cglib`, `cglib`,
`hibernate_proxy`, `bytebuddy`, `scaffolding`, `plain`).

## Interfaces

Pure-function interface only; no REST, CLI, or worker changes:

| Interface | Shape | Description |
| --------- | ----- | ----------- |
| `canonicalize_frame_class(class_name)` | `(str) -> (kind, canonical, remainder)` | Total function; never raises; unknown names return `("plain", name, "")` |
| Frame dict extension | `canonical_function`, `canonical_file`, `frame_kind`, `raw_remainder` keys | Optional keys; absent means unprocessed, never means plain |
| Bank D4 rule | derivation-table row | Frame-derived identity keys use canonical names |
| Fingerprint input | preprocessing step | Canonicalize before hashing; raw trace stays attached |

## Implementation Detail

- Extend `parse_java_traceback` (and only the Java path initially) to attach canonical
  fields per frame; Python tracebacks untouched.
- Scaffolding classification: `jdk.internal.reflect.*`, `GeneratedMethodAccessor*`,
  `DelegatingMethodAccessorImpl`, `java.lang.reflect.Method.invoke` frames get
  `frame_kind="scaffolding"`; consumers default to showing them dimmed/filterable, never
  deleted. Exact default (drop vs flag) is the D-owner-question below.
- `method_facts.py` proxy abstention stays byte-identical; optionally record which
  canonical kind triggered it in the limitation detail (additive diagnostic, no contract change).
- Fingerprint/signature call sites gain one preprocessing call; no signature version bump
  required provided both sides canonicalize (old + new signatures diverge during rollout —
  document as a known transient, bound by signature TTL).
- Fixtures: real (redacted) proxy traces per family, three minimum each, committed as
  canonicalization fixtures with raw→canonical expectations.

## Migrations

1. Pure `canonicalize_frame_class` + unit fixtures (no wiring; zero production effect).
2. Wire into `parse_java_traceback`; serve both forms; no consumer changes.
3. Switch signature/fingerprint inputs to canonical (document the transient-mismatch window).
4. Bank D4 rule activation (depends on knowledge-bank component Phase 1 landing first).
5. Hibernate phase 2 + scaffolding-disposition decision.

Each step is independently shippable and additive; rollback is deletion of the wiring call.

## Testing Philosophy

### Canonicalization table

Table-driven raw→(kind, canonical, remainder) cases per family, including adversarial
near-misses: legitimate class names containing `$$` that must stay `plain`, nested-class
`Outer$Inner` names that must not match proxy patterns, numeric inner classes (`Outer$1`)
that must not match `$Proxy\d+` without the proxy prefix context.

### Signature convergence

Same logical trace with different generated numbers/hashes yields identical signatures;
different proxied classes still diverge. Redacted real-incident traces as fixtures.

### Abstention preservation

Proxy frames still resolve to `reflection_boundary`, now once per canonical shape; assert
the limitation code, not the generated name, in tests.

### No-loss audit

Every canonical frame carries its raw remainder; a rebuild-raw-from-canonical+remainder
round-trip test guards the audit trail.

## Documentation Plan

### `docs/IAP-java-proxy-canonicalization-design-v1.0.md` (this file)

**Audience:** Implementers and reviewers.

Normative canonical table, function contracts, and phased rollout. PROPOSED items are
explicitly marked and require the owner mark before implementation treats them as decided.

### `docs/IAP-architecture-v1.0.md` changes

**Audience:** Architects and future maintainers.

Add proxy canonicalization to the evidence-intake data flow: raw trace → parsed frames →
canonical pairs → signatures/fingerprints/bank. Note the transient signature-mismatch window
during rollout and the standing abstention rule for proxy resolution.

### `docs/IAP-java-query-vocabulary-v1.0.md` changes

**Audience:** Query authors.

Document the canonical-frame rule (matching keys, never proof) in the vocabulary's
limitation section when D3 is accepted; until then, no vocabulary change.

## Risks / Trade-offs

### Over-canonicalization of legitimate names

**Risk:** A regex strips a meaningful suffix from a real class (e.g. a hand-written
`Foo$$Bar`), merging distinct entities.

**Mitigation:** Patterns require the full family marker (`EnhancerBy`, `ByteBuddy$`,
`_javassist_`, `$Proxy` + digits), not bare `$$`; adversarial near-miss fixtures are
permanent; canonicalization is matching-only, so a collision degrades to grouping, never
to a false proof.

### Signature transient during rollout

**Risk:** Old verbatim signatures and new canonical signatures coexist, splitting incident
linking mid-migration.

**Mitigation:** Bounded by signature TTL; document the window; no backfill (re-hashing
history would rewrite audit records).

### Scaffolding disposition argument

**Risk:** Dropping reflection-scaffolding frames hides invocation depth that occasionally
matters; keeping them clutters every trace view.

**Mitigation:** Flag-don't-drop default; the disposition default is an explicit owner
question, reversible per consumer without re-parsing.

## Accessibility

No user-facing surface is added by this design: pure functions plus data keys on existing
structures. If a future trace view renders canonical frames, that surface must distinguish
canonical from raw text non-visually (labels, not color alone) and expose the raw remainder
on demand.
