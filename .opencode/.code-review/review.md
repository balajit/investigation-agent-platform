```markdown
# Engineering Code Review

Review the implementation as a production codebase.

For every material issue, establish:

`trigger → code path → failure/incorrect behavior → impact`

Do not report a concern without evidence.

## 1. Correctness

Check:

- incorrect logic
- incorrect state transitions
- unreachable or missing branches
- incorrect conditions
- incorrect parameter/return-value usage
- null/empty/boundary failures
- exception paths
- resource lifecycle errors
- incorrect assumptions about dependencies

## 2. Architecture and design

Check:

- inappropriate coupling
- misplaced responsibilities
- abstraction leaks
- circular dependencies
- excessive complexity
- duplicated business logic
- incorrect layering
- domain logic embedded in infrastructure
- hidden side effects
- difficult-to-test design

Prefer concrete architectural problems over subjective style opinions.

## 3. API and contracts

Check:

- request/response validation
- schema mismatches
- incorrect status/error handling
- backward compatibility
- missing authorization checks
- contract violations between modules/services
- unsafe assumptions about external callers

## 4. Data and database

When applicable, check:

- transaction boundaries
- rollback behavior
- consistency
- query correctness
- N+1 access
- unbounded queries
- locking
- race conditions
- connection/session lifecycle
- migration/schema assumptions
- partial failure behavior

## 5. Concurrency and asynchronous execution

When applicable, check:

- shared mutable state
- races
- duplicate execution
- task cancellation
- task cleanup
- deadlocks
- lock ordering
- unsafe retries
- background task lifecycle
- timeout behavior

## 6. External services

Check:

- timeout handling
- retry behavior
- retry amplification
- malformed responses
- connection lifecycle
- dependency failure propagation
- partial failures
- authentication/credential handling
- idempotency of externally visible operations

## 7. Security

Check:

- authentication
- authorization
- input validation
- injection
- unsafe deserialization
- command/code execution
- path traversal
- SSRF
- secret exposure
- sensitive-data leakage
- insecure defaults
- trust-boundary violations

Only report vulnerabilities supported by the implementation.

## 8. Resilience and idempotency

Check whether operations remain safe under:

- retry
- timeout
- replay
- duplicate delivery
- process restart
- partial completion
- dependency failure

Pay particular attention to operations that mutate state.

## 9. Observability

Check whether important operations provide sufficient diagnostic information:

`request → operation → dependency → state change → failure`

Look for:

- missing correlation/trace context
- insufficient structured logging
- swallowed exceptions
- missing failure context
- sensitive information in logs
- inability to determine which operation failed

## 10. Performance

Check only material issues:

- unbounded work
- repeated expensive operations
- N+1 behavior
- unnecessary serialization
- excessive memory usage
- blocking I/O
- uncontrolled concurrency
- missing pagination
- inefficient algorithms

Do not micro-optimize ordinary code.

## 11. Testing

Check whether important behavior is actually tested.

Prioritize:

- failure paths
- boundaries
- state transitions
- security controls
- concurrency
- external dependency failures
- regression-prone logic

Do not equate line coverage with correctness.

## 12. CI/build/dependency hygiene

When applicable, check:

- reproducible builds
- dependency pinning/policy
- test execution
- static checks
- secret handling
- unsafe build scripts
- dependency/security scanning
- production code excluded from required validation

## Review discipline

For every suspected issue ask:

1. Is this actually possible?
2. Can I identify the execution path?
3. Is there repository evidence?
4. Does existing code already prevent it?
5. Is it already covered by a test?
6. Is it a duplicate of another finding?
7. What is the concrete impact?

If the answer cannot be established, do not present speculation as a confirmed defect.
```

