#!/usr/bin/env bash
# Reconcile an uncertain capability call for the default id42-work factory.
# Runs `id42-work reconcile` inside the gateway. Not an MCP tool.
# The command seals id42-operator with only reconcile:<work_id>.
# applied and not-applied resume only when the caller passes --resume.
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
  id42_die "usage: work-reconcile.sh list|show|applied|not-applied|abandon ..."
fi

id42_require_cmd docker

command=$1
shift

case "$command" in
  list)
    if [[ $# -ne 0 ]]; then
      id42_die "list takes no arguments"
    fi
    set -- reconcile list --factory "$FACTORY"
    ;;
  show)
    if [[ $# -ne 2 ]]; then
      id42_die "usage: work-reconcile.sh show EXECUTION_ID ACTION_ID"
    fi
    set -- reconcile show "$1" "$2" --factory "$FACTORY"
    ;;
  applied)
    if [[ $# -lt 2 ]]; then
      id42_die "usage: work-reconcile.sh applied EXECUTION_ID ACTION_ID --reason TEXT --result JSON [--resume]"
    fi
    set -- reconcile applied "$@" --factory "$FACTORY" --actor "$ACTOR"
    ;;
  not-applied)
    if [[ $# -lt 2 ]]; then
      id42_die "usage: work-reconcile.sh not-applied EXECUTION_ID ACTION_ID --reason TEXT [--resume]"
    fi
    set -- reconcile not-applied "$@" --factory "$FACTORY" --actor "$ACTOR"
    ;;
  abandon)
    if [[ $# -lt 2 ]]; then
      id42_die "usage: work-reconcile.sh abandon EXECUTION_ID ACTION_ID --reason TEXT"
    fi
    set -- reconcile abandon "$@" --factory "$FACTORY" --actor "$ACTOR"
    ;;
  *)
    id42_die "unknown command: $command (expected list, show, applied, not-applied, or abandon)"
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
