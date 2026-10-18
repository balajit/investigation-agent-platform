# Oracle SQLcl MCP Server (per-database instances, shared image)

Native SQLcl MCP over stdio; no Python MCP code anywhere in this path.
Full design: `docs/IAP-implementation-part3-knowledge-v1.0-addendum-oracle-mcp.md`.

Base: `container-registry.oracle.com/database/sqlcl` pinned by digest
(current: SQLcl 26.2.2.233.1901, Java 17, non-root `sqlcl` user added here).
The vendored `docker/dependencies/sqlcl-latest.zip` is an incomplete bundle
(hollow `dbtools-sqlcl.jar`) and is NOT used — do not wire it back in.

## Datasource store (outside the repo)

No datasource names, connections, or keys live in this repo. All state lives
under (override with env vars, defaults shown):

```text
${IAP_ORACLE_MCP_HOME:-${HOME}/workspace/docker/iap/oracle-mcp}/
  connections/   ($IAP_ORACLE_MCP_CONNECTIONS_DIR) one *.backup per datasource
  secrets/       ($IAP_ORACLE_MCP_SECRETS_DIR)     sqlcl-encryption-key (0600)
```

Add a datasource (needs local `sql` on PATH and DB reachability):

```bash
# TNS descriptor (default merges into the single existing *.backup, if one):
oracle-mcp/add-datasource.sh --name <conn-name> --tns <tns> --user <db-user>
# ...or a full custom JDBC string, merged into the existing connections:
oracle-mcp/add-datasource.sh --name <conn-name> \
  --jdbc 'jdbc:oracle:thin:@(DESCRIPTION=...)' --user <db-user> \
  --into ~/workspace/docker/iap/oracle-mcp/connections/<existing>.backup
# ...or a standalone single-connection archive:
oracle-mcp/add-datasource.sh --name <conn-name> --tns <tns> --user <db-user> --standalone
```

Quoting rules: user/target are quoted, password is saved unquoted, so
passwords containing `/ @ "` or whitespace are rejected. `--password-env`
/ `--password-file` avoid the interactive prompt; `--replace` overwrites.

## Build (no download needed)

```bash
docker build -t oracle-sqlcl-mcp:26.2 oracle-mcp/
docker run --rm --entrypoint /opt/oracle/sqlcl/bin/sql oracle-sqlcl-mcp:26.2 -version
```

## Run (one instance per database)

```bash
docker compose -f oracle-mcp/docker-compose.yml run --rm -T oracle-sqlcl-mcp
```

The external store is mounted read-only; it never enters image layers.
`oracle-mcp/connections/` and `oracle-mcp/secrets/` are legacy git-ignored
placeholders — do not put real exports or keys there.

## Per-scope credential files (`scopes/<org>-<app>-<env>/oracle.yaml`)

One YAML file per operator scope in the external store (never in this repo):

```text
${IAP_OPERATOR_HOME:-~/workspace/.iap}/scopes/<scope>/oracle.yaml   # chmod 0600
```
Scope is a free-form operator namespace in canonical `<org>-<app>-<env>` slug shape (>=3 dash-segments). Legacy path `${IAP_ORACLE_MCP_HOME}/apps/<app>/oracle.yaml` still resolves with a deprecation warning — move it into a scope with:
```bash
SCOPE=sample-orders-dev  # your <org>-<app>-<env>
mkdir -p "${IAP_OPERATOR_HOME:-~/workspace/.iap}/scopes/${SCOPE}"
mv "${IAP_ORACLE_MCP_HOME:-~/workspace/docker/iap/oracle-mcp}/apps/<app>/oracle.yaml" \
   "${IAP_OPERATOR_HOME:-~/workspace/.iap}/scopes/${SCOPE}/oracle.yaml"
chmod 0600 "${IAP_OPERATOR_HOME:-~/workspace/.iap}/scopes/${SCOPE}/oracle.yaml"
```

Schema: `per-app-oracle.yaml.example` (empty values). Logical database names
are dotted `<domain>.<subdomain>.<env>` with three secret keys per database
(`user_name`, `password`, `jdbcUrl`) plus optional non-secret
`default_schema` (kept next to the user so grants are eyeballed in one
place; the profile datasource's `schema_name` is the enforced source of
truth — keep them identical). The loader emits the three `IAP_SECRET_*`
exports plus informational `IAP_ORACLE_<D>_<SD>_<E>_DEFAULT_SCHEMA`.
The three secret names double as the AWS Secrets Manager JSON fields
(`iap/oracle/<d>/<sd>/<e>`) and the OpenBao KV fields
(`secret/iap/oracle/<d>/<sd>/<e>`).

```bash
# Bring one database's credentials into the environment (eval-only output):
set -a
eval "$(oracle-mcp/load-oracle-env.sh --scope sample-orders-dev --app spring-sample-service --db sample.orders.prod)"
set +a
# ... then import (password travels via env -> SQLcl stdin, never argv/logs):
oracle-mcp/add-datasource.sh --name SAMPLEDB-conn --user "${IAP_SECRET_ORACLE_SAMPLE_ORDERS_PROD_USER_NAME}" \
  --jdbc "${IAP_SECRET_ORACLE_SAMPLE_ORDERS_PROD_JDBC_URL}" \
  --password-env IAP_SECRET_ORACLE_SAMPLE_ORDERS_PROD_PASSWORD
```

Loader fail-closed rules: file must be `0600` (no group/other read),
`app:` must equal `--app` (content match), `environment: prod` refuses
`account_kind: personal` (personal accounts are local-only; prod requires a
service account). Dotted names map to `IAP_SECRET_ORACLE_<D>_<SD>_<E>_PASSWORD`
etc. — the same names the app's `secret://oracle/...` resolver fallback reads.
