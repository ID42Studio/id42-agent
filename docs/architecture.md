# Architecture

```mermaid
flowchart LR
  user[You on this machine]
  web[WebUI 127.0.0.1:8765]
  health[Health 127.0.0.1:18790]
  gateway[nanobot-gateway]
  volume[(nanobot-data)]
  ollama[Ollama on the host or the ollama profile]
  telegram[Telegram long poll]

  user --> web
  user --> health
  web --> gateway
  health --> gateway
  gateway --> volume
  gateway --> workvol[(id42-work-data)]
  gateway -->|"providers.ollama.apiBase"| ollama
  gateway -.->|optional| telegram
  gateway -.->|"stdio child, opt-in"| work["id42-work mcp serve"]
  user -.->|"optional profile"| workui["Operator UI 127.0.0.1:8767"]
  workui --> workvol
  work -.->|"ID42_WORK_STORAGE=postgres"| pg[(Postgres profile)]
  workui -.->|"same URL"| pg
```

`nanobot-gateway` is the upstream image at commit `0dc8679edcc6061fbcdf3de13c7e16000e9d1b65`, started with `gateway`. `docker/entrypoint.sh` chowns the id42-work store when that directory is mounted, refuses to start when `config.json` is missing, then execs upstream `entrypoint.sh`. That script drops to uid 1000 and, because `NANOBOT_DOCKER_BOOTSTRAP=1`, notices the existing file and does not rewrite it (`nanobot/cli/docker_bootstrap.py`).

The image directory `/home/nanobot/.nanobot` is owned by uid 1000. With every capability dropped except `CHOWN`, `SETGID`, and `SETUID`, root cannot write that directory. Bootstrap `chown`s it to `nanobot` (the same `CHOWN` capability the upstream entrypoint uses) and writes `config.json` as uid 1000.

`nanobot-cli` is the same image under profile `cli`. Its default command is `status`. Pass another nanobot command as arguments to `docker compose run`.

`ollama` is profile `ollama` and is not started by `make up`.

`id42-work-web` is profile `id42-work-web` and is not started by `make up`. It runs the operator UI for approvals and reconciliation against the same factory, the same `id42-work-data` volume, and the same authority key. `id42-work web serve` binds `127.0.0.1:8768`. A forwarder in that container listens on port 8767 so Docker can publish `127.0.0.1:8767` on the host. The operator token is set only on that service.

The data directory inside the container is `/home/nanobot/.nanobot` (`Dockerfile` `mkdir` and `ENV HOME=/home/nanobot`). The workspace path written into config is `/home/nanobot/.nanobot/workspace`. The ID42 Work volume `id42-work-data` is mounted at `/var/lib/id42-work`, outside the workspace. SQLite uses `/var/lib/id42-work/work.sqlite` there. The authority key stays on that volume for SQLite and for Postgres. Postgres data, when the profile is enabled, is the named volume `id42-postgres-data`.

## Why the rendered file sets the bind addresses

Upstream Docker bootstrap writes `gateway.host` and `channels.websocket.host` as `0.0.0.0`, plus a WebUI secret, and then stops touching the file. Because we pre-create `config.json`, we have to include those fields ourselves. Docker publishes them only on the host loopback. A process bound to `127.0.0.1` inside the container would not receive traffic forwarded to the container's eth0 (`docs/deployment.md` in the upstream tree: Docker port forwarding cannot reach `127.0.0.1` inside a container).

## Keys bootstrap writes

Every key below exists at `0dc8679edcc6061fbcdf3de13c7e16000e9d1b65`. Line numbers refer to that tree. id42-agent env names are not nanobot keys. They are copied into the fields in the last column.

| Written JSON | Upstream definition | id42-agent source |
|---|---|---|
| `agents.defaults.workspace` | `AgentDefaults.workspace`, `nanobot/config/schema.py:180` | fixed path inside the volume |
| `agents.defaults.modelPreset` | `AgentDefaults.model_preset`, `schema.py:181` | constant `primary` |
| `agents.defaults.timezone` | `AgentDefaults.timezone`, `schema.py:204` | `TZ` |
| `agents.defaults.timezoneMode` | `AgentDefaults.timezone_mode`, `schema.py:205` | constant `manual` |
| `providers.ollama` | `ProvidersConfig.ollama`, `schema.py:346` | constant |
| `providers.ollama.apiBase` | `ProviderConfig.api_base`, `schema.py:263` | `OLLAMA_BASE_URL` |
| `modelPresets` | `Config.model_presets`, `schema.py:509-512` | constant object |
| `modelPresets.primary.provider` | `ModelPresetConfig.provider`, `schema.py:160` | constant `ollama` |
| `modelPresets.primary.model` | `ModelPresetConfig.model`, `schema.py:159` | `OLLAMA_MODEL` |
| `modelPresets.primary.maxTokens` | `ModelPresetConfig.max_tokens`, `schema.py:162` | `4096`, as in upstream `docs/providers.md` Ollama example |
| `modelPresets.primary.contextWindowTokens` | `ModelPresetConfig.context_window_tokens`, `schema.py:163` | `32768`, same example |
| `gateway.host` | `GatewayConfig.host`, `schema.py:404` | `0.0.0.0` inside the container |
| `gateway.port` | `GatewayConfig.port`, `schema.py:405` | `18790` |
| `channels.websocket.enabled` | `WebSocketConfig.enabled`, `nanobot/channels/websocket/runtime.py:178` | `true` |
| `channels.websocket.host` | `WebSocketConfig.host`, `websocket/runtime.py:179` | `0.0.0.0` inside the container |
| `channels.websocket.port` | `WebSocketConfig.port`, `websocket/runtime.py:180` | `8765` |
| `channels.websocket.tokenIssueSecret` | `WebSocketConfig.token_issue_secret`, `websocket/runtime.py:186` | `NANOBOT_WEB_TOKEN` |
| `channels.websocket.websocketRequiresToken` | `WebSocketConfig.websocket_requires_token`, `websocket/runtime.py:189` | `true` |
| `channels.telegram.enabled` | `TelegramConfig.enabled`, `nanobot/channels/telegram/runtime.py:453` | only if `TELEGRAM_ENABLED=true` |
| `channels.telegram.token` | `TelegramConfig.token`, `telegram/runtime.py:454` | `TELEGRAM_BOT_TOKEN` |
| `channels.telegram.mode` | `TelegramConfig.mode`, `telegram/runtime.py:455` | `polling` |
| `channels.telegram.allowFrom` | `TelegramConfig.allow_from`, `telegram/runtime.py:456` | `TELEGRAM_ALLOWED_USERS` |
| `tools.restrictToWorkspace` | `ToolsConfig.restrict_to_workspace`, `schema.py:438` | `true` |
| `tools.exec.enable` | `ExecToolConfig.enable`, `nanobot/agent/tools/shell_config.py:11` | `false` |
| `tools.mcpServers.id42-work.command` | `MCPServerConfig.command`, `schema.py:415` | constant `id42-work`, only when `ID42_WORK_ENABLED=true` |
| `tools.mcpServers.id42-work.args` | `MCPServerConfig.args`, `schema.py:416` | `mcp`, `serve`, `--factory`, then `ID42_WORK_FACTORY` or `id42_agent.mcp_host:server` |
| `tools.mcpServers.id42-work.toolTimeout` | `MCPServerConfig.tool_timeout`, `schema.py:422` | `120` |
| `tools.mcpServers.id42-work.env.PYTHONPATH` | `MCPServerConfig.env`, `schema.py:417` | `/opt/id42-agent`, then `ID42_WORK_PYTHONPATH` when that is set. nanobot's stdio client does not inherit `PYTHONPATH` |
| `tools.mcpServers.id42-work.env.ID42_WORK_AUTHORITY_KEY_PATH` | `MCPServerConfig.env`, `schema.py:417` | only when `ID42_WORK_AUTHORITY_KEY_PATH` is set. The factory reads it. Empty leaves the default `/var/lib/id42-work/authority.key` |
| `tools.mcpServers.id42-work.env.ID42_WORK_STORAGE` | `MCPServerConfig.env`, `schema.py:417` | only when `ID42_WORK_STORAGE=postgres`. Empty or `sqlite` leaves SQLite and omits the key |
| `tools.mcpServers.id42-work.env.ID42_WORK_DATABASE_URL` | `MCPServerConfig.env`, `schema.py:417` | only together with `postgres`. The factory reads it. The value is a secret and is not printed |

`channels` accepts channel objects as extra fields (`ChannelsConfig` `extra="allow"`, `schema.py:48`). Camel case is the on-disk form (`nanobot/config_base.py` `alias_generator=to_camel`). Ollama's registry default base is `http://localhost:11434/v1` (`nanobot/providers/registry.py`, provider `ollama`).

Env vars passed through from upstream, not invented here:

| Variable | Where it is read |
|---|---|
| `NANOBOT_DOCKER_BOOTSTRAP` | `entrypoint.sh` (gateway command runs `nanobot.cli.docker_bootstrap` when this is `1`) |
| `NANOBOT_WEB_TOKEN` | `docker_bootstrap.py` when that bootstrap creates the file. We also copy it into `tokenIssueSecret` because the file already exists by then |
| `NANOBOT_CHANNELS` | Docker build arg, upstream `Dockerfile` (default `whatsapp`) |

Not set, because they are not part of this pin's Compose path: `NANOBOT_API_KEY`, port `8900`, command `serve`.

`ID42_WORK_REF` is a build arg of this repository's `Dockerfile`. It is not a nanobot variable. The default is `ae07623aa67c0b5d0535cb58f6a2e2b204736e50`.

## Workspace seed

nanobot reads `AGENTS.md`, `SOUL.md`, and `USER.md` from the workspace (`ContextBuilder.BOOTSTRAP_FILES` in `nanobot/agent/context.py`). It creates missing templates, including a `skills/` directory, without overwriting files that exist (`sync_workspace_templates` in `nanobot/utils/helpers.py`). A skill is a subdirectory of `workspace/skills` containing `SKILL.md` (`SkillsLoader` in `nanobot/agent/skills.py`).

The seed in this repository is copied only while `config.json` is created, and only into paths that are still absent. Built-in skills stay inside the image.

## Health

`GET /health` on `gateway.host`:`gateway.port` returns JSON. HTTP 200 with `ready: true` means the process is up and the websocket channel is running (`nanobot/cli/gateway_runtime.py`). Compose runs `curl --fail` against `http://127.0.0.1:18790/health` inside the container. `curl` is installed by the upstream Dockerfile.

## ID42 Work

`tools.mcpServers` is `ToolsConfig.mcp_servers` (`schema.py:455`). When `ID42_WORK_ENABLED=true`, bootstrap writes one entry, `id42-work`. `type` and `url` are omitted. A `command` is stdio (`nanobot/agent/tools/mcp.py`). The command is `id42-work mcp serve --factory <module:callable>`. Nothing is bound to a port, and `--http` is not passed. `tools.ssrfWhitelist` is left unset because the server is not an HTTP client of the gateway.

The package is installed from `ID42Studio/id42-work` at `ae07623aa67c0b5d0535cb58f6a2e2b204736e50` into `/opt/id42-work/venv`, with the `mcp`, `postgres`, and `web` extras. The `mcp` extra requires `mcp>=2.3`. The `postgres` extra installs `psycopg` in that same virtualenv. The `web` extra installs the operator UI in that same virtualenv. `id42-work-odoo` and `id42-work-github` are installed in that same virtualenv and stay unregistered until `ID42_WORK_ODOO_ENABLED` or `ID42_WORK_GITHUB_ENABLED` is true. This nanobot pin requires `mcp<2`, so the packages do not share a virtualenv. `/usr/local/bin/id42-work` is a symlink to the console script. `python` on `PATH` remains `/app/.venv/bin/python`.

The default factory `id42_agent.mcp_host:server` lives at `/opt/id42-agent`. It passes `session_actor` with the factory actor as the stdio fallback, so the process has one sealed identity. Listing tools return one page. `ID42_WORK_STORAGE` selects the backend. Empty or `sqlite` opens `SqliteStorage` at `/var/lib/id42-work/work.sqlite`. `postgres` opens `PostgresStorage` with `ID42_WORK_DATABASE_URL`. It registers `echo`, `note.list`, and `note`. `note` is supervised: the side-effecting capability waits for an approval the MCP client cannot grant. `id42-work approvals` decides that approval in another process, using the same factory. `scripts/work-approve.sh` runs it inside the gateway. The note provider probes `operation_id` when a call is `UNCERTAIN`. A process killed while that journal row is `IN_FLIGHT` is probed on the next resume. A process killed after the execution is stored as `VALIDATING` is finished from the `SUCCEEDED` journal and does not call the provider again. `id42-work reconcile` seals `reconcile:note` for `id42-operator` when a person has to decide. `scripts/work-reconcile.sh` runs that command. Both commands and the MCP server load one HMAC key file, `/var/lib/id42-work/authority.key` unless `ID42_WORK_AUTHORITY_KEY_PATH` names another absolute path. The factory creates that file on first use. A seal from one process verifies in the next. `scripts/work-rotate-key.sh` retires the current key. `id42_agent.mcp_stub:server` is the previous empty in-memory factory and does not use the key file. Bootstrap always sets `tools.mcpServers.id42-work.env.PYTHONPATH` to `/opt/id42-agent`, then appends `ID42_WORK_PYTHONPATH` when it is set. When `ID42_WORK_AUTHORITY_KEY_PATH` is set, bootstrap also copies that absolute path into the same `env` map. When `ID42_WORK_STORAGE=postgres`, it copies that name and `ID42_WORK_DATABASE_URL` into the same map. nanobot's stdio client keeps `PATH` and `HOME` and does not inherit other variables (`mcp` 1.26 `get_default_environment`). Without the explicit `PYTHONPATH`, `id42_agent` would not import, and without the explicit key path the child would keep the default file. Without the explicit storage keys the child would keep SQLite. A factory you supply is host code: set `ID42_WORK_FACTORY` and, when the module is not in the image, `ID42_WORK_PYTHONPATH`. Do not put that module, the SQLite file, the database URL, or the key file in the agent workspace. See [id42-work.md](id42-work.md).

`enabledTools` is omitted, so it stays at the upstream default `["*"]` (`schema.py:423`), which registers tools and resources. `toolTimeout` is 120 seconds (`schema.py:422`; the upstream default is 30). A tool call returns when the runner returns, including at `WAITING_APPROVAL`.

Changing `ID42_WORK_ENABLED` after the first init does not rewrite `config.json`. Same rule as the Ollama URL and the Telegram token.
