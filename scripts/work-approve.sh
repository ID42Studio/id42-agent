#!/usr/bin/env bash
# Decide a pending approval for the default id42-work factory.
# Runs `id42-work approvals` inside the gateway. Not an MCP tool.
# `approve` resumes as the host actor. `reject` does not.
# The process uses the same authority key file as the MCP server.
set -euo pipefail

# shellcheck source=scripts/lib/common.sh
source "$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

ROOT=$(id42_root)
ENV_FILE=${ID42_ENV_FILE:-$ROOT/.env}
FACTORY=id42_agent.mcp_host:server
ACTOR=id42-operator

if [[ ! -f "$ENV_FILE" ]]; then
  id42_die ".env is missing ($ENV_FILE). Copy .env.example and run make init."
fi

if [[ $# -lt 1 ]]; then
  id42_die "usage: work-approve.sh list|show|approve|reject ..."
fi

id42_require_cmd docker

command=$1
shift

case "$command" in
  pending|list)
    if [[ $# -ne 0 ]]; then
      id42_die "list takes no arguments"
    fi
    set -- approvals list --factory "$FACTORY"
    ;;
  show)
    if [[ $# -ne 1 ]]; then
      id42_die "usage: work-approve.sh show APPROVAL_ID"
    fi
    set -- approvals show "$1" --factory "$FACTORY"
    ;;
  approve)
    if [[ $# -lt 1 ]]; then
      id42_die "usage: work-approve.sh approve APPROVAL_ID [--reason TEXT]"
    fi
    set -- approvals approve "$@" --factory "$FACTORY" --actor "$ACTOR" --resume
    ;;
  reject)
    if [[ $# -lt 1 ]]; then
      id42_die "usage: work-approve.sh reject APPROVAL_ID --reason TEXT"
    fi
    set -- approvals reject "$@" --factory "$FACTORY" --actor "$ACTOR"
    ;;
  *)
    id42_die "unknown command: $command (expected list, show, approve, or reject)"
    ;;
esac

# exec cannot run a shell function. id42_gateway_exec execs docker compose.
id42_gateway_exec "$ENV_FILE" "$ROOT" /usr/local/bin/id42-work "$@"
