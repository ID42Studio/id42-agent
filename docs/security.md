# Security

This phase is a personal agent on one machine. The defaults are tighter than upstream Compose, and they are still not a sandbox for untrusted people.

## What the container is allowed to do

| Control | Setting |
|---|---|
| Published ports | `127.0.0.1:8765` (WebUI) and `127.0.0.1:18790` (health) only |
| Capabilities | `cap_drop: ALL`, then `CHOWN`, `SETGID`, `SETUID` |
| Privilege gain | `no-new-privileges:true` |
| Docker socket | not mounted |
| Host home | not mounted |
| Data | named volume `nanobot-data` at `/home/nanobot/.nanobot` |
| Process | upstream entrypoint starts as root, `chown`s the volume, then `setpriv` to uid 1000. It refuses to run the agent as root |
| CPU / memory | 1 CPU, 1 GiB limit (same order of magnitude as upstream Compose) |

`CHOWN`, `SETGID`, and `SETUID` are the capabilities upstream names for that entrypoint (`entrypoint.sh` `chown` and `setpriv --reuid=nanobot --regid=nanobot --init-groups`, and the `cap_add` list in upstream `docker-compose.yml`). No others are added. Bubblewrap's extra `SYS_ADMIN` override (`docker-compose.bwrap.yml`) is not used, because the shell tool is off.

The health handler is unauthenticated (`GET /health` in `nanobot/cli/gateway_runtime.py`). Upstream warns when it binds beyond loopback. Keep `18790` on `127.0.0.1`.

The WebUI binds `0.0.0.0` **inside** the container so Docker's port map can reach it, and the host map is loopback-only. `channels.websocket.tokenIssueSecret` is required for that wildcard bind (`WebSocketConfig.wildcard_host_requires_auth`). The password is `NANOBOT_WEB_TOKEN` in `.env`, mode `0600`. Init does not print it. Upstream's own Compose publishes `8765` on all host interfaces. This file does not.

## What the agent is allowed to do

Rendered on first boot, and checked by loading the file with nanobot:

- `tools.exec.enable` = `false`. The shell tool is not registered (`ExecToolConfig.enable`, default `true` upstream; `docs/configuration.md` says `false` removes it).
- `tools.restrictToWorkspace` = `true` (default `false` in `ToolsConfig`).
- Telegram, when enabled, has an explicit `allowFrom` and not `*`.
- ID42 Work, when enabled, is one stdio MCP server. `tools.mcpServers.id42-work` has a `command` and no `url`. The image does not pass `--http`, and Compose does not publish `8766`. The default factory registers no works. See [id42-work.md](id42-work.md).

File tools and web tools keep their upstream defaults, with workspace restriction on for the tools that honor it. Web fetch can still reach the network. Turn web tools off in the WebUI if you do not want that (`tools.web.enable` exists upstream; this phase does not change it).

Re-enabling `tools.exec.enable` gives the process a shell as uid 1000 inside the container. That user can write the data volume, including `config.json`, sessions, and memory. Do that only on purpose. OS-level `tools.exec.sandbox` = `bwrap` needs host user namespaces and capabilities this Compose file does not grant.

## Secrets

- `.env` is gitignored. `.env.example` has empty secrets.
- The first-boot `config.json` contains the WebUI password and, when Telegram is on, the bot token. The volume is sensitive. `docker compose down` does not delete it. `docker compose down -v` does.
- Doctor and init are written so those values are not printed. A failed `getMe` reports the HTTP status, not the request URL.
- Do not put `.env` or the volume into a backup that other people can read. Backup tooling is out of scope here; the warning still applies.

## Ollama and Telegram

Ollama must be reachable from the container and must not listen on the public internet. See [ollama.md](ollama.md).

Telegram long polling means the host makes outbound calls to `api.telegram.org`. It does not open an inbound public port. One token must have one consumer. See [telegram.md](telegram.md).

Cloud model providers are not configured. Adding one sends conversation content to that provider.

## What this does not claim

Loopback binding protects a single-user host. It does not help if the host account is shared, if you later change the port maps to `0.0.0.0`, or if the model is instructed by untrusted text to use tools that are still enabled. There is no Postgres and no separate API service. The ID42 Work server is a child process of the gateway, not a sidecar on the network. It does not approve work. A person still decides outside the server. The default factory registers nothing, so turning the server on is not by itself a policy.
