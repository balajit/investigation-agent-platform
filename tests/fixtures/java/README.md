# Java fixture battery (`tests/fixtures/java/`)

Deterministic Phase 8 fixtures for the Java analyzers. Each `.java` file is
fed to resolvers as a revision-pinned range read; `manifest.json` declares the
expected outcome per case.

## Manifest format

```text
{
  "schema_version": "v1",
  "pinned_commit": "<repo HEAD the fixtures were validated against>",
  "cases": [
    {
      "file": "FieldDefaults.java",
      "area": "A/C",              // JAV_PROMPT coverage area(s)
      "language_version": 17,     // caller-attested trusted source level
      "resolver": "field",        // field|local|init|call|dataflow|control|type|boundary
      "symbol": "retryCount",
      "kwargs": {"declaring_type": "FieldDefaults"},
      "expect_status": "RESOLVED",
      "expect_value": "3",        // resolved_value assertion (when present)
      "expect_target": "...",     // call_target assertion (call cases)
      "expect_fact": "...",       // fact_category assertion (V2 cases)
      "expect_codes": []          // required subset of limitation_codes
    }
  ]
}
```

## Pinning rule

`pinned_commit` records the repository HEAD the fixtures were validated
against. Fixture source is fed inline (not checked out at the SHA), so the pin
certifies the analyzer revision, not the fixture text. Re-validate
(`test_java_fixture_battery.py`) and update the pin whenever analyzer,
grammar, or fixture content changes.

## Adding a fixture

1. Add the `.java` file (self-contained, package-qualified, compilable shape).
2. Append manifest cases with the exact expected status, codes, and values.
3. Every supported feature needs a correct-claim case and an incorrect-claim
   counterexample; every unsupported construct needs a partial/unresolved case
   naming its limitation code.
4. Run the battery; never weaken a verifier gate to make a case pass.
