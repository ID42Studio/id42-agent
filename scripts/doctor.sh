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

compose_config=$(mktemp)
if ! "${COMPOSE[@]}" config --format json >"$compose_config"; then
  bad "docker compose config rejected docker-compose.yml"
  rm -f "$compose_config"
else
  good "docker compose config accepted the file"
  if ! python3 - "$compose_config" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1]))
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
  rm -f "$compose_config"
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

if "${COMPOSE[@]}" run --rm --no-deps --user 0 --entrypoint /bin/sh nanobot-gateway \
  -c 'chmod 700 /var/lib/id42-work 2>/dev/null || true; chown nanobot:nanobot /var/lib/id42-work'
then
  good "id42-work store directory is owned by the gateway user"
else
  bad "could not prepare the id42-work store directory"
fi

store_out=$(mktemp)
if "${COMPOSE[@]}" run --rm --no-deps -T --user 1000 \
  --entrypoint /opt/id42-work/venv/bin/python \
  -e "PYTHONPATH=/opt/id42-agent" \
  nanobot-gateway - <<'PY' | tee "$store_out"
import json
import os
from pathlib import Path

import id42_agent.mcp_host as host

store = Path("/var/lib/id42-work")
if not store.is_dir() or not os.access(store, os.W_OK | os.X_OK):
    print("store-mount: id42-work store is not writable")
    raise SystemExit(1)
# A named volume has a different device from the container root filesystem.
if store.stat().st_dev == store.parent.stat().st_dev:
    print("store-mount: id42-work store is not a separate mount")
    raise SystemExit(1)
probe = store / ".doctor-write-probe"
probe.write_text("ok\n", encoding="utf-8")
if probe.read_text(encoding="utf-8") != "ok\n":
    print("store-mount: id42-work store write did not read back")
    raise SystemExit(1)
probe.unlink()
try:
    backend, version = host.storage_status()
except RuntimeError as exc:
    print(f"store-backend: {exc}")
    raise SystemExit(1) from None
print(f"store-ok backend={backend} schema={version}")

config_path = Path("/home/nanobot/.nanobot/config.json")
if not config_path.is_file():
    print("storage-config-absent")
    raise SystemExit(0)
document = json.loads(config_path.read_text(encoding="utf-8"))
tools = document.get("tools") if isinstance(document, dict) else None
servers = tools.get("mcpServers") if isinstance(tools, dict) else None
server = servers.get("id42-work") if isinstance(servers, dict) else None
if not isinstance(server, dict):
    print("storage-config-absent")
    raise SystemExit(0)
extra = server.get("env") if isinstance(server.get("env"), dict) else {}
configured = extra.get("ID42_WORK_STORAGE", "")
configured_url = extra.get("ID42_WORK_DATABASE_URL", "")
if not isinstance(configured, str) or not isinstance(configured_url, str):
    print("store-backend: config storage settings are not strings")
    raise SystemExit(1)
configured = configured.strip().lower()
configured_url = configured_url.strip()
expected_url = os.environ.get("ID42_WORK_DATABASE_URL", "").strip() if backend == "postgres" else ""
if backend == "sqlite":
    aligned = configured in {"", "sqlite"} and not configured_url
else:
    aligned = configured == "postgres" and bool(configured_url) and configured_url == expected_url
if not aligned:
    print("store-backend: config.json storage does not match ID42_WORK_STORAGE")
    raise SystemExit(1)
print("storage-config-ok")
PY
then
  sed -i 's/\r$//' "$store_out"
  backend=$(sed -n 's/^store-ok backend=\([^ ]*\).*/\1/p' "$store_out")
  if [[ "$backend" == "sqlite" ]]; then
    good "id42-work storage backend is sqlite"
    good "id42-work store is writable, mounted, and opens SQLite"
  elif [[ "$backend" == "postgres" ]]; then
    good "id42-work storage backend is postgres"
    good "id42-work postgres storage is reachable"
  else
    bad "id42-work store check did not report a storage backend"
  fi
  if grep -qx 'storage-config-ok' "$store_out"; then
    good "id42-work storage config matches ID42_WORK_STORAGE"
  elif grep -qx 'storage-config-absent' "$store_out"; then
    good "id42-work storage config has no MCP server env to compare"
  else
    bad "id42-work storage config check did not report a result"
  fi
else
  sed -i 's/\r$//' "$store_out" || true
  if grep -q '^store-backend:' "$store_out"; then
    bad "id42-work storage backend check failed"
  else
    bad "id42-work store is not writable or not a persistent mount"
  fi
fi
rm -f "$store_out"

pg_container=$("${COMPOSE[@]}" --profile postgres ps -q postgres 2>/dev/null || true)
if [[ -n "$pg_container" ]]; then
  if python3 - "$pg_container" <<'PY'
import json, subprocess, sys
info = json.loads(subprocess.check_output(["docker", "inspect", sys.argv[1]], text=True))[0]
bindings = (info.get("HostConfig") or {}).get("PortBindings") or {}
published = (info.get("NetworkSettings") or {}).get("Ports") or {}
if bindings or any(value for value in published.values()):
    raise SystemExit(1)
PY
  then
    good "postgres service publishes no host port"
  else
    bad "postgres service publishes a host port"
  fi
fi

probe_out=$(mktemp)
if "${COMPOSE[@]}" run --rm --no-deps -T --user 1000 \
  --entrypoint /app/.venv/bin/python \
  nanobot-gateway - <"$ROOT/scripts/lib/mcp_probe.py" | tee "$probe_out"
then
  sed -i 's/\r$//' "$probe_out"
  if grep -qx 'works=echo,note,note.list' "$probe_out"; then
    good "id42-work lists echo, note, and note.list"
  else
    bad "id42-work did not list echo, note, and note.list"
  fi
  good "id42-work MCP server answered on stdio"
else
  bad "id42-work MCP server did not answer on stdio"
fi
rm -f "$probe_out"

key_path=$(id42_authority_key_path "$ENV_FILE")
key_out=$(mktemp)
if "${COMPOSE[@]}" run --rm --no-deps -T --user 1000 \
  --entrypoint /opt/id42-work/venv/bin/python \
  -e "PYTHONPATH=/opt/id42-agent" \
  -e "ID42_WORK_AUTHORITY_KEY_PATH=${key_path}" \
  nanobot-gateway - <<'PY' | tee "$key_out"
import json
import os
import stat
from pathlib import Path

import id42_agent.mcp_host as host

path = host.authority_key_file()
parent = path.parent
if not parent.is_dir():
    raise SystemExit(f"authority key directory does not exist: {parent}")
if parent.stat().st_mode & 0o002:
    raise SystemExit("authority key directory is writable by others")
if not os.access(parent, os.W_OK | os.X_OK):
    raise SystemExit("authority key directory is not writable")

config_path = Path("/home/nanobot/.nanobot/config.json")
if config_path.is_file():
    document = json.loads(config_path.read_text(encoding="utf-8"))
    tools = document.get("tools") if isinstance(document, dict) else None
    servers = tools.get("mcpServers") if isinstance(tools, dict) else None
    server = servers.get("id42-work") if isinstance(servers, dict) else None
    if isinstance(server, dict):
        extra = server.get("env") if isinstance(server.get("env"), dict) else {}
        configured = extra.get("ID42_WORK_AUTHORITY_KEY_PATH", "")
        if not isinstance(configured, str):
            raise SystemExit("config authority key path is not a string")
        configured = configured.strip()
        expected = str(path)
        if configured and configured != expected:
            raise SystemExit(
                "config.json authority key path does not match ID42_WORK_AUTHORITY_KEY_PATH"
            )
        if not configured and expected != str(host.DEFAULT_AUTHORITY_KEY):
            raise SystemExit(
                "config.json does not pass ID42_WORK_AUTHORITY_KEY_PATH to the MCP server"
            )

if path.is_symlink():
    raise SystemExit("authority key file is a symlink")
if not path.exists():
    print("key-absent")
    raise SystemExit(0)
info = path.lstat()
if not stat.S_ISREG(info.st_mode):
    raise SystemExit("authority key file is not a regular file")
mode = stat.S_IMODE(info.st_mode)
if mode != 0o600:
    raise SystemExit(f"authority key file mode is {mode:03o}")
if info.st_uid != os.getuid():
    raise SystemExit("authority key file is not owned by the gateway user")
print("key-present")
PY
then
  sed -i 's/\r$//' "$key_out"
  if grep -qx 'key-present' "$key_out"; then
    good "authority key file is mode 0600 at ${key_path}; rotate it with scripts/work-rotate-key.sh"
  elif grep -qx 'key-absent' "$key_out"; then
    good "authority key file is absent and will be created mode 0600 on first use; rotate it later with scripts/work-rotate-key.sh"
  else
    bad "authority key file check failed"
  fi
else
  bad "authority key file check did not run"
fi
rm -f "$key_out"

if [[ -n "$container_id" ]]; then
  # Keep in sync with the Dockerfile default for ID42_WORK_REF.
  expected_pin=d3be75dceb1ebfd7c6832f61fa905dd27dbfb4f9
  pin=$("${COMPOSE[@]}" exec -T -u 1000 nanobot-gateway /bin/sh -c 'cat /opt/id42-agent/ID42_WORK_REF' 2>/dev/null || true)
  pin=${pin//$'\r'/}
  pin=${pin//$'\n'/}
  if [[ "$pin" != "$expected_pin" ]]; then
    bad "id42-work pin is ${pin:-missing}, expected $expected_pin"
  else
    good "id42-work pin is $pin"
  fi

  help_out=$(mktemp)
  if "${COMPOSE[@]}" exec -T -u 1000 nanobot-gateway \
    /usr/local/bin/id42-work approvals approve --help >"$help_out" 2>&1 \
    && grep -q -- '--factory' "$help_out" \
    && grep -q -- '--actor' "$help_out" \
    && grep -q -- '--resume' "$help_out" \
    && grep -q -- '--reason' "$help_out"
  then
    good "id42-work approvals command is available"
  else
    bad "id42-work approvals command is missing or has no --factory, --actor, --reason, or --resume"
    if [[ -s "$help_out" ]]; then
      tail -n 20 "$help_out" >&2 || true
    fi
  fi
  rm -f "$help_out"

  help_out=$(mktemp)
  if "${COMPOSE[@]}" exec -T -u 1000 nanobot-gateway \
    /usr/local/bin/id42-work reconcile abandon --help >"$help_out" 2>&1 \
    && grep -q -- '--factory' "$help_out" \
    && grep -q -- '--actor' "$help_out" \
    && grep -q -- '--reason' "$help_out"
  then
    good "id42-work reconcile command is available"
  else
    bad "id42-work reconcile command is missing or has no --factory, --actor, or --reason"
    if [[ -s "$help_out" ]]; then
      tail -n 20 "$help_out" >&2 || true
    fi
  fi
  rm -f "$help_out"

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
