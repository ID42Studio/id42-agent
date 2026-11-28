# id42-agent

Self-hosted, personal distribution of [nanobot](https://github.com/HKUDS/nanobot) for daily use with an existing Ollama server, optional Telegram, and the bundled WebUI.

This repository does not fork the agent. It pins one upstream commit, builds that Dockerfile, and supplies a tighter Compose file plus a first-boot config. The agent decides what to do. ID42 Work governs execution through `id42-work[mcp,postgres,web]`, installed in the image and attached over stdio when you opt in. See [ID42 Work](docs/id42-work.md).

## Pin

| | |
|---|---|
| Upstream | `HKUDS/nanobot` |
| Release | `v0.4.2` |
| Commit | `0dc8679edcc6061fbcdf3de13c7e16000e9d1b65` |
| ID42 Work | `ae07623aa67c0b5d0535cb58f6a2e2b204736e50` (`id42-work[mcp,postgres,web]`) |

Do not track `main`. There is no official published image. Compose builds the upstream Dockerfile with `NANOBOT_CHANNELS=telegram`, then installs the pinned id42-work commit into a separate virtualenv. Override that commit with the build arg `ID42_WORK_REF`.

nanobot is MIT licensed (see `NOTICE`). Original files in this repository are Apache-2.0 (`LICENSE`).

## What you get

- `nanobot-gateway`: WebUI on `127.0.0.1:8765`, health on `127.0.0.1:18790`
- Named volume for `/home/nanobot/.nanobot` (config, workspace, sessions, memory)
- Named volume `id42-work-data` at `/var/lib/id42-work` for the default ID42 Work store and its authority key (outside the workspace)
- Ollama via `providers.ollama.apiBase` (default: the host, through `host.docker.internal`)
- Telegram off until you set a token and an allowlist
- Shell tool off, workspace restriction on
- `id42-work` on `PATH`. The MCP server is off until `ID42_WORK_ENABLED=true`, then it is a stdio child of the gateway. No extra published port. The default factory stores executions in SQLite and registers `echo`, `note`, and `note.list`. `ID42_WORK_STORAGE=postgres` with the `postgres` profile uses Postgres instead. The authority key stays on `id42-work-data`
- `scripts/work-approve.sh` runs `id42-work approvals` for that factory. `approve` resumes the execution as the host actor
- `scripts/work-reconcile.sh` runs `id42-work reconcile` for that factory. The note provider probes an uncertain append. The operator can mark it applied, not applied, or abandon it
- The factory, the approval command, and the reconcile command share one HMAC key file on `id42-work-data`. `scripts/work-rotate-key.sh` retires the current key and prints only the new key id
- `nanobot-cli` under the `cli` profile (`status`, and any other nanobot command)
- Optional `ollama` service under the `ollama` profile
- Optional Postgres service under the `postgres` profile, used only when `ID42_WORK_STORAGE=postgres`
- Optional operator UI under the `id42-work-web` profile, on `127.0.0.1:8767`, for approvals and reconciliation. It uses the same store and authority key. Set `ID42_WORK_WEB_TOKEN` before starting it. See [ID42 Work](docs/id42-work.md)
- Optional Odoo and GitHub connectors, installed but off until `ID42_WORK_ODOO_ENABLED` or `ID42_WORK_GITHUB_ENABLED`. Allow-lists are required. GitHub work cannot merge, close, delete, or push

Not in this phase: backup/restore or a public API service. Upstream `v0.4.2` removed `nanobot serve` and has no `doctor` command. `scripts/doctor.sh` is this distribution's check, not a nanobot subcommand.

## Requirements

- Docker Engine and Docker Compose v2
- Python 3 on the host (the init and doctor scripts)
- Git (Compose clones the pinned nanobot commit while building)
- An Ollama server the container can reach, and that is not exposed to the public internet

## First run

```bash
cp .env.example .env
# Set OLLAMA_MODEL to a name from `ollama list`.
chmod 600 .env
make init
make up
```

`make init` writes a WebUI password into `.env` as `NANOBOT_WEB_TOKEN` and does not print it. Open `http://127.0.0.1:8765` and use that value.

The same flow without Make:

```bash
cp .env.example .env
chmod 600 .env
./scripts/bootstrap.sh
docker compose up -d nanobot-gateway
docker compose logs -f nanobot-gateway
curl --fail http://127.0.0.1:18790/health
```

`docker compose up` alone refuses to start until `config.json` exists. The upstream image would otherwise create a config that leaves the shell tool enabled and workspace restriction off.

## Day to day

| | Make | Compose |
|---|---|---|
| Init | `make init` | `./scripts/bootstrap.sh` |
| Build | `make build` | `docker compose build nanobot-upstream nanobot-gateway` |
| Start | `make up` | `docker compose up -d nanobot-gateway` |
| Stop | `make down` | `docker compose down` |
| Restart | `make restart` | `docker compose restart nanobot-gateway` |
| Logs | `make logs` | `docker compose logs -f nanobot-gateway` |
| Status | `make status` | `docker compose --profile cli run --rm --no-deps -T nanobot-cli` |
| Doctor | `make doctor` | `./scripts/doctor.sh` |
| Test | `make test` | `./scripts/smoke-test.sh` |

`make down` keeps the data volumes. `docker compose down -v` deletes them, including the ID42 Work store.

Interactive nanobot commands:

```bash
docker compose --profile cli run --rm -T nanobot-cli agent -m "Hello"
```

Changing `OLLAMA_MODEL`, the Ollama URL, or the Telegram token in `.env` after the first successful init does nothing to `config.json`. Change those in the WebUI, or edit the file in the volume, then restart the gateway. Delete `config.json` inside the volume only when you intend to render again. That discards WebUI edits to that file.

## Docs

- [Installation](docs/installation.md)
- [Ollama](docs/ollama.md)
- [Telegram](docs/telegram.md)
- [ID42 Work](docs/id42-work.md)
- [Security](docs/security.md)
- [Architecture](docs/architecture.md)
- [Roadmap](docs/roadmap.md)

## Security sketch

Ports are published on loopback only. The container drops all capabilities except `CHOWN`, `SETGID`, and `SETUID`, which the upstream entrypoint needs to fix volume ownership and drop to uid 1000. `no-new-privileges` is set. The Docker socket and the host home directory are not mounted. The health route is unauthenticated, which is why it stays on loopback.
