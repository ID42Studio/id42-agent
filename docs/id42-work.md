# ID42 Work

The gateway can start [id42-work](https://github.com/ID42Studio/id42-work) as an MCP server. The server is a child process of the gateway. It speaks stdio. It does not open a port.

The package is `id42-work[mcp]` at commit `07eaa2a0787c206398b1296091fea1317f1ff27b`. PyPI publishing is not used. The image installs it from git:

```text
id42-work[mcp] @ git+https://github.com/ID42Studio/id42-work.git@07eaa2a0787c206398b1296091fea1317f1ff27b
```

`make build` passes that commit as the build arg `ID42_WORK_REF`. Set the same name in the environment to build a different commit, then rebuild. Use a full commit SHA. A branch name moves.

```bash
ID42_WORK_REF=07eaa2a0787c206398b1296091fea1317f1ff27b make build
```

## Why the virtualenv is separate

nanobot `v0.4.2` requires `mcp<2`. `id42-work[mcp]` requires `mcp>=2.3`. The install goes into `/opt/id42-work/venv`. `/usr/local/bin/id42-work` points at that environment's console script, so the shebang is that interpreter. `python` on `PATH` stays `/app/.venv/bin/python`, which is nanobot.

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

`id42_agent.mcp_host:server` is copied into the image at `/opt/id42-agent` and placed on `PYTHONPATH`. It builds a `WorkMcpServer` with SQLite at `/var/lib/id42-work/work.sqlite` and three works.

| Work | Version | What it does | Policy |
|---|---|---|---|
| `echo` | `1.0.0` | Returns the input text. No capability is called. | `AUTONOMOUS`. No approval. |
| `note.list` | `1.0.0` | Returns the most recent notes already stored. Calls read-only `notes.read`. | `AUTONOMOUS`. No approval. |
| `note` | `1.0.0` | Appends one note. Calls side-effecting `notes.append`. | `SUPERVISED`. The action waits for approval. Pass an `idempotency_key`. |

The actor id is `id42-agent`, tenant `local`. That actor holds `notes.read` and `notes.append`. It does not hold `approve:note`. A supervised side effect therefore returns `WAITING_APPROVAL`. `resume_execution` does not decide.

Notes are one JSON object per line in `/var/lib/id42-work/notes.jsonl`, next to the database. The file stops accepting new notes at 1 MiB. Each note is 1 to 2000 non-blank characters. `note.list` takes `limit` from 1 to 50 and defaults to 20. `echo` accepts up to 4000 characters.

`id42_agent.mcp_stub:server` is the empty factory from the previous image. It uses `MemoryStorage` and registers no works. Set `ID42_WORK_FACTORY=id42_agent.mcp_stub:server` before the first init to keep that behavior. State from that factory disappears when the server process exits.

## Store

Compose mounts the named volume `id42-work-data` at `/var/lib/id42-work`. The path is outside `/home/nanobot/.nanobot/workspace` and outside any home directory. The image creates the directory mode `0700`, owned by uid 1000. The entrypoint chowns it again on start, because a new named volume can be root-owned until then. Root has `CAP_CHOWN` and no `CAP_DAC_OVERRIDE`, so it changes the owner and does not recurse.

id42-work's `SqliteStorage` is the durable backend with no extra dependency. The schema is created on open. The journal mode is WAL, so `work.sqlite-wal` and `work.sqlite-shm` sit in the same directory and have to stay on the volume. `PostgresStorage` is not installed.

`ID42_WORK_STORE` overrides the SQLite path inside the server process. It has to be absolute and must not resolve under `/home` or `/root`. nanobot does not pass the gateway environment through to the stdio child, apart from `PATH`, `HOME`, and the `env` map in config. Leave the variable unset so the child uses the volume path. Putting the file in the workspace would let file tools read execution inputs.

`docker compose down` keeps the volume. `docker compose down -v` deletes it. A container restart does not.

An existing `config.json` that already names `id42_agent.mcp_host:server` picks up this factory after the image is rebuilt. Init does not rewrite that file. The new volume starts empty.

## Approvals

id42-work's MCP server has no approve or reject tool, and `id42-work` has no approve command. Decisions are host code. This image ships `python -m id42_agent.approve`, run with the id42-work interpreter so it can import the SDK:

```bash
./scripts/work-approve.sh pending
./scripts/work-approve.sh approve APPROVAL_ID --reason "checked"
./scripts/work-approve.sh reject APPROVAL_ID --reason "no"
```

`approve` grants the decision as actor `id42-operator` (grant `approve:note` only) and then resumes the execution as `id42-agent`, which is the call that performs `notes.append`. The reviewer and the execution actor are different ids, which is what id42-work requires. `reject` ends the execution as `REJECTED`.

The helper is not registered as an MCP tool. The model cannot invoke it while the shell tool is off.

Actor seals are not stored. id42-work keeps the sealing key in process memory and generates a new one on every process. Executions record `actor_id` and `tenant_id`, so a later process that issues those same ids can resume, and `id42-operator` can still approve.

## Gaps in id42-work

Left as they are at commit `07eaa2a0787c206398b1296091fea1317f1ff27b`:

- No approve or reject tool, and no CLI for a decision. The helper above lives in this repository.
- `ActorAuthority` keys stay in memory. There is no durable seal to reload.
- An `IN_FLIGHT` capability call becomes `UNCERTAIN` after a crash. There is no reconciliation tool. The note provider does not retry a write it cannot prove.
- `PostgresStorage` needs the `postgres` extra. This image does not install it.

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

`make doctor` prepares `/var/lib/id42-work` for uid 1000, checks that the directory is writable and a separate mount, opens the SQLite file, then runs `id42-work mcp serve` against `id42_agent.mcp_host:server`. The probe sends `initialize` and `tools/list`, calls `list_works`, and requires `echo`, `note`, and `note.list`. It also reads `/opt/id42-agent/ID42_WORK_REF`. When the gateway is up, it checks that `config.json` either omits `id42-work` or describes the stdio command above. A `url`, a non-stdio `type`, or `--http` fails the check.

The smoke test renders an enabled config, asks nanobot in the image to load it, runs the same stdio probe, and probes the stub factory. It then starts an `echo` execution and a `note` execution, restarts the gateway, and requires both ids to still be listed. `echo` is `COMPLETED`. `note` is still `WAITING_APPROVAL`.
