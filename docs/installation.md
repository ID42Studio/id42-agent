# Installation

## Prerequisites

- Linux or another host that runs Docker Engine and Docker Compose v2
- Python 3.11 or newer on the host, used only by `scripts/bootstrap.sh` and `scripts/doctor.sh`
- Git available to the Docker daemon (the image build clones the pinned nanobot commit and the pinned id42-work commit)
- An Ollama server. See [ollama.md](ollama.md) before the first init
- Optional: a Telegram bot token and your numeric user id. See [telegram.md](telegram.md)

The agent runs inside the image. You do not install nanobot on the host.

## Configure

```bash
cp .env.example .env
chmod 600 .env
```

Set `OLLAMA_MODEL` to a model you have already pulled (`ollama list`). Leave `TELEGRAM_ENABLED=false` until the bot token and allowlist are filled in. Leave `ID42_WORK_ENABLED=false` until you have read [ID42 Work](id42-work.md).

`NANOBOT_WEB_TOKEN` may stay empty. Init generates a 32-byte hex token, writes it to `.env`, and does not print it.

## Initialize and start

```bash
make init
make up
```

Equivalent commands:

```bash
./scripts/bootstrap.sh
docker compose up -d nanobot-gateway
```

The first `make init` builds the image from `https://github.com/HKUDS/nanobot.git` at commit `0dc8679edcc6061fbcdf3de13c7e16000e9d1b65` with build arg `NANOBOT_CHANNELS=telegram`, then installs `id42-work[mcp,postgres]` at commit `d3be75dceb1ebfd7c6832f61fa905dd27dbfb4f9`. That build is the slow step (WebUI compile and Python dependencies). Later inits reuse the local image `id42-agent-nanobot:0.4.2`. `make build` is `docker compose build nanobot-upstream nanobot-gateway`.

Init then:

1. Refuses to continue if `OLLAMA_MODEL` is empty, the Ollama URL is not an `http(s)` URL ending in `/v1`, or Telegram is enabled without a token and an explicit allowlist.
2. Renders `config.json` into the `nanobot-data` volume only when that file is absent.
3. Copies `workspace/AGENTS.md`, `workspace/SOUL.md`, `workspace/USER.md`, and `workspace/skills/README.md` if those paths are absent.
4. Asks the nanobot package inside the image to load the file. If that load fails, init deletes `config.json` so the next attempt can rewrite it.

A second init prints that `config.json` already exists and leaves it, including later WebUI edits.

## Open the WebUI

The password is the `NANOBOT_WEB_TOKEN` line in `.env`.

```bash
# local machine
xdg-open http://127.0.0.1:8765   # or open that URL yourself
```

From another computer, tunnel rather than publishing the port:

```bash
ssh -L 8765:127.0.0.1:8765 -L 18790:127.0.0.1:18790 user@host
```

Then open `http://127.0.0.1:8765` on your computer.

## Check

```bash
curl --fail http://127.0.0.1:18790/health
make doctor
make status
```

`make doctor` checks Docker, `.env`, loopback port bindings, `/health`, that the ID42 Work store is writable and mounted, which storage backend is active, and that backend's connectivity. SQLite opens the file on the volume. Postgres must answer, and its container must not publish a host port. It never prints the database URL. It checks that `id42-work mcp serve` answers on stdio and lists `echo`, `note`, and `note.list`, that the image pin matches `d3be75dceb1ebfd7c6832f61fa905dd27dbfb4f9`, that the authority key file is absent or mode `0600`, that `id42-work approvals` and `id42-work reconcile` are on `PATH`, Ollama from inside the gateway container, and (only when Telegram is enabled) `getMe` plus the allowlist. It never prints the bot token, the WebUI token, or the authority key.

## Stop

```bash
make down
# or
docker compose down
```

The named volumes remain. To delete config, sessions, memory, and the ID42 Work store as well:

```bash
docker compose down -v
```

## Optional Ollama container

The default is an Ollama you already run on the host. To run Ollama in Compose instead, set this in `.env` **before** the first init:

```bash
OLLAMA_BASE_URL=http://ollama:11434/v1
```

Then:

```bash
docker compose --profile ollama up -d
make init
make up
docker compose --profile ollama exec ollama ollama pull "$OLLAMA_MODEL"
```

The Ollama port is published on `127.0.0.1:11434` only. Pull models through `docker compose exec` or through that loopback port. Do not change the mapping to `0.0.0.0`.

## Where the keys come from

Every value written into `config.json` is an upstream nanobot field at the pinned commit. The table is in [architecture.md](architecture.md#keys-bootstrap-writes). `.env` names such as `OLLAMA_MODEL` and `TELEGRAM_BOT_TOKEN` belong to id42-agent. They are not nanobot environment variables.
