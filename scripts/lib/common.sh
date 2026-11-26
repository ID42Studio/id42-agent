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
