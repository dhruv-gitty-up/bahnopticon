# MCP setup for BahnOpticon agents

Codex reads project-scoped MCP configuration from [`.codex/config.toml`](../.codex/config.toml). The adjacent [`.codex/mcp.json`](../.codex/mcp.json) is a portable mirror for clients that use the common JSON format; Codex itself uses the TOML file. Project MCP configuration is loaded only for trusted projects.

## Setup

1. Install Node.js with `npx`, the Codex CLI, [`uv`](https://docs.astral.sh/uv/) so `uvx` is available, and [`flyctl`](https://fly.io/docs/flyctl/install/).
2. Copy `.env.example` to `.env` and replace `DATABASE_URL` with the local or remote PostGIS connection string. The `.env` file is ignored by Git. Prefer a dedicated database role with read-only access to the schemas the agents need.
3. Run:

   ```sh
   scripts/setup_mcps.sh
   ```

The script reads only `DATABASE_URL`, `SUPABASE_DB_URL`, and `FLY_APP` from `.env` without executing the file. It validates both configuration files, performs MCP initialize and `tools/list` handshakes (including Fly's native MCP server), and calls the PostgreSQL `query` tool with `PostGIS_Version()` to verify the database and extension. Start a new Codex session or restart the IDE extension after changing MCP configuration. In Codex, run `/mcp`; from a terminal, run `codex mcp list`.

NPX downloads use the ignored `.mcp-cache/npm` directory, avoiding dependence on the permissions or contents of a developer's global npm cache.

The requested npm name `@modelcontextprotocol/server-fetch` is not published. The `fetch` entry therefore uses the official Python server with `uvx --with "mcp<2" mcp-server-fetch`. Sequential Thinking and PostgreSQL use the requested `npx` packages. The configured PostgreSQL reference server exposes read-only queries.

## Agent workflows

MCP tools appear as native tools after Codex initializes the servers. Agents should inspect `/mcp` for the exact exposed tool names rather than starting these servers manually.

### Track interpolation and routing

Use `sequential-thinking` before changing `backend/engine.py` when a routing problem has multiple interacting constraints. Record the assumptions in order: coordinate reference system, snapping tolerance, candidate segment choice, graph connectivity, edge weights, path reconstruction, timing across vertices, and the straight-line fallback. Then verify the conclusion against focused tests rather than treating the reasoning trace as evidence.

Use the PostgreSQL `query` tool for read-only spatial checks when track or vehicle geometry is stored in PostGIS. Useful patterns include:

```sql
SELECT PostGIS_Version();

SELECT id,
       ST_Distance(
         geom::geography,
         ST_SetSRID(ST_Point(13.405, 52.52), 4326)::geography
       ) AS distance_m
FROM railway_edges
ORDER BY geom <-> ST_SetSRID(ST_Point(13.405, 52.52), 4326)
LIMIT 10;

SELECT ST_AsGeoJSON(ST_LineMerge(ST_Collect(geom ORDER BY path_order)))
FROM routed_edges
WHERE route_id = $1;
```

Use `EXPLAIN (ANALYZE, BUFFERS)` only against non-production data or when query execution is explicitly acceptable. Confirm geometry SRIDs before distance calculations, use `geography` for meter-based Germany-wide distances, and use GiST/SP-GiST indexes appropriate to the stored geometry.

### Database schema design

Use PostgreSQL resources and the `query` tool to inspect existing schemas, types, constraints, indexes, and PostGIS metadata before proposing migrations. Agents can draft DDL from those results, but the configured reference server is read-only; apply migrations through the repository's normal migration workflow. Recommended design questions include:

- whether railway nodes and directed edges need separate tables;
- which stable OSM/HAFAS identifiers form unique keys;
- whether geometries use `geometry(..., 4326)` or a projected SRID;
- which temporal fields distinguish observation, scheduled, expected, and ingestion time;
- which GiST, B-tree, and partial indexes match nearest-edge and active-vehicle queries.

### Live protocol documentation

Use the `fetch` tool for authoritative HAFAS, GTFS-Realtime, Overpass, PostGIS, Deck.gl, and MCP documentation URLs. Ask it for a bounded page section, cite the fetched URL in design notes, and compare the documentation with the actual installed package version before changing code. Do not send database credentials, `.env` contents, private endpoints, or internal documents to fetched URLs.

### Fly.io deployments

The `fly` MCP entry runs Fly.io's native `flyctl mcp server`. Authenticate once with `flyctl auth login`, or provide `FLY_API_TOKEN` in the environment that starts Codex. Restart Codex after changing the MCP configuration, confirm the server appears in `/mcp`, and use its Fly tools to inspect applications, deployments, logs, and status. Deployment tools can change production resources, so agents should inspect the current app and configuration before invoking them.

For the repository's standard backend deployment, agents can execute:

```sh
scripts/deploy_fly.sh
```

The script safely reads `DATABASE_URL`, `SUPABASE_DB_URL`, and optional `FLY_APP` from `.env` and `.env.local`. `DATABASE_URL` takes precedence when both database variables are set. When `fly.toml` does not exist, it runs `fly launch --no-deploy --region fra` against `backend/Dockerfile`, using `FLY_APP` as the application name when set and a generated name otherwise. It then sets the Fly secret `DATABASE_URL` and runs `fly deploy` with the repository root as the Docker build context so the image includes both the Python backend and Node HAFAS adapter. The secret value is never printed by the script. Once `fly.toml` exists, later agent runs skip launch and deploy the current checkout directly.

Before running it autonomously, an agent should verify that tests pass, `SUPABASE_DB_URL` points to the intended database, `flyctl auth whoami` identifies the intended Fly account, and the checked-in `fly.toml` targets the intended app. The agent can then run the script through its command tool without opening an interactive terminal. A first launch may still require Fly to allocate an app name if `FLY_APP` is unset, so setting `FLY_APP` is recommended for unattended runs.

## Troubleshooting

- `postgres` fails immediately: confirm `.env` exists, `DATABASE_URL` is not the example value, the database is reachable, and `CREATE EXTENSION postgis` has been applied.
- `fetch` is unavailable: install `uv`/`uvx`, then rerun `scripts/setup_mcps.sh`.
- `npx` stalls on first use: allow registry access and rerun; the first launch downloads the package.
- `fly` is unavailable: install a current `flyctl`, authenticate, rerun `scripts/setup_mcps.sh`, and restart Codex.
- `scripts/deploy_fly.sh` stops before launch: set `SUPABASE_DB_URL` and authenticate with `flyctl auth login` or `FLY_API_TOKEN`.
- Codex does not list the servers: trust the project, restart the local Codex client, and run `codex mcp list` or `/mcp`.
