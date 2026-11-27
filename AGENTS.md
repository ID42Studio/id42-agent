# Contributors

This file is for people changing the id42-agent repository. It is not the workspace file nanobot loads. That one is `workspace/AGENTS.md` and is copied into the data volume on first boot.

## Pin

Runtime commits stay on HKUDS/nanobot `v0.4.2` / `0dc8679edcc6061fbcdf3de13c7e16000e9d1b65` until an intentional bump. Do not point Compose at `main`. When bumping, read the upstream release notes, re-check every key in `docs/architecture.md`, and update `NOTICE`.

There is no official nanobot image. Build the upstream Dockerfile. The build arg `NANOBOT_CHANNELS` defaults to `whatsapp` upstream. This distribution sets `telegram`.

## Config keys

Do not invent nanobot settings. A key may be written into `config.json` only when it exists at the pinned commit. Add the file and line to the table in `docs/architecture.md` in the same change. id42-agent names in `.env` (`OLLAMA_MODEL`, `TELEGRAM_ENABLED`, and the others in `.env.example`) are local. They must be rendered into real upstream fields, not passed through as if nanobot understood them.

`v0.4.2` has no `doctor` command, no working `serve`, no `NANOBOT_API_KEY`, and no port `8900`. Do not document those as if they were on this pin.

## id42-work

`id42-work[mcp]` is installed into `/opt/id42-work/venv`, not into `/app/.venv`. nanobot v0.4.2 requires `mcp<2`. id42-work requires `mcp>=2.3`. The console script on `PATH` is `/usr/local/bin/id42-work`. Do not install that extra into the nanobot virtualenv. The pin and the build arg `ID42_WORK_REF` are in `Dockerfile` and `docker-compose.yml`.

## First boot

`scripts/bootstrap.sh` may create `config.json` only when it is missing. It must not overwrite a file the WebUI has edited. If you add a field, existing volumes keep the old file until the operator edits it or deletes it.

The wrapper `docker/entrypoint.sh` must keep refusing to start without that file. Upstream bootstrap would otherwise write a config with `tools.exec.enable` left at its default `true` and `tools.restrictToWorkspace` left at `false`.

## Secrets

Do not commit `.env`, session stores, memory, or a copied data volume. Scripts must not print `NANOBOT_WEB_TOKEN` or `TELEGRAM_BOT_TOKEN`. Do not pass those values as process arguments.

## Checks

```bash
shellcheck -x -s bash scripts/bootstrap.sh scripts/doctor.sh scripts/smoke-test.sh scripts/lib/common.sh
shellcheck -s sh docker/entrypoint.sh
docker compose --env-file .env.example config --quiet
./scripts/smoke-test.sh
```

`make test` runs the smoke test. The smoke test builds the image, boots a gateway with a dummy model URL, and requires `GET /health` to report ready. It does not call a real Ollama or Telegram.

## License

Original files are Apache-2.0. nanobot stays MIT. Keep `NOTICE` with any distribution that includes the upstream program. Do not relicense nanobot.
