# docker/postgres-mcp — secret-injection pattern for crystaldba instances (P3-3).

## Rule

No secret is ever baked into compose files, YAML configs, or the repo. Database
passwords resolve at container start from (in order):

1. `IAP_MCP_<NAME>_DB_URI` shell environment (operator export or secret-manager
   agent rendering into the process environment before `start-platform.sh` runs).
2. Fallback `${IAP_DATABASE_URI}` for the IAP application database instance only
   (`postgres-mcp-iap` service) — same value the API/worker already consume.

## How it works

`docker/docker-compose.yml` passes `DATABASE_URI: ${IAP_MCP_IAP_DB_URI:-${IAP_DATABASE_URI}}`.
Compose interpolates from the environment that `scripts/start-platform.sh` sources
(`.env.local` + `~/bin/.iap.env.local`); the value never lands in a file under `docker/`.

## Production posture

Replace the env fallback with a secret-store agent (OpenBao `SecretStore` port already
exists in-platform): render `DATABASE_URI` into the MCP container's environment via
Docker secrets (`/run/secrets/...` mounted read-only) or an entrypoint that fetches
the URI and `exec`s the server. The crystaldba container itself needs no changes —
it already reads a single `DATABASE_URI` at startup.

## Adding another database

1. Add a compose service `postgres-mcp-<name>` (image pinned, `--access-mode=restricted`,
   `--transport=sse`, internal network only, `DATABASE_URI: ${IAP_MCP_<NAME>_DB_URI}`).
2. Export `IAP_MCP_<NAME>_DB_URI` from the secret store (never commit it).
3. Add an entry to `config/postgres-mcp-servers.yaml` with `url: http://postgres-mcp-<name>:8000/sse`
   and the tenant allowlist.
4. Restart the stack; verify `postgres-mcp-<name>` registered in the API log.
