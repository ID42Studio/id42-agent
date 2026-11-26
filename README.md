# id42-agent

Self-hosted, personal distribution of [nanobot](https://github.com/HKUDS/nanobot) for daily use with an existing Ollama server, optional Telegram, and the bundled WebUI.

This repository does not fork the agent. It pins one upstream commit, builds that Dockerfile, and supplies a tighter Compose file plus a first-boot config. The agent decides what to do. A later release of ID42 Work will govern execution through an MCP server (`id42-work[mcp]`). That server is not part of this phase.

## Pin

| | |
|---|---|
| Upstream | `HKUDS/nanobot` |
| Release | `v0.4.2` |
| Commit | `0dc8679edcc6061fbcdf3de13c7e16000e9d1b65` |

Do not track `main`. There is no official published image. Compose builds the upstream Dockerfile with `NANOBOT_CHANNELS=telegram`.

nanobot is MIT licensed (see `NOTICE`). Original files in this repository are Apache-2.0 (`LICENSE`).

## What you get

- `nanobot-gateway`: WebUI on `127.0.0.1:8765`, health on `127.0.0.1:18790`
- Named volume for `/home/nanobot/.nanobot` (config, workspace, sessions, memory)
- Ollama via `providers.ollama.apiBase` (default: the host, through `host.docker.internal`)
- Telegram off until you set a token and an allowlist
- Shell tool off, workspace restriction on
- `nanobot-cli` under the `cli` profile (`status`, and any other nanobot command)
- Optional `ollama` service under the `ollama` profile

Not in this phase: backup/restore, a public API service, Postgres, or the ID42 Work MCP server. Upstream `v0.4.2` removed `nanobot serve` and has no `doctor` command. `scripts/doctor.sh` is this distribution's check, not a nanobot subcommand.

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
| Build | `make build` | `docker compose build nanobot-gateway` |
| Start | `make up` | `docker compose up -d nanobot-gateway` |
| Stop | `make down` | `docker compose down` |
| Restart | `make restart` | `docker compose restart nanobot-gateway` |
| Logs | `make logs` | `docker compose logs -f nanobot-gateway` |
| Status | `make status` | `docker compose --profile cli run --rm --no-deps -T nanobot-cli` |
| Doctor | `make doctor` | `./scripts/doctor.sh` |
| Test | `make test` | `./scripts/smoke-test.sh` |

`make down` keeps the data volume. `docker compose down -v` deletes it.

Interactive nanobot commands:

```bash
docker compose --profile cli run --rm nanobot-cli agent -m "Hello"
```

Changing `OLLAMA_MODEL`, the Ollama URL, or the Telegram token in `.env` after the first successful init does nothing to `config.json`. Change those in the WebUI, or edit the file in the volume, then restart the gateway. Delete `config.json` inside the volume only when you intend to render again. That discards WebUI edits to that file.

## Docs

- [Installation](docs/installation.md)
- [Ollama](docs/ollama.md)
- [Telegram](docs/telegram.md)
- [Security](docs/security.md)
- [Architecture](docs/architecture.md)
- [Roadmap](docs/roadmap.md)

## Security sketch

Ports are published on loopback only. The container drops all capabilities except `CHOWN`, `SETGID`, and `SETUID`, which the upstream entrypoint needs to fix volume ownership and drop to uid 1000. `no-new-privileges` is set. The Docker socket and the host home directory are not mounted. The health route is unauthenticated, which is why it stays on loopback.
