# Knowledge artifacts: reading and provenance

Audience: analysts and agent developers.

## Artifact kinds

| Kind | Refresh policy | Store | Shared? |
|---|---|---|---|
| `preference`, `micro_fact` | TTL / immutable | Mem0 (+envelope) | Never |
| `interpretation` | Conditional or immutable | Envelope (+Mem0 projection) | Never |
| `flag_evaluation`, `effective_config`, `policy_outcome` | Conditional | Envelope (+Graphiti episode) | Never |
| `error_signature`, `domain_attribution` | Immutable | Envelope (+Graphiti episode) | Only these, and only with a `code_issue_fingerprint` |

Runtime/session content (`flag_evaluation`, `effective_config`,
`policy_outcome`, `interpretation`) is always tenant-private. Only static,
tenant-free content may carry `SHARED_CODE_ISSUE` visibility.

## Validity semantics

- `IMMUTABLE`: true forever (code facts, completed interpretations).
- `CONDITIONAL`: re-verified against the live source on every reuse;
  mismatch supersedes, endpoint-down quarantines after bounded retries.
- `TTL`: expires by clock; expired items are excluded and counted, never
  silently reused.

`INCONCLUSIVE`-vs-stale: `INCONCLUSIVE` is a reasoning outcome (evidence
insufficient); stale means excluded-by-validity. They are different fields
— check `excluded_stale_count` on the reasoner context for the latter.

## API examples

```bash
# Artifacts for one investigation (superseded shown, never hidden)
curl -H "X-Tenant-ID: acme" \
  localhost:8000/api/v1/investigations/<id>/knowledge

# Tenant-scoped search
curl -H "X-Tenant-ID: acme" \
  "localhost:8000/api/v1/knowledge/search?q=pool+exhaustion&application_id=payments"
```

On merged investigations, other tenants' sessions render as
`{"session_number": N, "tenant": "[redacted]", ...}` — counts stay
truthful, identities hidden.

## Provenance

Every artifact carries `source_evidence_ids`, `source_log_refs`
(Elasticsearch pointers, not payloads), `code_refs` (`repo@rev:path#line`
into Layer 3), and `store_refs` (Mem0/Graphiti backend IDs). Graphiti
episodes additionally record `reference_time` (validity start, never ingest
time) so temporal extraction resolves relative dates correctly.

## Retention-classification smoke query (post-restore check)

```cypher
MATCH (e) WHERE e.group_id STARTS WITH 'inv_'
RETURN e.group_id AS group_id, count(*) AS episodes
ORDER BY group_id LIMIT 20
```

Every group must match `^[a-zA-Z0-9_-]+$`; any other value indicates a
namespace violation — quarantine and investigate before re-enabling
Graphiti writes.
