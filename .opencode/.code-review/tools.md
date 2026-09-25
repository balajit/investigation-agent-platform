```markdown
# Repository Tool Discovery

The coding agent must discover and use the tools already applicable to the
repository.

Do not assume a specific language, framework, package manager, test framework,
or scanner.

## Discover

Inspect:

- project manifests
- build files
- Makefile/task runners
- formatter configuration
- linter configuration
- type-checker configuration
- test configuration
- CI workflows
- container/build configuration
- dependency/security configuration

Determine the available commands for:

1. Build
2. Test
3. Format
4. Lint
5. Type checking
6. Static analysis
7. Security/dependency scanning
8. Application-specific validation

Prefer commands documented by the project.

## Use tools proportionally

Run the cheapest useful validation first:

1. targeted tests
2. targeted lint/type checks
3. relevant static analysis
4. broader tests/build
5. security/dependency scans when relevant

Do not run expensive repository-wide operations merely because they exist.

## Tool evidence

When a tool reports a problem:

- capture the relevant output;
- connect it to the source location;
- determine whether it represents a real defect;
- avoid duplicating the same issue as a separate manual finding.

When a tool passes:

- treat that as evidence for that tool's scope only;
- do not interpret a passing lint/test command as proof that the code is correct.

## Missing tools

If an important validation tool is absent:

- do not invent its result;
- perform static/manual analysis where possible;
- identify the missing validation as a test or verification gap when material.

## Tool installation

Do not install new dependencies or modify project configuration during review
unless explicitly requested.

If a useful tool is unavailable, report the limitation rather than silently
changing the repository.
```

