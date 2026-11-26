#!/usr/bin/env bash
# Host-side config checks, then a dummy gateway that must answer /health.
# Does not call a real Ollama or Telegram account.
set -euo pipefail

# shellcheck source=scripts/lib/common.sh
source "$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

ROOT=$(id42_root)
CONFIG_PY=$ROOT/scripts/lib/config.py
PROJECT=${COMPOSE_PROJECT_NAME:-id42smoke$$}
WORKDIR=$(mktemp -d)
ENV_FILE=$WORKDIR/smoke.env
LOG=$WORKDIR/gateway.log

cleanup() {
  if [[ -f "$ENV_FILE" ]]; then
    ID42_ENV_FILE=$ENV_FILE COMPOSE_PROJECT_NAME=$PROJECT \
      docker compose --env-file "$ENV_FILE" -f "$ROOT/docker-compose.yml" down -v --remove-orphans \
      >/dev/null 2>&1 || true
  fi
  rm -rf "$WORKDIR"
}
trap cleanup EXIT

id42_require_cmd python3 docker
docker info >/dev/null 2>&1 || id42_die "docker daemon is not reachable"

echo "== host config checks =="

expect_fail() {
  local label=$1
  shift
  if "$@" >"$WORKDIR/out" 2>"$WORKDIR/err"; then
    id42_die "expected failure: $label"
  fi
echo "OK rejects: $label"
}

cat >"$WORKDIR/no-model.env" <<'EOF'
TZ=America/Sao_Paulo
OLLAMA_BASE_URL=http://host.docker.internal:11434/v1
OLLAMA_MODEL=
TELEGRAM_ENABLED=false
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_USERS=
NANOBOT_WEB_TOKEN=smoke-web-token-do-not-leak
EOF
expect_fail "missing model" python3 "$CONFIG_PY" render --env-file "$WORKDIR/no-model.env" --output "$WORKDIR/no-model.json"

cat >"$WORKDIR/bad-url.env" <<'EOF'
TZ=America/Sao_Paulo
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=smoke-model
TELEGRAM_ENABLED=false
NANOBOT_WEB_TOKEN=smoke-web-token-do-not-leak
EOF
expect_fail "url without /v1" python3 "$CONFIG_PY" render --env-file "$WORKDIR/bad-url.env" --output "$WORKDIR/bad-url.json"

cat >"$WORKDIR/telegram.env" <<'EOF'
TZ=America/Sao_Paulo
OLLAMA_BASE_URL=http://host.docker.internal:9/v1
OLLAMA_MODEL=smoke-model
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_USERS=123
NANOBOT_WEB_TOKEN=smoke-web-token-do-not-leak
EOF
expect_fail "telegram without token" python3 "$CONFIG_PY" render --env-file "$WORKDIR/telegram.env" --output "$WORKDIR/telegram.json"

cat >"$WORKDIR/star.env" <<'EOF'
TZ=America/Sao_Paulo
OLLAMA_BASE_URL=http://host.docker.internal:9/v1
OLLAMA_MODEL=smoke-model
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=123456:smoke
TELEGRAM_ALLOWED_USERS=*
NANOBOT_WEB_TOKEN=smoke-web-token-do-not-leak
EOF
expect_fail "telegram star allowlist" python3 "$CONFIG_PY" render --env-file "$WORKDIR/star.env" --output "$WORKDIR/star.json"

cat >"$ENV_FILE" <<'EOF'
TZ=America/Sao_Paulo
OLLAMA_BASE_URL=http://host.docker.internal:9/v1
OLLAMA_MODEL=smoke-model
TELEGRAM_ENABLED=false
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_USERS=
NANOBOT_WEB_TOKEN=smoke-web-token-do-not-leak
EOF
chmod 600 "$ENV_FILE"

python3 "$CONFIG_PY" render --env-file "$ENV_FILE" --output "$WORKDIR/rendered.json" \
  >"$WORKDIR/render-stdout"
if grep -q "smoke-web-token-do-not-leak" "$WORKDIR/render-stdout"; then
  id42_die "render printed the web token"
fi
python3 - "$WORKDIR/rendered.json" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1]))
assert doc["tools"]["restrictToWorkspace"] is True
assert doc["tools"]["exec"]["enable"] is False
assert doc["providers"]["ollama"]["apiBase"].endswith("/v1")
assert doc["modelPresets"]["primary"]["model"] == "smoke-model"
assert doc["channels"]["websocket"]["tokenIssueSecret"] == "smoke-web-token-do-not-leak"
assert "telegram" not in doc["channels"]
print("rendered config matches the phase 1 contract")
PY

echo "== compose config =="
COMPOSE_PROJECT_NAME=$PROJECT docker compose --env-file "$ENV_FILE" -f "$ROOT/docker-compose.yml" config >/dev/null

echo "== bootstrap into a scratch project =="
bootstrap_log=$WORKDIR/bootstrap.log
ID42_ENV_FILE=$ENV_FILE COMPOSE_PROJECT_NAME=$PROJECT \
  "$ROOT/scripts/bootstrap.sh" >"$bootstrap_log"
if grep -q "smoke-web-token-do-not-leak" "$bootstrap_log"; then
  id42_die "bootstrap printed the web token"
fi
grep -q "Initialized config.json" "$bootstrap_log"

ID42_ENV_FILE=$ENV_FILE COMPOSE_PROJECT_NAME=$PROJECT \
  docker compose --env-file "$ENV_FILE" -f "$ROOT/docker-compose.yml" run --rm --no-deps --user 1000 \
  --entrypoint /app/.venv/bin/python \
  -v "$CONFIG_PY:/tmp/id42_config.py:ro" \
  nanobot-gateway /tmp/id42_config.py assert-volume | tee "$WORKDIR/assert1"
grep -q "config-ok" "$WORKDIR/assert1"

ID42_ENV_FILE=$ENV_FILE COMPOSE_PROJECT_NAME=$PROJECT \
  "$ROOT/scripts/bootstrap.sh" >"$WORKDIR/bootstrap2.log"
grep -q "leaving WebUI edits unchanged" "$WORKDIR/bootstrap2.log"
ID42_ENV_FILE=$ENV_FILE COMPOSE_PROJECT_NAME=$PROJECT \
  docker compose --env-file "$ENV_FILE" -f "$ROOT/docker-compose.yml" run --rm --no-deps --user 1000 \
  --entrypoint /app/.venv/bin/python \
  -v "$CONFIG_PY:/tmp/id42_config.py:ro" \
  nanobot-gateway /tmp/id42_config.py assert-volume | tee "$WORKDIR/assert2"
digest1=$(awk -F= '/token_sha256=/{print $2}' "$WORKDIR/assert1")
digest2=$(awk -F= '/token_sha256=/{print $2}' "$WORKDIR/assert2")
[[ -n "$digest1" && "$digest1" == "$digest2" ]] || id42_die "second bootstrap changed the web token"

echo "== start gateway =="
ID42_ENV_FILE=$ENV_FILE COMPOSE_PROJECT_NAME=$PROJECT \
  docker compose --env-file "$ENV_FILE" -f "$ROOT/docker-compose.yml" up -d nanobot-gateway

ready=0
for _ in $(seq 1 60); do
  code=$(curl --silent --output "$WORKDIR/health.json" --write-out "%{http_code}" --max-time 5 http://127.0.0.1:18790/health || true)
  if [[ "$code" == "200" ]] && python3 - "$WORKDIR/health.json" <<'PY'
import json, sys
payload = json.load(open(sys.argv[1]))
raise SystemExit(0 if payload.get("ready") is True and payload.get("status") == "ok" else 1)
PY
  then
    ready=1
    break
  fi
  sleep 5
done

if [[ "$ready" != "1" ]]; then
  ID42_ENV_FILE=$ENV_FILE COMPOSE_PROJECT_NAME=$PROJECT \
    docker compose --env-file "$ENV_FILE" -f "$ROOT/docker-compose.yml" logs >"$LOG" || true
  tail -n 80 "$LOG" >&2 || true
  id42_die "gateway did not become ready on /health"
fi
echo "OK health endpoint is ready"

container=$(ID42_ENV_FILE=$ENV_FILE COMPOSE_PROJECT_NAME=$PROJECT \
  docker compose --env-file "$ENV_FILE" -f "$ROOT/docker-compose.yml" ps -q nanobot-gateway)
[[ -n "$container" ]] || id42_die "gateway container id is empty"

python3 - "$container" <<'PY'
import json, subprocess, sys
info = json.loads(subprocess.check_output(["docker", "inspect", sys.argv[1]], text=True))[0]
bindings = info["HostConfig"].get("PortBindings") or {}
for port in ("8765/tcp", "18790/tcp"):
    items = bindings.get(port) or []
    if not items:
        raise SystemExit(f"missing binding for {port}")
    for item in items:
        ip = item.get("HostIp") or ""
        if ip not in {"127.0.0.1", "::1"}:
            raise SystemExit(f"{port} bound to {ip or 'all interfaces'}")
mounts = info.get("Mounts") or []
for mount in mounts:
    blob = f"{mount.get('Source', '')} {mount.get('Destination', '')}"
    if "docker.sock" in blob:
        raise SystemExit("docker socket is mounted")
def _caps(items: list[str]) -> set[str]:
    names = set()
    for item in items:
        name = item.upper()
        if name.startswith("CAP_"):
            name = name[4:]
        names.add(name)
    return names

added = _caps(info["HostConfig"].get("CapAdd") or [])
dropped = _caps(info["HostConfig"].get("CapDrop") or [])
if "ALL" not in dropped:
    raise SystemExit(f"cap drop is {sorted(dropped)}")
extra = added - {"CHOWN", "SETGID", "SETUID"}
if extra:
    raise SystemExit(f"unexpected caps {sorted(extra)}")
security = " ".join(info["HostConfig"].get("SecurityOpt") or [])
if "no-new-privileges" not in security:
    raise SystemExit("no-new-privileges missing")
memory = info["HostConfig"].get("Memory") or 0
nano = info["HostConfig"].get("NanoCpus") or 0
if memory <= 0 or nano <= 0:
    raise SystemExit(f"resource limits were not applied (memory={memory}, nano={nano})")
print(f"runtime baseline ok memory={memory} nano_cpus={nano}")
PY

uid=$(ID42_ENV_FILE=$ENV_FILE COMPOSE_PROJECT_NAME=$PROJECT \
  docker compose --env-file "$ENV_FILE" -f "$ROOT/docker-compose.yml" exec -T nanobot-gateway \
  /bin/sh -c 'awk "/^Uid:/{print \$2}" /proc/1/status')
if [[ "$uid" != "1000" ]]; then
  id42_die "pid 1 is uid ${uid}, expected 1000 (nanobot)"
fi
echo "OK gateway process runs as uid 1000"

healthy=0
for _ in $(seq 1 24); do
  status=$(docker inspect --format '{{.State.Health.Status}}' "$container" 2>/dev/null || true)
  if [[ "$status" == "healthy" ]]; then
    healthy=1
    break
  fi
  sleep 5
done
if [[ "$healthy" != "1" ]]; then
  id42_die "compose healthcheck did not report healthy (last status: ${status:-unknown})"
fi
echo "OK compose healthcheck is healthy"
echo "smoke test passed"
