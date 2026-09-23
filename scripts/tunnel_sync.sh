#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
BACKEND_DIR="$PROJECT_ROOT/backend"
FRONTEND_DIR="$PROJECT_ROOT/frontend"

TMP_ROOT="${TMPDIR:-/tmp}"
TMP_ROOT="${TMP_ROOT%/}"
BACKEND_LOG="$(mktemp "$TMP_ROOT/bahnopticon-uvicorn.XXXXXX")"
TUNNEL_LOG="$(mktemp "$TMP_ROOT/bahnopticon-cloudflared.XXXXXX")"
DEPLOY_LOG="$(mktemp "$TMP_ROOT/bahnopticon-vercel.XXXXXX")"
NPM_CACHE="${npm_config_cache:-$TMP_ROOT/bahnopticon-npm-cache}"
BACKEND_PID=""
TUNNEL_PID=""
COMPLETED=false

cleanup_on_error() {
  local status=$?
  trap - EXIT INT TERM
  if [[ "$COMPLETED" != true ]]; then
    [[ -z "$TUNNEL_PID" ]] || kill -TERM "$TUNNEL_PID" 2>/dev/null || true
    [[ -z "$BACKEND_PID" ]] || kill -TERM "$BACKEND_PID" 2>/dev/null || true
    printf '\nTunnel synchronization failed.\n' >&2
    printf 'Uvicorn log: %s\nCloudflared log: %s\nVercel log: %s\n' \
      "$BACKEND_LOG" "$TUNNEL_LOG" "$DEPLOY_LOG" >&2
  fi
  exit "$status"
}
trap cleanup_on_error EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    printf 'Required command is unavailable: %s\n' "$1" >&2
    exit 127
  }
}

matching_pids() {
  local mode="$1"
  local pattern="$2"
  pgrep "$mode" "$pattern" 2>/dev/null || true
}

stop_existing() {
  local label="$1"
  local mode="$2"
  local pattern="$3"
  local pids remaining attempt

  pids="$(matching_pids "$mode" "$pattern")"
  [[ -n "$pids" ]] || return 0
  printf 'Stopping existing %s process(es): %s\n' "$label" "$(printf '%s' "$pids" | tr '\n' ' ')"
  # Word splitting is intentional: pgrep emits one numeric PID per line.
  kill -TERM $pids 2>/dev/null || true
  for attempt in 1 2 3 4 5; do
    remaining="$(matching_pids "$mode" "$pattern")"
    [[ -z "$remaining" ]] && return 0
    sleep 1
  done
  printf 'Force-stopping unresponsive %s process(es).\n' "$label" >&2
  kill -KILL $remaining 2>/dev/null || true
}

require_command cloudflared
require_command curl
require_command grep
require_command npx
require_command pgrep

PYTHON_BIN="$BACKEND_DIR/venv/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python"
fi
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo 'No project Python virtual environment was found.' >&2
  exit 1
fi
if [[ ! -f "$FRONTEND_DIR/.vercel/project.json" ]]; then
  echo 'The frontend is not linked to Vercel. Run `vercel link` in frontend first.' >&2
  exit 1
fi

# Load only the database variables recognized by the existing safe env parser.
# shellcheck source=mcp_env.sh
source "$SCRIPT_DIR/mcp_env.sh"
load_mcp_env "$PROJECT_ROOT/.env"
load_mcp_env "$PROJECT_ROOT/.env.local"
if [[ -z "${DATABASE_URL:-}" ]] || is_placeholder_database_url; then
  echo 'DATABASE_URL is required in .env, .env.local, or the current environment.' >&2
  exit 1
fi

mkdir -p "$NPM_CACHE"
export npm_config_cache="$NPM_CACHE"
VERCEL=(npx --yes vercel)

# Authenticate before replacing working local processes.
(cd "$FRONTEND_DIR" && "${VERCEL[@]}" whoami >/dev/null)

stop_existing uvicorn -f '[u]vicorn.*main:app'
stop_existing cloudflared -x cloudflared

printf 'Starting FastAPI on http://127.0.0.1:8000 ...\n'
(
  cd "$BACKEND_DIR"
  nohup "$PYTHON_BIN" -m uvicorn main:app \
    --host 127.0.0.1 --port 8000 --timeout-graceful-shutdown 5 \
    >"$BACKEND_LOG" 2>&1 &
  printf '%s\n' "$!"
) >"$TMP_ROOT/bahnopticon-uvicorn.pid"
BACKEND_PID="$(cat "$TMP_ROOT/bahnopticon-uvicorn.pid")"

backend_ready=false
for _ in $(seq 1 60); do
  if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
    tail -n 50 "$BACKEND_LOG" >&2
    echo 'Uvicorn exited before becoming healthy.' >&2
    exit 1
  fi
  if curl --fail --silent --show-error --max-time 2 \
    http://127.0.0.1:8000/health >/dev/null 2>&1; then
    backend_ready=true
    break
  fi
  sleep 1
done
if [[ "$backend_ready" != true ]]; then
  tail -n 50 "$BACKEND_LOG" >&2
  echo 'Uvicorn did not become healthy within 60 seconds.' >&2
  exit 1
fi

printf 'Starting Cloudflare Quick Tunnel ...\n'
nohup cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8000 \
  >"$TUNNEL_LOG" 2>&1 &
TUNNEL_PID="$!"
printf '%s\n' "$TUNNEL_PID" >"$TMP_ROOT/bahnopticon-cloudflared.pid"

TUNNEL_URL=""
for _ in $(seq 1 60); do
  if ! kill -0 "$TUNNEL_PID" 2>/dev/null; then
    tail -n 50 "$TUNNEL_LOG" >&2
    echo 'Cloudflared exited before publishing a tunnel URL.' >&2
    exit 1
  fi
  TUNNEL_URL="$(grep -Eo 'https://[[:alnum:]-]+\.trycloudflare\.com' "$TUNNEL_LOG" | head -n 1 || true)"
  [[ -z "$TUNNEL_URL" ]] || break
  sleep 1
done
if [[ -z "$TUNNEL_URL" ]]; then
  tail -n 50 "$TUNNEL_LOG" >&2
  echo 'Cloudflared did not publish a tunnel URL within 60 seconds.' >&2
  exit 1
fi

printf 'Synchronizing VITE_API_URL across Vercel environments ...\n'
cd "$FRONTEND_DIR"
# With no environment argument, current Vercel CLI versions remove the variable
# from preview, production, and development in one operation.
"${VERCEL[@]}" env rm VITE_API_URL -y >/dev/null 2>&1 || true
for environment in preview production development; do
  printf '%s' "$TUNNEL_URL" | \
    "${VERCEL[@]}" env add VITE_API_URL "$environment"
done

printf 'Triggering a fresh Vercel Preview deployment ...\n'
"${VERCEL[@]}" --yes 2>&1 | tee "$DEPLOY_LOG"
VERCEL_URL="$(grep -Eo 'https://[[:alnum:].-]+\.vercel\.app' "$DEPLOY_LOG" | tail -n 1 || true)"
if [[ -z "$VERCEL_URL" ]]; then
  echo 'Vercel completed without returning a Preview URL.' >&2
  exit 1
fi

COMPLETED=true
trap - EXIT INT TERM
GREEN=$'\033[1;32m'
RESET=$'\033[0m'
printf '\n%sCloudflare Tunnel: %s%s\n' "$GREEN" "$TUNNEL_URL" "$RESET"
printf '%sVercel Preview:   %s%s\n' "$GREEN" "$VERCEL_URL" "$RESET"
printf 'Background logs: %s and %s\n' "$BACKEND_LOG" "$TUNNEL_LOG"
