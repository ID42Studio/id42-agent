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

`id42_agent.mcp_host:server` is copied into the image at `/opt/id42-agent` and placed on `PYTHONPATH`. It builds a `WorkMcpServer` with:

- no registered works
- an actor issued in the process (`actor_id` `id42-agent`)
- `MemoryStorage`, which disappears when the server process exits

`list_works` answers with an empty list. `start_execution` cannot run anything until a factory registers a work. This factory is the safe default so the server can start. It is not a policy for a real system.

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

`make doctor` runs `id42-work mcp serve` against `id42_agent.mcp_host:server` in a one-shot container, sends `initialize` and `tools/list`, and calls `list_works`. It also reads `/opt/id42-agent/ID42_WORK_REF`. When the gateway is up, it checks that `config.json` either omits `id42-work` or describes the stdio command above. A `url`, a non-stdio `type`, or `--http` fails the check.

The smoke test renders an enabled config, asks nanobot in the image to load it, and runs the same stdio probe.
