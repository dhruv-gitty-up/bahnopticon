#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"

# Load only the deployment variables recognized by mcp_env.sh. This avoids
# executing shell expressions that may appear in a developer-owned env file.
# shellcheck source=mcp_env.sh
source "$SCRIPT_DIR/mcp_env.sh"
load_mcp_env "$PROJECT_ROOT/.env"
load_mcp_env "$PROJECT_ROOT/.env.local"

if ! command -v flyctl >/dev/null 2>&1; then
  echo 'flyctl is required: https://fly.io/docs/flyctl/install/' >&2
  exit 1
fi

database_url="${DATABASE_URL:-${SUPABASE_DB_URL:-}}"
if [[ -z "$database_url" ]]; then
  echo 'DATABASE_URL or SUPABASE_DB_URL must be set in the environment, .env, or .env.local.' >&2
  exit 1
fi

if ! flyctl auth whoami >/dev/null 2>&1; then
  echo 'Fly.io authentication is required. Run `flyctl auth login` or set FLY_API_TOKEN.' >&2
  exit 1
fi

cd "$PROJECT_ROOT"

if [[ ! -f fly.toml ]]; then
  launch_args=(launch --no-deploy --region fra --primary-region fra
    --dockerfile backend/Dockerfile --internal-port 8080 --no-db --no-redis
    --no-object-storage --no-github-workflow --ha=false)
  if [[ -n "${FLY_APP:-}" ]]; then
    launch_args+=(--name "$FLY_APP")
  else
    launch_args+=(--generate-name)
  fi
  flyctl "${launch_args[@]}"
else
  echo 'Using existing fly.toml.'
fi

flyctl secrets set "DATABASE_URL=$database_url"
flyctl deploy --config fly.toml --dockerfile backend/Dockerfile .
