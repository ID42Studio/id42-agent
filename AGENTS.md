# Contributors

This file is for people changing the id42-agent repository. It is not the workspace file nanobot loads. That one is `workspace/AGENTS.md` and is copied into the data volume on first boot.

## Pin

Runtime commits stay on HKUDS/nanobot `v0.4.2` / `0dc8679edcc6061fbcdf3de13c7e16000e9d1b65` until an intentional bump. Do not point Compose at `main`. When bumping, read the upstream release notes, re-check every key in `docs/architecture.md`, and update `NOTICE`.

There is no official nanobot image. Build the upstream Dockerfile. The build arg `NANOBOT_CHANNELS` defaults to `whatsapp` upstream. This distribution sets `telegram`.

## Config keys

Do not invent nanobot settings. A key may be written into `config.json` only when it exists at the pinned commit. Add the file and line to the table in `docs/architecture.md` in the same change. id42-agent names in `.env` (`OLLAMA_MODEL`, `TELEGRAM_ENABLED`, and the others in `.env.example`) are local. They must be rendered into real upstream fields, not passed through as if nanobot understood them.

`v0.4.2` has no `doctor` command, no working `serve`, no `NANOBOT_API_KEY`, and no port `8900`. Do not document those as if they were on this pin.

## id42-work

`id42-work[mcp,postgres,web]` is installed into `/opt/id42-work/venv`, not into `/app/.venv`. nanobot v0.4.2 requires `mcp<2`. id42-work requires `mcp>=2.3`. The `postgres` extra (`psycopg`) and the `web` extra stay in that same virtualenv. The console script on `PATH` is `/usr/local/bin/id42-work`. Do not install either extra into the nanobot virtualenv. The pin and the build arg `ID42_WORK_REF` are in `Dockerfile` and `docker-compose.yml`. The operator UI is the Compose profile `id42-work-web`. It is off unless that profile is started. It uses the same store and the same authority key. `id42-work web serve` binds `127.0.0.1` inside the container. `docker/web-entrypoint.sh` forwards the published port to that listener, and Compose publishes `127.0.0.1` only. The token is `ID42_WORK_WEB_TOKEN` or `ID42_WORK_WEB_TOKEN_FILE`. It is not an argument and it is not written into `config.json`. `id42-work-odoo` and `id42-work-github` are installed in the same virtualenv, pinned by `ID42_WORK_ODOO_REF` and `ID42_WORK_GITHUB_REF`. The factory imports and registers them only when `ID42_WORK_ODOO_ENABLED` or `ID42_WORK_GITHUB_ENABLED` is true. The execution actor is not given approve or reconcile grants for those works.

The default factory is `id42_agent.mcp_host:server`. `ID42_WORK_STORAGE` selects the backend: empty or `sqlite` (the default) uses SQLite at `/var/lib/id42-work/work.sqlite`. `postgres` uses `PostgresStorage` and `ID42_WORK_DATABASE_URL`, with the Compose profile `postgres`. One HMAC key file stays at `/var/lib/id42-work` on the `id42-work-data` volume for either backend, outside the workspace and outside any home directory. Do not mount that store into the workspace. The key file is `/var/lib/id42-work/authority.key` unless `ID42_WORK_AUTHORITY_KEY_PATH` is an absolute path. The factory creates it on first use, mode `0600`. The MCP server, `scripts/work-approve.sh`, `scripts/work-reconcile.sh`, and `scripts/work-rotate-key.sh` must keep working with either backend and must use that same key file. `scripts/work-rotate-key.sh` retires the current key and prints only the new key id. `id42_agent.mcp_stub:server` is the empty in-memory factory and does not use the key file. Approvals and reconciliation are not MCP tools. `scripts/work-approve.sh` runs `id42-work approvals` for that default factory, with `--actor id42-operator`. `approve` adds `--resume`. `scripts/work-reconcile.sh` runs `id42-work reconcile` the same way. The command seals only `reconcile:<work_id>`. Pass `--resume` only when the caller sets it. Doctor reports the active backend and checks connectivity without printing the database URL.

## First boot

`scripts/bootstrap.sh` may create `config.json` only when it is missing. It must not overwrite a file the WebUI has edited. If you add a field, existing volumes keep the old file until the operator edits it or deletes it.

The wrapper `docker/entrypoint.sh` must keep refusing to start without that file. Upstream bootstrap would otherwise write a config with `tools.exec.enable` left at its default `true` and `tools.restrictToWorkspace` left at `false`.

## Secrets

Do not commit `.env`, session stores, memory, or a copied data volume. Scripts must not print `NANOBOT_WEB_TOKEN`, `ID42_WORK_WEB_TOKEN`, `TELEGRAM_BOT_TOKEN`, `ID42_WORK_DATABASE_URL`, `ID42_POSTGRES_PASSWORD`, authority key bytes, or actor credentials. Do not pass those values as process arguments.

## Checks

```bash
shellcheck -x -s bash scripts/bootstrap.sh scripts/doctor.sh scripts/smoke-test.sh scripts/work-approve.sh scripts/work-reconcile.sh scripts/work-rotate-key.sh scripts/lib/common.sh
shellcheck -s sh docker/entrypoint.sh docker/web-entrypoint.sh
docker compose --env-file .env.example config --quiet
docker compose --env-file .env.example --profile postgres config --quiet
docker compose --env-file .env.example --profile id42-work-web config --quiet
docker compose --env-file .env.example --profile postgres --profile id42-work-web config --quiet
./scripts/smoke-test.sh
```

`make test` runs the smoke test. The smoke test builds the image, boots a gateway with a dummy model URL, and requires `GET /health` to report ready. It keeps that SQLite run, then starts the `postgres` profile and checks one cross-process approval and one reconciliation. Both backends also SIGKILL a governed note append and a validation phase and recover them. It does not call a real Ollama or Telegram.

## License

Original files are Apache-2.0. nanobot stays MIT. Keep `NOTICE` with any distribution that includes the upstream program. Do not relicense nanobot.
