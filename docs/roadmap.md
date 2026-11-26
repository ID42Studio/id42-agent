# Roadmap

Versions here are id42-agent milestones. They are not nanobot release numbers. The runtime stays a pinned upstream nanobot unless a milestone says otherwise.

## v0.1 — personal agent

This repository. One person, one machine.

- Compose build of nanobot `v0.4.2` at `0dc8679edcc6061fbcdf3de13c7e16000e9d1b65`
- External Ollama by default, optional local Ollama profile
- WebUI and health on loopback
- Telegram long polling with a mandatory allowlist when enabled
- Shell tool off, workspace restriction on
- First-boot config that does not overwrite later WebUI edits
- `scripts/doctor.sh` and a CI smoke test that only checks that the gateway becomes healthy

Out of this version: backup and restore, a standalone API, Postgres, and any ID42 Work connection.

## v0.2 — governed agent

The agent still decides what it wants to do. ID42 Work governs what is allowed to run.

- Ship an MCP server as `id42-work[mcp]`
- Attach it through nanobot's existing `tools.mcpServers` entry (stdio or HTTP). No fork of nanobot required for that slot
- The MCP server is the policy point for tools that have side effects: what may run, against which records, and with which audit trail
- Keep the v0.1 loopback deployment working without that server

## v0.3 — business connectors

Connectors behind the same MCP boundary, not inside the agent image.

- Systems of record the operator actually uses (mail, calendar, files, internal APIs), each as a separate server or tool set
- Credentials stay with the connector, not in the agent workspace
- The personal v0.1 path remains usable with local Ollama and no connector installed
