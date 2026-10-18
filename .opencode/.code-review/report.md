````markdown
# Code Review Report

Produce a concise engineering report.

## Summary

Include:

- repository/scope reviewed
- applicable language/framework
- tools detected and executed
- tests executed
- significant validation limitations

Do not provide an overall numeric score or claim the repository is defect-free.

## Findings

Order findings by severity.

For each finding:

```text
ID: CR-001
Severity: HIGH
Category: SECURITY | CORRECTNESS | RELIABILITY | DATA | PERFORMANCE | DESIGN | TEST
Confidence: CONFIRMED | LIKELY | UNVERIFIED

Title:
<short description>

Location:
<file:line or precise symbol>

Evidence:
<what the code actually does>

Failure scenario:
<how the problem can occur>

Impact:
<concrete consequence>

Recommendation:
<specific remediation>
````

## Test gaps

Report missing tests separately when they represent meaningful risk.

A missing test is not automatically a defect.

## Unverified areas

List material areas that could not be validated because of:

* missing dependencies
* unavailable infrastructure
* unavailable credentials
* missing tests
* unavailable tools
* incomplete repository context

## Recommendations

Only include recommendations that materially improve correctness,
security, reliability, maintainability, or operability.

Do not turn ordinary style preferences into recommendations.

## Final quality check

Before returning the report:

* remove duplicates;
* verify every location;
* verify every claim against repository evidence;
* remove speculative claims;
* normalize severity;
* distinguish defects from test gaps;
* distinguish confirmed findings from unverified concerns;
* ensure recommendations address the stated root cause.

```
```

