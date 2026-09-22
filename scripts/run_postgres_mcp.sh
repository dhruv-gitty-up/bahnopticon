#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"

# shellcheck source=mcp_env.sh
source "$SCRIPT_DIR/mcp_env.sh"
load_mcp_env "$PROJECT_ROOT/.env"

if [[ -z "${DATABASE_URL:-}" ]] || is_placeholder_database_url; then
  echo "DATABASE_URL is missing or still a placeholder; copy .env.example to .env and configure PostGIS." >&2
  exit 64
fi

export npm_config_cache="${npm_config_cache:-$PROJECT_ROOT/.mcp-cache/npm}"
exec npx -y @modelcontextprotocol/server-postgres "$DATABASE_URL"
