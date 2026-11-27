#!/usr/bin/env bash
# Decide a pending approval for the default id42-work factory.
# Runs `id42-work approvals` inside the gateway. Not an MCP tool.
# `approve` resumes as the host actor. `reject` does not.
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

id42_compose "$ENV_FILE" -f "$ROOT/docker-compose.yml" run --rm --no-deps --user 0 \
  --entrypoint /bin/sh nanobot-gateway \
  -c 'chmod 700 /var/lib/id42-work 2>/dev/null || true; chown nanobot:nanobot /var/lib/id42-work'

# exec cannot run a shell function. id42_compose is one, so the compose
# invocation is written out here and replaces this process.
exec docker compose --env-file "$ENV_FILE" -f "$ROOT/docker-compose.yml" run --rm --no-deps -T --user 1000 \
  --entrypoint /usr/local/bin/id42-work \
  -e "PYTHONPATH=/opt/id42-agent" \
  nanobot-gateway "$@"
