#!/usr/bin/env bash

# Read only the MCP variables we recognize. This deliberately does not source
# .env, so shell syntax or command substitutions in that file are never run.
load_mcp_env() {
  local env_file="${1:?env file is required}"
  local raw line value

  [[ -f "$env_file" ]] || return 0
  while IFS= read -r raw || [[ -n "$raw" ]]; do
    line="${raw%$'\r'}"
    [[ "$line" =~ ^[[:space:]]*$ ]] && continue
    [[ "$line" =~ ^[[:space:]]*# ]] && continue

    if [[ "$line" =~ ^[[:space:]]*DATABASE_URL[[:space:]]*=(.*)$ ]]; then
      value="${BASH_REMATCH[1]}"
      value="${value#"${value%%[![:space:]]*}"}"
      value="${value%"${value##*[![:space:]]}"}"
      if [[ ${#value} -ge 2 ]]; then
        if [[ "${value:0:1}" == '"' && "${value: -1}" == '"' ]]; then
          value="${value:1:${#value}-2}"
        elif [[ "${value:0:1}" == "'" && "${value: -1}" == "'" ]]; then
          value="${value:1:${#value}-2}"
        fi
      fi
      export DATABASE_URL="$value"
    fi
  done < "$env_file"
}

is_placeholder_database_url() {
  [[ "${DATABASE_URL:-}" == *"change-me"* || "${DATABASE_URL:-}" == *"<"* ]]
}
