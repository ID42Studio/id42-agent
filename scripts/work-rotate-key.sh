#!/usr/bin/env bash
# Retire the current ActorAuthority signing key.
# Prints the new key id only. Key bytes and actor credentials are not printed.
set -euo pipefail

# shellcheck source=scripts/lib/common.sh
source "$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

ROOT=$(id42_root)
ENV_FILE=${ID42_ENV_FILE:-$ROOT/.env}

if [[ ! -f "$ENV_FILE" ]]; then
  id42_die ".env is missing ($ENV_FILE). Copy .env.example and run make init."
fi

if [[ $# -ne 0 ]]; then
  id42_die "usage: work-rotate-key.sh"
fi

id42_require_cmd docker

# exec cannot run a shell function. id42_gateway_exec execs docker compose.
# The heredoc is this process's stdin and becomes the container's python stdin.
id42_gateway_exec "$ENV_FILE" "$ROOT" /opt/id42-work/venv/bin/python - <<'PY'
import sys

from id42_agent.mcp_host import rotate_authority_key

try:
    key_id = rotate_authority_key()
except RuntimeError as exc:
    print(f"id42-agent: {exc}", file=sys.stderr)
    raise SystemExit(1) from None
print(f"signing_key_id={key_id}")
PY
