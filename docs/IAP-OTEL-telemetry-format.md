# IAP OpenTelemetry Telemetry Format (normative)

Source: extracted from the telemetry format plan (2026-10-16) into this durable
record. The span IS the record — no separate "record" type is emitted.

## 1. Span name

`{layer}.{component}.{operation}` — all lowercase, dots only:

- `layer`: `worker` | `reason` | `gateway` | `provider` | `knowledge` | `topology` | `mcp`
- `component`: module-ish noun (`execute_action`, `mcp_adapter`, `retrieval`, `verifier`)
- `operation`: function/method name (`reason`, `get_application_state`, `call_tool`)

Examples: `worker.execute_action_activity`, `reason.coordinator.reason`,
`gateway.gateway.get_application_state`, `provider.mcp.call_tool`,
`knowledge.retrieval.retrieve_for_reasoning`, `reason.verifier.evaluate`.

The decorator derives this automatically: `@traced("worker")` on
`execute_action_activity` → `worker.activities.execute_action_activity`
(component defaults to module short name, overridable).

## 2. Required attributes (EVERY span, no exceptions)

| Attribute | Type | Rule |
|---|---|---|
| `iap.tenant_id` | string | ALWAYS. Tenant-first: extract from kwargs (`tenant_id`), else `context.tenant_id`, else span is non-compliant (log warning, still emit). |
| `iap.investigation_id` | string | Whenever in scope (all loop activities, gateway, providers, knowledge). Omit only where truly absent (e.g. janitor tenant sweep → use `iap.tenant_id` + `iap.sweep=true`). |
| `iap.component` | string | Dotted component path (matches span-name middle). |
| `iap.outcome` | string | Set on exit: `ok` \| `error` \| `degraded` \| `skipped` \| `blocked`. Degraded/skipped/blocked are FIRST-CLASS (not errors) — dashboards alert on `error` rate, track the rest as signals. |

## 3. Optional attribute families (per kind, bounded)

- Actions: `iap.action_type`, `iap.items_count`, `iap.evidence_ids_count` (counts, never IDs — cardinality).
- Providers: `iap.provider`, `iap.tool`, `iap.transport`, `iap.source` (per fan-out leg).
- LLM: `iap.model`, `iap.tokens.{prompt,completion,total}`, `iap.cost_usd`, `iap.prompt_version`, `iap.prompt_hash`.
- Budgets/gates: `iap.readiness`, `iap.verdict` (VERIFIED/INCONCLUSIVE), `iap.blockers_count`, `iap.hypotheses_count`.
- Cache: `iap.cache={hit,miss,disabled}`, `iap.cache_backend={redis,memory,none}`.
- Errors: `iap.error_type`, `iap.error_message` (truncated 500ch). On exception the span status is ERROR and these two are set; stack traces OFF by default.

## 4. Forbidden content (hard rules)

- NEVER: prompt/response bodies, problem descriptions, evidence snippets, SQL/query text, connection strings, headers, secrets, raw IDs beyond investigation/tenant (no evidence-ID lists — counts only).
- Values truncated: any string attribute > 256ch is cut (defense in depth behind code review).
- Tenant boundary: a span carrying `iap.tenant_id=A` MUST NOT carry data from tenant B (shared-artifact reads are attributed to the CALLER tenant).

## 5. Mechanism — decorator first

Shared module `infrastructure/observability/tracing.py`:

```python
@traced("worker")                                    # name: worker.activities.<fn>
@traced("provider", component="mcp_adapter")         # override component
@traced("knowledge", tenant_from="ctx", inv_from="ctx")  # custom extractors
async def execute_action_activity(params): ...
```

Decorator contract (sync + async variants, `functools.wraps` preserved):

1. Resolve tracer `trace.get_tracer("iap.<component>")` once per module.
2. Extract `tenant_id` / `investigation_id`: kwargs by name → `context`/`scope` attrs → explicit `tenant_from`/`inv_from` callables → absent (warn once per process, emit without).
3. `with tracer.start_as_current_span(name) as span:` set required attrs from §2.
4. Await/call fn; on return set `iap.outcome` (mapper arg: default `ok`; provider fns map return status → `ok/degraded/skipped/blocked` via `outcome_from` callable).
5. On exception: set status ERROR + `iap.error_*`, re-raise (no stack export by default; `record_exception` opt-in per call site).
6. OTel SDK absent/unconfigured → global no-op tracer → zero-cost, zero-branch at call sites.

Manual `with` blocks ONLY where decorators cannot go: generator bodies needing
per-item spans, dynamic span names (per-source fan-out legs), and Temporal
WORKFLOW code (replay determinism). Aligns with the existing
`ObservabilityPort` (spans carry what `record_llm_call` / `record_tool_call` /
`record_evidence_retrieval` already model — those methods stay as the
METRICS/EVENTS path; spans are the TRACE path; same attribute names).

## 6. Coverage status

Per `IAP-architecture-v1.0.md` §42/§130: 15 worker activities, reasoning core
(coordinator/compiler/verifier/disposition/services/memory), gateway + per-source
fan-out legs + selector, MCP/probe/code/state/log/sanitizer providers, knowledge
(retrieve/recalls/capture/intake/janitor), topology, attribution, correlation
engine, 4 MCP tools. No spans in Temporal workflow code (replay determinism).
