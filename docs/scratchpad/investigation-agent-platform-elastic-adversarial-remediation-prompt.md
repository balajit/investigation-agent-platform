# Adversarial Remediation Prompt — investigation-agent-platform Elastic Evidence Layer

## Role

You are a principal engineer performing a production-hardening remediation of the **`investigation-agent-platform`**.

This task is deliberately **outside the MCP implementation work**.

Do not implement an MCP server in this task.

Instead, remediate defects, correctness gaps, security weaknesses, data-integrity issues, observability problems, and architectural risks in the existing Elastic evidence implementation represented by:

- `src/investigation_agent_platform/infrastructure/evidence/runtime/elastic.py`
- `src/investigation_agent_platform/infrastructure/evidence/logs/elastic_bridge.py`

The supplied source is the authoritative basis for the findings below. Do not assume behavior that is not established by the repository. Inspect surrounding domain models, ports, gateways, configuration, tests, and deployment code before making changes.

---

# 1. Mission

Perform an adversarial hardening pass over the existing Elasticsearch runtime evidence path.

The desired result is an evidence subsystem that is:

- tenant-safe;
- investigation-safe;
- deterministic;
- pagination-safe;
- semantically correct;
- resilient to malformed provider data;
- bounded against expensive queries;
- explicit about incomplete/truncated evidence;
- provenance-preserving;
- safe for downstream investigation agents;
- observable;
- testable;
- configurable;
- free of hard-coded security material.

Do not weaken existing controls to make tests pass.

Do not perform unrelated refactors.

---

# 2. Files under review

## Package

`investigation_agent_platform`

## Source files

### File 1

`src/investigation_agent_platform/infrastructure/evidence/runtime/elastic.py`

Current responsibilities include:

- Elasticsearch runtime evidence retrieval;
- runtime evidence search;
- query construction;
- provider-side request bounds;
- signed pagination;
- evidence mapping;
- provenance;
- freshness;
- OpenTelemetry instrumentation.

### File 2

`src/investigation_agent_platform/infrastructure/evidence/logs/elastic_bridge.py`

Current responsibilities include:

- trace-based runtime telemetry retrieval;
- delegation to `AsyncElasticAdapter`;
- ECS code-location extraction;
- SQL statement extraction;
- SQLGlot parsing;
- accessed-table extraction.

---

# 3. Review methodology

Before modifying anything:

1. Inspect the complete repository.
2. Inspect the domain definitions used by these files.
3. Inspect:
   - `RuntimeEvidenceRequest`
   - `Evidence`
   - `EvidenceProvenance`
   - `EvidenceFreshness`
   - `QueryFingerprint`
   - `SourceLocation`
   - `ObservabilityProfile`
   - `EvidenceQueryResult`
   - related exceptions
   - evidence gateway/provider protocols.
4. Find every caller of:
   - `get_runtime_evidence`
   - `search_runtime_evidence`
   - `extract_runtime_telemetry_for_trace`
   - `_map_hit_to_evidence`
   - cursor helpers.
5. Inspect existing tests.
6. Inspect configuration/secrets conventions.
7. Inspect Elasticsearch version/client configuration.
8. Inspect deployment/runtime configuration.
9. Determine whether index mappings are documented or tested.
10. Only then implement the remediations.

Where repository evidence contradicts an assumption in this prompt, follow the repository and document the discrepancy.

---

# 4. Severity model

Use:

- **CRITICAL** — exploitable cross-tenant/cross-investigation access, severe integrity violation, secret exposure, or potentially catastrophic operational behavior.
- **HIGH** — significant security, correctness, provenance, or availability defect.
- **MEDIUM** — material reliability, observability, semantic, or maintainability issue.
- **LOW** — hardening, consistency, or maintainability issue.

Do not downgrade an issue merely because it requires a malformed or malicious request. Agent-generated input must be treated as untrusted.

---

# 5. Finding: hard-coded HMAC cursor secret

**Severity: CRITICAL**

**File:**

`src/investigation_agent_platform/infrastructure/evidence/runtime/elastic.py`

The file currently defines:

```python
CURSOR_HMAC_SECRET = b"iap-elastic-cursor-binding-key"
```

This is a production security boundary implemented with a source-controlled constant.

The cursor signature is intended to prevent tampering, so the signing secret must be secret.

The current implementation therefore defeats the intended trust model if an attacker can inspect the source or otherwise derive the constant.

## Required remediation

Move the signing secret/key into the platform's secure configuration mechanism.

Requirements:

- no production secret in source;
- no secret in tests that is accidentally reused in production;
- startup validation;
- fail closed when required secure configuration is absent;
- support deterministic key loading;
- document key rotation;
- define behavior for cursor invalidation after rotation;
- avoid silently generating an ephemeral key at every process start unless that behavior is explicitly desired and documented.

Use the repository's existing settings/secrets mechanism.

Do not invent a parallel configuration framework.

## Tests

Add tests proving:

- production configuration cannot use the source constant;
- missing required key fails safely;
- valid configured key signs and validates cursors;
- incorrect key rejects cursors;
- key rotation behavior is deterministic.

---

# 6. Finding: cursor is not bound to the query

**Severity: HIGH**

**File:**

`src/investigation_agent_platform/infrastructure/evidence/runtime/elastic.py`

The signed cursor currently contains:

- sort values;
- tenant ID;
- application/investigation ID.

It does not visibly bind the cursor to the actual search filters.

That means a valid cursor generated from one logical query may be presented with another query.

Example:

```text
Request A:
  trace_id = TRACE-A
  service = checkout
  cursor = C

Request B:
  trace_id = TRACE-B
  service = billing
  cursor = C
```

The cursor signature remains valid because the tenant/investigation identity remains valid.

This can cause semantically invalid pagination.

## Required remediation

Create a canonical representation of the effective query.

Bind a cryptographic query fingerprint to the cursor.

The cursor should represent at least:

- tenant;
- investigation;
- environment;
- identifiers;
- keywords;
- services;
- severities;
- time range;
- effective limit if it affects pagination semantics;
- sort definition/version.

Canonicalization must be deterministic.

Do not hash arbitrary Python `repr()` output.

Use stable JSON serialization with explicit ordering and normalized timestamps/enums.

## Compatibility

If existing cursors are already deployed:

- determine whether compatibility is required;
- if yes, version cursor formats;
- if no, invalidate old cursors cleanly.

Never silently interpret an old cursor as a new-format cursor.

## Tests

Test:

- same query accepts cursor;
- changed trace ID rejects cursor;
- changed service rejects cursor;
- changed severity rejects cursor;
- changed time range rejects cursor;
- changed environment rejects cursor;
- changed investigation rejects cursor;
- changed tenant rejects cursor;
- malformed cursor rejects safely.

---

# 7. Finding: invalid cursor silently degrades to first page

**Severity: HIGH**

**File:**

`elastic.py`

The current `_decode_signed_cursor(...)` returns `None` for:

- invalid signature;
- tenant/application mismatch;
- malformed cursor;
- decoding errors.

The caller then simply does not add `search_after`.

This creates an important semantic problem:

```text
agent sends cursor
    |
cursor invalid
    |
decode -> None
    |
query runs without search_after
    |
first page is returned
```

A client may believe it received the next page while actually receiving the first page.

This can cause:

- duplicate evidence;
- investigation loops;
- misleading result ordering;
- hidden tampering;
- difficult debugging.

## Required remediation

Distinguish:

1. no cursor supplied;
2. valid cursor;
3. invalid/tampered cursor.

Invalid cursor must result in a deterministic domain/application error.

Do not silently convert invalid pagination state into a new search.

Create or reuse an appropriate invalid-cursor exception.

Do not expose signature details.

## Tests

Verify invalid cursors produce a structured failure and never execute a first-page fallback.

---

# 8. Finding: broad exception handling hides programmer/configuration defects

**Severity: HIGH**

**File:**

`elastic.py`

`_decode_signed_cursor(...)` catches `Exception`.

This is overly broad.

It can conceal:

- programming errors;
- unexpected type errors;
- configuration failures;
- logic defects.

## Required remediation

Catch only expected parsing/decoding/validation exceptions.

Examples may include:

- JSON decoding;
- base64 decoding;
- Unicode decoding;
- missing cursor fields;
- type validation.

Do not catch arbitrary exceptions and reinterpret them as malformed user input.

Unexpected exceptions should propagate to the appropriate internal error boundary.

## Tests

Test expected malformed inputs and separately verify unexpected implementation exceptions are not silently swallowed.

---

# 9. Finding: malformed cursor is logged with potentially sensitive full token

**Severity: HIGH**

**File:**

`elastic.py`

The malformed cursor logging currently includes the cursor value in structured logging context.

A cursor may contain signed payload information including tenant/investigation identifiers and query state.

Even if the cursor is not a secret, logging full attacker-controlled opaque tokens is unnecessary and creates avoidable log exposure.

## Required remediation

Never log the complete cursor.

Log safe metadata such as:

- failure category;
- cursor version;
- correlation ID;
- tenant context where permitted;
- a short non-reversible fingerprint if debugging requires correlation.

Do not log the raw token.

---

# 10. Finding: `get_runtime_evidence` does not visibly bind retrieval to an investigation

**Severity: HIGH**

**File:**

`elastic.py`

The direct fetch API is:

```python
get_runtime_evidence(
    tenant_id: str,
    evidence_id: UUID
)
```

The method does not accept an explicit investigation ID.

The returned evidence mapping is given a deterministic UUID derived from the evidence ID as an investigation context:

```python
uuid.uuid5(
    uuid.NAMESPACE_DNS,
    f"elastic-fetch:{evidence_id}"
)
```

This is not equivalent to verifying that the requested evidence belongs to the caller's investigation.

## Required remediation

Inspect the domain model and determine how evidence ownership/scope is supposed to work.

If investigation scoping is mandatory:

- require investigation context;
- validate evidence against that context;
- preserve the actual investigation ID in provenance;
- reject cross-investigation retrieval.

If the repository intentionally defines evidence as tenant-scoped rather than investigation-scoped, document that explicitly and ensure downstream consumers cannot misinterpret the synthetic UUID as a real investigation.

Do not invent ownership semantics without checking the domain.

## Tests

Test both:

- valid same-investigation retrieval;
- attempted cross-investigation retrieval.

---

# 11. Finding: evidence ID semantics are ambiguous

**Severity: HIGH**

**File:**

`elastic.py`

There are at least two identifiers:

1. Elasticsearch `_id`;
2. platform `Evidence.evidence_id`.

The platform evidence ID is generated as:

```python
uuid.uuid5(
    uuid.NAMESPACE_DNS,
    f"elastic:{hit['_index']}:{hit['_id']}"
)
```

But direct retrieval uses:

```python
ids.values = [str(evidence_id)]
```

against the Elasticsearch document ID.

These are different identifiers.

A caller that receives a platform `evidence_id` cannot necessarily use it with `get_runtime_evidence(...)`.

## Required remediation

Determine the intended public identifier contract.

Choose one of:

- direct provider ID;
- platform evidence UUID;
- explicit provider-reference translation.

Prefer a stable platform-level identity for domain APIs.

If a translation is required:

- make it explicit;
- preserve provider ID in provenance/source location;
- never guess whether an input is one identifier or another.

## Tests

Round-trip test:

```text
search result evidence_id
    -> retrieval API
    -> same evidence
```

The test must prove the identifier returned by search can be used according to the documented retrieval contract.

---

# 12. Finding: direct fetch may query an overly broad index pattern

**Severity: HIGH**

**File:**

`elastic.py`

The direct fetch uses:

```python
logs-{tenant_id}-*-*
```

whereas search uses:

```python
logs-{tenant_id}-*-{environment}-*
```

This creates a difference in scoping semantics.

A direct fetch may search across environments and other dimensions that a search request intentionally constrains.

## Required remediation

Determine the repository's index naming contract.

If environment is security or isolation relevant:

- require environment for direct fetch;
- constrain the index pattern accordingly;
- validate evidence's actual environment before returning it.

If environment is intentionally not part of evidence identity, document that and test it.

Do not assume the wildcard is harmless.

---

# 13. Finding: direct fetch does not explicitly cap provider response size

**Severity: MEDIUM**

**File:**

`elastic.py`

The direct fetch requests:

```python
"size": 1
```

which is good, but the method does not appear to share the same provider request timeout/query policy abstraction as the search path beyond the local timeout.

Review whether all provider calls consistently enforce:

- timeout;
- allowed index scope;
- cancellation;
- response size;
- tracing;
- error mapping.

## Required remediation

Unify provider-call policy where appropriate without unnecessarily refactoring the adapter.

Tests should verify the direct-fetch call cannot become an unbounded provider operation.

---

# 14. Finding: index-pattern construction is based directly on tenant input

**Severity: HIGH**

**File:**

`elastic.py`

Index patterns are constructed from `tenant_id`.

The code assumes tenant IDs are safe to interpolate into an Elasticsearch index expression.

Even if tenant IDs normally originate from authenticated context, this is a dangerous boundary.

## Required remediation

Validate tenant IDs according to the repository's tenant identifier format before constructing index names.

Prefer a dedicated index-name builder that:

- validates allowed characters;
- rejects wildcard/control characters;
- normalizes representation;
- prevents accidental pattern injection.

Do not solve this with arbitrary string stripping that could turn an invalid tenant into a different valid tenant.

Reject invalid identifiers.

## Tests

Include:

- wildcard characters;
- spaces;
- slashes;
- quotes;
- braces;
- backslashes;
- control characters;
- Unicode edge cases if relevant to the repository.

---

# 15. Finding: identifier values are insufficiently constrained

**Severity: MEDIUM**

**File:**

`elastic.py`

Identifier keys are checked against `ALLOWED_LABEL_KEYS`, which is good.

However, identifier values are passed directly into term queries.

The query type is safe from DSL injection because the value is data, but unrestricted value size can still create:

- oversized requests;
- log/trace amplification;
- expensive provider processing;
- pathological agent-generated inputs.

## Required remediation

Introduce domain-appropriate maximum lengths for identifier values.

Validate:

- type;
- length;
- Unicode normalization if required;
- empty values.

Do not arbitrarily alter identifiers.

Reject values outside documented bounds.

---

# 16. Finding: keywords are truncated rather than rejected

**Severity: MEDIUM**

**File:**

`elastic.py`

The implementation does:

```python
keywords = list(request.keywords or [])[:ELASTIC_MAX_KEYWORD_TERMS]
```

This means an oversized request is silently changed.

For agent-facing investigation semantics, silent truncation can make the result differ from the requested investigation without telling the caller.

## Required remediation

Decide, based on existing domain semantics, whether limits should:

- reject requests above the limit; or
- normalize them and explicitly report truncation.

Prefer explicit rejection for API correctness unless existing contracts require truncation.

Apply the same principle consistently to:

- services;
- severities;
- result limits.

Do not silently discard investigation constraints.

---

# 17. Finding: services are truncated silently

**Severity: MEDIUM**

**File:**

`elastic.py`

The service list is silently limited:

```python
services = list(request.services or [])[:ELASTIC_MAX_SERVICES]
```

An investigation requesting 15 services may unknowingly search only the first 10.

## Required remediation

Use explicit validation or an explicit normalized-request response.

Do not silently alter query semantics.

Add tests.

---

# 18. Finding: severity values are filtered rather than rejected

**Severity: MEDIUM**

**File:**

`elastic.py`

Severity values are filtered through:

```python
re.fullmatch(r"[A-Z_]{1,16}", s)
```

Invalid values disappear rather than producing an invalid-request error.

If all supplied severities are invalid, the query can become effectively unfiltered by severity.

This is dangerous because a malformed agent request can broaden results.

## Required remediation

Validate severity values against the actual domain enum/schema.

Reject invalid severity values.

Never turn invalid restrictive criteria into absence of a filter.

Add a regression test for:

```text
requested severities = invalid values
expected = request rejection
not = query without severity filter
```

---

# 19. Finding: identifier sanitization can transform invalid keys before rejecting them

**Severity: MEDIUM**

**File:**

`elastic.py`

The implementation does:

```python
sanitized_key = re.sub(r"[^a-zA-Z0-9_]", "", key)
if sanitized_key not in ALLOWED_LABEL_KEYS:
    raise ...
```

This transforms input before validation.

Although the subsequent allow-list is restrictive, validation should not silently transform attacker input because it can produce surprising equivalence.

## Required remediation

Validate the original key directly against the allow-list.

If normalization is required, define a canonicalization rule explicitly and validate the canonical result only where that behavior is intentional.

Prefer exact allow-list matching.

---

# 20. Finding: query semantics for multiple keywords are ambiguous

**Severity: MEDIUM**

**File:**

`elastic.py`

Keywords are joined into one string:

```python
" ".join(keywords)
```

and passed to `multi_match`.

This does not necessarily mean:

```text
keyword1 AND keyword2 AND keyword3
```

The agent may interpret a list of keywords as multiple required terms while Elasticsearch applies different matching semantics.

## Required remediation

Inspect the intended `RuntimeEvidenceRequest` semantics.

Define and document whether keywords mean:

- OR;
- AND;
- phrase;
- independent clauses.

Implement the domain semantics explicitly.

Add tests with multiple keywords.

Do not let the Elastic default determine the platform's contract accidentally.

---

# 21. Finding: `multi_match` field semantics are fixed without domain verification

**Severity: MEDIUM**

**File:**

`elastic.py`

The query targets:

```text
message
error.message
```

Review actual index mappings and domain expectations.

Potential issues include:

- fields absent in some mappings;
- analyzed versus keyword behavior;
- nested structures;
- ECS compatibility;
- case sensitivity;
- query-string expectations.

## Required remediation

Verify the actual supported mappings.

Centralize supported field definitions if appropriate.

Add integration tests against representative mappings or realistic query fixtures.

Do not expand fields blindly because broader search can materially increase query cost.

---

# 22. Finding: `track_total_hits` semantics are not clearly aligned with result contract

**Severity: MEDIUM**

**File:**

`elastic.py`

The implementation uses:

```python
"track_total_hits": effective_limit
```

Then exposes `total_count`.

This means the total may be capped/partial rather than the actual total.

A consumer could interpret `total_count` as an exact count.

## Required remediation

Define whether `EvidenceQueryResult.total_count` means:

- exact total;
- bounded total;
- lower/upper bound;
- unavailable.

If exactness is not guaranteed, represent that explicitly.

Do not expose a number that looks exact when Elasticsearch may have only counted up to a threshold.

Add tests covering:

- fewer results than limit;
- exactly limit;
- more than limit.

---

# 23. Finding: `has_more = len(hits) == effective_limit` can be semantically fragile

**Severity: MEDIUM**

**File:**

`elastic.py`

The implementation infers more pages exist from the page size.

This can work with a bounded search but should be validated against provider semantics and edge cases.

Potential concerns:

- provider behavior;
- filtering;
- unexpected response truncation;
- zero/invalid limits;
- future changes to query size semantics.

## Required remediation

Validate the invariant that:

```text
len(hits) == effective_limit
```

is sufficient for this provider/query contract.

If not, use an explicit provider signal.

At minimum, enforce:

```text
effective_limit >= 1
```

and test boundary cases.

---

# 24. Finding: result limit may not be validated before `min(...)`

**Severity: MEDIUM**

**File:**

`elastic.py`

The code does:

```python
effective_limit = min(request.limit, ELASTIC_MAX_HITS)
```

Review behavior for:

- zero;
- negative;
- `None`;
- unexpected types.

A negative or zero limit should never result in undefined provider behavior.

## Required remediation

Validate the request model and provider boundary.

Require:

```text
1 <= limit <= configured/provider maximum
```

or explicitly normalize according to domain semantics.

Prefer rejection of invalid values.

---

# 25. Finding: request timeout configuration is inconsistent

**Severity: MEDIUM**

**File:**

`elastic.py`

The adapter has:

```python
request_timeout
```

but the search body also hard-codes:

```python
"timeout": "25s"
```

while the outer `asyncio.wait_for(...)` uses the configured timeout.

This creates multiple timeout authorities:

1. Elasticsearch query timeout;
2. client request timeout;
3. asyncio timeout.

These can diverge.

## Required remediation

Define explicit semantics for each timeout.

Prefer configuration-driven values.

Document:

- provider query timeout;
- network/client timeout;
- application cancellation timeout.

Ensure the outer timeout is not shorter/longer in an accidental way.

Test timeout behavior.

---

# 26. Finding: `asyncio.wait_for` and cancellation semantics need review

**Severity: MEDIUM**

**File:**

`elastic.py`

The adapter wraps provider calls in `asyncio.wait_for(...)`.

Review how cancellation propagates to `AsyncElasticsearch`.

The goal is to avoid:

- orphaned provider work;
- connection leaks;
- misleading success/failure state;
- duplicated requests after cancellation.

## Required remediation

Verify behavior using the installed Elasticsearch client version.

Add tests for:

- timeout;
- task cancellation;
- provider exception.

Do not catch cancellation as a generic provider error.

---

# 27. Finding: broad provider exception conversion can hide important categories

**Severity: MEDIUM**

**File:**

`elastic.py`

The provider call catches `Exception` and converts everything into `ExecutionError`.

This may collapse materially different conditions:

- timeout;
- authentication failure;
- authorization failure;
- connection failure;
- malformed request;
- provider-side rejection;
- application cancellation.

## Required remediation

Map known provider exception classes into meaningful domain exceptions.

At minimum distinguish:

- timeout;
- unavailable;
- invalid request;
- authorization/configuration;
- unexpected provider failure.

Do not leak provider internals.

Do not convert cancellation into ordinary execution failure.

---

# 28. Finding: evidence mapping assumes `_id` exists

**Severity: HIGH**

**File:**

`elastic.py`

The mapper uses:

```python
hit["_id"]
```

in multiple places.

Malformed or mocked provider responses can therefore produce `KeyError`.

For a production evidence adapter, provider response validation should be explicit.

## Required remediation

Validate required provider fields before mapping.

Required minimum fields should be defined.

If `_id` is missing:

- do not fabricate identity;
- return a controlled provider/data-integrity error;
- include safe diagnostics in logs.

Test malformed hits.

---

# 29. Finding: evidence mapping assumes `_source` is a dictionary

**Severity: MEDIUM**

**File:**

`elastic.py`

The mapper uses:

```python
src = hit.get("_source", {})
```

and later:

```python
src.get(...)
```

If `_source` is present but not a dictionary, mapping fails.

## Required remediation

Validate provider response shape.

Handle:

- missing source;
- null source;
- invalid source type.

Do not silently convert arbitrary data to a dictionary.

---

# 30. Finding: timestamp fallback fabricates observation time

**Severity: HIGH**

**File:**

`elastic.py`

The implementation currently does:

```python
datetime.now(UTC)
```

when `@timestamp` is missing or invalid.

This makes a retrieved event appear to have occurred at the current time.

That can corrupt:

- incident timelines;
- causal ordering;
- time-window analysis;
- freshness interpretation.

## Required remediation

Separate:

- observed event time;
- retrieval time;
- timestamp validity.

If `@timestamp` is missing:

```text
observed_at = None
retrieved_at = actual retrieval time
```

if the domain model permits it.

If `Evidence.observed_at` is mandatory, determine the correct domain representation for unknown observation time rather than fabricating one.

If an invalid timestamp is encountered, preserve a structured data-quality indicator.

Never silently substitute retrieval time as observation time.

---

# 31. Finding: timestamp parser may accept values without adequate timezone semantics

**Severity: MEDIUM**

**File:**

`elastic.py`

The parser uses:

```python
datetime.fromisoformat(...)
```

and only explicitly replaces `Z`.

Review whether naive datetimes can enter the system.

A naive timestamp can create comparison/serialization ambiguity.

## Required remediation

Require timezone-aware timestamps.

Reject or explicitly normalize naive timestamps according to repository conventions.

Add tests for:

- `Z`;
- positive offset;
- negative offset;
- naive timestamp;
- malformed timestamp;
- impossible date.

---

# 32. Finding: provenance query fingerprint is not actually a query fingerprint

**Severity: HIGH**

**File:**

`elastic.py`

The mapper constructs:

```python
QueryFingerprint(
    provider_type="ELASTIC",
    operation=operation,
    normalized_query_hash=hit["_id"]
)
```

The field is named `normalized_query_hash`, but the value is the Elasticsearch document ID.

This is semantically misleading.

A document ID is not a normalized query hash.

## Required remediation

Inspect `QueryFingerprint` semantics.

If the field genuinely represents the normalized query:

- calculate a deterministic hash of the effective normalized query.

If the domain intentionally uses a provider-result fingerprint:

- change the domain representation/name or use the appropriate field.

Do not populate a semantic field with an unrelated identifier merely to satisfy the model.

## Tests

Verify identical logical queries produce identical fingerprints.

Verify materially different queries produce different fingerprints.

Do not include secrets or raw sensitive content in the fingerprint.

---

# 33. Finding: search and direct-fetch provenance semantics differ

**Severity: MEDIUM**

**File:**

`elastic.py`

Search mapping defaults to:

```text
operation = "SEARCH"
```

Direct fetch uses:

```text
operation = "GET"
```

This is reasonable, but the query fingerprint semantics are currently based on `_id` in both cases.

## Required remediation

Ensure provenance accurately records:

- operation;
- actual provider;
- requested provider;
- investigation context;
- retrieval timestamp;
- source location;
- query identity where applicable.

Do not claim query-level provenance when only document-level identity is available.

---

# 34. Finding: `source` and `content_uri` expose provider-specific identifiers without policy review

**Severity: MEDIUM**

**File:**

`elastic.py`

The evidence object exposes:

```text
elasticsearch://{index}/{id}
elastic://{index}/{id}
```

Review whether these URIs are intended to be agent-visible.

They may disclose:

- index naming;
- environment naming;
- provider topology;
- tenant-derived identifiers.

## Required remediation

Keep provider-native source location internally where required.

Create a safe external projection if downstream consumers should not see infrastructure identifiers.

Do not remove provenance; separate internal provider location from public-safe representation.

---

# 35. Finding: full `_source` is copied into evidence attributes

**Severity: HIGH**

**File:**

`elastic.py`

The implementation does:

```python
attributes={**src, "tenant_id": tenant_id}
```

This is an unrestricted pass-through of provider data into the domain evidence object.

This can expose:

- credentials;
- tokens;
- cookies;
- authorization headers;
- PII;
- request bodies;
- secrets;
- large payloads;
- arbitrary attacker-controlled content.

It also increases memory and downstream serialization costs.

## Required remediation

Create an evidence projection/sanitization policy.

At minimum:

- explicitly allow investigation-relevant fields;
- redact obvious secret-bearing fields;
- bound string lengths;
- bound nested structure depth/size;
- bound total serialized attribute size;
- preserve source evidence identity;
- retain raw provider data only behind an appropriate internal boundary if required.

Do not rely on field names alone if the repository has a classification mechanism.

Use existing classification/authorization infrastructure if available.

---

# 36. Finding: tenant ID is injected into evidence attributes

**Severity: MEDIUM**

**File:**

`elastic.py`

The mapper adds:

```python
"tenant_id": tenant_id
```

to provider attributes.

Review whether this duplicates trusted domain context and whether it could be mistaken for provider-observed data.

## Required remediation

Prefer trusted domain metadata outside arbitrary provider attributes.

If tenant ID must be included, clearly distinguish:

```text
context.tenant_id
```

from:

```text
observed_attributes
```

Do not allow provider data to overwrite trusted context.

---

# 37. Finding: evidence classification is hard-coded

**Severity: HIGH**

**File:**

`elastic.py`

Every mapped evidence item is assigned:

```python
ClassificationLevel.INTERNAL
```

This assumes every log record has the same classification.

If the platform supports classification levels, this may downgrade or misclassify sensitive data.

## Required remediation

Inspect the domain classification model and upstream metadata.

Determine whether:

- classification is derived from source metadata;
- provider/index has a configured classification;
- tenant policy determines classification.

Do not automatically downgrade unknown data to a lower sensitivity class.

If `INTERNAL` is the intentional provider-wide classification, document and test that policy.

---

# 38. Finding: evidence type is hard-coded to runtime log

**Severity: MEDIUM**

**File:**

`elastic.py`

Every provider hit becomes:

```text
EvidenceType.RUNTIME_LOG
```

The bridge separately extracts trace/code/database semantics from those records.

Review whether the provider can return other evidence types or whether all current Elastic records are guaranteed runtime logs.

## Required remediation

Confirm the domain/provider contract.

If mixed evidence is possible, derive type safely.

If runtime logs are the only supported source, enforce/validate that assumption.

---

# 39. Finding: evidence title and summary are weakly normalized

**Severity: LOW**

**File:**

`elastic.py`

The title uses the first eight characters of `_id`, while summary uses the first 200 characters of `message`.

Review:

- Unicode truncation;
- newline/control characters;
- excessive whitespace;
- secret leakage;
- empty messages.

## Required remediation

Introduce bounded presentation normalization.

Do not attempt to create meaning not present in evidence.

Preserve a stable evidence ID separately from presentation fields.

---

# 40. Finding: `json.dumps(src, default=str)` can serialize arbitrary data into content

**Severity: MEDIUM**

**File:**

`elastic.py`

The full `_source` is serialized into `content_snippet` with:

```python
json.dumps(src, default=str)[:4000]
```

Potential problems:

- secrets copied into agent-facing content;
- very large serialization before truncation;
- non-JSON objects converted to strings;
- misleading representation of provider-native values;
- expensive serialization for large documents.

## Required remediation

Perform bounded projection before serialization.

Avoid serializing the entire source only to truncate afterward.

Implement deterministic size-limited serialization.

Ensure sensitive fields are redacted before serialization.

---

# 41. Finding: truncating JSON after serialization can produce invalid JSON

**Severity: LOW**

**File:**

`elastic.py`

The code takes the first 4000 characters of the serialized JSON.

The resulting `content_snippet` may be syntactically invalid JSON.

## Required remediation

Treat the field explicitly as a text snippet if that is the intended contract.

If consumers expect JSON, serialize a structured bounded projection instead.

Document the semantics.

---

# 42. Finding: `attributes` and `content_snippet` duplicate potentially large data

**Severity: MEDIUM**

**File:**

`elastic.py`

The evidence object can contain both:

- all source attributes;
- serialized source content.

This doubles data propagation and increases:

- memory;
- network;
- model context usage;
- persistence costs.

## Required remediation

Determine which fields are authoritative.

Prefer a controlled projection:

- summary;
- selected attributes;
- optional bounded content;
- provider reference.

Avoid storing the same full payload twice.

---

# 43. Finding: bridge uses fixed `limit=100` with no completeness signal

**Severity: HIGH**

**File:**

`elastic_bridge.py`

The trace bridge creates:

```python
RuntimeEvidenceRequest(
    ...
    limit=100,
)
```

and then returns extracted telemetry.

There is no visible representation of:

- `has_more`;
- cursor;
- truncation;
- number of pages consumed.

Therefore the caller can receive an incomplete trace and interpret it as complete.

## Required remediation

Define trace retrieval semantics.

Options include:

1. paginate internally to a bounded maximum and report truncation;
2. expose continuation state;
3. require a bounded caller-controlled page;
4. define a maximum trace evidence budget.

Whichever model is chosen, the result must explicitly say whether the telemetry is complete.

Example:

```json
{
  "trace_id": "...",
  "complete": false,
  "evidence_count": 100,
  "truncation_reason": "provider_result_limit"
}
```

Do not claim completeness when the underlying query was truncated.

---

# 44. Finding: trace search time range is optional and may produce broad searches

**Severity: HIGH**

**File:**

`elastic_bridge.py`

The bridge allows `time_range=None`.

The request still filters by `trace_id`, which may be acceptable, but trace cardinality and retention characteristics must be considered.

A trace ID lookup without a time window can potentially scan a large amount of data.

## Required remediation

Inspect index mappings and deployment architecture.

Determine whether trace ID is sufficiently selective.

If not, require or derive a bounded time window.

Do not invent a time window that could exclude valid evidence without documenting the behavior.

Add provider-cost tests or integration coverage.

---

# 45. Finding: trace ID input is not explicitly validated in the bridge

**Severity: MEDIUM**

**File:**

`elastic_bridge.py`

The bridge passes:

```python
identifiers={"trace_id": trace_id}
```

to the runtime request.

The adapter validates only the identifier key, not a trace ID-specific format/length.

## Required remediation

Inspect the platform's trace ID contract.

Validate:

- type;
- maximum length;
- allowed representation;
- empty value.

If the platform supports multiple trace formats, preserve them.

Do not enforce a format unsupported by existing telemetry sources.

---

# 46. Finding: bridge assumes nested ECS structures have expected types

**Severity: MEDIUM**

**File:**

`elastic_bridge.py`

The code does:

```python
code_info = attrs.get("code") or attrs.get("log", {}).get("origin", {})
```

If `attrs["log"]` is not a dictionary, `.get(...)` can fail.

Similarly, nested fields can have unexpected provider shapes.

## Required remediation

Implement defensive type checks at provider-data boundaries.

Examples:

```text
code: dict | unexpected
log: dict | unexpected
origin: dict | unexpected
file: dict | unexpected
db: dict | unexpected
```

Malformed one-event metadata must not abort the entire trace investigation.

Add malformed ECS fixtures.

---

# 47. Finding: code-location extraction does not normalize or deduplicate paths

**Severity: MEDIUM**

**File:**

`elastic_bridge.py`

The bridge appends every discovered location.

Repeated log events can produce many duplicate locations.

Paths may also appear in inconsistent forms.

## Required remediation

Deduplicate using a stable key such as:

```text
(function, file_path, line)
```

where each component is normalized safely.

Do not modify paths in a way that destroys source identity.

Preserve the evidence IDs contributing to each location.

---

# 48. Finding: line number validation is absent

**Severity: LOW**

**File:**

`elastic_bridge.py`

The extracted `line` is passed through without validating type/range.

## Required remediation

Normalize valid line numbers to integers.

Reject or represent invalid values as `None`.

Do not accept arbitrary objects or negative values as valid source lines.

---

# 49. Finding: function/file values may contain unbounded provider data

**Severity: MEDIUM**

**File:**

`elastic_bridge.py`

Function names and paths are extracted directly from log attributes.

An attacker-controlled log could insert extremely large strings.

## Required remediation

Bound the length of:

- function;
- file path;
- table name;
- raw SQL;
- other extracted telemetry fields.

Preserve truncation metadata if truncation can affect investigation conclusions.

---

# 50. Finding: SQL extraction assumes `db` is dictionary-like

**Severity: MEDIUM**

**File:**

`elastic_bridge.py`

The implementation handles non-dictionary `db` by treating it as absent, which is safe, but does not distinguish malformed provider data from genuine absence.

## Required remediation

Track data-quality information if useful:

```text
sql_present
sql_parse_failed
sql_malformed
```

Do not infer "no SQL" when the field existed but was malformed.

---

# 51. Finding: SQLGlot `parse_one` may not represent multi-statement logs

**Severity: MEDIUM**

**File:**

`elastic_bridge.py`

The bridge uses:

```python
sqlglot.parse_one(sql_statement)
```

If one log field contains multiple SQL statements, only one expression may be represented or parsing may fail depending on syntax.

## Required remediation

Determine whether the source emits:

- single statements;
- multi-statements;
- database-specific batches.

If multi-statement SQL is supported, parse using the appropriate SQLGlot API and process each statement.

Do not execute any statement.

Add representative tests.

---

# 52. Finding: SQL dialect is unspecified

**Severity: MEDIUM**

**File:**

`elastic_bridge.py`

SQLGlot parsing is invoked without an explicit dialect.

This can produce dialect-dependent parsing behavior.

## Required remediation

Inspect observability metadata for database system/dialect.

If dialect is available:

- use it safely.

If dialect is not available:

- use generic parsing;
- report uncertainty where appropriate.

Do not silently label a parse as authoritative when parser dialect is unknown.

---

# 53. Finding: accessed table extraction can lose schema/catalog context

**Severity: MEDIUM**

**File:**

`elastic_bridge.py`

The bridge stores:

```python
table.name.lower()
```

This can collapse distinct objects:

```text
schema_a.orders
schema_b.orders
```

into:

```text
orders
```

It can also lose catalog/database context.

## Required remediation

Use the SQLGlot AST to preserve available:

- catalog;
- database;
- schema;
- table.

Define a canonical table reference.

Do not lower-case identifiers blindly if the source database uses case-sensitive quoted identifiers.

If canonicalization is required, follow the database dialect rules.

Add tests for qualified and quoted identifiers.

---

# 54. Finding: table lineage is overstated

**Severity: HIGH**

**File:**

`elastic_bridge.py`

The output field is:

```text
accessed_tables
```

This is derived by traversing SQL table nodes.

It does not necessarily establish:

- read versus write semantics;
- source versus target lineage;
- actual execution;
- successful execution;
- dynamic SQL;
- stored procedure dependencies;
- subquery semantics beyond parsed table references.

## Required remediation

Use terminology that matches what the parser actually establishes.

If the platform calls this lineage, document the exact meaning.

Distinguish, where possible:

- referenced tables;
- read sources;
- write targets.

Do not claim runtime database lineage beyond the evidence actually observed.

---

# 55. Finding: raw SQL is copied without bounding

**Severity: HIGH**

**File:**

`elastic_bridge.py`

The bridge appends raw SQL directly:

```python
telemetry["raw_sql_statements"].append(sql_statement)
```

A malicious or unusually large log field can cause:

- memory amplification;
- oversized downstream responses;
- model-context exhaustion;
- sensitive data exposure.

## Required remediation

Bound SQL statement length.

Define a maximum based on repository/domain requirements.

Preserve:

- whether truncation occurred;
- statement identity/fingerprint;
- parsed table references.

Never execute the statement.

---

# 56. Finding: SQL may contain secrets/literal credentials

**Severity: HIGH**

**File:**

`elastic_bridge.py`

Runtime SQL can contain:

- literal credentials;
- tokens;
- email addresses;
- customer data;
- API values;
- large `IN (...)` lists.

Returning raw SQL to an agent without redaction may expose sensitive data.

## Required remediation

Implement a safe SQL projection.

Prefer:

- parameter/literal redaction;
- normalized SQL;
- structural AST representation;
- table lineage.

Retain raw SQL only where authorized.

Do not attempt to build a perfect secret detector as the only security mechanism.

Use classification and source-policy controls where available.

---

# 57. Finding: SQL parse failure is only logged at debug level

**Severity: MEDIUM**

**File:**

`elastic_bridge.py`

The code logs:

```text
Failed to parse runtime SQL log
```

at debug level and otherwise silently continues.

The resulting telemetry can look complete despite parsing failure.

## Required remediation

Track parse failure explicitly in returned telemetry.

Example:

```json
{
  "sql": {
    "statements_observed": 3,
    "statements_parsed": 2,
    "parse_failures": 1
  }
}
```

Do not expose raw exception details to untrusted callers.

---

# 58. Finding: telemetry has no source evidence references

**Severity: HIGH**

**File:**

`elastic_bridge.py`

The bridge returns derived:

- code locations;
- accessed tables;
- raw SQL.

But the current output does not visibly associate each derived fact with the evidence records from which it was derived.

This weakens auditability and agent reasoning.

## Required remediation

For each derived result, retain source evidence IDs.

Example:

```json
{
  "function": "...",
  "file_path": "...",
  "line": 123,
  "evidence_ids": ["..."]
}
```

For tables:

```json
{
  "table": "...",
  "evidence_ids": ["..."]
}
```

For SQL statements:

```json
{
  "statement": "...",
  "evidence_id": "..."
}
```

Do not invent provenance when source association is unavailable.

---

# 59. Finding: set-to-list conversion produces unstable ordering

**Severity: LOW**

**File:**

`elastic_bridge.py`

The bridge uses:

```python
telemetry["accessed_tables"] = list(telemetry["accessed_tables"])
```

Set iteration order should not be treated as an API ordering guarantee.

This can produce nondeterministic serialized output.

## Required remediation

Sort canonical table references deterministically.

Define the ordering contract.

Add a deterministic serialization test.

---

# 60. Finding: trace telemetry result lacks explicit evidence count

**Severity: LOW**

**File:**

`elastic_bridge.py`

The current result does not report how many evidence records were examined.

This makes it difficult to assess completeness.

## Required remediation

Return:

- evidence count examined;
- evidence count contributing to derived facts;
- truncation/completeness state.

Do not confuse result count with total provider count.

---

# 61. Finding: bridge passes `profile` without demonstrating how it constrains Elastic access

**Severity: MEDIUM**

**File:**

`elastic_bridge.py`

The method receives:

```python
profile: ObservabilityProfile
```

and forwards it to the adapter, but the current `search_runtime_evidence(...)` implementation shown does not visibly use the profile to derive the index pattern or constrain the query.

The comment says:

```text
Derive application context from profile provider if available
```

but the implementation uses a tenant/environment index pattern.

## Required remediation

Inspect `ObservabilityProfile` and its intended semantics.

Determine whether profile should constrain:

- provider selection;
- application/service scope;
- environments;
- index patterns;
- allowed fields.

If profile is intended as an authorization/selection boundary, ensure it is actually enforced.

If it is only a future extension point, remove misleading comments or document the current behavior.

Do not claim application isolation that is not enforced.

---

# 62. Finding: provider identity semantics need verification

**Severity: MEDIUM**

**File:**

`elastic.py`

The adapter accepts:

```python
provider_id: str = "elastic-primary"
```

and writes it as both:

- requested provider ID;
- actual provider ID.

This is only correct if routing has already guaranteed that the requested and actual providers are identical.

## Required remediation

Inspect provider selection/routing architecture.

If the adapter can represent fallback/routing:

- distinguish requested provider from actual provider.

If this adapter is always the selected provider:

- document that invariant.

Do not populate provenance fields with duplicated values merely because the model requires them.

---

# 63. Finding: provider request timeout and Elasticsearch timeout are hard-coded/inconsistent

**Severity: MEDIUM**

**File:**

`elastic.py`

The adapter constructor uses a configurable `request_timeout`, while the search body contains `"timeout": "25s"`.

## Required remediation

Move provider timeout settings into configuration.

Define clear relationship between:

```text
application timeout
provider timeout
network timeout
```

Add boundary tests.

---

# 64. Finding: hard-coded provider ceilings

**Severity: MEDIUM**

**File:**

`elastic.py`

The provider ceilings are source constants:

```text
ELASTIC_MAX_HITS
ELASTIC_MAX_TIME_WINDOW_SECONDS
ELASTIC_MAX_KEYWORD_TERMS
ELASTIC_MAX_SERVICES
```

Security limits should not necessarily be runtime-configurable, but operational limits often need controlled configuration.

## Required remediation

Classify each limit:

- immutable security ceiling;
- deployment-specific operational limit;
- provider-specific capability.

If configurable, use validated configuration with safe maximum ceilings.

Never allow configuration to disable mandatory security boundaries.

---

# 65. Finding: allowed label keys are hard-coded

**Severity: LOW/MEDIUM**

**File:**

`elastic.py`

`ALLOWED_LABEL_KEYS` is source-defined.

This may be correct, but it can become inconsistent with actual ECS/domain mappings.

## Required remediation

Inspect domain and observability schema.

If the allow-list is intentional:

- document it;
- test every supported field.

If configuration is required:

- centralize it.

Do not simply allow arbitrary labels.

---

# 66. Finding: direct evidence fetch creates deterministic synthetic context that can collide semantically

**Severity: MEDIUM**

**File:**

`elastic.py`

The synthetic UUID:

```text
elastic-fetch:{evidence_id}
```

is deterministic.

This makes repeated fetches share an identifier that looks like an investigation ID.

## Required remediation

Do not use an identifier that semantically resembles an investigation ID for a fetch operation.

If the domain permits an optional investigation ID, use `None` or an explicit operation context.

If a non-null UUID is technically required, define a distinct context type rather than overloading investigation identity.

---

# 67. Finding: direct fetch lacks correlation ID input

**Severity: LOW**

**File:**

`elastic.py`

Search accepts:

```python
correlation_id: str | None = None
```

Direct fetch does not.

This can create observability discontinuity.

## Required remediation

Add correlation context consistently through the evidence provider path.

Prefer request/context propagation rather than requiring every caller to manually provide it.

---

# 68. Finding: logging may expose tenant information without policy review

**Severity: LOW/MEDIUM**

**File:**

`elastic.py`

Some logs include tenant ID in structured context.

This may be acceptable, but the platform should have an explicit policy for tenant identifiers in logs.

## Required remediation

Follow existing logging/privacy standards.

If tenant IDs are sensitive:

- use a stable safe hash;
- avoid raw tenant identifiers.

Do not change this blindly; inspect repository conventions first.

---

# 69. Finding: provider data quality failures are not distinguished from empty results

**Severity: MEDIUM**

Both of these situations can currently look like normal results:

```text
no code metadata
```

and:

```text
malformed code metadata
```

Likewise:

```text
no SQL
```

versus:

```text
SQL exists but parser failed
```

## Required remediation

Introduce structured data-quality metadata where it materially affects investigation correctness.

Do not flood normal results with diagnostics.

The agent should be able to distinguish:

- no evidence;
- evidence without optional metadata;
- malformed provider data;
- extraction failure;
- truncated evidence.

---

# 70. Finding: no explicit query-cost model for keyword search

**Severity: MEDIUM**

**File:**

`elastic.py`

The implementation bounds keyword count but does not visibly bound:

- total keyword character count;
- combined query size;
- potentially expensive wildcard-like content if future query types are added.

Current `multi_match` is safer than raw query-string syntax, but agent-generated large strings remain a resource concern.

## Required remediation

Add bounded input lengths.

Consider:

- per-keyword maximum;
- total keyword payload maximum.

Reject oversized requests.

---

# 71. Finding: no explicit duplicate elimination for evidence results

**Severity: LOW/MEDIUM**

**File:**

`elastic.py`

The query sort is deterministic, but there is no explicit duplicate protection at the application layer.

Normally Elasticsearch document IDs should prevent duplicate hits within one page, but duplicate records can still become problematic if index aliasing/reindexing semantics change.

## Required remediation

Determine whether duplicate provider documents are possible under the index pattern.

If they are, define a stable evidence identity and deduplicate appropriately without hiding distinct evidence.

Do not add unnecessary deduplication if Elasticsearch guarantees uniqueness in the selected scope.

---

# 72. Finding: cross-index identity collision must be explicitly governed

**Severity: MEDIUM**

**File:**

`elastic.py`

Evidence identity incorporates:

```text
index + _id
```

which is appropriate if `_id` can repeat across indices.

Verify index naming/versioning behavior.

If aliases or index migrations can expose duplicate logical records across indices, document how identity and freshness are handled.

---

# 73. Finding: index alias/data migration behavior is not defined

**Severity: MEDIUM**

**File:**

`elastic.py`

The adapter assumes an index pattern directly represents the authoritative source.

Investigate:

- rolling indices;
- reindexing;
- aliases;
- retention;
- deleted documents;
- duplicate migration records.

## Required remediation

Document expected consistency guarantees.

If the platform requires a stable investigation snapshot, identify whether point-in-time or another mechanism is needed.

Do not introduce PIT automatically without evaluating resource cost and lifecycle.

---

# 74. Finding: pagination consistency across changing data is not defined

**Severity: MEDIUM**

**File:**

`elastic.py`

The implementation uses `search_after`.

`search_after` does not by itself create a snapshot across changing indices.

Between pages:

- new logs can arrive;
- old logs can be deleted;
- index segments can change.

This can cause missing or repeated evidence in a long investigation.

## Required remediation

Determine whether the investigation requires snapshot consistency.

If yes, use an appropriate Elasticsearch point-in-time strategy with bounded lifecycle.

If not, document that pagination is best-effort over a changing dataset.

Do not introduce PIT casually; it has operational costs.

Add tests/documentation for the selected semantics.

---

# 75. Finding: time-window boundaries need explicit inclusive/exclusive semantics

**Severity: LOW/MEDIUM**

**File:**

`elastic.py`

The query uses:

```text
gte start
lte end
```

This is valid, but the platform's time-range semantics should explicitly state whether both endpoints are inclusive.

## Required remediation

Document and test the boundary semantics.

Especially test adjacent pages/windows to prevent duplicate boundary events.

---

# 76. Finding: `request.environment` is interpolated into the index pattern

**Severity: HIGH**

**File:**

`elastic.py`

The search index is:

```python
f"logs-{tenant_id}-*-{request.environment}-*"
```

Tenant validation alone is insufficient.

Environment is also caller-controlled input.

## Required remediation

Validate environment against the platform's environment model.

Prefer enum/domain validation over string interpolation.

Reject:

- wildcard characters;
- path separators;
- whitespace where unsupported;
- control characters;
- unexpected patterns.

Do not strip dangerous characters and continue.

---

# 77. Finding: provider index naming logic is duplicated inline

**Severity: LOW**

**File:**

`elastic.py`

Index patterns are built in multiple places with different shapes.

This increases the risk of divergent isolation semantics.

## Required remediation

Introduce a small, tested index-scope builder responsible for:

- tenant validation;
- environment validation;
- allowed pattern construction.

Use it consistently for search and retrieval.

Do not turn it into a generic arbitrary-index API.

---

# 78. Finding: no explicit Elasticsearch mapping contract

**Severity: MEDIUM**

**Files:**

Both.

The code depends on fields including:

```text
@timestamp
_id
message
error.message
service.name
log.level
labels.*
code.*
log.origin.*
db.statement
```

The repository should define the supported provider schema.

## Required remediation

Document or test the expected Elastic/ECS mapping.

Create representative fixtures.

If mappings vary across deployments, make supported variants explicit.

---

# 79. Finding: no resilience strategy for partial provider responses

**Severity: MEDIUM**

**File:**

`elastic.py`

The mapper assumes every hit can become an `Evidence`.

One malformed hit can potentially fail the entire page.

## Required remediation

Choose explicit behavior:

- fail entire page because evidence integrity is required; or
- skip malformed records and return structured data-quality information.

Do not silently skip records.

For investigations, preserving evidence completeness information is critical.

---

# 80. Finding: provider response shape is trusted too much

**Severity: MEDIUM**

**File:**

`elastic.py`

The adapter accesses nested response fields without explicit schema validation.

## Required remediation

Create a small provider-response normalization/validation layer.

Do not over-engineer a complete Elasticsearch response model if unnecessary.

Validate only fields the platform actually consumes.

---

# 81. Finding: source location identifier may be insufficiently stable

**Severity: LOW/MEDIUM**

**File:**

`elastic.py`

The source location is:

```text
SourceLocation(
    system="Elasticsearch",
    identifier=hit["_id"]
)
```

If the same `_id` can exist across indices, the source location is ambiguous.

## Required remediation

Include the index or canonical provider document reference where the domain model permits.

Do not remove index context from provider provenance.

---

# 82. Finding: fingerprint semantics need alignment across Evidence and Provenance

**Severity: MEDIUM**

**File:**

`elastic.py`

The evidence:

```text
fingerprint = hit["_id"]
```

while the provenance:

```text
normalized_query_hash = hit["_id"]
```

uses the same identifier for different semantic purposes.

## Required remediation

Define separate concepts:

- evidence/provider document identity;
- evidence content fingerprint;
- query fingerprint.

Do not conflate them.

If content fingerprints are required, use a deterministic content hash.

---

# 83. Finding: content fingerprint is not stable across source mutation

**Severity: LOW/MEDIUM**

**File:**

`elastic.py`

If `fingerprint` is `_id`, it identifies the document but not necessarily the content version.

If Elasticsearch documents can be updated, the same evidence ID can point to changing content.

## Required remediation

Determine whether runtime log documents are immutable.

If immutable, document that invariant.

If mutable, distinguish document identity from content version/fingerprint.

---

# 84. Finding: no provider health/backpressure behavior is visible

**Severity: MEDIUM**

**File:**

`elastic.py`

Repeated agent investigations can generate concurrent Elastic requests.

The adapter has per-request timeouts but no visible concurrency/backpressure policy.

## Required remediation

Inspect application-level concurrency controls.

If absent and required by deployment scale, introduce bounded concurrency at the appropriate application/provider layer.

Do not put arbitrary global locks into the adapter.

---

# 85. Finding: cancellation/error behavior in bridge needs explicit testing

**Severity: MEDIUM**

**File:**

`elastic_bridge.py`

The bridge delegates to an async adapter but has no visible handling for cancellation or provider failures.

## Required remediation

Ensure:

- cancellation propagates;
- domain exceptions propagate appropriately;
- SQL extraction does not catch cancellation;
- one malformed event does not hide provider-level failure.

Add tests.

---

# 86. Finding: bridge's `profile` parameter may create a false sense of authorization

**Severity: MEDIUM**

**File:**

`elastic_bridge.py`

Passing an `ObservabilityProfile` into the method can look like an authorization boundary even if it is merely configuration.

## Required remediation

Determine the actual authorization model.

Authorization must come from trusted caller context and application policy, not from an LLM-supplied profile object.

If profile is a configuration input, keep it separate from authorization.

---

# 87. Required cross-cutting remediation: define a canonical normalized query

Create a normalized internal representation for Elastic searches.

It should contain only supported semantics:

```text
tenant
investigation
environment
identifiers
keywords
services
severities
time_range
sort_version
```

Canonicalize:

- list ordering where order is semantically irrelevant;
- UUID representation;
- timestamps;
- enum representation;
- empty/null values.

Use this representation for:

1. query construction;
2. cursor binding;
3. query fingerprints;
4. observability metadata where appropriate;
5. deterministic testing.

Do not include secrets.

---

# 88. Required cross-cutting remediation: separate trusted context from user query

The provider boundary should clearly distinguish:

## Trusted context

- authenticated tenant;
- investigation authorization;
- provider selection;
- authorization policy;
- classification policy.

## User/agent-controlled query

- keywords;
- service filters;
- severity filters;
- identifiers;
- time range;
- pagination request.

Never allow user-controlled fields to override trusted context.

---

# 89. Required cross-cutting remediation: evidence projection

Create a controlled projection from provider hit to domain evidence.

The projection should explicitly decide:

- identity;
- timestamps;
- summary;
- content;
- attributes;
- classification;
- provenance;
- freshness.

Avoid:

```python
attributes = entire_provider_source
```

unless the domain explicitly requires it.

---

# 90. Required cross-cutting remediation: data-quality contract

Introduce explicit status for evidence extraction where necessary.

Examples:

```text
complete
partial
truncated
malformed_provider_record
timestamp_missing
sql_parse_failed
```

Do not overpopulate ordinary results with diagnostics.

Expose only data-quality state that changes how an investigator should interpret the evidence.

---

# 91. Required cross-cutting remediation: deterministic behavior

The same logical request against the same stable dataset should produce:

- stable ordering;
- stable evidence IDs;
- stable fingerprints;
- stable derived table ordering;
- stable code-location ordering.

Any unavoidable nondeterminism must be documented.

---

# 92. Required test matrix

Build a comprehensive test suite.

## Search validation

- valid request;
- invalid limit;
- zero limit;
- negative limit;
- excessive limit;
- invalid environment;
- excessive keywords;
- excessive keyword length;
- invalid severity;
- excessive services;
- invalid identifier key;
- excessive identifier value;
- invalid time range;
- time range over maximum.

## Isolation

- tenant A cannot access tenant B;
- environment cannot escape tenant index scope;
- investigation A cannot reuse investigation B cursor;
- direct evidence retrieval honors intended ownership;
- trusted tenant cannot be overridden by request body.

## Pagination

- first page;
- next page;
- last page;
- malformed cursor;
- tampered cursor;
- wrong tenant;
- wrong investigation;
- changed query;
- changed environment;
- changed time range;
- cursor version mismatch.

## Provider failures

- timeout;
- connection failure;
- authentication failure;
- malformed provider response;
- missing `_id`;
- malformed `_source`;
- missing total;
- unexpected response type;
- cancellation.

## Evidence mapping

- valid timestamp;
- missing timestamp;
- malformed timestamp;
- naive timestamp;
- missing message;
- oversized message;
- sensitive fields;
- classification metadata;
- duplicate evidence.

## Bridge

- one trace event;
- multiple trace events;
- no events;
- 100 events;
- >100 events;
- missing code metadata;
- malformed code metadata;
- repeated code location;
- invalid line number;
- SQL success;
- SQL parse failure;
- multi-statement SQL;
- qualified table;
- quoted table;
- duplicate table;
- raw SQL truncation;
- SQL containing sensitive literals;
- malformed DB metadata.

## Determinism

Run the same logical input multiple times and verify stable:

- cursor payload semantics;
- evidence ordering;
- table ordering;
- code-location ordering;
- fingerprints.

---

# 93. Required integration tests

Use a mocked or test Elasticsearch provider to verify actual query bodies.

Tests must assert that generated requests:

- use the correct tenant index;
- use the correct environment scope;
- contain only allowed fields;
- contain no arbitrary caller-provided DSL;
- contain the expected time filter;
- contain expected service/severity filters;
- use deterministic sort;
- use `search_after` only with validated cursors;
- enforce size limits;
- enforce provider timeout.

Do not merely assert returned values.

Inspect the actual generated provider request.

---

# 94. Required adversarial tests

Create malicious-input tests representing an LLM generating unsafe requests.

Examples:

```text
tenant_id = "*"
tenant_id = "tenant-*"
environment = "*"
environment = "prod-*"
identifier key = "labels.foo]..."
keyword = extremely large string
service = extremely large string
cursor = arbitrary base64
cursor = valid cursor from another tenant
cursor = valid cursor from another query
trace_id = huge payload
SQL = credentials embedded in literals
log message = huge nested JSON
```

Expected outcome:

- rejection;
- safe bounded processing;
- or controlled provider error.

Never execute attacker-controlled infrastructure instructions.

---

# 95. Required observability remediation

Instrument the Elastic and bridge operations with:

- operation name;
- provider;
- safe tenant context;
- investigation context;
- correlation ID;
- result count;
- truncation state;
- error category;
- duration.

Do not record:

- full SQL;
- full log content;
- cursor token;
- secrets;
- authorization headers;
- uncontrolled user content.

Ensure bridge-derived telemetry remains connected to the provider span.

---

# 96. Required documentation

Document:

1. supported Elastic index naming;
2. required ECS/provider fields;
3. tenant isolation;
4. environment isolation;
5. investigation semantics;
6. evidence identity;
7. cursor semantics;
8. pagination consistency;
9. time-window semantics;
10. classification;
11. data-quality states;
12. SQL extraction semantics;
13. table-reference semantics;
14. known limitations;
15. configuration;
16. operational limits.

Do not document behavior that the implementation does not enforce.

---

# 97. Implementation constraints

The coding agent must:

- preserve public domain contracts where possible;
- avoid unrelated refactors;
- reuse existing exceptions;
- reuse existing configuration infrastructure;
- reuse existing tracing;
- reuse existing evidence/provenance models;
- maintain async behavior;
- avoid blocking operations in async paths;
- preserve tenant boundaries;
- preserve provider-side safety limits;
- add regression tests for every fixed issue.

If a proposed fix changes a public API, update all callers and tests.

---

# 98. Required implementation order

## Phase 1 — Repository and domain inspection

Inspect all dependencies and callers.

## Phase 2 — Security boundary hardening

Fix:

1. cursor secret;
2. cursor/query binding;
3. invalid cursor behavior;
4. tenant/environment validation;
5. direct-fetch scope;
6. sensitive attribute exposure.

## Phase 3 — Evidence correctness

Fix:

1. evidence ID semantics;
2. timestamp semantics;
3. provenance/query fingerprint;
4. classification;
5. source location;
6. evidence/content fingerprints.

## Phase 4 — Trace extraction correctness

Fix:

1. trace completeness;
2. code-location normalization;
3. evidence references;
4. SQL parsing;
5. table canonicalization;
6. SQL bounding/redaction.

## Phase 5 — Provider resilience

Fix:

1. response validation;
2. exception mapping;
3. timeout semantics;
4. cancellation;
5. malformed records.

## Phase 6 — Determinism and operational hardening

Fix:

1. deterministic ordering;
2. query normalization;
3. pagination semantics;
4. provider cost controls;
5. configuration.

## Phase 7 — Tests and documentation

Complete the entire matrix above.

---

# 99. Definition of done

The remediation is complete only when:

- [ ] No production cursor secret is hard-coded.
- [ ] Cursor validity is bound to query semantics.
- [ ] Invalid cursors fail rather than silently returning page one.
- [ ] Tenant IDs are validated before index construction.
- [ ] Environment values are validated before index construction.
- [ ] Direct retrieval has documented and enforced scope.
- [ ] Platform evidence IDs have unambiguous semantics.
- [ ] Search result evidence IDs can be used according to the retrieval contract.
- [ ] Observation time is never fabricated from retrieval time.
- [ ] Timezone semantics are explicit.
- [ ] Query fingerprints actually fingerprint normalized queries.
- [ ] Evidence fingerprints are not confused with query fingerprints.
- [ ] Sensitive provider attributes are projected/redacted.
- [ ] Raw content is bounded.
- [ ] Classification semantics are explicit.
- [ ] Trace truncation/completeness is explicit.
- [ ] Code locations are deduplicated and normalized.
- [ ] Derived telemetry retains evidence provenance.
- [ ] SQL is treated as untrusted evidence.
- [ ] SQL length is bounded.
- [ ] SQL parsing failure is represented explicitly.
- [ ] Table references preserve meaningful qualification.
- [ ] Table output ordering is deterministic.
- [ ] Provider responses are validated.
- [ ] Provider exceptions are categorized.
- [ ] Cancellation is not swallowed.
- [ ] Timeout semantics are coherent.
- [ ] Query inputs have explicit size limits.
- [ ] Invalid restrictive filters cannot silently broaden a query.
- [ ] Pagination semantics are documented.
- [ ] Tests cover malicious input.
- [ ] Tests inspect generated Elasticsearch requests.
- [ ] Existing tests remain green.
- [ ] Formatting passes.
- [ ] Linting passes.
- [ ] Type checking passes.
- [ ] Documentation matches actual behavior.
- [ ] No unrelated MCP implementation is introduced in this remediation.

---

# 100. Final report required from the coding agent

At completion, report:

## Executive summary

What was hardened and why.

## Findings fixed

For every remediation:

```text
ID
Severity
Package
File
Original defect
Implemented fix
Security/correctness impact
Tests added
```

## API/domain changes

List all changed public/internal contracts.

## Security changes

List isolation, input-validation, secret, cursor, and data-exposure changes.

## Evidence semantics

Explain:

- evidence ID;
- provider ID;
- content fingerprint;
- query fingerprint;
- observation timestamp;
- retrieval timestamp;
- provenance.

## Trace telemetry semantics

Explain:

- completeness;
- truncation;
- code locations;
- SQL;
- table references;
- evidence references.

## Validation

Report exact commands/results for:

- unit tests;
- integration tests;
- security tests;
- type checking;
- linting;
- formatting.

## Remaining risks

List anything not fixed and why.

Do not claim the system is secure or complete merely because tests pass. Identify residual assumptions and repository-level dependencies explicitly.

---

# Final engineering principle

Treat Elasticsearch output as **untrusted external evidence**.

Treat every agent-generated investigation request as **untrusted input**.

Treat provenance as part of correctness, not metadata decoration.

Treat timestamps, identifiers, cursors, and derived telemetry as semantic contracts.

The remediation must make the existing evidence subsystem harder to misuse, harder to misinterpret, and easier to audit without turning it into a new architecture.
