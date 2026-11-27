#!/usr/bin/env bash
# Decide a pending approval for the default id42-work factory.
# This is host code. It is not an MCP tool and the model cannot call it.
set -euo pipefail

# shellcheck source=scripts/lib/common.sh
source "$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

ROOT=$(id42_root)
ENV_FILE=${ID42_ENV_FILE:-$ROOT/.env}

if [[ ! -f "$ENV_FILE" ]]; then
  id42_die ".env is missing ($ENV_FILE). Copy .env.example and run make init."
fi

id42_require_cmd docker

id42_compose "$ENV_FILE" -f "$ROOT/docker-compose.yml" run --rm --no-deps --user 0 \
  --entrypoint /bin/sh nanobot-gateway \
  -c 'chmod 700 /var/lib/id42-work 2>/dev/null || true; chown nanobot:nanobot /var/lib/id42-work'

exec id42_compose "$ENV_FILE" -f "$ROOT/docker-compose.yml" run --rm --no-deps -T --user 1000 \
  --entrypoint /opt/id42-work/venv/bin/python \
  nanobot-gateway -m id42_agent.approve "$@"
