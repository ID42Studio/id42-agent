#!/usr/bin/env bash
# Check the host, the rendered config, loopback ports, Ollama, and Telegram.
# Tokens are never printed. Exits non-zero when a required check fails.
set -euo pipefail

# shellcheck source=scripts/lib/common.sh
source "$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

ROOT=$(id42_root)
ENV_FILE=${ID42_ENV_FILE:-$ROOT/.env}
CONFIG_PY=$ROOT/scripts/lib/config.py
COMPOSE=(docker compose --env-file "$ENV_FILE" -f "$ROOT/docker-compose.yml")
failures=0

bad() {
  echo "FAIL: $*" >&2
  failures=$((failures + 1))
}

good() {
  echo "OK: $*"
}

id42_require_cmd python3 docker

if ! docker info >/dev/null 2>&1; then
  bad "docker daemon is not reachable"
else
  good "docker daemon is reachable ($(docker version --format '{{.Server.Version}}' 2>/dev/null || echo unknown))"
fi

if docker compose version >/dev/null 2>&1; then
  good "docker compose is available ($(docker compose version --short 2>/dev/null || docker compose version))"
else
  bad "docker compose is not available"
fi

if [[ ! -f "$ENV_FILE" ]]; then
  bad ".env is missing ($ENV_FILE). Copy .env.example and run make init."
  echo "id42-agent: $failures check(s) failed"
  exit 1
fi
good ".env exists"

if ! python3 "$CONFIG_PY" summary --env-file "$ENV_FILE"; then
  bad ".env could not be parsed"
fi

if ! "${COMPOSE[@]}" config --format json >/tmp/id42-compose-config.json; then
  bad "docker compose config rejected docker-compose.yml"
else
  good "docker compose config accepted the file"
  if ! python3 - "$ROOT/docker-compose.yml" <<'PY'
import json, sys
doc = json.load(open("/tmp/id42-compose-config.json"))
services = doc.get("services") or {}
gateway = services.get("nanobot-gateway") or {}
ports = gateway.get("ports") or []
wanted = {"8765/tcp", "18790/tcp"}
seen = set()
errors = []
for item in ports:
    published = str(item.get("published") or item.get("target") or "")
    target = str(item.get("target") or "")
    host_ip = item.get("host_ip") or ""
    key = f"{target}/tcp"
    seen.add(key)
    if host_ip not in {"127.0.0.1", "::1"}:
        errors.append(f"published port {published}->{target} is bound to {host_ip or 'all interfaces'}")
missing = wanted - seen
extra = seen - wanted
errors.extend(f"missing published port {name}" for name in sorted(missing))
errors.extend(f"unexpected published port {name}" for name in sorted(extra))
if errors:
    print("\n".join(errors), file=sys.stderr)
    sys.exit(1)
print("compose publishes 8765 and 18790 on loopback only")
PY
  then
    bad "compose port bindings are not loopback-only"
  else
    good "compose publishes 8765 and 18790 on loopback only"
  fi
fi

container_id=$("${COMPOSE[@]}" ps -q nanobot-gateway 2>/dev/null || true)
if [[ -z "$container_id" ]]; then
  bad "nanobot-gateway is not running (start it with make up)"
else
  good "nanobot-gateway container is present"
  # baseline.py exits 0 only when the baseline holds. A missing "!"
  # would report FAIL for a compliant container and hide a real violation.
  if ! python3 "$ROOT/scripts/lib/baseline.py" "$container_id"; then
    bad "running container does not match the security baseline"
  else
    good "running container binds loopback ports, drops capabilities, and has no docker socket or home mount"
  fi

  health_code=$(curl --silent --show-error --output /tmp/id42-health.json --write-out "%{http_code}" --max-time 10 http://127.0.0.1:18790/health || true)
  if [[ "$health_code" != "200" ]]; then
    bad "GET http://127.0.0.1:18790/health returned ${health_code:-no response}"
  elif ! python3 - <<'PY'
import json, sys
payload = json.load(open("/tmp/id42-health.json"))
if payload.get("status") != "ok" or payload.get("ready") is not True:
    print(f"health payload is not ready: status={payload.get('status')!r} ready={payload.get('ready')!r}", file=sys.stderr)
    sys.exit(1)
PY
  then
    bad "health endpoint is up but not ready"
  else
    good "GET /health returned ready"
  fi
fi

if "${COMPOSE[@]}" run --rm --no-deps -T --user 1000 \
  --entrypoint /app/.venv/bin/python \
  nanobot-gateway - <"$ROOT/scripts/lib/mcp_probe.py"
then
  good "id42-work MCP server answered on stdio"
else
  bad "id42-work MCP server did not answer on stdio"
fi

if [[ -n "$container_id" ]]; then
  pin=$("${COMPOSE[@]}" exec -T -u 1000 nanobot-gateway /bin/sh -c 'cat /opt/id42-agent/ID42_WORK_REF' 2>/dev/null || true)
  pin=${pin//$'\r'/}
  pin=${pin//$'\n'/}
  if [[ ! "$pin" =~ ^[A-Za-z0-9._-]+$ ]]; then
    bad "id42-work pin file is missing or invalid"
  else
    good "id42-work pin is $pin"
  fi

  work_enabled=$(
    python3 - "$ENV_FILE" "$ROOT/scripts/lib" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[2])
from config import load_env_file

env = load_env_file(Path(sys.argv[1]))
print(env.get("ID42_WORK_ENABLED", "false").strip().lower() or "false")
PY
  )
  case "$work_enabled" in
    true|yes|on|1) work_enabled=true ;;
    false|no|off|0) work_enabled=false ;;
    *)
      bad "ID42_WORK_ENABLED must be true or false"
      work_enabled=""
      ;;
  esac
  if [[ -n "$work_enabled" ]]; then
    if "${COMPOSE[@]}" exec -T -u 1000 nanobot-gateway /app/.venv/bin/python - \
      assert-mcp --enabled "$work_enabled" <"$CONFIG_PY"
    then
      good "id42-work config is stdio-only"
    else
      bad "id42-work config check failed"
    fi
  fi
else
  bad "skipped the id42-work config check because the gateway is not running"
fi

probe_script=$(mktemp)
cat >"$probe_script" <<'PY'
import json, sys, urllib.error, urllib.request
cfg = json.load(open("/home/nanobot/.nanobot/config.json"))
base = cfg["providers"]["ollama"]["apiBase"].rstrip("/")
model = cfg["modelPresets"]["primary"]["model"]
url = base + "/models"
try:
    with urllib.request.urlopen(url, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
except Exception as exc:
    print(f"UNREACHABLE {url} {exc}", file=sys.stderr)
    sys.exit(1)
names = []
for item in payload.get("data") or []:
    if isinstance(item, dict) and isinstance(item.get("id"), str):
        names.append(item["id"])
print("MODELS " + ",".join(names))
candidates = {model, model + ":latest"}
ok = any(name in candidates or name.split(":", 1)[0] == model for name in names)
if not ok:
    print(f"MISSING {model}", file=sys.stderr)
    sys.exit(2)
print(f"MODEL_OK {model}")
PY

if [[ -n "$container_id" ]]; then
  if "${COMPOSE[@]}" exec -T -u 1000 nanobot-gateway /app/.venv/bin/python - <"$probe_script"; then
    good "Ollama is reachable from the gateway container and the configured model is listed"
  else
    bad "Ollama check from inside the gateway container failed"
  fi
else
  bad "skipped the in-container Ollama check because the gateway is not running"
fi
rm -f "$probe_script"

telegram_enabled=$(
  python3 - "$ENV_FILE" "$ROOT/scripts/lib" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[2])
from config import load_env_file

env = load_env_file(Path(sys.argv[1]))
print(env.get("TELEGRAM_ENABLED", "false").strip().lower())
PY
)

case "$telegram_enabled" in
  true|yes|on|1)
    if [[ -n "$container_id" ]]; then
      if "${COMPOSE[@]}" exec -T -u 1000 nanobot-gateway /app/.venv/bin/python - <<'PY'
import json, sys, urllib.error, urllib.request
cfg = json.load(open("/home/nanobot/.nanobot/config.json"))
telegram = (cfg.get("channels") or {}).get("telegram") or {}
if telegram.get("enabled") is not True:
    print("telegram is not enabled in config.json", file=sys.stderr)
    sys.exit(1)
allow = telegram.get("allowFrom") or []
if not isinstance(allow, list) or not allow or "*" in allow:
    print("channels.telegram.allowFrom is not an explicit allowlist", file=sys.stderr)
    sys.exit(1)
token = telegram.get("token") or ""
if not isinstance(token, str) or not token.strip():
    print("channels.telegram.token is empty", file=sys.stderr)
    sys.exit(1)
request = urllib.request.Request(f"https://api.telegram.org/bot{token}/getMe")
try:
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
except urllib.error.HTTPError as exc:
    print(f"Telegram getMe returned HTTP {exc.code}", file=sys.stderr)
    sys.exit(1)
except Exception as exc:
    text = str(exc).replace(token, "[redacted]")
    print(f"Telegram getMe failed: {text}", file=sys.stderr)
    sys.exit(1)
result = payload.get("result") or {}
if payload.get("ok") is not True:
    print("Telegram getMe rejected the token", file=sys.stderr)
    sys.exit(1)
print(f"telegram-bot id={result.get('id')} username={result.get('username')}")
print("telegram-allow-count=" + str(len(allow)))
PY
      then
        good "Telegram getMe succeeded and the allowlist is set"
      else
        bad "Telegram check failed"
      fi
    else
      bad "skipped in-container Telegram getMe because the gateway is not running"
    fi
    ;;
  false|no|off|0|"")
    good "Telegram is disabled; getMe was not called"
    ;;
  *)
    bad "TELEGRAM_ENABLED must be true or false"
    ;;
esac

if [[ "$failures" -ne 0 ]]; then
  echo "id42-agent: $failures check(s) failed"
  exit 1
fi

echo "id42-agent: doctor passed"
