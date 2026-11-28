# Changelog

## Unreleased

### Changed

- The nanobot WebUI stays enabled and still requires `NANOBOT_WEB_TOKEN`. The gateway is on the `id42-store` network, not the Compose default network. A container on the default network cannot open port 8765. The host map stays `127.0.0.1:8765`. Governed `id42-work` calls from chat still wait for approval. An authenticated WebUI session can still change upstream settings, including `tools.exec.enable`.
- The operator UI forwarder listens only on the `id42-operator` network. `id42-work-web-publish` is the container published on `127.0.0.1:8767`. The application container publishes no host port. Start it with `docker compose --profile id42-work-web up -d id42-work-web-publish`.
- A new `config.json` stores `${NANOBOT_WEB_TOKEN}`, `${TELEGRAM_BOT_TOKEN}`, and file paths for the Postgres URL, the Odoo key, and the GitHub token. Inline values are written to mode `0600` files under `/var/lib/id42-work/secrets/` at startup when no `*_FILE` is set. Doctor warns about inline secrets and when `config.json` still contains one.

### Added

- id42-work is pinned at `ae07623aa67c0b5d0535cb58f6a2e2b204736e50`. The default factory passes `session_actor` so stdio keeps one sealed actor for the process, and HTTP sessions present a credential the authority verifies. `list_works` and `list_executions` are paged.
- Optional operator UI. Profile `id42-work-web` runs `id42-work web serve` on the same store and authority key. The host port is `127.0.0.1:8767`. The token comes from `ID42_WORK_WEB_TOKEN` or `ID42_WORK_WEB_TOKEN_FILE`.
- Optional Odoo and GitHub connectors in the id42-work virtualenv, pinned by `ID42_WORK_ODOO_REF` and `ID42_WORK_GITHUB_REF`. They register only when `ID42_WORK_ODOO_ENABLED` or `ID42_WORK_GITHUB_ENABLED` is true. The default works stay `echo`, `note`, and `note.list`.
- Optional Postgres storage for the default id42-work factory. SQLite remains the default.
  `ID42_WORK_STORAGE=postgres` and `ID42_WORK_DATABASE_URL` select `PostgresStorage`.
  The `postgres` Compose profile runs Postgres from credentials in `.env` and publishes
  no host port. The authority key file stays on `id42-work-data`.
- `id42-work[mcp,postgres,web]` in `/opt/id42-work/venv`. The nanobot virtualenv is unchanged.
- Doctor reports the active backend and checks that it opens. The database URL is not printed.
- The smoke test still covers SQLite, and also approves one note across processes and
  reconciles one uncertain note on the Postgres profile.
- Smoke coverage for process death during a governed note append, on SQLite and on Postgres.
  `SIGKILL` during `notes.append` leaves the execution `RUNNING` and the journal `IN_FLIGHT`.
  Resume probes the note file: `APPLIED` does not append again, and `NOT_APPLIED` retries
  the same operation id once after a new approval. An unreadable note file becomes
  `UNCERTAIN`, and `scripts/work-reconcile.sh applied` or `not-applied` resolves it.
  `SIGKILL` during validation leaves `VALIDATING` with a `SUCCEEDED` journal. Resume
  finishes it without calling `notes.append` again.
