```markdown
# Engineering Standards

## 1. Determine the applicable standard

Before reviewing implementation details, inspect the repository for:

- `README*`
- `CONTRIBUTING*`
- `AGENTS.md`
- `CLAUDE.md`
- `pyproject.toml`
- `package.json`
- `tsconfig.json`
- `go.mod`
- `Cargo.toml`
- `pom.xml`
- `build.gradle*`
- formatter/linter configuration
- type-checker configuration
- test configuration
- CI configuration
- architecture/design documentation

Project-local instructions take precedence over generic engineering conventions.

Identify:

- language and framework conventions
- formatting rules
- lint rules
- type-checking requirements
- naming conventions
- error-handling conventions
- logging conventions
- testing conventions
- dependency policies
- API/schema conventions
- security requirements

## 2. Coding standards

Evaluate code for:

### Correctness
- clear control flow
- correct state transitions
- correct parameter usage
- correct return values
- correct exception handling
- correct resource ownership
- correct boundary handling

### Maintainability
- cohesive modules
- appropriate abstraction boundaries
- minimal duplication
- understandable naming
- functions/classes with focused responsibilities
- limited unnecessary complexity
- no hidden coupling

### Type safety
- correct declared types
- no unsafe casts without justification
- correct optional/null handling
- validated external input
- consistent domain types

### Error handling
- errors are handled at the appropriate layer
- exceptions are not silently swallowed
- errors retain useful context
- expected failures are distinguishable from unexpected failures
- retryable and non-retryable failures are not confused

### Resource management
Check lifecycle of:

- database connections
- transactions
- files
- sockets
- HTTP clients
- sessions
- locks
- tasks/background workers
- temporary resources

### Configuration
- configuration is explicit
- required settings are validated
- unsafe defaults are avoided
- secrets are not embedded in source
- environment-specific behavior is controlled by configuration

### Logging and diagnostics
- meaningful operational events are logged
- logs contain useful context
- sensitive information is not logged
- exceptions preserve diagnostic information
- correlation/request identifiers are propagated where applicable

### Tests
Tests should demonstrate behavior rather than merely execute lines.

Look for coverage of:

- normal behavior
- invalid input
- boundary conditions
- failure paths
- concurrency where applicable
- external dependency failures
- authorization/security behavior
- regression cases

## 3. Standard hierarchy

Use this order when standards conflict:

1. Explicit repository/project instructions
2. Existing enforced tooling configuration
3. Existing architectural/API contracts
4. Language/framework conventions
5. General engineering best practices

Do not invent a project rule that is not evidenced by the repository.
```

