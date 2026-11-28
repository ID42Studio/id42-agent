# Changelog

## Unreleased

### Added

- id42-work is pinned at `ae07623aa67c0b5d0535cb58f6a2e2b204736e50`. The default factory passes `session_actor` so stdio keeps one sealed actor for the process, and HTTP sessions present a credential the authority verifies. `list_works` and `list_executions` are paged.
- Optional Postgres storage for the default id42-work factory. SQLite remains the default.
  `ID42_WORK_STORAGE=postgres` and `ID42_WORK_DATABASE_URL` select `PostgresStorage`.
  The `postgres` Compose profile runs Postgres from credentials in `.env` and publishes
  no host port. The authority key file stays on `id42-work-data`.
- `id42-work[mcp,postgres]` in `/opt/id42-work/venv`. The nanobot virtualenv is unchanged.
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
