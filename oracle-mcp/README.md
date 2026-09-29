# Oracle SQLcl MCP Server (per-database instances, shared image)

Native SQLcl MCP over stdio; no Python MCP code anywhere in this path.
Full design: `docs/IAP-implementation-part3-knowledge-v1.0-addendum-oracle-mcp.md`.
Source requirements: `docs/scratchpad/oracle-mcpserver.txt`.

Base: `container-registry.oracle.com/database/sqlcl` pinned by digest
(current: SQLcl 26.2.2.233.1901, Java 17, non-root `sqlcl` user added here).
The vendored `docker/dependencies/sqlcl-latest.zip` is an incomplete bundle
(hollow `dbtools-sqlcl.jar`) and is NOT used — do not wire it back in.

## Build (no download needed)

```bash
docker build -t oracle-sqlcl-mcp:26.2 oracle-mcp/
docker run --rm --entrypoint /opt/oracle/sqlcl/bin/sql oracle-sqlcl-mcp:26.2 -version
```

## Run (one instance per database)

```bash
cp scripts/tolam_stg.json oracle-mcp/connections/
# place the SQLcl encryption key at oracle-mcp/secrets/sqlcl-encryption-key (0600)
docker compose -f oracle-mcp/docker-compose.yml run --rm -T oracle-sqlcl-mcp-tolam-stg
```

`connections/` and `secrets/` are git-ignored and mounted read-only; they never enter image layers.
