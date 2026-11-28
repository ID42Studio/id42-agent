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
PG_PROJECT=""
PG_ENV=""

cleanup() {
  if [[ -n "$PG_PROJECT" && -n "$PG_ENV" && -f "$PG_ENV" ]]; then
    COMPOSE_PROJECT_NAME=$PG_PROJECT \
      docker compose --env-file "$PG_ENV" --profile postgres -f "$ROOT/docker-compose.yml" \
      down -v --remove-orphans >/dev/null 2>&1 || true
  fi
  if [[ -f "$ENV_FILE" ]]; then
    ID42_ENV_FILE=$ENV_FILE COMPOSE_PROJECT_NAME=$PROJECT \
      docker compose --env-file "$ENV_FILE" --profile id42-work-web -f "$ROOT/docker-compose.yml" \
      down -v --remove-orphans \
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

cat >"$WORKDIR/relative-key.env" <<'EOF'
TZ=America/Sao_Paulo
OLLAMA_BASE_URL=http://host.docker.internal:9/v1
OLLAMA_MODEL=smoke-model
TELEGRAM_ENABLED=false
ID42_WORK_ENABLED=true
ID42_WORK_AUTHORITY_KEY_PATH=keys/authority.key
NANOBOT_WEB_TOKEN=smoke-web-token-do-not-leak
EOF
expect_fail "relative authority key" python3 "$CONFIG_PY" render --env-file "$WORKDIR/relative-key.env" --output "$WORKDIR/relative-key.json"

cat >"$WORKDIR/home-key.env" <<'EOF'
TZ=America/Sao_Paulo
OLLAMA_BASE_URL=http://host.docker.internal:9/v1
OLLAMA_MODEL=smoke-model
TELEGRAM_ENABLED=false
ID42_WORK_ENABLED=true
ID42_WORK_AUTHORITY_KEY_PATH=/home/nanobot/authority.key
NANOBOT_WEB_TOKEN=smoke-web-token-do-not-leak
EOF
expect_fail "authority key under home" python3 "$CONFIG_PY" render --env-file "$WORKDIR/home-key.env" --output "$WORKDIR/home-key.json"

cat >"$WORKDIR/bad-storage.env" <<'EOF'
TZ=America/Sao_Paulo
OLLAMA_BASE_URL=http://host.docker.internal:9/v1
OLLAMA_MODEL=smoke-model
TELEGRAM_ENABLED=false
ID42_WORK_ENABLED=true
ID42_WORK_STORAGE=mysql
NANOBOT_WEB_TOKEN=smoke-web-token-do-not-leak
EOF
expect_fail "storage backend" python3 "$CONFIG_PY" render --env-file "$WORKDIR/bad-storage.env" --output "$WORKDIR/bad-storage.json"

cat >"$WORKDIR/postgres-no-url.env" <<'EOF'
TZ=America/Sao_Paulo
OLLAMA_BASE_URL=http://host.docker.internal:9/v1
OLLAMA_MODEL=smoke-model
TELEGRAM_ENABLED=false
ID42_WORK_ENABLED=true
ID42_WORK_STORAGE=postgres
ID42_WORK_DATABASE_URL=
NANOBOT_WEB_TOKEN=smoke-web-token-do-not-leak
EOF
expect_fail "postgres without url" python3 "$CONFIG_PY" render --env-file "$WORKDIR/postgres-no-url.env" --output "$WORKDIR/postgres-no-url.json"

cat >"$WORKDIR/postgres-bad-url.env" <<'EOF'
TZ=America/Sao_Paulo
OLLAMA_BASE_URL=http://host.docker.internal:9/v1
OLLAMA_MODEL=smoke-model
TELEGRAM_ENABLED=false
ID42_WORK_ENABLED=true
ID42_WORK_STORAGE=postgres
ID42_WORK_DATABASE_URL=http://postgres:5432/id42_work
NANOBOT_WEB_TOKEN=smoke-web-token-do-not-leak
EOF
expect_fail "postgres url scheme" python3 "$CONFIG_PY" render --env-file "$WORKDIR/postgres-bad-url.env" --output "$WORKDIR/postgres-bad-url.json"

cat >"$WORKDIR/postgres-home-socket.env" <<'EOF'
TZ=America/Sao_Paulo
OLLAMA_BASE_URL=http://host.docker.internal:9/v1
OLLAMA_MODEL=smoke-model
TELEGRAM_ENABLED=false
ID42_WORK_ENABLED=true
ID42_WORK_STORAGE=postgres
ID42_WORK_DATABASE_URL=host=/home/nanobot dbname=id42_work
NANOBOT_WEB_TOKEN=smoke-web-token-do-not-leak
EOF
expect_fail "postgres socket under home" python3 "$CONFIG_PY" render --env-file "$WORKDIR/postgres-home-socket.env" --output "$WORKDIR/postgres-home-socket.json"

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
assert "ID42_WORK_AUTHORITY_KEY_PATH" not in server["env"]
assert "ID42_WORK_STORAGE" not in server["env"]
assert "ID42_WORK_DATABASE_URL" not in server["env"]
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
assert "ID42_WORK_AUTHORITY_KEY_PATH" not in server["env"]
assert "ID42_WORK_STORAGE" not in server["env"]
assert "ID42_WORK_DATABASE_URL" not in server["env"]
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

cat >"$WORKDIR/authority-key.env" <<'EOF'
TZ=America/Sao_Paulo
OLLAMA_BASE_URL=http://host.docker.internal:9/v1
OLLAMA_MODEL=smoke-model
TELEGRAM_ENABLED=false
ID42_WORK_ENABLED=true
ID42_WORK_FACTORY=
ID42_WORK_PYTHONPATH=
ID42_WORK_AUTHORITY_KEY_PATH=/var/lib/id42-work/keys/authority.key
NANOBOT_WEB_TOKEN=smoke-web-token-do-not-leak
EOF
python3 "$CONFIG_PY" render --env-file "$WORKDIR/authority-key.env" \
  --output "$WORKDIR/authority-key.json" >"$WORKDIR/authority-key-stdout"
if grep -q "smoke-web-token-do-not-leak" "$WORKDIR/authority-key-stdout"; then
  id42_die "render printed the web token"
fi
python3 - "$WORKDIR/authority-key.json" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1]))
server = doc["tools"]["mcpServers"]["id42-work"]
assert server["env"]["ID42_WORK_AUTHORITY_KEY_PATH"] == "/var/lib/id42-work/keys/authority.key"
assert server["env"]["PYTHONPATH"] == "/opt/id42-agent"
print("authority key path is passed to the MCP child")
PY

python3 - "$CONFIG_PY" "$WORKDIR" <<'PY'
import importlib.util
import json
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("id42_config", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
workdir = Path(sys.argv[2])
password = "smoke-db-password-do-not-leak"
url = f"postgresql://id42:{password}@postgres:5432/id42_work"
base = {
    "TZ": "America/Sao_Paulo",
    "OLLAMA_BASE_URL": "http://host.docker.internal:9/v1",
    "OLLAMA_MODEL": "smoke-model",
    "TELEGRAM_ENABLED": "false",
    "ID42_WORK_ENABLED": "true",
    "NANOBOT_WEB_TOKEN": "smoke-web-token-do-not-leak",
}
postgres_env = dict(base)
postgres_env["ID42_WORK_STORAGE"] = "postgres"
postgres_env["ID42_WORK_DATABASE_URL"] = url
env_path = workdir / "postgres-storage.env"
env_path.write_text("".join(f"{key}={value}\n" for key, value in postgres_env.items()), encoding="utf-8")
rendered = workdir / "postgres-storage.json"
stdout = workdir / "postgres-storage.out"
# render() prints only the output path.
import contextlib
import io
buffer = io.StringIO()
with contextlib.redirect_stdout(buffer):
    mod.render(env_path, rendered)
summary_buffer = io.StringIO()
with contextlib.redirect_stdout(summary_buffer):
    mod.public_summary(env_path, None)
stdout.write_text(buffer.getvalue() + summary_buffer.getvalue(), encoding="utf-8")
if password in stdout.read_text(encoding="utf-8") or url in stdout.read_text(encoding="utf-8"):
    raise SystemExit("render or summary printed the database URL")
document = json.loads(rendered.read_text(encoding="utf-8"))
server = document["tools"]["mcpServers"]["id42-work"]
if server["env"].get("ID42_WORK_STORAGE") != "postgres":
    raise SystemExit("postgres storage was not written for the MCP child")
if server["env"].get("ID42_WORK_DATABASE_URL") != url:
    raise SystemExit("postgres URL was not written for the MCP child")
summary = json.loads(summary_buffer.getvalue())
if summary["env"]["ID42_WORK_STORAGE"] != "postgres":
    raise SystemExit("summary did not report postgres")
if summary["env"]["id42_work_database_url_set"] is not True:
    raise SystemExit("summary did not report that a database URL is set")
if password in json.dumps(summary) or url in json.dumps(summary):
    raise SystemExit("summary included the database URL")
sqlite_env = dict(base)
sqlite_env["ID42_WORK_STORAGE"] = "sqlite"
sqlite_env["ID42_WORK_DATABASE_URL"] = url
sqlite_path = workdir / "sqlite-ignores-url.env"
sqlite_path.write_text("".join(f"{key}={value}\n" for key, value in sqlite_env.items()), encoding="utf-8")
sqlite_doc = mod.build_config(mod.load_env_file(sqlite_path))
sqlite_server = sqlite_doc["tools"]["mcpServers"]["id42-work"]
if "ID42_WORK_STORAGE" in sqlite_server["env"] or "ID42_WORK_DATABASE_URL" in sqlite_server["env"]:
    raise SystemExit("sqlite config copied the database URL")
print("postgres storage is passed to the MCP child and is not printed")
PY

python3 - "$CONFIG_PY" <<'PY'
import importlib.util
import json
import sys

spec = importlib.util.spec_from_file_location("id42_config", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
base = {
    "TZ": "America/Sao_Paulo",
    "OLLAMA_BASE_URL": "http://host.docker.internal:9/v1",
    "OLLAMA_MODEL": "smoke-model",
    "TELEGRAM_ENABLED": "false",
    "NANOBOT_WEB_TOKEN": "smoke-web-token-do-not-leak",
    "ID42_WORK_ENABLED": "false",
    "ODOO_URL": "http://odoo.internal",
    "GITHUB_TOKEN": "smoke-github-token-do-not-leak",
}
off = mod.build_config(base)
assert "mcpServers" not in off["tools"] or "id42-work" not in off["tools"].get("mcpServers", {})
summary = {}
# Flags off must not require credentials, and a later enabled render must not
# copy a GitHub token when a file is configured.
enabled = dict(base)
enabled["ID42_WORK_ENABLED"] = "true"
enabled["ID42_WORK_ODOO_ENABLED"] = "true"
enabled["ODOO_DB"] = "probe"
enabled["ODOO_USER"] = "probe"
enabled["ODOO_API_KEY_FILE"] = "/var/lib/id42-work/odoo.key"
enabled["ID42_WORK_ODOO_READ_MODELS"] = "res.partner"
enabled["ID42_WORK_ODOO_WRITE_MODELS"] = "res.partner"
enabled["ID42_WORK_GITHUB_ENABLED"] = "true"
enabled["ID42_WORK_GITHUB_REPOSITORIES"] = "acme/widgets"
enabled["ID42_WORK_GITHUB_TOKEN_FILE"] = "/var/lib/id42-work/github.token"
del enabled["GITHUB_TOKEN"]
doc = mod.build_config(enabled)
server_env = doc["tools"]["mcpServers"]["id42-work"]["env"]
assert server_env["ID42_WORK_ODOO_ENABLED"] == "true"
assert server_env["ID42_WORK_GITHUB_ENABLED"] == "true"
assert server_env["ODOO_API_KEY_FILE"] == "/var/lib/id42-work/odoo.key"
assert "ODOO_API_KEY" not in server_env
assert "GITHUB_TOKEN" not in server_env
assert "odoo.search" not in json.dumps(server_env)
both = dict(enabled)
both["GITHUB_TOKEN"] = "smoke-github-token-do-not-leak"
try:
    mod.build_config(both)
except mod.ConfigError as exc:
    if "not both" not in str(exc):
        raise SystemExit(f"unexpected connector error: {exc}")
else:
    raise SystemExit("github token and token file were both accepted")
missing = dict(enabled)
del missing["ODOO_URL"]
try:
    mod.build_config(missing)
except mod.ConfigError as exc:
    if "ODOO_URL" not in str(exc):
        raise SystemExit(f"unexpected odoo error: {exc}")
else:
    raise SystemExit("odoo without a URL was accepted")
if "smoke-github-token-do-not-leak" in json.dumps(doc):
    raise SystemExit("rendered config contained the github token")
print("connector settings stay out until enabled and do not print secrets")
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

echo "== connector registration =="
ID42_ENV_FILE=$ENV_FILE COMPOSE_PROJECT_NAME=$PROJECT \
  docker compose --env-file "$ENV_FILE" -f "$ROOT/docker-compose.yml" run --rm --no-deps -T --user 1000 \
  --entrypoint /opt/id42-work/venv/bin/python \
  -e PYTHONPATH=/opt/id42-agent \
  nanobot-gateway -m id42_agent.connector_probe | tee "$WORKDIR/connector-probe.out"
sed -i 's/\r$//' "$WORKDIR/connector-probe.out"
grep -q "connector-probe-ok" "$WORKDIR/connector-probe.out" \
  || id42_die "connector probe did not pass"
if grep -q "probe-token-not-secret" "$WORKDIR/connector-probe.out" \
  || grep -q "odoo-probe-key" "$WORKDIR/connector-probe.out"
then
  id42_die "connector probe printed a credential"
fi
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
grep -q "execution_pages=2" "$WORKDIR/persist-check.out" \
  || id42_die "list_executions did not return one page per execution"
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

echo "== id42-work operator UI lists the pending approval =="
web_port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')
web_token=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
printf 'ID42_WORK_WEB_TOKEN=%s\nID42_WORK_WEB_PUBLISH_PORT=%s\n' "$web_token" "$web_port" >>"$ENV_FILE"
ID42_ENV_FILE=$ENV_FILE COMPOSE_PROJECT_NAME=$PROJECT \
  docker compose --env-file "$ENV_FILE" --profile id42-work-web -f "$ROOT/docker-compose.yml" \
  up -d id42-work-web
web_id=""
web_health=""
for _ in $(seq 1 40); do
  web_id=$(ID42_ENV_FILE=$ENV_FILE COMPOSE_PROJECT_NAME=$PROJECT \
    docker compose --env-file "$ENV_FILE" --profile id42-work-web -f "$ROOT/docker-compose.yml" \
    ps -q id42-work-web || true)
  web_health=$(docker inspect --format '{{.State.Health.Status}}' "$web_id" 2>/dev/null || true)
  if [[ "$web_health" == "healthy" ]]; then
    break
  fi
  sleep 2
done
capture_web_logs() {
  ID42_ENV_FILE=$ENV_FILE COMPOSE_PROJECT_NAME=$PROJECT \
    docker compose --env-file "$ENV_FILE" --profile id42-work-web -f "$ROOT/docker-compose.yml" \
    logs id42-work-web >"$WORKDIR/web.log" || true
  if grep -q "$web_token" "$WORKDIR/web.log"; then
    id42_die "operator UI logs contained the token"
  fi
}
if [[ "$web_health" != "healthy" ]]; then
  capture_web_logs
  tail -n 80 "$WORKDIR/web.log" >&2 || true
  id42_die "operator UI did not become healthy"
fi
web_bind=$(docker inspect --format '{{range $p, $conf := .NetworkSettings.Ports}}{{if eq $p "8767/tcp"}}{{range $conf}}{{.HostIp}}{{end}}{{end}}{{end}}' "$web_id")
[[ "$web_bind" == "127.0.0.1" ]] || id42_die "operator UI is not published on 127.0.0.1"
ID42_WORK_WEB_TOKEN=$web_token ID42_WORK_WEB_URL="http://127.0.0.1:${web_port}" \
  python3 - "$WORKDIR/web-ui.out" <<'PY'
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

out_path, = sys.argv[1:]
base = os.environ["ID42_WORK_WEB_URL"].rstrip("/")
token = os.environ["ID42_WORK_WEB_TOKEN"]
origin = base


class Response:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Keep the status from / and from sign-in. Both answer 303."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


_OPENER = urllib.request.build_opener(_NoRedirect)


def request(method, path, *, headers=None, data=None):
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers or {})
    try:
        with _OPENER.open(req, timeout=15) as response:
            return Response(response.status, response.headers, response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read() if exc.fp is not None else b""
        return Response(exc.code, exc.headers, body)


anonymous = request("GET", "/")
if anonymous.status not in {302, 303, 307, 401}:
    raise SystemExit(f"anonymous home status {anonymous.status}")
login = request("GET", "/login")
if login.status != 200 or b"Sign in" not in login.body:
    raise SystemExit(f"login page status {login.status}")
sheet = request("GET", "/static/operator.css")
if sheet.status != 200 or b"text/css" not in sheet.headers.get("Content-Type", "").encode():
    raise SystemExit(f"stylesheet status {sheet.status}")
match = re.search(br'name="csrf" value="([^"]+)"', login.body)
if match is None:
    raise SystemExit("login page had no csrf field")
csrf = match.group(1).decode("ascii")
cookie = login.headers.get("Set-Cookie", "")
if "id42_csrf=" not in cookie:
    raise SystemExit("login page set no csrf cookie")
form = urllib.parse.urlencode(
    {"csrf": csrf, "token": token, "actor_id": "id42-operator", "credential": ""}
).encode("ascii")
signed = request(
    "POST",
    "/login",
    data=form,
    headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": origin,
        "Cookie": cookie.split(";", 1)[0],
    },
)
if signed.status not in {302, 303}:
    raise SystemExit(f"sign-in status {signed.status}")
session = ""
for value in signed.headers.get_all("Set-Cookie") or []:
    if value.startswith("id42_session="):
        session = value.split(";", 1)[0]
if not session:
    raise SystemExit("sign-in set no session cookie")
home = request("GET", "/", headers={"Cookie": session})
text = home.body.decode("utf-8", errors="replace")
if home.status != 200 or ">note<" not in text or "PENDING" not in text:
    raise SystemExit(f"home did not list the pending note (status {home.status})")
reconcile = request("GET", "/reconcile", headers={"Cookie": session})
if reconcile.status != 200 or b"Reconciliation" not in reconcile.body:
    raise SystemExit(f"reconcile page status {reconcile.status}")
report = "\n".join(
    [
        f"anonymous={anonymous.status}",
        "login=200",
        f"sign_in={signed.status}",
        "home=pending-note",
        "reconcile=200",
        "",
    ]
)
if token in report or token in text:
    raise SystemExit("operator UI page contained the token")
open(out_path, "w", encoding="utf-8").write(report)
print("operator UI listed the pending note")
PY
if grep -q "$web_token" "$WORKDIR/web-ui.out"; then
  id42_die "operator UI check output contained the token"
fi
grep -q "home=pending-note" "$WORKDIR/web-ui.out"
capture_web_logs
ID42_ENV_FILE=$ENV_FILE COMPOSE_PROJECT_NAME=$PROJECT \
  docker compose --env-file "$ENV_FILE" --profile id42-work-web -f "$ROOT/docker-compose.yml" \
  stop id42-work-web >/dev/null
echo "OK operator UI serves on loopback and lists the pending approval"

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

echo "== authority key survives an MCP host restart =="
authority_cmd() {
  gateway_run --entrypoint /opt/id42-work/venv/bin/python \
    -e "PYTHONPATH=/opt/id42-agent" \
    nanobot-gateway - "$@" <"$ROOT/scripts/lib/authority_restart.py"
}

auth_field() {
  awk -F= -v key="$1" '$1 == key { print substr($0, index($0, "=") + 1); exit }' "$2"
}

probe_cmd start-note "smoke-authority-note" "smoke-authority" | tee "$WORKDIR/authority-start.out"
sed -i 's/\r$//' "$WORKDIR/authority-start.out"
authority_exec=$(auth_field execution_id "$WORKDIR/authority-start.out")
authority_status=$(auth_field status "$WORKDIR/authority-start.out")
[[ -n "$authority_exec" && "$authority_status" == "WAITING_APPROVAL" ]] \
  || id42_die "authority-key note did not wait for approval"

authority_cmd seal "$authority_exec" | tee "$WORKDIR/authority-seal.out"
sed -i 's/\r$//' "$WORKDIR/authority-seal.out"
if grep -q 'id42a1\.' "$WORKDIR/authority-seal.out"; then
  id42_die "seal printed an actor credential"
fi
grep -qx "sealed" "$WORKDIR/authority-seal.out" || id42_die "operator seal was not stored"
[[ "$(auth_field execution_status "$WORKDIR/authority-seal.out")" == "COMPLETED" ]] \
  || id42_die "operator approval did not resume to COMPLETED"
[[ "$(auth_field decision_actor_id "$WORKDIR/authority-seal.out")" == "id42-operator" ]] \
  || id42_die "operator approval did not record id42-operator"
[[ "$(auth_field key_mode "$WORKDIR/authority-seal.out")" == "600" ]] \
  || id42_die "authority key file was not created mode 0600"
seal_key=$(auth_field signing_key_id "$WORKDIR/authority-seal.out")
[[ "$seal_key" =~ ^[0-9a-f]{16}$ ]] || id42_die "signing key id was not printed"
authority_approval=$(auth_field approval_id "$WORKDIR/authority-seal.out")
[[ -n "$authority_approval" ]] || id42_die "seal did not print an approval id"

probe_cmd execution-status "$authority_exec" | tee "$WORKDIR/authority-restart.out"
sed -i 's/\r$//' "$WORKDIR/authority-restart.out"
grep -qx "status=COMPLETED" "$WORKDIR/authority-restart.out" \
  || id42_die "restarted MCP host did not keep the approved note"

authority_cmd verify "$authority_approval" | tee "$WORKDIR/authority-verify.out"
sed -i 's/\r$//' "$WORKDIR/authority-verify.out"
if grep -q 'id42a1\.' "$WORKDIR/authority-verify.out"; then
  id42_die "verify printed an actor credential"
fi
grep -qx "verified" "$WORKDIR/authority-verify.out" \
  || id42_die "operator seal did not verify in a new process"
[[ "$(auth_field signing_key_id "$WORKDIR/authority-verify.out")" == "$seal_key" ]] \
  || id42_die "a new process loaded a different authority key"
[[ "$(auth_field approval_status "$WORKDIR/authority-verify.out")" == "CONSUMED" ]] \
  || id42_die "stored approval was not consumed after resume"
[[ "$(auth_field decision_actor_id "$WORKDIR/authority-verify.out")" == "id42-operator" ]] \
  || id42_die "verified operator does not match the stored decision"

ID42_ENV_FILE=$ENV_FILE COMPOSE_PROJECT_NAME=$PROJECT \
  "$ROOT/scripts/work-rotate-key.sh" | tee "$WORKDIR/authority-rotate.out"
sed -i 's/\r$//' "$WORKDIR/authority-rotate.out"
if grep -q 'id42a1\.' "$WORKDIR/authority-rotate.out"; then
  id42_die "rotation printed an actor credential"
fi
rotated_key=$(auth_field signing_key_id "$WORKDIR/authority-rotate.out")
[[ "$rotated_key" =~ ^[0-9a-f]{16}$ ]] || id42_die "rotation did not print a key id"
[[ "$rotated_key" != "$seal_key" ]] || id42_die "rotation kept the same signing key id"

authority_cmd verify "$authority_approval" | tee "$WORKDIR/authority-verify-rotated.out"
sed -i 's/\r$//' "$WORKDIR/authority-verify-rotated.out"
grep -qx "verified" "$WORKDIR/authority-verify-rotated.out" \
  || id42_die "operator seal did not verify after key rotation"
[[ "$(auth_field signing_key_id "$WORKDIR/authority-verify-rotated.out")" == "$rotated_key" ]] \
  || id42_die "rotated key id did not load in the next process"
authority_cmd wipe | tee "$WORKDIR/authority-wipe.out"
sed -i 's/\r$//' "$WORKDIR/authority-wipe.out"
grep -qx "wiped" "$WORKDIR/authority-wipe.out" || id42_die "actor credentials were not removed"
echo "OK operator approval verifies after the MCP host restarts"

authority_key=$(id42_authority_key_path "$ENV_FILE")
loose_fail() {
  gateway_run --entrypoint /bin/sh nanobot-gateway -c "chmod 600 ${authority_key}" || true
  id42_die "$1"
}
gateway_run --entrypoint /bin/sh nanobot-gateway -c "chmod 644 ${authority_key}"
if authority_cmd refuse >"$WORKDIR/authority-loose.out" 2>"$WORKDIR/authority-loose.err"; then
  :
else
  loose_fail "a mode 0644 authority key was not refused"
fi
sed -i 's/\r$//' "$WORKDIR/authority-loose.out" || loose_fail "could not read the refuse result"
grep -qx "refused reason=permissions" "$WORKDIR/authority-loose.out" \
  || loose_fail "a mode 0644 authority key was not refused for permissions"
if grep -q 'id42a1\.' "$WORKDIR/authority-loose.out" "$WORKDIR/authority-loose.err"; then
  loose_fail "the permission refusal printed an actor credential"
fi
if probe_cmd >"$WORKDIR/authority-loose-mcp.out" 2>"$WORKDIR/authority-loose-mcp.err"; then
  loose_fail "MCP host accepted a mode 0644 authority key"
fi
if ! grep -q "permissions are too open" "$WORKDIR/authority-loose-mcp.err" \
  && ! grep -q "permissions are too open" "$WORKDIR/authority-loose-mcp.out"; then
  cat "$WORKDIR/authority-loose-mcp.err" >&2 || true
  loose_fail "MCP host did not report that the authority key permissions are too open"
fi
gateway_run --entrypoint /bin/sh nanobot-gateway -c "chmod 600 ${authority_key}"
probe_cmd | tee "$WORKDIR/authority-restored.out"
sed -i 's/\r$//' "$WORKDIR/authority-restored.out"
grep -q "mcp-stdio-ok" "$WORKDIR/authority-restored.out" \
  || id42_die "MCP host did not start again after the authority key mode was restored"
echo "OK a mode 0644 authority key is refused"

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

# SIGKILL during notes.append or during validation, then a new process.
# Both storage backends call this. scan_outputs=yes redacts the database URL.
governed_process_crashes() {
  local env_file=$1
  local project=$2
  local prefix=$3
  local scan_outputs=$4

  crash_py() {
    local name=$1
    shift
    local file=$WORKDIR/${prefix}-${name}.out
    local errors=$WORKDIR/${prefix}-${name}.err
    if ! COMPOSE_PROJECT_NAME=$project \
      docker compose --env-file "$env_file" -f "$ROOT/docker-compose.yml" \
      run --rm --no-deps -T --user 1000 \
      --entrypoint /opt/id42-work/venv/bin/python \
      -e PYTHONPATH=/opt/id42-agent \
      -v "$ROOT/scripts/lib/process_crash.py:/tmp/process_crash.py:ro" \
      nanobot-gateway /tmp/process_crash.py "$@" >"$file" 2>"$errors"
    then
      cat "$errors" >&2 || true
      id42_die "process crash ${name} failed"
    fi
    sed -i 's/\r$//' "$file" "$errors" || true
    if [[ "$scan_outputs" == "yes" ]]; then
      scan_secrets "$file" "$errors"
    fi
    if grep -q 'id42a1\.' "$file" "$errors"; then
      id42_die "process crash ${name} printed an actor credential"
    fi
    printf '%s\n' "$file"
  }

  host_script() {
    local name=$1
    local script=$2
    shift 2
    local file=$WORKDIR/${prefix}-${name}.out
    local errors=$WORKDIR/${prefix}-${name}.err
    if ! ID42_ENV_FILE=$env_file COMPOSE_PROJECT_NAME=$project \
      "$ROOT/scripts/${script}" "$@" >"$file" 2>"$errors"
    then
      cat "$errors" >&2 || true
      id42_die "process crash ${name} failed"
    fi
    sed -i 's/\r$//' "$file" "$errors" || true
    if [[ "$scan_outputs" == "yes" ]]; then
      scan_secrets "$file" "$errors"
    fi
    if grep -q 'id42a1\.' "$file" "$errors"; then
      id42_die "process crash ${name} printed an actor credential"
    fi
    printf '%s\n' "$file"
  }

  assert_killed_claim() {
    local file=$1
    require_field "$file" child_signal 9
    require_field "$file" before_approval_consumed 1
    require_field "$file" before_approval_granted 0
    require_field "$file" before_approval_pending 0
    require_field "$file" before_requires_reconciliation false
    require_field "$file" before_invoked 1
    require_field "$file" before_replayed 0
    require_field "$file" before_probe none
    require_field "$file" before_outcome false
    require_field "$file" notes_mode 600
  }

  local report exec_id operation_id byte_count approval_id

  echo "== ${prefix}: SIGKILL after the note line, probe APPLIED =="
  report=$(crash_py probe-after drive probe-after "${prefix}-probe-applied" "${prefix}-probe-applied")
  assert_killed_claim "$report"
  require_field "$report" before_status RUNNING
  require_field "$report" before_journal_status IN_FLIGHT
  require_field "$report" before_operation_notes 1
  require_field "$report" before_validation_started 0
  require_field "$report" after_status COMPLETED
  require_field "$report" after_journal_status SUCCEEDED
  require_field "$report" after_probe APPLIED
  require_field "$report" after_operation_notes 1
  require_field "$report" after_invoked 1
  require_field "$report" after_replayed 1
  require_field "$report" after_requires_reconciliation false
  require_field "$report" after_outcome true
  exec_id=$(crash_field execution_id "$report")
  operation_id=$(crash_field operation_id "$report")
  report=$(crash_py probe-after-again resume "$exec_id")
  require_field "$report" state_status COMPLETED
  require_field "$report" state_operation_notes 1
  require_field "$report" state_invoked 1
  require_field "$report" state_operation_id "$operation_id"

  echo "== ${prefix}: SIGKILL before the note line, probe NOT_APPLIED =="
  report=$(crash_py probe-before drive probe-before "${prefix}-probe-missing" "${prefix}-probe-missing")
  assert_killed_claim "$report"
  require_field "$report" before_status RUNNING
  require_field "$report" before_journal_status IN_FLIGHT
  require_field "$report" before_operation_notes 0
  require_field "$report" after_status WAITING_APPROVAL
  require_field "$report" after_status_reason awaiting_action_approval
  require_field "$report" after_journal_status PENDING_APPROVAL
  require_field "$report" after_probe NOT_APPLIED
  require_field "$report" after_operation_notes 0
  require_field "$report" after_invoked 1
  require_field "$report" after_replayed 0
  require_field "$report" after_requires_reconciliation false
  require_field "$report" after_approval_pending 1
  exec_id=$(crash_field execution_id "$report")
  operation_id=$(crash_field operation_id "$report")
  [[ "$(crash_field after_operation_id "$report")" == "$operation_id" ]] \
    || id42_die "NOT_APPLIED changed the operation id"
  approval_id=$(crash_field after_pending_approval_id "$report")
  [[ -n "$approval_id" ]] || id42_die "NOT_APPLIED did not request approval"
  report=$(host_script probe-before-list work-approve.sh list)
  require_pending_line "$report" "$exec_id"
  report=$(host_script probe-before-approve work-approve.sh approve "$approval_id" --reason "retry once")
  grep -q "execution_status=COMPLETED" "$report" \
    || id42_die "the single NOT_APPLIED retry did not complete"
  report=$(crash_py probe-before-done inspect "$exec_id")
  require_field "$report" state_status COMPLETED
  require_field "$report" state_journal_status SUCCEEDED
  require_field "$report" state_operation_notes 1
  require_field "$report" state_invoked 2
  require_field "$report" state_operation_id "$operation_id"
  report=$(crash_py probe-before-again resume "$exec_id")
  require_field "$report" state_operation_notes 1
  require_field "$report" state_invoked 2
  require_field "$report" state_operation_id "$operation_id"

  echo "== ${prefix}: SIGKILL after the line, operator applied =="
  report=$(crash_py operator-after drive operator-after "${prefix}-op-applied" "${prefix}-op-applied")
  assert_killed_claim "$report"
  require_field "$report" before_status RUNNING
  require_field "$report" before_journal_status IN_FLIGHT
  require_field "$report" before_operation_notes 1
  require_field "$report" after_status FAILED
  require_field "$report" after_status_reason uncertain_outcome
  require_field "$report" after_requires_reconciliation true
  require_field "$report" after_journal_status UNCERTAIN
  require_field "$report" after_operation_notes 1
  require_field "$report" after_invoked 1
  require_field "$report" after_replayed 0
  require_field "$report" after_probe none
  exec_id=$(crash_field execution_id "$report")
  operation_id=$(crash_field operation_id "$report")
  byte_count=$(crash_field byte_count "$report")
  [[ "$byte_count" =~ ^[0-9]+$ ]] || id42_die "byte_count is not an integer"
  report=$(host_script operator-after-list work-reconcile.sh list)
  grep -q "execution_id=${exec_id} " "$report" \
    || id42_die "work-reconcile.sh list missed the killed append"
  grep -q "operation_id=${operation_id}" "$report" \
    || id42_die "work-reconcile.sh list changed the operation id"
  report=$(host_script operator-after-show work-reconcile.sh show "$exec_id" append-note)
  grep -q '"journal_status": "UNCERTAIN"' "$report" \
    || id42_die "work-reconcile.sh show did not report UNCERTAIN"
  grep -q "\"operation_id\": \"${operation_id}\"" "$report" \
    || id42_die "work-reconcile.sh show changed the operation id"
  report=$(host_script operator-after-applied work-reconcile.sh applied "$exec_id" append-note \
    --reason "the line is in the file" \
    --result "{\"appended\": true, \"byte_count\": ${byte_count}}" \
    --resume)
  grep -q "applied execution_id=${exec_id} " "$report" \
    || id42_die "work-reconcile.sh applied did not report the execution"
  grep -q "journal_status=SUCCEEDED" "$report" \
    || id42_die "work-reconcile.sh applied did not record the call"
  grep -q "execution_status=COMPLETED" "$report" \
    || id42_die "work-reconcile.sh applied did not resume to COMPLETED"
  grep -q "resumed=true" "$report" \
    || id42_die "work-reconcile.sh applied did not resume"
  grep -q "decision_actor_id=id42-operator" "$report" \
    || id42_die "work-reconcile.sh applied did not record id42-operator"
  report=$(crash_py operator-after-done inspect "$exec_id")
  require_field "$report" state_status COMPLETED
  require_field "$report" state_operation_notes 1
  require_field "$report" state_invoked 1
  require_field "$report" state_replayed 1
  require_field "$report" state_operation_id "$operation_id"
  report=$(host_script operator-after-clear work-reconcile.sh list)
  if grep -q "execution_id=${exec_id}" "$report"; then
    id42_die "work-reconcile.sh list still shows the applied call"
  fi

  echo "== ${prefix}: SIGKILL before the line, operator not-applied =="
  report=$(crash_py operator-before drive operator-before "${prefix}-op-missing" "${prefix}-op-missing")
  assert_killed_claim "$report"
  require_field "$report" before_status RUNNING
  require_field "$report" before_journal_status IN_FLIGHT
  require_field "$report" before_operation_notes 0
  require_field "$report" after_status FAILED
  require_field "$report" after_status_reason uncertain_outcome
  require_field "$report" after_requires_reconciliation true
  require_field "$report" after_journal_status UNCERTAIN
  require_field "$report" after_operation_notes 0
  require_field "$report" after_invoked 1
  require_field "$report" after_probe none
  exec_id=$(crash_field execution_id "$report")
  operation_id=$(crash_field operation_id "$report")
  report=$(host_script operator-before-decide work-reconcile.sh not-applied "$exec_id" append-note \
    --reason "nothing was written" \
    --resume)
  grep -q "not-applied execution_id=${exec_id} " "$report" \
    || id42_die "work-reconcile.sh not-applied did not report the execution"
  grep -q "execution_status=WAITING_APPROVAL" "$report" \
    || id42_die "work-reconcile.sh not-applied did not ask for approval again"
  grep -q "journal_status=PENDING_APPROVAL" "$report" \
    || id42_die "work-reconcile.sh not-applied did not keep the same action pending"
  grep -q "resumed=true" "$report" \
    || id42_die "work-reconcile.sh not-applied did not resume"
  grep -q "decision_actor_id=id42-operator" "$report" \
    || id42_die "work-reconcile.sh not-applied did not record id42-operator"
  report=$(crash_py operator-before-waiting inspect "$exec_id")
  require_field "$report" state_status WAITING_APPROVAL
  require_field "$report" state_operation_notes 0
  require_field "$report" state_invoked 1
  require_field "$report" state_operation_id "$operation_id"
  report=$(host_script operator-before-list work-approve.sh list)
  approval_id=$(approval_id_for "$report" "$exec_id") \
    || id42_die "the not-applied retry did not ask for approval"
  require_pending_line "$report" "$exec_id"
  report=$(host_script operator-before-approve work-approve.sh approve "$approval_id" --reason "retry once")
  grep -q "execution_status=COMPLETED" "$report" \
    || id42_die "the single not-applied retry did not complete"
  report=$(crash_py operator-before-done inspect "$exec_id")
  require_field "$report" state_status COMPLETED
  require_field "$report" state_journal_status SUCCEEDED
  require_field "$report" state_operation_notes 1
  require_field "$report" state_invoked 2
  require_field "$report" state_operation_id "$operation_id"
  report=$(crash_py operator-before-again resume "$exec_id")
  require_field "$report" state_operation_notes 1
  require_field "$report" state_invoked 2
  require_field "$report" state_operation_id "$operation_id"

  echo "== ${prefix}: SIGKILL during validation =="
  report=$(crash_py validating drive validating "${prefix}-validating" "${prefix}-validating")
  assert_killed_claim "$report"
  require_field "$report" before_status VALIDATING
  require_field "$report" before_journal_status SUCCEEDED
  require_field "$report" before_operation_notes 1
  require_field "$report" before_validation_started 1
  require_field "$report" before_validation_report 0
  require_field "$report" after_status COMPLETED
  require_field "$report" after_journal_status SUCCEEDED
  require_field "$report" after_operation_notes 1
  require_field "$report" after_invoked 1
  require_field "$report" after_replayed 1
  require_field "$report" after_validation_started 1
  require_field "$report" after_validation_report 1
  require_field "$report" after_outcome true
  require_field "$report" after_requires_reconciliation false
  require_field "$report" after_approval_pending 0
  require_field "$report" after_approval_consumed 1
  exec_id=$(crash_field execution_id "$report")
  operation_id=$(crash_field operation_id "$report")
  [[ "$(crash_field after_operation_id "$report")" == "$operation_id" ]] \
    || id42_die "validation recovery changed the operation id"
  report=$(host_script validating-reconcile work-reconcile.sh list)
  if grep -q "execution_id=${exec_id}" "$report"; then
    id42_die "validation recovery left a call for reconcile"
  fi
  report=$(host_script validating-approvals work-approve.sh list)
  if grep -q "execution_id=${exec_id}" "$report"; then
    id42_die "validation recovery asked for another approval"
  fi
  report=$(crash_py validating-again resume "$exec_id")
  require_field "$report" state_status COMPLETED
  require_field "$report" state_operation_notes 1
  require_field "$report" state_invoked 1
  require_field "$report" state_operation_id "$operation_id"
  report=$(host_script crash-reconcile-clear work-reconcile.sh list)
  grep -qx "uncertain-none" "$report" \
    || id42_die "a process-death call was still uncertain"
}

echo "== id42-work process death =="
governed_process_crashes "$ENV_FILE" "$PROJECT" "sqlite" "no"
echo "OK process death recovers on sqlite"

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
if ! grep -q "OK: id42-work storage backend is sqlite" "$WORKDIR/doctor.out"; then
  cat "$WORKDIR/doctor.err" >&2
  id42_die "doctor did not report the sqlite backend"
fi
if ! grep -q "OK: id42-work store is writable, mounted, and opens SQLite" "$WORKDIR/doctor.out"; then
  cat "$WORKDIR/doctor.err" >&2
  id42_die "doctor did not confirm the id42-work store"
fi
if ! grep -q "OK: id42-work storage config has no MCP server env to compare" "$WORKDIR/doctor.out"; then
  cat "$WORKDIR/doctor.err" >&2
  id42_die "doctor did not report the absent storage config"
fi
if ! grep -q "OK: id42-work list_works is paged" "$WORKDIR/doctor.out"; then
  cat "$WORKDIR/doctor.err" >&2
  id42_die "doctor did not confirm paged list_works"
fi
if ! grep -q "OK: id42-work lists echo, note, and note.list" "$WORKDIR/doctor.out"; then
  cat "$WORKDIR/doctor.err" >&2
  id42_die "doctor did not confirm the default works"
fi
if ! grep -q "OK: id42-work pin is ae07623aa67c0b5d0535cb58f6a2e2b204736e50" "$WORKDIR/doctor.out"; then
  cat "$WORKDIR/doctor.err" >&2
  id42_die "doctor did not confirm the id42-work pin"
fi
if ! grep -q "OK: id42-work-odoo pin is 3797544d8a3580657548d000110915578efe87c3" "$WORKDIR/doctor.out"; then
  cat "$WORKDIR/doctor.err" >&2
  id42_die "doctor did not confirm the id42-work-odoo pin"
fi
if ! grep -q "OK: id42-work-github pin is 2e2a23d8f64d6c2313523bbf7ee24554fb3a4cb3" "$WORKDIR/doctor.out"; then
  cat "$WORKDIR/doctor.err" >&2
  id42_die "doctor did not confirm the id42-work-github pin"
fi
if ! grep -q "OK: authority key file is mode 0600" "$WORKDIR/doctor.out"; then
  cat "$WORKDIR/doctor.err" >&2
  id42_die "doctor did not confirm the authority key file"
fi
if ! grep -q "OK: id42-work approvals command is available" "$WORKDIR/doctor.out"; then
  cat "$WORKDIR/doctor.err" >&2
  id42_die "doctor did not confirm the id42-work approvals command"
fi
if ! grep -q "OK: id42-work web command is available" "$WORKDIR/doctor.out"; then
  cat "$WORKDIR/doctor.err" >&2
  id42_die "doctor did not confirm the id42-work web command"
fi
if ! grep -q "OK: id42-work operator UI is not running" "$WORKDIR/doctor.out"; then
  cat "$WORKDIR/doctor.err" >&2
  id42_die "doctor did not confirm the operator UI was stopped"
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

echo "== id42-work postgres profile =="
PG_PROJECT=id42pg$$
PG_ENV=$WORKDIR/postgres-runtime.env
python3 - "$PG_ENV" <<'PY'
import secrets
import sys
from pathlib import Path

password = secrets.token_hex(24)
user = "id42"
database = "id42_work"
url = f"postgresql://{user}:{password}@postgres:5432/{database}"
text = "\n".join(
    [
        "TZ=America/Sao_Paulo",
        "OLLAMA_BASE_URL=http://host.docker.internal:9/v1",
        "OLLAMA_MODEL=smoke-model",
        "TELEGRAM_ENABLED=false",
        "TELEGRAM_BOT_TOKEN=",
        "TELEGRAM_ALLOWED_USERS=",
        "NANOBOT_WEB_TOKEN=smoke-web-token-do-not-leak",
        "ID42_WORK_ENABLED=true",
        "ID42_WORK_STORAGE=postgres",
        f"ID42_WORK_DATABASE_URL={url}",
        f"ID42_POSTGRES_USER={user}",
        f"ID42_POSTGRES_PASSWORD={password}",
        f"ID42_POSTGRES_DB={database}",
        "",
    ]
)
path = Path(sys.argv[1])
path.write_text(text, encoding="utf-8")
path.chmod(0o600)
PY

scan_secrets() {
  python3 - "$PG_ENV" "$@" <<'PY'
import sys
from pathlib import Path

found = {}
for raw in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    if not raw or raw.startswith("#") or "=" not in raw:
        continue
    key, value = raw.split("=", 1)
    found[key] = value
secrets = [found.get("ID42_WORK_DATABASE_URL", ""), found.get("ID42_POSTGRES_PASSWORD", "")]
secrets = [item for item in secrets if item]
failed = False
for name in sys.argv[2:]:
    text = Path(name).read_text(encoding="utf-8", errors="replace")
    if any(secret in text for secret in secrets):
        print(f"id42-agent: {name} contained a database secret", file=sys.stderr)
        failed = True
if failed:
    raise SystemExit(1)
PY
}

pg_compose() {
  COMPOSE_PROJECT_NAME=$PG_PROJECT \
    docker compose --env-file "$PG_ENV" --profile postgres -f "$ROOT/docker-compose.yml" "$@"
}

pg_run() {
  COMPOSE_PROJECT_NAME=$PG_PROJECT \
    docker compose --env-file "$PG_ENV" -f "$ROOT/docker-compose.yml" \
    run --rm --no-deps -T --user 1000 "$@"
}

ID42_ENV_FILE=$ENV_FILE COMPOSE_PROJECT_NAME=$PROJECT \
  docker compose --env-file "$ENV_FILE" -f "$ROOT/docker-compose.yml" stop nanobot-gateway

ID42_ENV_FILE=$PG_ENV COMPOSE_PROJECT_NAME=$PG_PROJECT \
  "$ROOT/scripts/bootstrap.sh" >"$WORKDIR/pg-bootstrap.log"
scan_secrets "$WORKDIR/pg-bootstrap.log"
grep -q "Initialized config.json" "$WORKDIR/pg-bootstrap.log"

pg_compose up -d postgres
pg_ready=0
for _ in $(seq 1 60); do
  pg_id=$(pg_compose ps -q postgres || true)
  pg_health=""
  if [[ -n "$pg_id" ]]; then
    pg_health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$pg_id" || true)
  fi
  if [[ "$pg_health" == "healthy" ]]; then
    pg_ready=1
    break
  fi
  sleep 2
done
if [[ "$pg_ready" != "1" ]]; then
  pg_compose logs >"$WORKDIR/pg-logs.txt" || true
  scan_secrets "$WORKDIR/pg-logs.txt" || id42_die "postgres logs contained a database secret"
  tail -n 80 "$WORKDIR/pg-logs.txt" >&2 || true
  id42_die "postgres did not become ready"
fi

pg_run --entrypoint /opt/id42-work/venv/bin/python \
  -e "PYTHONPATH=/opt/id42-agent" \
  nanobot-gateway - <<'PY' | tee "$WORKDIR/pg-migrate.out"
import id42_agent.mcp_host as host

backend, version = host.storage_status()
if backend != "postgres" or not isinstance(version, int) or version < 1:
    raise SystemExit(f"schema is {backend} {version}")
print(f"migrated backend={backend} schema={version}")
PY
sed -i 's/\r$//' "$WORKDIR/pg-migrate.out"
scan_secrets "$WORKDIR/pg-migrate.out"
grep -q '^migrated backend=postgres ' "$WORKDIR/pg-migrate.out" \
  || id42_die "postgres schema was not created before the gateway started"

pg_compose up -d nanobot-gateway
gw_ready=0
for _ in $(seq 1 60); do
  code=$(curl --silent --output "$WORKDIR/pg-health.json" --write-out "%{http_code}" --max-time 5 http://127.0.0.1:18790/health || true)
  if [[ "$code" == "200" ]] && python3 - "$WORKDIR/pg-health.json" <<'PY'
import json, sys
payload = json.load(open(sys.argv[1]))
raise SystemExit(0 if payload.get("ready") is True and payload.get("status") == "ok" else 1)
PY
  then
    gw_ready=1
    break
  fi
  sleep 5
done
if [[ "$gw_ready" != "1" ]]; then
  pg_compose logs >"$WORKDIR/pg-logs.txt" || true
  scan_secrets "$WORKDIR/pg-logs.txt" || id42_die "postgres logs contained a database secret"
  tail -n 80 "$WORKDIR/pg-logs.txt" >&2 || true
  id42_die "postgres-profile gateway did not become ready"
fi

pg_run --entrypoint /app/.venv/bin/python nanobot-gateway - start-note "pg-approve-note" "pg-approve" \
  <"$ROOT/scripts/lib/mcp_probe.py" | tee "$WORKDIR/pg-approve-start.out"
sed -i 's/\r$//' "$WORKDIR/pg-approve-start.out"
scan_secrets "$WORKDIR/pg-approve-start.out"
pg_exec=$(awk -F= '/^execution_id=/{print $2}' "$WORKDIR/pg-approve-start.out")
pg_status=$(awk -F= '/^status=/{print $2}' "$WORKDIR/pg-approve-start.out")
[[ -n "$pg_exec" && "$pg_status" == "WAITING_APPROVAL" ]] \
  || id42_die "postgres note did not wait for approval"

ID42_ENV_FILE=$PG_ENV COMPOSE_PROJECT_NAME=$PG_PROJECT \
  "$ROOT/scripts/work-approve.sh" list | tee "$WORKDIR/pg-approve-list.out"
sed -i 's/\r$//' "$WORKDIR/pg-approve-list.out"
scan_secrets "$WORKDIR/pg-approve-list.out"
pg_approval=$(approval_id_for "$WORKDIR/pg-approve-list.out" "$pg_exec") \
  || id42_die "work-approve.sh list did not include the postgres note"
require_pending_line "$WORKDIR/pg-approve-list.out" "$pg_exec"
ID42_ENV_FILE=$PG_ENV COMPOSE_PROJECT_NAME=$PG_PROJECT \
  "$ROOT/scripts/work-approve.sh" approve "$pg_approval" --reason "postgres" \
  | tee "$WORKDIR/pg-approve-decision.out"
sed -i 's/\r$//' "$WORKDIR/pg-approve-decision.out"
scan_secrets "$WORKDIR/pg-approve-decision.out"
grep -q "execution_status=COMPLETED" "$WORKDIR/pg-approve-decision.out" \
  || id42_die "postgres approval did not resume to COMPLETED"
grep -q "resumed=true" "$WORKDIR/pg-approve-decision.out" \
  || id42_die "postgres approval did not resume in a second process"
grep -q "decision_actor_id=id42-operator" "$WORKDIR/pg-approve-decision.out" \
  || id42_die "postgres approval did not record id42-operator"
pg_run --entrypoint /app/.venv/bin/python nanobot-gateway - execution-status "$pg_exec" \
  <"$ROOT/scripts/lib/mcp_probe.py" | tee "$WORKDIR/pg-approve-status.out"
sed -i 's/\r$//' "$WORKDIR/pg-approve-status.out"
scan_secrets "$WORKDIR/pg-approve-status.out"
grep -qx "status=COMPLETED" "$WORKDIR/pg-approve-status.out" \
  || id42_die "postgres note is not COMPLETED in a later process"
echo "OK postgres approval resumes across processes"

pg_run --entrypoint /opt/id42-work/venv/bin/python \
  -e "PYTHONPATH=/opt/id42-agent" \
  nanobot-gateway - crash before "pg-recon-note" "pg-recon" \
  <"$ROOT/scripts/lib/reconcile_crash.py" | tee "$WORKDIR/pg-crash.out"
sed -i 's/\r$//' "$WORKDIR/pg-crash.out"
scan_secrets "$WORKDIR/pg-crash.out"
pg_recon=$(crash_field execution_id "$WORKDIR/pg-crash.out")
pg_action=$(crash_field action_id "$WORKDIR/pg-crash.out")
require_field "$WORKDIR/pg-crash.out" journal_status UNCERTAIN
require_field "$WORKDIR/pg-crash.out" note_count 0
[[ -n "$pg_recon" && -n "$pg_action" ]] || id42_die "postgres crash returned no ids"
ID42_ENV_FILE=$PG_ENV COMPOSE_PROJECT_NAME=$PG_PROJECT \
  "$ROOT/scripts/work-reconcile.sh" abandon "$pg_recon" "$pg_action" \
  --reason "postgres" | tee "$WORKDIR/pg-abandon.out"
sed -i 's/\r$//' "$WORKDIR/pg-abandon.out"
scan_secrets "$WORKDIR/pg-abandon.out"
grep -q "abandoned execution_id=${pg_recon} " "$WORKDIR/pg-abandon.out" \
  || id42_die "postgres reconcile did not abandon the call"
grep -q "journal_status=ABANDONED" "$WORKDIR/pg-abandon.out" \
  || id42_die "postgres reconcile did not report ABANDONED"
grep -q "decision_actor_id=id42-operator" "$WORKDIR/pg-abandon.out" \
  || id42_die "postgres reconcile did not record id42-operator"
pg_run --entrypoint /opt/id42-work/venv/bin/python \
  -e "PYTHONPATH=/opt/id42-agent" \
  nanobot-gateway - inspect "$pg_recon" "pg-recon-note" \
  <"$ROOT/scripts/lib/reconcile_crash.py" | tee "$WORKDIR/pg-inspect.out"
sed -i 's/\r$//' "$WORKDIR/pg-inspect.out"
scan_secrets "$WORKDIR/pg-inspect.out"
require_field "$WORKDIR/pg-inspect.out" journal_status ABANDONED
require_field "$WORKDIR/pg-inspect.out" status FAILED
echo "OK postgres reconciliation abandons one uncertain note"

echo "== id42-work process death on postgres =="
governed_process_crashes "$PG_ENV" "$PG_PROJECT" "pg" "yes"
echo "OK process death recovers on postgres"

ID42_ENV_FILE=$PG_ENV COMPOSE_PROJECT_NAME=$PG_PROJECT \
  "$ROOT/scripts/work-rotate-key.sh" | tee "$WORKDIR/pg-rotate.out"
sed -i 's/\r$//' "$WORKDIR/pg-rotate.out"
scan_secrets "$WORKDIR/pg-rotate.out"
if grep -q 'id42a1\.' "$WORKDIR/pg-rotate.out"; then
  id42_die "postgres key rotation printed an actor credential"
fi
pg_rotated=$(auth_field signing_key_id "$WORKDIR/pg-rotate.out")
[[ "$pg_rotated" =~ ^[0-9a-f]{16}$ ]] || id42_die "postgres key rotation did not print a key id"

pg_run --entrypoint /opt/id42-work/venv/bin/python \
  -e "PYTHONPATH=/opt/id42-agent" \
  nanobot-gateway - <<'PY' | tee "$WORKDIR/pg-files.out"
import stat
from pathlib import Path

store = Path("/var/lib/id42-work")
if (store / "work.sqlite").exists():
    raise SystemExit("sqlite file was created for the postgres backend")
key = store / "authority.key"
if not key.is_file() or key.is_symlink():
    raise SystemExit("authority key file is missing")
mode = stat.S_IMODE(key.stat().st_mode)
if mode != 0o600:
    raise SystemExit(f"authority key mode is {mode:03o}")
print("postgres-files-ok")
PY
sed -i 's/\r$//' "$WORKDIR/pg-files.out"
scan_secrets "$WORKDIR/pg-files.out"
grep -qx "postgres-files-ok" "$WORKDIR/pg-files.out" \
  || id42_die "postgres run did not keep the authority key on the data volume"

ID42_ENV_FILE=$PG_ENV COMPOSE_PROJECT_NAME=$PG_PROJECT \
  "$ROOT/scripts/doctor.sh" >"$WORKDIR/pg-doctor.out" 2>"$WORKDIR/pg-doctor.err" || true
scan_secrets "$WORKDIR/pg-doctor.out" "$WORKDIR/pg-doctor.err"
if ! grep -q "OK: id42-work storage backend is postgres" "$WORKDIR/pg-doctor.out"; then
  cat "$WORKDIR/pg-doctor.err" >&2
  id42_die "doctor did not report the postgres backend"
fi
if ! grep -q "OK: id42-work postgres storage is reachable" "$WORKDIR/pg-doctor.out"; then
  cat "$WORKDIR/pg-doctor.err" >&2
  id42_die "doctor did not confirm postgres connectivity"
fi
if ! grep -q "OK: id42-work storage config matches ID42_WORK_STORAGE" "$WORKDIR/pg-doctor.out"; then
  cat "$WORKDIR/pg-doctor.err" >&2
  id42_die "doctor did not confirm the postgres storage config"
fi
if ! grep -q "OK: postgres service publishes no host port" "$WORKDIR/pg-doctor.out"; then
  cat "$WORKDIR/pg-doctor.err" >&2
  id42_die "doctor did not confirm that postgres publishes no host port"
fi
echo "OK doctor reports postgres and reaches it without printing the URL"

pg_compose logs >"$WORKDIR/pg-logs.txt" || true
scan_secrets "$WORKDIR/pg-logs.txt"
echo "OK postgres profile keeps secrets out of the smoke output"
echo "smoke test passed"
