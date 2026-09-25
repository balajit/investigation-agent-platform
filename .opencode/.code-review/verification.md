```markdown
# Finding Verification

Before a finding becomes a confirmed issue, verify it.

## Verification process

For each candidate finding:

1. Locate the exact source.
2. Inspect surrounding control flow.
3. Inspect callers and relevant callees.
4. Inspect related configuration/schema/contracts.
5. Check existing tests.
6. Run a targeted test or static check when practical.
7. Determine whether another mechanism already prevents the problem.
8. Determine the realistic failure condition.
9. Determine the resulting impact.

## Evidence levels

### CONFIRMED
Repository evidence demonstrates the issue and its execution path.

### LIKELY
Strong evidence indicates the issue, but execution cannot be completely
demonstrated with available information.

### UNVERIFIED
The issue is plausible but cannot be established from repository evidence.

Do not report `LIKELY` or `UNVERIFIED` findings as confirmed defects.

## Severity

### CRITICAL
Likely catastrophic security, data-integrity, availability, or system-level
impact with a credible execution path.

### HIGH
Material production failure, security issue, data corruption, or reliability
problem with a credible execution path.

### MEDIUM
Meaningful defect or operational risk with limited scope, mitigations, or
less severe impact.

### LOW
Minor defect, maintainability problem, diagnostic gap, or low-impact risk.

Severity must describe impact, not how easy the issue is to fix.

## Duplicate handling

Merge findings when multiple symptoms share the same root cause.

Prefer:

`one root cause → one finding → multiple affected locations`

over multiple findings describing the same defect.
```

