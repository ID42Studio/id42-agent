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

echo "== security baseline rules =="
python3 - "$ROOT/scripts/lib/baseline.py" <<'PY'
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("baseline", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

cases = [
    ("/home/runner/work/id42-agent/id42-agent/docker/entrypoint.sh", "/id42/entrypoint.sh", False),
    ("/home/marcel/src/id42-agent/docker/entrypoint.sh", "/id42/entrypoint.sh", False),
    ("/workspace/docker/entrypoint.sh", "/id42/entrypoint.sh", False),
    ("/home/marcel/.nanobot", "/home/nanobot/.nanobot", True),
    ("/home/marcel", "/home/nanobot", True),
    ("/root/.ssh", "/tmp/ssh", True),
    ("/var/run/docker.sock", "/var/run/docker.sock", True),
    ("/var/lib/docker/volumes/nanobot-data/_data", "/home/nanobot/.nanobot", False),
    ("/var/lib/docker/volumes/id42-work-data/_data", "/var/lib/id42-work", False),
    ("/home/marcel/id42-work", "/var/lib/id42-work", True),
]
failed = False
for source, dest, expect in cases:
    got = bool(mod.mount_errors(source, dest))
    if got != expect:
        print(
            f"baseline mismatch {source} -> {dest}: flagged={got} expected={expect}",
            file=sys.stderr,
        )
        failed = True

compliant = {
    "HostConfig": {
        "PortBindings": {
            "8765/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8765"}],
            "18790/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18790"}],
        },
        "CapAdd": ["CAP_CHOWN", "CAP_SETGID", "CAP_SETUID"],
        "CapDrop": ["CAP_ALL"],
        "SecurityOpt": ["no-new-privileges:true"],
    },
    "Mounts": [
        {
            "Source": "/var/lib/docker/volumes/nanobot-data/_data",
            "Destination": "/home/nanobot/.nanobot",
        },
        {
            "Source": "/home/runner/work/id42-agent/id42-agent/docker/entrypoint.sh",
            "Destination": "/id42/entrypoint.sh",
        },
        {
            "Source": "/var/lib/docker/volumes/id42-work-data/_data",
            "Destination": "/var/lib/id42-work",
        },
    ],
}
if mod.security_errors(compliant):
    print("compliant container was rejected:", mod.security_errors(compliant), file=sys.stderr)
    failed = True
violating = {
    "HostConfig": dict(compliant["HostConfig"]),
    "Mounts": list(compliant["Mounts"]),
}
violating["HostConfig"]["PortBindings"] = {
    "8765/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8765"}],
    "18790/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18790"}],
}
if not mod.security_errors(violating):
    print("violating container was accepted", file=sys.stderr)
    failed = True
raise SystemExit(1 if failed else 0)
PY
echo "OK baseline accepts a checkout under /home and rejects a real home mount"

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

cat >"$WORKDIR/bad-factory.env" <<'EOF'
TZ=America/Sao_Paulo
OLLAMA_BASE_URL=http://host.docker.internal:9/v1
OLLAMA_MODEL=smoke-model
TELEGRAM_ENABLED=false
ID42_WORK_ENABLED=true
ID42_WORK_FACTORY=../secrets:server
NANOBOT_WEB_TOKEN=smoke-web-token-do-not-leak
EOF
expect_fail "factory path" python3 "$CONFIG_PY" render --env-file "$WORKDIR/bad-factory.env" --output "$WORKDIR/bad-factory.json"

cat >"$WORKDIR/bad-pythonpath.env" <<'EOF'
TZ=America/Sao_Paulo
OLLAMA_BASE_URL=http://host.docker.internal:9/v1
OLLAMA_MODEL=smoke-model
TELEGRAM_ENABLED=false
ID42_WORK_ENABLED=true
ID42_WORK_PYTHONPATH=relative/mod
NANOBOT_WEB_TOKEN=smoke-web-token-do-not-leak
EOF
expect_fail "relative pythonpath" python3 "$CONFIG_PY" render --env-file "$WORKDIR/bad-pythonpath.env" --output "$WORKDIR/bad-pythonpath.json"

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
assert "mcpServers" not in doc["tools"]
print("rendered config matches the default contract")
PY

cat >"$WORKDIR/mcp.env" <<'EOF'
TZ=America/Sao_Paulo
OLLAMA_BASE_URL=http://host.docker.internal:9/v1
OLLAMA_MODEL=smoke-model
TELEGRAM_ENABLED=false
ID42_WORK_ENABLED=true
ID42_WORK_FACTORY=
ID42_WORK_PYTHONPATH=/srv/id42-work-host
NANOBOT_WEB_TOKEN=smoke-web-token-do-not-leak
EOF
python3 "$CONFIG_PY" render --env-file "$WORKDIR/mcp.env" --output "$WORKDIR/mcp.json" \
  >"$WORKDIR/mcp-render-stdout"
if grep -q "smoke-web-token-do-not-leak" "$WORKDIR/mcp-render-stdout"; then
  id42_die "render printed the web token"
fi
python3 "$CONFIG_PY" assert-mcp --env-file "$WORKDIR/mcp.env" --config "$WORKDIR/mcp.json"
python3 - "$WORKDIR/mcp.json" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1]))
server = doc["tools"]["mcpServers"]["id42-work"]
assert server["command"] == "id42-work"
assert server["args"] == ["mcp", "serve", "--factory", "id42_agent.mcp_host:server"]
assert server["toolTimeout"] == 120
assert server["env"]["PYTHONPATH"] == "/opt/id42-agent:/srv/id42-work-host"
assert "url" not in server
assert "--http" not in server["args"]
assert "ssrfWhitelist" not in doc["tools"]
print("rendered id42-work server is stdio only")
PY

cat >"$WORKDIR/mcp-default-path.env" <<'EOF'
TZ=America/Sao_Paulo
OLLAMA_BASE_URL=http://host.docker.internal:9/v1
OLLAMA_MODEL=smoke-model
TELEGRAM_ENABLED=false
ID42_WORK_ENABLED=true
ID42_WORK_FACTORY=
ID42_WORK_PYTHONPATH=
NANOBOT_WEB_TOKEN=smoke-web-token-do-not-leak
EOF
python3 "$CONFIG_PY" render --env-file "$WORKDIR/mcp-default-path.env" \
  --output "$WORKDIR/mcp-default-path.json" >"$WORKDIR/mcp-default-path-stdout"
python3 - "$WORKDIR/mcp-default-path.json" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1]))
server = doc["tools"]["mcpServers"]["id42-work"]
assert server["env"]["PYTHONPATH"] == "/opt/id42-agent"
print("default factory PYTHONPATH is the image module path")
PY

cat >"$WORKDIR/custom-factory.env" <<'EOF'
TZ=America/Sao_Paulo
OLLAMA_BASE_URL=http://host.docker.internal:9/v1
OLLAMA_MODEL=smoke-model
TELEGRAM_ENABLED=false
ID42_WORK_ENABLED=true
ID42_WORK_FACTORY=myapp.governed:server
ID42_WORK_PYTHONPATH=
NANOBOT_WEB_TOKEN=smoke-web-token-do-not-leak
EOF
python3 "$CONFIG_PY" render --env-file "$WORKDIR/custom-factory.env" \
  --output "$WORKDIR/custom-factory.json" >"$WORKDIR/custom-factory-stdout"
python3 - "$WORKDIR/custom-factory.json" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1]))
server = doc["tools"]["mcpServers"]["id42-work"]
assert server["args"] == ["mcp", "serve", "--factory", "myapp.governed:server"]
assert server["env"]["PYTHONPATH"] == "/opt/id42-agent"
print("custom factory override is preserved")
PY

cat >"$WORKDIR/stub-factory.env" <<'EOF'
TZ=America/Sao_Paulo
OLLAMA_BASE_URL=http://host.docker.internal:9/v1
OLLAMA_MODEL=smoke-model
TELEGRAM_ENABLED=false
ID42_WORK_ENABLED=true
ID42_WORK_FACTORY=id42_agent.mcp_stub:server
ID42_WORK_PYTHONPATH=
NANOBOT_WEB_TOKEN=smoke-web-token-do-not-leak
EOF
python3 "$CONFIG_PY" render --env-file "$WORKDIR/stub-factory.env" \
  --output "$WORKDIR/stub-factory.json" >"$WORKDIR/stub-factory-stdout"
python3 - "$WORKDIR/stub-factory.json" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1]))
server = doc["tools"]["mcpServers"]["id42-work"]
assert server["args"] == ["mcp", "serve", "--factory", "id42_agent.mcp_stub:server"]
assert server["env"]["PYTHONPATH"] == "/opt/id42-agent"
print("stub factory override is preserved")
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

echo "== id42-work image =="
# The rendered file is mode 0600. GitHub runners are not uid 1000, so the
# container user could not read a 0600 bind. The temp directory is private.
chmod a+r "$WORKDIR/mcp.json"
ID42_ENV_FILE=$ENV_FILE COMPOSE_PROJECT_NAME=$PROJECT \
  docker compose --env-file "$ENV_FILE" -f "$ROOT/docker-compose.yml" run --rm --no-deps -T --user 1000 \
  --entrypoint /app/.venv/bin/python \
  -v "$CONFIG_PY:/tmp/id42_config.py:ro" \
  -v "$WORKDIR/mcp.json:/tmp/mcp-config.json:ro" \
  nanobot-gateway /tmp/id42_config.py validate-file --config /tmp/mcp-config.json \
  | tee "$WORKDIR/mcp-valid"
grep -q "config-valid" "$WORKDIR/mcp-valid"
if grep -q "smoke-web-token-do-not-leak" "$WORKDIR/mcp-valid"; then
  id42_die "nanobot validation printed the web token"
fi
ID42_ENV_FILE=$ENV_FILE COMPOSE_PROJECT_NAME=$PROJECT \
  docker compose --env-file "$ENV_FILE" -f "$ROOT/docker-compose.yml" run --rm --no-deps -T --user 1000 \
  --entrypoint /app/.venv/bin/python \
  nanobot-gateway - <"$ROOT/scripts/lib/mcp_probe.py" | tee "$WORKDIR/mcp-probe"
sed -i 's/\r$//' "$WORKDIR/mcp-probe"
grep -q "mcp-stdio-ok" "$WORKDIR/mcp-probe"
grep -qx "works=echo,note,note.list" "$WORKDIR/mcp-probe"
ID42_ENV_FILE=$ENV_FILE COMPOSE_PROJECT_NAME=$PROJECT \
  docker compose --env-file "$ENV_FILE" -f "$ROOT/docker-compose.yml" run --rm --no-deps -T --user 1000 \
  --entrypoint /app/.venv/bin/python \
  -e ID42_MCP_FACTORY=id42_agent.mcp_stub:server \
  nanobot-gateway - <"$ROOT/scripts/lib/mcp_probe.py" | tee "$WORKDIR/mcp-stub"
sed -i 's/\r$//' "$WORKDIR/mcp-stub"
grep -q "mcp-stdio-ok" "$WORKDIR/mcp-stub"
grep -qx "works=" "$WORKDIR/mcp-stub"

wait_gateway() {
  local ready=0
  local _
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
}

echo "== start gateway =="
ID42_ENV_FILE=$ENV_FILE COMPOSE_PROJECT_NAME=$PROJECT \
  docker compose --env-file "$ENV_FILE" -f "$ROOT/docker-compose.yml" up -d nanobot-gateway

wait_gateway
echo "OK health endpoint is ready"

container=$(ID42_ENV_FILE=$ENV_FILE COMPOSE_PROJECT_NAME=$PROJECT \
  docker compose --env-file "$ENV_FILE" -f "$ROOT/docker-compose.yml" ps -q nanobot-gateway)
[[ -n "$container" ]] || id42_die "gateway container id is empty"

python3 - "$container" "$ROOT/scripts/lib/baseline.py" <<'PY'
import importlib.util, json, subprocess, sys
spec = importlib.util.spec_from_file_location("baseline", sys.argv[2])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
info = json.loads(subprocess.check_output(["docker", "inspect", sys.argv[1]], text=True))[0]
errors = mod.security_errors(info)
if errors:
    raise SystemExit("\n".join(errors))
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

echo "== id42-work store survives a container restart =="
ID42_ENV_FILE=$ENV_FILE COMPOSE_PROJECT_NAME=$PROJECT \
  docker compose --env-file "$ENV_FILE" -f "$ROOT/docker-compose.yml" run --rm --no-deps -T --user 1000 \
  --entrypoint /app/.venv/bin/python \
  nanobot-gateway - persist-start <"$ROOT/scripts/lib/mcp_probe.py" | tee "$WORKDIR/persist-start.out"
echo_id=$(awk -F= '/^execution_id=/{print $2}' "$WORKDIR/persist-start.out")
note_id=$(awk -F= '/^note_execution_id=/{print $2}' "$WORKDIR/persist-start.out")
note_status=$(awk -F= '/^note_status=/{print $2}' "$WORKDIR/persist-start.out")
[[ -n "$echo_id" && -n "$note_id" && "$note_status" == "WAITING_APPROVAL" ]] \
  || id42_die "persist-start did not report an echo id and a waiting note"
ID42_ENV_FILE=$ENV_FILE COMPOSE_PROJECT_NAME=$PROJECT \
  docker compose --env-file "$ENV_FILE" -f "$ROOT/docker-compose.yml" restart nanobot-gateway
wait_gateway
ID42_ENV_FILE=$ENV_FILE COMPOSE_PROJECT_NAME=$PROJECT \
  docker compose --env-file "$ENV_FILE" -f "$ROOT/docker-compose.yml" run --rm --no-deps -T --user 1000 \
  --entrypoint /app/.venv/bin/python \
  nanobot-gateway - persist-check "$echo_id" "$note_id" <"$ROOT/scripts/lib/mcp_probe.py" \
  | tee "$WORKDIR/persist-check.out"
grep -q "persist-ok" "$WORKDIR/persist-check.out"
echo "OK id42-work executions are still listed after a gateway restart"

echo "== id42-work native approvals =="
gateway_run() {
  ID42_ENV_FILE=$ENV_FILE COMPOSE_PROJECT_NAME=$PROJECT \
    docker compose --env-file "$ENV_FILE" -f "$ROOT/docker-compose.yml" run --rm --no-deps -T --user 1000 \
    "$@"
}

probe_cmd() {
  gateway_run --entrypoint /app/.venv/bin/python nanobot-gateway - "$@" <"$ROOT/scripts/lib/mcp_probe.py"
}

approvals_cmd() {
  gateway_run --entrypoint /usr/local/bin/id42-work -e "PYTHONPATH=/opt/id42-agent" \
    nanobot-gateway approvals "$@"
}

approval_id_for() {
  python3 - "$1" "$2" <<'PY'
import sys

path, exec_id = sys.argv[1], sys.argv[2]
for raw in open(path, encoding="utf-8"):
    line = raw.strip()
    if not line.startswith("pending "):
        continue
    fields = {}
    for part in line.split()[1:]:
        key, sep, value = part.partition("=")
        if sep:
            fields[key] = value
    if fields.get("execution_id") == exec_id and fields.get("approval_id"):
        print(fields["approval_id"])
        raise SystemExit(0)
raise SystemExit(1)
PY
}

require_pending_line() {
  python3 - "$1" "$2" <<'PY'
import sys

path, exec_id = sys.argv[1], sys.argv[2]
required = (
    "gate=ACTION",
    "reviewer_capability=approve:note",
    "actor_id=id42-agent",
    "execution_status=WAITING_APPROVAL",
    "work_id=note",
)
for raw in open(path, encoding="utf-8"):
    line = raw.strip()
    if f"execution_id={exec_id}" not in line.split():
        continue
    missing = [item for item in required if item not in line.split()]
    if missing:
        raise SystemExit("pending line missing " + ",".join(missing))
    raise SystemExit(0)
raise SystemExit("pending line for the execution is missing")
PY
}

probe_cmd start-note "smoke-approve-note" "smoke-approve" | tee "$WORKDIR/approve-start.out"
sed -i 's/\r$//' "$WORKDIR/approve-start.out"
approve_exec=$(awk -F= '/^execution_id=/{print $2}' "$WORKDIR/approve-start.out")
approve_status=$(awk -F= '/^status=/{print $2}' "$WORKDIR/approve-start.out")
[[ -n "$approve_exec" && "$approve_status" == "WAITING_APPROVAL" ]] \
  || id42_die "approve-path note did not wait for approval"

approvals_cmd list --factory id42_agent.mcp_host:server | tee "$WORKDIR/approve-list.out"
sed -i 's/\r$//' "$WORKDIR/approve-list.out"
approve_approval=$(approval_id_for "$WORKDIR/approve-list.out" "$approve_exec") \
  || id42_die "approvals list did not include the waiting note"
require_pending_line "$WORKDIR/approve-list.out" "$approve_exec"

approvals_cmd approve "$approve_approval" \
  --factory id42_agent.mcp_host:server \
  --actor id42-operator \
  --reason "smoke" \
  --resume | tee "$WORKDIR/approve-decision.out"
sed -i 's/\r$//' "$WORKDIR/approve-decision.out"
grep -q "approved approval_id=${approve_approval} " "$WORKDIR/approve-decision.out" \
  || id42_die "approve did not report the approval id"
grep -q "execution_status=COMPLETED" "$WORKDIR/approve-decision.out" \
  || id42_die "approve --resume did not report COMPLETED"
grep -q "resumed=true" "$WORKDIR/approve-decision.out" \
  || id42_die "approve --resume did not report resumed=true"
grep -q "decision_actor_id=id42-operator" "$WORKDIR/approve-decision.out" \
  || id42_die "approve did not record id42-operator"

probe_cmd execution-status "$approve_exec" | tee "$WORKDIR/approve-status.out"
sed -i 's/\r$//' "$WORKDIR/approve-status.out"
grep -qx "status=COMPLETED" "$WORKDIR/approve-status.out" \
  || id42_die "approved note is not COMPLETED in the store"
grep -qx "work_id=note" "$WORKDIR/approve-status.out"
grep -qx "appended=True" "$WORKDIR/approve-status.out" \
  || id42_die "approved note did not append"

if approvals_cmd approve "$approve_approval" \
  --factory id42_agent.mcp_host:server \
  --actor id42-operator \
  --reason "again" \
  --resume >"$WORKDIR/approve-again.out" 2>"$WORKDIR/approve-again.err"
then
  id42_die "a second approval decision was accepted"
fi
grep -q "id42-work:" "$WORKDIR/approve-again.err" \
  || {
    cat "$WORKDIR/approve-again.err" >&2 || true
    id42_die "a second approval decision failed without an id42-work error"
  }

probe_cmd execution-status "$approve_exec" | tee "$WORKDIR/approve-status-again.out"
sed -i 's/\r$//' "$WORKDIR/approve-status-again.out"
grep -qx "status=COMPLETED" "$WORKDIR/approve-status-again.out" \
  || id42_die "a refused second decision changed the completed note"

probe_cmd start-note "smoke-reject-note" "smoke-reject" | tee "$WORKDIR/reject-start.out"
sed -i 's/\r$//' "$WORKDIR/reject-start.out"
reject_exec=$(awk -F= '/^execution_id=/{print $2}' "$WORKDIR/reject-start.out")
reject_status=$(awk -F= '/^status=/{print $2}' "$WORKDIR/reject-start.out")
[[ -n "$reject_exec" && "$reject_status" == "WAITING_APPROVAL" ]] \
  || id42_die "reject-path note did not wait for approval"

approvals_cmd list --factory id42_agent.mcp_host:server | tee "$WORKDIR/reject-list.out"
sed -i 's/\r$//' "$WORKDIR/reject-list.out"
reject_approval=$(approval_id_for "$WORKDIR/reject-list.out" "$reject_exec") \
  || id42_die "approvals list did not include the note to reject"
if grep -q "execution_id=${approve_exec}" "$WORKDIR/reject-list.out"; then
  id42_die "completed note is still listed as pending"
fi

approvals_cmd reject "$reject_approval" \
  --factory id42_agent.mcp_host:server \
  --actor id42-operator \
  --reason "no" | tee "$WORKDIR/reject-decision.out"
sed -i 's/\r$//' "$WORKDIR/reject-decision.out"
grep -q "rejected approval_id=${reject_approval} " "$WORKDIR/reject-decision.out" \
  || id42_die "reject did not report the approval id"
grep -q "execution_status=REJECTED" "$WORKDIR/reject-decision.out" \
  || id42_die "reject did not report REJECTED"
grep -q "resumed=false" "$WORKDIR/reject-decision.out" \
  || id42_die "reject reported a resume"

probe_cmd execution-status "$reject_exec" | tee "$WORKDIR/reject-status.out"
sed -i 's/\r$//' "$WORKDIR/reject-status.out"
grep -qx "status=REJECTED" "$WORKDIR/reject-status.out" \
  || id42_die "rejected note is not REJECTED in the store"
grep -qx "work_id=note" "$WORKDIR/reject-status.out"
if grep -qx "appended=True" "$WORKDIR/reject-status.out"; then
  id42_die "rejected note appended"
fi

ID42_ENV_FILE=$ENV_FILE COMPOSE_PROJECT_NAME=$PROJECT \
  "$ROOT/scripts/work-approve.sh" list | tee "$WORKDIR/wrapper-list.out"
sed -i 's/\r$//' "$WORKDIR/wrapper-list.out"
grep -q "execution_id=${note_id}" "$WORKDIR/wrapper-list.out" \
  || id42_die "work-approve.sh list did not show the note that is still waiting"
if grep -q "execution_id=${approve_exec}" "$WORKDIR/wrapper-list.out"; then
  id42_die "work-approve.sh list showed the completed note"
fi
if grep -q "execution_id=${reject_exec}" "$WORKDIR/wrapper-list.out"; then
  id42_die "work-approve.sh list showed the rejected note"
fi

probe_cmd start-note "smoke-wrapper-note" "smoke-wrapper" | tee "$WORKDIR/wrapper-start.out"
sed -i 's/\r$//' "$WORKDIR/wrapper-start.out"
wrapper_exec=$(awk -F= '/^execution_id=/{print $2}' "$WORKDIR/wrapper-start.out")
wrapper_status=$(awk -F= '/^status=/{print $2}' "$WORKDIR/wrapper-start.out")
[[ -n "$wrapper_exec" && "$wrapper_status" == "WAITING_APPROVAL" ]] \
  || id42_die "wrapper approve note did not wait for approval"
ID42_ENV_FILE=$ENV_FILE COMPOSE_PROJECT_NAME=$PROJECT \
  "$ROOT/scripts/work-approve.sh" list | tee "$WORKDIR/wrapper-list-2.out"
sed -i 's/\r$//' "$WORKDIR/wrapper-list-2.out"
wrapper_approval=$(approval_id_for "$WORKDIR/wrapper-list-2.out" "$wrapper_exec") \
  || id42_die "work-approve.sh list did not include the wrapper note"
ID42_ENV_FILE=$ENV_FILE COMPOSE_PROJECT_NAME=$PROJECT \
  "$ROOT/scripts/work-approve.sh" approve "$wrapper_approval" --reason "wrapper" \
  | tee "$WORKDIR/wrapper-approve.out"
sed -i 's/\r$//' "$WORKDIR/wrapper-approve.out"
grep -q "execution_status=COMPLETED" "$WORKDIR/wrapper-approve.out" \
  || id42_die "work-approve.sh approve did not resume to COMPLETED"
grep -q "resumed=true" "$WORKDIR/wrapper-approve.out" \
  || id42_die "work-approve.sh approve did not pass --resume"
probe_cmd execution-status "$wrapper_exec" | tee "$WORKDIR/wrapper-status.out"
sed -i 's/\r$//' "$WORKDIR/wrapper-status.out"
grep -qx "status=COMPLETED" "$WORKDIR/wrapper-status.out" \
  || id42_die "wrapper-approved note is not COMPLETED in the store"
echo "OK id42-work approvals list, approve --resume, and reject"

echo "== id42-work uncertain note reconciliation =="
crash_cmd() {
  gateway_run --entrypoint /opt/id42-work/venv/bin/python \
    -e "PYTHONPATH=/opt/id42-agent" \
    nanobot-gateway - "$@" <"$ROOT/scripts/lib/reconcile_crash.py"
}

crash_field() {
  awk -F= -v key="$1" '$1 == key { print substr($0, index($0, "=") + 1); exit }' "$2"
}

require_field() {
  local file=$1 key=$2 expected=$3 got
  got=$(crash_field "$key" "$file")
  [[ "$got" == "$expected" ]] || id42_die "$key is ${got:-missing}, expected $expected ($file)"
}

crash_cmd seed-legacy "smoke-legacy-note" | tee "$WORKDIR/legacy-seed.out"
probe_cmd list-notes | tee "$WORKDIR/legacy-list.out"
sed -i 's/\r$//' "$WORKDIR/legacy-list.out"
grep -qx "note=smoke-legacy-note" "$WORKDIR/legacy-list.out" \
  || id42_die "a note written without operation_id did not list"

crash_cmd crash after "smoke-recon-applied" "smoke-recon-applied" | tee "$WORKDIR/applied-crash.out"
sed -i 's/\r$//' "$WORKDIR/applied-crash.out"
applied_exec=$(crash_field execution_id "$WORKDIR/applied-crash.out")
require_field "$WORKDIR/applied-crash.out" status FAILED
require_field "$WORKDIR/applied-crash.out" requires_reconciliation true
require_field "$WORKDIR/applied-crash.out" journal_status UNCERTAIN
require_field "$WORKDIR/applied-crash.out" action_id append-note
require_field "$WORKDIR/applied-crash.out" capability notes.append
require_field "$WORKDIR/applied-crash.out" note_count 1
[[ -n "$applied_exec" ]] || id42_die "applied crash returned no execution id"

crash_cmd corrupt-notes | tee "$WORKDIR/applied-corrupt.out"
probe_cmd resume-note "$applied_exec" | tee "$WORKDIR/applied-unknown.out"
sed -i 's/\r$//' "$WORKDIR/applied-unknown.out"
require_field "$WORKDIR/applied-unknown.out" status FAILED
require_field "$WORKDIR/applied-unknown.out" requires_reconciliation true
crash_cmd inspect "$applied_exec" "smoke-recon-applied" | tee "$WORKDIR/applied-unknown-journal.out"
sed -i 's/\r$//' "$WORKDIR/applied-unknown-journal.out"
require_field "$WORKDIR/applied-unknown-journal.out" journal_status UNCERTAIN
require_field "$WORKDIR/applied-unknown-journal.out" note_count 1

crash_cmd repair-notes | tee "$WORKDIR/applied-repair.out"
probe_cmd resume-note "$applied_exec" | tee "$WORKDIR/applied-resume.out"
sed -i 's/\r$//' "$WORKDIR/applied-resume.out"
require_field "$WORKDIR/applied-resume.out" status COMPLETED
require_field "$WORKDIR/applied-resume.out" requires_reconciliation false
require_field "$WORKDIR/applied-resume.out" appended True
crash_cmd inspect "$applied_exec" "smoke-recon-applied" | tee "$WORKDIR/applied-done.out"
sed -i 's/\r$//' "$WORKDIR/applied-done.out"
require_field "$WORKDIR/applied-done.out" journal_status SUCCEEDED
require_field "$WORKDIR/applied-done.out" note_count 1

crash_cmd crash before "smoke-recon-missing" "smoke-recon-missing" | tee "$WORKDIR/missing-crash.out"
sed -i 's/\r$//' "$WORKDIR/missing-crash.out"
missing_exec=$(crash_field execution_id "$WORKDIR/missing-crash.out")
require_field "$WORKDIR/missing-crash.out" status FAILED
require_field "$WORKDIR/missing-crash.out" requires_reconciliation true
require_field "$WORKDIR/missing-crash.out" journal_status UNCERTAIN
require_field "$WORKDIR/missing-crash.out" note_count 0
[[ -n "$missing_exec" ]] || id42_die "missing crash returned no execution id"
probe_cmd resume-note "$missing_exec" | tee "$WORKDIR/missing-resume.out"
sed -i 's/\r$//' "$WORKDIR/missing-resume.out"
require_field "$WORKDIR/missing-resume.out" status WAITING_APPROVAL
require_field "$WORKDIR/missing-resume.out" requires_reconciliation false
crash_cmd inspect "$missing_exec" "smoke-recon-missing" | tee "$WORKDIR/missing-waiting.out"
sed -i 's/\r$//' "$WORKDIR/missing-waiting.out"
require_field "$WORKDIR/missing-waiting.out" note_count 0
require_field "$WORKDIR/missing-waiting.out" journal_status PENDING_APPROVAL
approvals_cmd list --factory id42_agent.mcp_host:server | tee "$WORKDIR/missing-list.out"
sed -i 's/\r$//' "$WORKDIR/missing-list.out"
missing_approval=$(approval_id_for "$WORKDIR/missing-list.out" "$missing_exec") \
  || id42_die "the retried note did not ask for approval again"
require_pending_line "$WORKDIR/missing-list.out" "$missing_exec"
approvals_cmd approve "$missing_approval" \
  --factory id42_agent.mcp_host:server \
  --actor id42-operator \
  --reason "retry" \
  --resume | tee "$WORKDIR/missing-approve.out"
sed -i 's/\r$//' "$WORKDIR/missing-approve.out"
grep -q "execution_status=COMPLETED" "$WORKDIR/missing-approve.out" \
  || id42_die "retried note did not complete after approval"
crash_cmd inspect "$missing_exec" "smoke-recon-missing" | tee "$WORKDIR/missing-done.out"
sed -i 's/\r$//' "$WORKDIR/missing-done.out"
require_field "$WORKDIR/missing-done.out" status COMPLETED
require_field "$WORKDIR/missing-done.out" journal_status SUCCEEDED
require_field "$WORKDIR/missing-done.out" note_count 1

crash_cmd crash before "smoke-recon-abandon" "smoke-recon-abandon" | tee "$WORKDIR/abandon-crash.out"
sed -i 's/\r$//' "$WORKDIR/abandon-crash.out"
abandon_exec=$(crash_field execution_id "$WORKDIR/abandon-crash.out")
abandon_action=$(crash_field action_id "$WORKDIR/abandon-crash.out")
require_field "$WORKDIR/abandon-crash.out" journal_status UNCERTAIN
require_field "$WORKDIR/abandon-crash.out" note_count 0
[[ -n "$abandon_exec" && -n "$abandon_action" ]] || id42_die "abandon crash returned no ids"

ID42_ENV_FILE=$ENV_FILE COMPOSE_PROJECT_NAME=$PROJECT \
  "$ROOT/scripts/work-reconcile.sh" list | tee "$WORKDIR/reconcile-list.out"
sed -i 's/\r$//' "$WORKDIR/reconcile-list.out"
grep -q "execution_id=${abandon_exec}" "$WORKDIR/reconcile-list.out" \
  || id42_die "work-reconcile.sh list did not show the uncertain note"
if grep -q "execution_id=${applied_exec}" "$WORKDIR/reconcile-list.out"; then
  id42_die "work-reconcile.sh list showed the reconciled note"
fi
if grep -q "execution_id=${missing_exec}" "$WORKDIR/reconcile-list.out"; then
  id42_die "work-reconcile.sh list showed the retried note"
fi

ID42_ENV_FILE=$ENV_FILE COMPOSE_PROJECT_NAME=$PROJECT \
  "$ROOT/scripts/work-reconcile.sh" show "$abandon_exec" "$abandon_action" \
  | tee "$WORKDIR/reconcile-show.out"
sed -i 's/\r$//' "$WORKDIR/reconcile-show.out"
grep -q '"journal_status": "UNCERTAIN"' "$WORKDIR/reconcile-show.out" \
  || id42_die "work-reconcile.sh show did not report UNCERTAIN"
grep -q '"work_id": "note"' "$WORKDIR/reconcile-show.out" \
  || id42_die "work-reconcile.sh show did not report the note work"

if gateway_run --entrypoint /usr/local/bin/id42-work -e "PYTHONPATH=/opt/id42-agent" \
  nanobot-gateway reconcile abandon "$abandon_exec" "$abandon_action" \
  --factory id42_agent.mcp_host:server \
  --actor id42-agent \
  --reason "the execution actor must not decide" \
  >"$WORKDIR/reconcile-self.out" 2>"$WORKDIR/reconcile-self.err"
then
  id42_die "the execution actor reconciled its own call"
fi
grep -q "id42-work:" "$WORKDIR/reconcile-self.err" \
  || {
    cat "$WORKDIR/reconcile-self.err" >&2 || true
    id42_die "a self-reconcile failed without an id42-work error"
  }
crash_cmd inspect "$abandon_exec" "smoke-recon-abandon" | tee "$WORKDIR/abandon-still.out"
sed -i 's/\r$//' "$WORKDIR/abandon-still.out"
require_field "$WORKDIR/abandon-still.out" journal_status UNCERTAIN

ID42_ENV_FILE=$ENV_FILE COMPOSE_PROJECT_NAME=$PROJECT \
  "$ROOT/scripts/work-reconcile.sh" abandon "$abandon_exec" "$abandon_action" \
  --reason "cannot be known" | tee "$WORKDIR/reconcile-abandon.out"
sed -i 's/\r$//' "$WORKDIR/reconcile-abandon.out"
grep -q "abandoned execution_id=${abandon_exec} " "$WORKDIR/reconcile-abandon.out" \
  || id42_die "work-reconcile.sh abandon did not report the execution"
grep -q "journal_status=ABANDONED" "$WORKDIR/reconcile-abandon.out" \
  || id42_die "work-reconcile.sh abandon did not abandon the call"
grep -q "execution_status=FAILED" "$WORKDIR/reconcile-abandon.out" \
  || id42_die "work-reconcile.sh abandon did not leave the execution FAILED"
grep -q "resumed=false" "$WORKDIR/reconcile-abandon.out" \
  || id42_die "work-reconcile.sh abandon resumed the execution"
grep -q "decision_actor_id=id42-operator" "$WORKDIR/reconcile-abandon.out" \
  || id42_die "work-reconcile.sh abandon did not record id42-operator"
grep -q "status_reason=reconciliation_abandoned" "$WORKDIR/reconcile-abandon.out" \
  || id42_die "work-reconcile.sh abandon did not record reconciliation_abandoned"

probe_cmd resume-note "$abandon_exec" | tee "$WORKDIR/abandon-resume.out"
sed -i 's/\r$//' "$WORKDIR/abandon-resume.out"
require_field "$WORKDIR/abandon-resume.out" status FAILED
require_field "$WORKDIR/abandon-resume.out" requires_reconciliation false
require_field "$WORKDIR/abandon-resume.out" status_reason reconciliation_abandoned
crash_cmd inspect "$abandon_exec" "smoke-recon-abandon" | tee "$WORKDIR/abandon-done.out"
sed -i 's/\r$//' "$WORKDIR/abandon-done.out"
require_field "$WORKDIR/abandon-done.out" journal_status ABANDONED
require_field "$WORKDIR/abandon-done.out" note_count 0

ID42_ENV_FILE=$ENV_FILE COMPOSE_PROJECT_NAME=$PROJECT \
  "$ROOT/scripts/work-reconcile.sh" list | tee "$WORKDIR/reconcile-list-done.out"
sed -i 's/\r$//' "$WORKDIR/reconcile-list-done.out"
grep -qx "uncertain-none" "$WORKDIR/reconcile-list-done.out" \
  || id42_die "an uncertain note was still listed after reconciliation"
probe_cmd list-notes | tee "$WORKDIR/recon-notes.out"
sed -i 's/\r$//' "$WORKDIR/recon-notes.out"
grep -qx "note=smoke-legacy-note" "$WORKDIR/recon-notes.out" \
  || id42_die "reconciliation dropped the legacy note"
applied_notes=$(grep -cx "note=smoke-recon-applied" "$WORKDIR/recon-notes.out" || true)
missing_notes=$(grep -cx "note=smoke-recon-missing" "$WORKDIR/recon-notes.out" || true)
abandon_notes=$(grep -cx "note=smoke-recon-abandon" "$WORKDIR/recon-notes.out" || true)
[[ "$applied_notes" == "1" && "$missing_notes" == "1" && "$abandon_notes" == "0" ]] \
  || id42_die "note counts applied=${applied_notes} missing=${missing_notes} abandon=${abandon_notes}"
echo "OK uncertain notes reconcile from the file, and abandon stays failed"

echo "== doctor security baseline on the running gateway =="
ID42_ENV_FILE=$ENV_FILE COMPOSE_PROJECT_NAME=$PROJECT \
  "$ROOT/scripts/doctor.sh" >"$WORKDIR/doctor.out" 2>"$WORKDIR/doctor.err" || true
if grep -q "FAIL: running container does not match the security baseline" "$WORKDIR/doctor.err"; then
  cat "$WORKDIR/doctor.err" >&2
  id42_die "doctor reported a security-baseline failure for a compliant gateway"
fi
if ! grep -q "OK: running container binds loopback ports" "$WORKDIR/doctor.out"; then
  cat "$WORKDIR/doctor.err" >&2
  id42_die "doctor did not confirm the security baseline"
fi
if ! grep -q "OK: id42-work MCP server answered on stdio" "$WORKDIR/doctor.out"; then
  cat "$WORKDIR/doctor.err" >&2
  id42_die "doctor did not confirm the id42-work MCP server"
fi
if ! grep -q "OK: id42-work config is stdio-only" "$WORKDIR/doctor.out"; then
  cat "$WORKDIR/doctor.err" >&2
  id42_die "doctor did not confirm the id42-work config"
fi
if ! grep -q "OK: id42-work store is writable, mounted, and opens SQLite" "$WORKDIR/doctor.out"; then
  cat "$WORKDIR/doctor.err" >&2
  id42_die "doctor did not confirm the id42-work store"
fi
if ! grep -q "OK: id42-work lists echo, note, and note.list" "$WORKDIR/doctor.out"; then
  cat "$WORKDIR/doctor.err" >&2
  id42_die "doctor did not confirm the default works"
fi
if ! grep -q "OK: id42-work pin is 4dc4cef40affc3baaa563f59564b143827f8f3d7" "$WORKDIR/doctor.out"; then
  cat "$WORKDIR/doctor.err" >&2
  id42_die "doctor did not confirm the id42-work pin"
fi
if ! grep -q "OK: id42-work approvals command is available" "$WORKDIR/doctor.out"; then
  cat "$WORKDIR/doctor.err" >&2
  id42_die "doctor did not confirm the id42-work approvals command"
fi
if ! grep -q "OK: id42-work reconcile command is available" "$WORKDIR/doctor.out"; then
  cat "$WORKDIR/doctor.err" >&2
  id42_die "doctor did not confirm the id42-work reconcile command"
fi
echo "OK doctor accepts the running gateway baseline"

echo "== bootstrap again after private data dirs exist =="
ID42_ENV_FILE=$ENV_FILE COMPOSE_PROJECT_NAME=$PROJECT \
  docker compose --env-file "$ENV_FILE" -f "$ROOT/docker-compose.yml" exec -T -u 1000 nanobot-gateway \
  /bin/sh -c 'mkdir -p /home/nanobot/.nanobot/sessions /home/nanobot/.nanobot/updates && chmod 700 /home/nanobot/.nanobot/sessions /home/nanobot/.nanobot/updates'
ID42_ENV_FILE=$ENV_FILE COMPOSE_PROJECT_NAME=$PROJECT \
  "$ROOT/scripts/bootstrap.sh" >"$WORKDIR/bootstrap3.log"
if grep -q "smoke-web-token-do-not-leak" "$WORKDIR/bootstrap3.log"; then
  id42_die "bootstrap printed the web token on re-run"
fi
grep -q "leaving WebUI edits unchanged" "$WORKDIR/bootstrap3.log"
echo "OK bootstrap is idempotent after mode-700 data directories exist"
echo "smoke test passed"
