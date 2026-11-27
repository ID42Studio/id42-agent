#!/usr/bin/env bash
# Validate .env, generate a WebUI token if needed, and on the first run only
# write nanobot config.json plus the workspace seed into the data volume.
set -euo pipefail

# shellcheck source=scripts/lib/common.sh
source "$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

ROOT=$(id42_root)
ENV_FILE=${ID42_ENV_FILE:-$ROOT/.env}
CONFIG_PY=$ROOT/scripts/lib/config.py

id42_require_cmd python3 docker

if [[ ! -f "$ENV_FILE" ]]; then
  cp "$ROOT/.env.example" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  id42_die "created $ENV_FILE from .env.example. Set OLLAMA_MODEL, then run make init again."
fi

chmod 600 "$ENV_FILE"
python3 "$CONFIG_PY" ensure-token --env-file "$ENV_FILE"

rendered=$(mktemp)
cleanup() {
  rm -f "$rendered"
}
trap cleanup EXIT

python3 "$CONFIG_PY" render --env-file "$ENV_FILE" --output "$rendered"
python3 "$CONFIG_PY" assert-mcp --env-file "$ENV_FILE" --config "$rendered"

echo "Building the pinned nanobot image and installing id42-work (first build fetches upstream and can take several minutes)..."
id42_compose "$ENV_FILE" -f "$ROOT/docker-compose.yml" build nanobot-upstream nanobot-gateway

# The image creates /home/nanobot/.nanobot as uid 1000. With cap_drop ALL, root
# cannot write that directory (no CAP_DAC_OVERRIDE). chown of the mount point
# uses CAP_CHOWN, which the upstream entrypoint also needs, then uid 1000 writes
# the files. Do not recurse: after the gateway runs, sessions/ and updates/ are
# mode 700 and owned by uid 1000, and root cannot traverse them.
state=$(
  id42_compose "$ENV_FILE" -f "$ROOT/docker-compose.yml" run --rm --no-deps --user 0 \
    --entrypoint /bin/sh nanobot-gateway \
    -c 'set -eu
chown nanobot:nanobot /home/nanobot/.nanobot
if [ -f /home/nanobot/.nanobot/config.json ]; then echo id42-state=present; else echo id42-state=missing; fi' \
    | sed -n 's/^id42-state=//p' \
    | tail -n 1
)

if [[ "$state" == "present" ]]; then
  echo "config.json already exists in the data volume; leaving WebUI edits unchanged."
  exit 0
fi
if [[ "$state" != "missing" ]]; then
  id42_die "could not tell whether config.json exists (got: ${state})"
fi

id42_compose "$ENV_FILE" -f "$ROOT/docker-compose.yml" run --rm --no-deps -T --user 1000 \
  --entrypoint /bin/sh \
  -v "$ROOT/workspace:/id42-workspace:ro" \
  -v "$ROOT/docker/seed-volume.sh:/id42/seed-volume.sh:ro" \
  nanobot-gateway \
  /id42/seed-volume.sh <"$rendered"

if ! id42_compose "$ENV_FILE" -f "$ROOT/docker-compose.yml" run --rm --no-deps --user 1000 \
  --entrypoint /app/.venv/bin/python \
  -v "$CONFIG_PY:/tmp/id42_config.py:ro" \
  nanobot-gateway /tmp/id42_config.py validate-volume; then
  id42_compose "$ENV_FILE" -f "$ROOT/docker-compose.yml" run --rm --no-deps --user 1000 \
    --entrypoint /bin/sh nanobot-gateway \
    -c 'rm -f /home/nanobot/.nanobot/config.json'
  id42_die "nanobot rejected the rendered config.json; removed it so the next init can retry"
fi

echo "Initialized config.json and the workspace seed in the data volume."
echo "The WebUI password is NANOBOT_WEB_TOKEN in $ENV_FILE (not printed)."
