# Application profiles (`config/profiles/`)

Each `*.yaml` file here is an `ApplicationProfile` document validated by
`load_application_profile_from_file` (`infrastructure/configuration/config.py`)
against `domain/profile/models.py`. Profiles are tenant-scoped, immutable
revisions keyed by `(tenant_id, id, version)`.

| File | What it is |
|---|---|
| `example.yaml` | Portable generic template covering every field (observability, state, code, correlation, investigation, `parametersSchema`; optional `referenceDocs` commented out). Uses the built-in `generic-ecs` mapping. |
| `gen-verbose.yaml` | Real `gen` backend HTTP-transaction stream, bound to `mappingSourceId: gen-verbose`. Code/state are `TBD` placeholders (`status: DRAFT`, log evidence only). |
| `gen-root-cause.yaml` | Real `gen` root-cause record family, bound to `mappingSourceId: gen-root-cause`. No `@timestamp`/severity in that stream. |

## Authoring rules

- **Top-level scalars are snake_case** — `tenant_id`, `allowed_capabilities`,
  `version`, `status`, `owner`, `environment`. There are no camelCase aliases
  for these, and the model silently ignores unknown keys, so a typo like
  `allowedCapabilities` becomes an empty list with no error.
- **Sub-blocks use camelCase keys** — `mappingSourceId`, `defaultBranch`,
  `queryTemplates`, `primaryIdentifiers`, `sourceRoots`, `referenceDocs`, …
- **`enabledEvidenceTypes` drives authorization.** `allowed_capabilities` is
  informational today; the authorizer in
  `infrastructure/security/profile_authorizer.py` derives permissions from
  enabled evidence types:
  `SEARCH_LOGS→LOG`, `QUERY_STATE→DATABASE_STATE`,
  `GET_CODE→COMMIT_HISTORY`, `CORRELATE→TRACE`.
- **`mappingSourceId` must resolve** to a `source_id` under
  `config/observability-mappings/`, or to the built-in `generic-ecs`, or boot
  fails closed.
- **Query templates are read-only** — `DROP`/`DELETE`/`TRUNCATE`/`ALTER`/
  `GRANT`/`REVOKE` are rejected. `repository`/`sourceRoots` reject `..` and NUL.
- **Never fabricate facts.** If code or state details are unknown, use `TBD`
  placeholders and `status: DRAFT` (as the `gen-*` profiles do).

## Validate and load

```bash
# Validate every sample (also covered by tests/unit/test_profile_samples.py):
uv run python scripts/seed_profile.py --dir config/profiles --dry-run

# Register into the profile store (idempotent on (tenant_id, id, version)):
IAP_DATABASE_URI='postgresql+asyncpg://iap:iap@localhost:5432/iap' \
  uv run python scripts/seed_profile.py --file config/profiles/example.yaml
```

The profiles API is read-only (`GET /profiles`); seeding is done through the
script above or directly in the database.
