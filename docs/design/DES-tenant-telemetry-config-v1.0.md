# DES-tenant-telemetry-config v1.0

- Status: ACCEPTED
- Satisfies: REQ-investigation-loop-reengineering-v1.0 (C3, C4, G4)
- Implements: ADR-BOUNDARY-001 (service scope); IAP-investigation-architecture-v2.0 (feedback edges)

## 1. Schema

Tenant operator-scope file, a list of named endpoint blocks. Each block: `name` (unique, required), `url` (full base URL, required), auth group (`user`+`password` basic pair or `api_key`, mutually exclusive, key wins; values never logged), `timeout` (default 30s), `pattern` (default `logs-*`), `services` (service names homed here, required). Invariant: every service maps to exactly one endpoint (configure-time check, fail closed).

## 2. Storage, precedence, secrets

File in tenant operator scope is source of truth (owner-readable only, never in repo). Env overrides per endpoint (`IAP_ES_<TENANT>_<NAME>_{URL,USER,PASSWORD,API_KEY,TIMEOUT,PATTERN}`). Secret handling follows the existing SecretStr/redaction standard.

## 3. Wiring

One adapter, tenant-resolved connections per request: service → home endpoint → that endpoint's auth/pattern/timeout. Global default applies only when the tenant configures nothing. Selector, mapping profiles, and profile authorization untouched.

## 4. Validation and failure

Configure-time per endpoint (reachable → auth → pattern matches → mappings parse → sanity read), fail closed. Runtime: required-fetch on a dead endpoint → BLOCKED_ON_TELEMETRY with ERROR-level structured logs (no credentials), backoff + heartbeat, then terminal DEFERRED with resume pointer; the condition stays open until recovery or acknowledgment. Code-side-only work never blocks on telemetry. Warning level is reserved for degraded-optional; required-evidence failure is always ERROR.
