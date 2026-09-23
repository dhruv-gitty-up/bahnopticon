#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# shellcheck source=mcp_env.sh
source "$SCRIPT_DIR/mcp_env.sh"
load_mcp_env "$PROJECT_ROOT/.env"
export npm_config_cache="${npm_config_cache:-$PROJECT_ROOT/.mcp-cache/npm}"
mkdir -p "$npm_config_cache"

failures=0
check_command() {
  local command_name="$1"
  local purpose="$2"
  if command -v "$command_name" >/dev/null 2>&1; then
    printf '[OK] %-20s %s\n' "$command_name" "$purpose"
    return 0
  fi
  printf '[FAIL] %-18s %s\n' "$command_name" "$purpose" >&2
  failures=$((failures + 1))
  return 1
}

printf 'BahnOpticon MCP setup check\n'
check_command node 'Node.js runtime found' || true
check_command npx 'NPX launcher found' || true
check_command codex 'Codex CLI found' || true
check_command python3 'Python available for config validation' || true
check_command uvx 'Required by the official fetch MCP server' || true
check_command flyctl 'Fly.io CLI and native MCP server' || true

if command -v node >/dev/null 2>&1; then
  printf '     Node %s; NPX %s\n' "$(node --version)" "$(npx --version 2>/dev/null || echo unavailable)"
fi

if command -v python3 >/dev/null 2>&1; then
  if python3 -c 'import json,tomllib,pathlib; tomllib.loads(pathlib.Path(".codex/config.toml").read_text()); json.loads(pathlib.Path(".codex/mcp.json").read_text())'; then
    echo '[OK] MCP configuration files parse successfully'
  else
    echo '[FAIL] MCP configuration validation failed' >&2
    failures=$((failures + 1))
  fi
fi

if command -v node >/dev/null 2>&1 && command -v npx >/dev/null 2>&1; then
  node scripts/mcp_smoke_test.mjs sequential-thinking \
    npx -y @modelcontextprotocol/server-sequential-thinking || failures=$((failures + 1))
else
  echo '[SKIP] sequential-thinking handshake (Node/NPX unavailable)'
fi

if [[ -z "${DATABASE_URL:-}" ]] || is_placeholder_database_url; then
  echo '[FAIL] postgres: set a real DATABASE_URL in .env to test PostGIS connectivity' >&2
  failures=$((failures + 1))
elif command -v node >/dev/null 2>&1 && command -v npx >/dev/null 2>&1; then
  MCP_SMOKE_TOOL=query \
  MCP_SMOKE_ARGS='{"sql":"SELECT current_database() AS database, PostGIS_Version() AS postgis_version"}' \
    node scripts/mcp_smoke_test.mjs postgres bash scripts/run_postgres_mcp.sh \
    || failures=$((failures + 1))
fi

if command -v node >/dev/null 2>&1 && command -v uvx >/dev/null 2>&1; then
  node scripts/mcp_smoke_test.mjs fetch uvx --with 'mcp<2' mcp-server-fetch \
    || failures=$((failures + 1))
else
  echo '[SKIP] fetch handshake (install uv/uvx first)'
fi

if command -v node >/dev/null 2>&1 && command -v flyctl >/dev/null 2>&1; then
  node scripts/mcp_smoke_test.mjs fly flyctl mcp server \
    || failures=$((failures + 1))
else
  echo '[SKIP] fly handshake (install flyctl first)'
fi

if command -v codex >/dev/null 2>&1; then
  echo
  echo 'Codex MCP registry:'
  codex mcp list || failures=$((failures + 1))
fi

if (( failures > 0 )); then
  printf '\nMCP setup has %d unresolved check(s). See docs/MCP.md.\n' "$failures" >&2
  exit 1
fi

echo
echo 'All MCP servers passed their checks. Start a new Codex session or restart the IDE extension to load them.'
