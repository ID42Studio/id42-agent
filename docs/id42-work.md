# ID42 Work

The gateway can start [id42-work](https://github.com/ID42Studio/id42-work) as an MCP server. The server is a child process of the gateway. It speaks stdio. It does not open a port.

The package is `id42-work[mcp,postgres,web]` at commit `ae07623aa67c0b5d0535cb58f6a2e2b204736e50`. PyPI publishing is not used. The image installs it from git:

```text
id42-work[mcp,postgres,web] @ git+https://github.com/ID42Studio/id42-work.git@ae07623aa67c0b5d0535cb58f6a2e2b204736e50
```

`make build` passes that commit as the build arg `ID42_WORK_REF`. Set the same name in the environment to build a different commit, then rebuild. Use a full commit SHA. A branch name moves.

```bash
ID42_WORK_REF=ae07623aa67c0b5d0535cb58f6a2e2b204736e50 make build
```

## Why the virtualenv is separate

nanobot `v0.4.2` requires `mcp<2`. `id42-work[mcp,postgres,web]` requires `mcp>=2.3`. The install goes into `/opt/id42-work/venv`. `/usr/local/bin/id42-work` points at that environment's console script, so the shebang is that interpreter. `python` on `PATH` stays `/app/.venv/bin/python`, which is nanobot. `psycopg` stays in the id42-work virtualenv.

The server still accepts the `initialize` handshake used by nanobot's MCP client (protocol `2025-03-26` and the other handshake revisions through `2025-11-25`).

## Opt in

`ID42_WORK_ENABLED` defaults to `false`. The first boot then leaves `tools.mcpServers` unset. Set it to `true` before the first `make init` to write:

```json
{
  "command": "id42-work",
  "args": ["mcp", "serve", "--factory", "id42_agent.mcp_host:server"],
  "env": {"PYTHONPATH": "/opt/id42-agent"},
  "toolTimeout": 120
}
```

`type` and `url` are omitted. A command is stdio. `--http` is not an argument, and Compose does not publish `8766`. Inside the container, `127.0.0.1` is the container, and id42-work's HTTP bind cannot be changed. Do not publish that port.

`toolTimeout` is 120 seconds. A call returns when the runner returns, including when the status is `WAITING_APPROVAL`. A long executor needs a higher value. Edit `config.json` after init. Init will not overwrite that edit.

`enabledTools` is omitted, so nanobot keeps the default `["*"]` and registers both tools and resources.

A later change to `ID42_WORK_ENABLED` in `.env` does not rewrite an existing `config.json`. Edit the file in the volume, or delete it when you intend to render again.

## Default factory

`id42_agent.mcp_host:server` is copied into the image at `/opt/id42-agent` and placed on `PYTHONPATH`. It builds a `WorkMcpServer` with three works. Executions go to SQLite unless `ID42_WORK_STORAGE=postgres`.

| Work | Version | What it does | Policy |
|---|---|---|---|
| `echo` | `1.0.0` | Returns the input text. No capability is called. | `AUTONOMOUS`. No approval. |
| `note.list` | `1.0.0` | Returns the most recent notes already stored. Calls read-only `notes.read`. | `AUTONOMOUS`. No approval. |
| `note` | `1.0.0` | Appends one note. Calls side-effecting `notes.append`. | `SUPERVISED`. The action waits for approval. Pass an `idempotency_key`. |

The actor id is `id42-agent`, tenant `local`. That actor holds `notes.read` and `notes.append`. It does not hold `approve:note` or `reconcile:note`. A supervised side effect therefore returns `WAITING_APPROVAL`. `resume_execution` does not decide an approval, and it does not let the client mark a call applied.

The factory passes `session_actor` so an HTTP session can present `Id42-Actor-Credential`. The server verifies that seal with the authority key and pins the first identity of the session. A later credential for a different actor in that session is refused. Stdio has no header, so the fallback is `issue_actor`: one sealed actor for the process. `host_actor()` stays that factory actor. `id42-work approvals --resume` and `id42-work reconcile` still continue as that actor, not as a session credential.

`list_works` and `list_executions` return one page. `limit` defaults to 50 and cannot exceed 100. `next_cursor` is null on the last page. The cursor is opaque (`id42c1.`) and is not a capability. The server still checks the actor before reading the next page.

Notes are one JSON object per line in `/var/lib/id42-work/notes.jsonl`, next to the SQLite file. With Postgres they stay in that same directory on `id42-work-data`. Each new line stores `operation_id`, the capability call's idempotency key, with `text` and `byte_count`. A line that only has `text` is still listed. It cannot prove a later append. The file stops accepting new notes at 1 MiB. Each note is 1 to 2000 non-blank characters. `note.list` takes `limit` from 1 to 50 and defaults to 20. `echo` accepts up to 4000 characters.

`id42_agent.mcp_stub:server` is the empty factory from the previous image. It uses `MemoryStorage` and registers no works. Set `ID42_WORK_FACTORY=id42_agent.mcp_stub:server` before the first init to keep that behavior. State from that factory disappears when the server process exits.

## Store

Compose mounts the named volume `id42-work-data` at `/var/lib/id42-work`. The path is outside `/home/nanobot/.nanobot/workspace` and outside any home directory. The image creates the directory mode `0700`, owned by uid 1000. The entrypoint chowns it again on start, because a new named volume can be root-owned until then. Root has `CAP_CHOWN` and no `CAP_DAC_OVERRIDE`, so it changes the owner and does not recurse.

`ID42_WORK_STORAGE` selects the backend. Empty or `sqlite` uses id42-work's `SqliteStorage`. The schema is created on open. The journal mode is WAL, so `work.sqlite-wal` and `work.sqlite-shm` sit in the same directory and have to stay on the volume.

`ID42_WORK_STORE` overrides the SQLite path inside the server process. It has to be absolute and must not resolve under `/home` or `/root`. It is not used when the backend is Postgres. nanobot does not pass the gateway environment through to the stdio child, apart from `PATH`, `HOME`, and the `env` map in config. Leave the variable unset so the child uses the volume path. Putting the file in the workspace would let file tools read execution inputs.

## Postgres

`postgres` selects `PostgresStorage`. The image already has the `postgres` extra in `/opt/id42-work/venv`. `psycopg` is not installed in the nanobot virtualenv. The factory reads `ID42_WORK_DATABASE_URL`, a libpq URL or connection string. A resume in another process takes a session advisory lock, so `scripts/work-approve.sh` and `scripts/work-reconcile.sh` keep working. `scripts/work-rotate-key.sh` still only touches the key file on `id42-work-data`.

Set these in `.env` before the first init, and start the profile:

```bash
ID42_WORK_STORAGE=postgres
ID42_WORK_DATABASE_URL=postgresql://id42:change-me@postgres:5432/id42_work
ID42_POSTGRES_USER=id42
ID42_POSTGRES_PASSWORD=change-me
ID42_POSTGRES_DB=id42_work
```

```bash
docker compose --profile postgres up -d
```

The URL's user, password, and database have to match `ID42_POSTGRES_USER`, `ID42_POSTGRES_PASSWORD`, and `ID42_POSTGRES_DB`. Compose reads those three for the database container. The service publishes no host port. The gateway reaches it as `postgres` on the Compose network. A `$` in a password is written as `$$` because Compose expands `$`.

When `ID42_WORK_ENABLED=true`, the first init copies `ID42_WORK_STORAGE` and `ID42_WORK_DATABASE_URL` into `tools.mcpServers.id42-work.env`. The stdio child does not inherit the gateway environment. The operator scripts do: `docker compose run` applies the gateway service environment, which carries the same two variables. An existing `config.json` is not rewritten. If you change the backend later, put the same values in that `env` map or the MCP child stays on the old backend while the scripts follow `.env`.

The database password is a secret. Doctor reports `postgres` and whether the connection works. It does not print the URL. `docker compose down -v` deletes `id42-postgres-data` as well as `id42-work-data`.

## Authority key

The same volume holds the HMAC key at `/var/lib/id42-work/authority.key`. The default factory creates that file the first time it opens, mode `0600`, owned by uid 1000. The directory is mode `0700`. id42-work refuses a symlink, a file that is readable by group or other, and a directory that is writable by others. The key is not stored in SQLite or in Postgres. Nothing in the factory, the operator scripts, or doctor prints the key bytes, an actor credential, or the database URL.

`ID42_WORK_AUTHORITY_KEY_PATH` selects another absolute path. The constructor of `ActorAuthority` does not read that variable. The factory reads it with `authority_key_path_from_env` and passes `key_path` with `create_key_file=True`. An unset or blank value keeps the default file. A relative path is an error.

The MCP child does not inherit the gateway environment. When the variable is set before the first init, bootstrap writes it into `tools.mcpServers.id42-work.env`. `scripts/work-approve.sh` and `scripts/work-reconcile.sh` pass the same path into their own process. Those processes and the MCP server then share one key, so a seal issued by one verifies in another. Leave the variable unset to keep the default file on the volume. An existing `config.json` is not rewritten. If you set a custom path later, add the same value to that `env` map or the MCP child will keep using the default file while the scripts use the new one.

`scripts/work-rotate-key.sh` calls `rotate_keys()` on that file. It prints the new signing key id and nothing else. A retired key still verifies seals it produced, until those actors expire. Do not run two rotations at once.

`id42_agent.mcp_stub:server` stays in memory. It does not read or write this file.

`docker compose down` keeps the volume. `docker compose down -v` deletes it. A container restart does not.

An existing `config.json` that already names `id42_agent.mcp_host:server` picks up this factory after the image is rebuilt. Init does not rewrite that file. The new volume starts empty.

## Approvals

The MCP server has no approve or reject tool. Decisions use `id42-work approvals`, which needs the `mcp` extra already installed in this image. The command loads the same factory contract as `mcp serve`. It is host code, run in its own process.

```bash
id42-work approvals list --factory id42_agent.mcp_host:server
id42-work approvals show APPROVAL_ID --factory id42_agent.mcp_host:server
id42-work approvals approve APPROVAL_ID \
  --factory id42_agent.mcp_host:server --actor id42-operator --reason "checked" --resume
id42-work approvals reject APPROVAL_ID \
  --factory id42_agent.mcp_host:server --actor id42-operator --reason "no"
```

The command seals `--actor` in the execution's tenant with only that approval's reviewer grant. For `note` the default grant is `approve:note`. The operator id is `id42-operator`. The execution actor is `id42-agent`. `--resume` continues as the factory's host actor, which performs `notes.append`, and the execution reaches `COMPLETED` when that call succeeds. `reject` ends the execution as `REJECTED`. A second decision on the same approval is refused. `WAITING_REVIEW`, the state for an OUTCOME gate, is accepted. This factory does not request an OUTCOME gate.

`scripts/work-approve.sh` runs that command inside the gateway as uid 1000, with `PYTHONPATH=/opt/id42-agent` and the authority key path from [Authority key](#authority-key). `approve` adds `--resume`, matching the previous helper: a grant is followed by the host actor's resume.

```bash
./scripts/work-approve.sh list
./scripts/work-approve.sh show APPROVAL_ID
./scripts/work-approve.sh approve APPROVAL_ID --reason "checked"
./scripts/work-approve.sh reject APPROVAL_ID --reason "no"
```

`pending` is accepted as another name for `list`. The script always passes `--factory id42_agent.mcp_host:server` and, for a decision, `--actor id42-operator`. It does not read `ID42_WORK_FACTORY`.

The command is not an MCP tool. The model cannot invoke it while the shell tool is off.

Executions record `actor_id` and `tenant_id`. The seal itself is not a column in the execution store. The HMAC key that produces it is the file in [Authority key](#authority-key). A later process that opens that file can verify a credential from this one, and can still issue `id42-operator` for a new decision. `TrustedActor.to_credential` carries the seal. `from_credential` only parses it. `ActorAuthority.verify` is what makes it trusted.

## Reconciliation

`notes.append` can be left `UNCERTAIN` when the process dies after the journal records the call and before the result is stored. The note file is the proof. `NoteStore.reconcile` reads it and does not append:

| File | Verdict |
|---|---|
| Readable, and the `operation_id` is on a line | `APPLIED`, with `{"appended": true, "byte_count": N}` from that line |
| Readable, and the id is absent, including a missing file | `NOT_APPLIED` |
| Unreadable, or a line is not a note | `UNKNOWN` |

`resume` of a `FAILED` execution that still requires reconciliation calls that probe. `APPLIED` is replayed, so the note is not written again, and the execution can reach `COMPLETED`. `NOT_APPLIED` retries the same operation id. The previous ACTION approval was consumed, so the retry waits for approval again. One approval then writes one line. `UNKNOWN` leaves the call open.

A crash while the execution is `VALIDATING` marks an `IN_FLIGHT` capability call `UNCERTAIN` without probing. The following resume of that `VALIDATION_FAILED` execution probes. A crash while the execution is still `RUNNING` probes on the first resume, before the call is stored as `UNCERTAIN`, when the provider defines `reconcile`.

### Process death

`SIGKILL` does not give the runner a chance to record `UNCERTAIN` itself. The smoke test kills the process at two committed points. A new process in the same PID namespace then resumes. SQLite can take the dead pid's resume claim. Postgres drops the advisory lock when the connection closes.

**Kill during `notes.append`.** Approval has already moved the grant to `CONSUMED`. The journal row for `append-note` is `IN_FLIGHT`. The execution is `RUNNING`. `requires_reconciliation` is still false, because nothing has classified the call yet.

| Kill point | Note file | Next `resume` |
|---|---|---|
| After the line is stored | `operation_id` is on a line | Probe `APPLIED`. Journal `SUCCEEDED`. Replay does not append. Execution `COMPLETED`. One line. |
| Before the line is stored | `operation_id` is absent | Probe `NOT_APPLIED`. The consumed approval is not reused. Execution `WAITING_APPROVAL` with the same `operation_id`. One new approval calls `notes.append` once. |

When the note file cannot be read, that same resume probes `UNKNOWN`, marks the journal `UNCERTAIN`, and leaves the execution `FAILED` with `requires_reconciliation`. `notes.append` is not called. `scripts/work-reconcile.sh applied --resume` replays the supplied result and does not append. `scripts/work-reconcile.sh not-applied --resume` retries the same `operation_id` and waits for approval. One approval writes one line.

**Kill during validation.** `notes.append` has returned, so the journal is `SUCCEEDED` and the line is stored. The runner has committed `VALIDATING` and `work.outcome.validation_started`. The validation report and the outcome are not stored. The approval stays `CONSUMED`.

The next `resume` replays `append-note` and does not call `notes.append`. It does not emit `work.outcome.validation_started` again. Schema validation runs, the report is stored, and the execution reaches `COMPLETED`. `scripts/work-reconcile.sh list` does not show the call. `scripts/work-approve.sh list` does not show it. A second resume leaves the one line in place.

```text
WAITING_APPROVAL
  approve --resume consumes the grant
RUNNING, journal IN_FLIGHT
  SIGKILL inside notes.append
RUNNING, journal IN_FLIGHT
  resume, file readable
    APPLIED  -> SUCCEEDED -> COMPLETED
    NOT_APPLIED -> WAITING_APPROVAL -> one approval -> COMPLETED
  resume, file unreadable
    UNCERTAIN, FAILED, requires_reconciliation
    work-reconcile.sh applied|not-applied resolves it
RUNNING, journal SUCCEEDED
  transition VALIDATING
  SIGKILL before the validation report
VALIDATING, journal SUCCEEDED, approval CONSUMED
  resume replays, no second append
COMPLETED
```

When the file cannot prove the call, a person decides. The MCP server has no reconcile tool. The command is `id42-work reconcile`. It loads the same factory. It seals `--actor` in the execution's tenant with only `reconcile:<work_id>`. For `note` that grant is `reconcile:note`. The operator id is `id42-operator`, the same id as approvals, and a different id from `id42-agent`. The execution actor cannot reconcile its own call. `applied` and `not-applied` take `--resume` when the host actor should continue. `abandon` does not resume. It leaves the execution `FAILED` with reason `reconciliation_abandoned`.

```bash
id42-work reconcile list --factory id42_agent.mcp_host:server
id42-work reconcile show EXECUTION_ID ACTION_ID --factory id42_agent.mcp_host:server
id42-work reconcile applied EXECUTION_ID ACTION_ID \
  --factory id42_agent.mcp_host:server --actor id42-operator \
  --reason "the line is in the file" --result '{"appended": true, "byte_count": 4}' --resume
id42-work reconcile not-applied EXECUTION_ID ACTION_ID \
  --factory id42_agent.mcp_host:server --actor id42-operator \
  --reason "nothing was written" --resume
id42-work reconcile abandon EXECUTION_ID ACTION_ID \
  --factory id42_agent.mcp_host:server --actor id42-operator \
  --reason "cannot be known"
```

`list` prints calls that are still `UNCERTAIN`. `show` prints one JSON object. The result is only a `result_digest`.

`scripts/work-reconcile.sh` runs that command inside the gateway as uid 1000, with `PYTHONPATH=/opt/id42-agent` and the same authority key path as approvals. It always passes `--factory id42_agent.mcp_host:server`. A decision also passes `--actor id42-operator`. `--resume` is passed through only when the caller sets it. `abandon` has no resume.

```bash
./scripts/work-reconcile.sh list
./scripts/work-reconcile.sh show EXECUTION_ID ACTION_ID
./scripts/work-reconcile.sh applied EXECUTION_ID ACTION_ID --reason "the line is in the file" --result '{"appended": true, "byte_count": 4}' --resume
./scripts/work-reconcile.sh not-applied EXECUTION_ID ACTION_ID --reason "nothing was written" --resume
./scripts/work-reconcile.sh abandon EXECUTION_ID ACTION_ID --reason "cannot be known"
```

The action id for `note` is `append-note`. The command is not an MCP tool.

## Operator UI

The same decisions can be made in a browser. Profile `id42-work-web` runs `id42-work web serve` against `id42_agent.mcp_host:server`, the same store, and the same authority key. It is not started by `make up`.

`id42-work web serve` accepts only a loopback bind. Docker's port map reaches the container address, not `127.0.0.1` inside the container, so `docker/web-entrypoint.sh` starts the UI on `127.0.0.1:8768` and copies bytes from port `8767` to that listener. Compose publishes `127.0.0.1:8767` on the host. Leave that host address on loopback. Another container on the Compose network can reach port 8767. The token is still required.

Set one secret in `.env`. Prefer a file so the value is not also in the process environment as text you typed into a shell:

```bash
# .env — one of these. Do not commit the file.
ID42_WORK_WEB_TOKEN=replace-with-a-long-random-value
# ID42_WORK_WEB_TOKEN_FILE=/var/lib/id42-work/web.token
```

A set `ID42_WORK_WEB_TOKEN_FILE` wins over `ID42_WORK_WEB_TOKEN`. The file must be a regular file inside the container, mode `0600`, not a symlink, one UTF-8 line, at most 4096 bytes. The token is compared as a SHA-256 digest. It is not a process argument, it is not written into `config.json`, and the pages do not render it.

```bash
docker compose --profile id42-work-web up -d id42-work-web
```

Open `http://127.0.0.1:8767/`. Sign in with the token and actor id `id42-operator`. Pending approvals are on `/`. Uncertain calls are on `/reconcile`. Approve and reject match `scripts/work-approve.sh`. Applied, not-applied, and abandon match `scripts/work-reconcile.sh`. Reject and abandon ask for confirmation first. Resume stays off unless the form asks for it, and resume continues as `id42-agent`. The page is not an MCP tool.

From another computer, tunnel rather than changing the host bind:

```bash
ssh -L 8767:127.0.0.1:8767 user@host
```

`ID42_WORK_WEB_PUBLISH_PORT` changes the host port. The container port stays 8767. When `ID42_WORK_STORAGE=postgres`, start the `postgres` profile as well. The UI uses the same `ID42_WORK_DATABASE_URL` as the gateway. `ID42_WORK_AUTHORITY_KEY_PATH` on this service follows `.env`. Empty keeps `/var/lib/id42-work/authority.key`.

## Gaps in id42-work

Left as they are at commit `ae07623aa67c0b5d0535cb58f6a2e2b204736e50`:

- No approve, reject, or reconcile tool on the MCP server. Decisions use the CLIs above or the operator UI.
- Choosing Postgres does not move the note file or the authority key off `id42-work-data`.

## Your factory

The host factory is `module:callable` and returns a `WorkMcpServer`. id42-work's `examples/mcp_greeting.py` is a complete example. The shape nanobot needs is the one in id42-work's `docs/mcp.md`:

```json
{
  "tools": {
    "mcpServers": {
      "id42-work": {
        "command": "id42-work",
        "args": ["mcp", "serve", "--factory", "myapp.governed:server"],
        "env": {"PYTHONPATH": "/opt/id42-agent:/opt/id42-work-host"},
        "toolTimeout": 120
      }
    }
  }
}
```

Set that factory before the first init:

```bash
ID42_WORK_ENABLED=true
ID42_WORK_FACTORY=myapp.governed:server
ID42_WORK_PYTHONPATH=/opt/id42-work-host
```

`ID42_WORK_PYTHONPATH` is a colon-separated list of absolute paths inside the container. The rendered `PYTHONPATH` always starts with `/opt/id42-agent`, so the default module still imports. nanobot's MCP client does not pass the gateway's `PYTHONPATH` through. It keeps `PATH` and `HOME`, and it adds the `env` map from config. That is why the path is written into `config.json` instead of relying on the image `ENV`.

Mount the package read-only, from a path that is not a home directory. Doctor rejects a bind of `/home` or `/root` other than the entrypoint script. Add the mount in your own Compose override. This repository does not add one.

```yaml
services:
  nanobot-gateway:
    volumes:
      - /srv/id42-work-host:/opt/id42-work-host:ro
```

Do not put the factory in the agent workspace. File tools can read that tree. The factory is host code: it holds the authority and issues the actor. The client cannot send an actor, a grant or an approval. Approvals stay outside the server, then the client may call `resume_execution`.

Rebuild the image instead of mounting when the factory should travel with the image. A module baked in next to `docker/id42_agent` is imported from `/opt/id42-agent` without `ID42_WORK_PYTHONPATH`.

## Checks

`make doctor` prepares `/var/lib/id42-work` for uid 1000, checks that the directory is writable and a separate mount, reports the storage backend, and opens it. SQLite is the usual line. Postgres must accept a connection, and a running Postgres container must not publish a host port. The URL is not printed. It then runs `id42-work mcp serve` against `id42_agent.mcp_host:server`. The probe sends `initialize` and `tools/list`, calls `list_works` with `limit` 1, follows `next_cursor`, and requires `echo`, `note`, and `note.list`. It reads `/opt/id42-agent/ID42_WORK_REF` and requires commit `ae07623aa67c0b5d0535cb58f6a2e2b204736e50`. It checks the authority key file: absent is accepted, because the factory creates it mode `0600` on first use; a file that exists must be a regular file, mode `0600`, owned by the gateway user, and not a symlink. The directory must not be writable by others. When `config.json` names `id42-work`, its `ID42_WORK_AUTHORITY_KEY_PATH` must match the path the operator scripts use. It also requires `id42-work approvals approve --help` to offer `--factory`, `--actor`, `--reason`, and `--resume`, `id42-work reconcile abandon --help` to offer `--factory`, `--actor`, and `--reason`, and `id42-work web serve --help` to offer `--factory`, `--token-file`, and `--host`. A running operator UI must be published on `127.0.0.1`. When the gateway is up, it checks that `config.json` either omits `id42-work` or describes the stdio command above. A `url`, a non-stdio `type`, or `--http` fails the check.

The smoke test renders an enabled config, asks nanobot in the image to load it, runs the same stdio probe, and probes the stub factory. It then starts an `echo` execution and a `note` execution, restarts the gateway, and requires both ids to still be listed. `echo` is `COMPLETED`. `note` is still `WAITING_APPROVAL`. It starts further `note` executions, lists them with `id42-work approvals list`, starts the `id42-work-web` profile on a loopback port, signs in, and requires that pending note on the approvals page. It then stops that container, approves one with `--resume` and requires `COMPLETED`, and rejects another and requires `REJECTED`. A second decision on the approved id is refused. `scripts/work-approve.sh approve` is checked the same way, because that script adds `--resume` itself. It starts one more note in an MCP process, stops that process, and has a second process approve it and keep the operator credential without printing it. A new MCP process then reads the completed execution. A third process verifies the credential and the stored grant. `scripts/work-rotate-key.sh` changes the signing key id, and the earlier credential still verifies. A key file changed to mode `0644` is refused by `ActorAuthority` and by a new MCP process. Restoring mode `0600` lets the server start again. It then leaves three `note` calls `UNCERTAIN`: one after the line was stored, one before any line was stored, and one to abandon. A corrupt note file stays `UNCERTAIN`. After the file is repaired, resume completes the stored note once. Resume of the missing note waits for approval again, and one approval writes one line. `scripts/work-reconcile.sh abandon` leaves the third call `FAILED`. The execution actor is refused. `resume` does not open that execution again. It then starts the `postgres` profile in a second Compose project, approves one note from another process, abandons one uncertain note with `scripts/work-reconcile.sh`, rotates the authority key, and requires doctor to report postgres without printing the database URL. The SQLite file is not created for that run. On each backend it also SIGKILLs `notes.append` after the line is stored and before it is stored, and SIGKILLs once the execution is `VALIDATING`. Resume of a readable file probes `APPLIED` or `NOT_APPLIED`. An unreadable file becomes `UNCERTAIN` and `scripts/work-reconcile.sh applied` or `not-applied` resolves it. Validation recovery does not append again.
