#!/usr/bin/env bash
# Shared helpers for id42-agent scripts. Source this file; do not execute it.

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  echo "id42-agent: source scripts/lib/common.sh; do not execute it" >&2
  exit 1
fi

id42_root() {
  local here
  here=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
  CDPATH='' cd -- "$here/../.." && pwd
}

id42_die() {
  echo "id42-agent: $*" >&2
  exit 1
}

id42_require_cmd() {
  local cmd
  for cmd in "$@"; do
    command -v "$cmd" >/dev/null 2>&1 || id42_die "required command not found: $cmd"
  done
}

id42_compose() {
  local env_file=$1
  shift
  docker compose --env-file "$env_file" "$@"
}

# Keep in sync with docker/id42_agent/mcp_host.py DEFAULT_AUTHORITY_KEY.
ID42_DEFAULT_AUTHORITY_KEY=/var/lib/id42-work/authority.key

# Print the authority key path from an env file. Empty uses the volume default.
# The path is not key material. Never print the file itself.
id42_authority_key_path() {
  local env_file=$1
  python3 - "$env_file" "$ID42_DEFAULT_AUTHORITY_KEY" <<'PY'
import sys

env_file, default = sys.argv[1], sys.argv[2]
found = ""
with open(env_file, encoding="utf-8") as handle:
    for raw in handle:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip() != "ID42_WORK_AUTHORITY_KEY_PATH":
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        found = value.strip()
chosen = found or default
if any(char.isspace() for char in chosen) or not chosen.startswith("/") or chosen.endswith("/"):
    sys.exit("ID42_WORK_AUTHORITY_KEY_PATH must be an absolute file path")
parts = [part for part in chosen.split("/") if part]
if not parts or ".." in parts or parts[0] in {"home", "root"}:
    sys.exit("ID42_WORK_AUTHORITY_KEY_PATH must not live under /home or /root")
print(chosen)
PY
}

# Prepare the store, then replace this process with a command in the gateway.
# The authority key path is passed in the environment and is not printed.
id42_gateway_exec() {
  local env_file=$1
  local root=$2
  local entrypoint=$3
  shift 3
  local key_path
  key_path=$(id42_authority_key_path "$env_file")
  # /dev/null so a caller heredoc stays available for the exec below.
  id42_compose "$env_file" -f "$root/docker-compose.yml" run --rm --no-deps --user 0 \
    --entrypoint /bin/sh nanobot-gateway \
    -c 'chmod 700 /var/lib/id42-work 2>/dev/null || true; chown nanobot:nanobot /var/lib/id42-work' \
    </dev/null
  exec docker compose --env-file "$env_file" -f "$root/docker-compose.yml" run --rm --no-deps -T --user 1000 \
    --entrypoint "$entrypoint" \
    -e "PYTHONPATH=/opt/id42-agent" \
    -e "ID42_WORK_AUTHORITY_KEY_PATH=${key_path}" \
    nanobot-gateway "$@"
}
