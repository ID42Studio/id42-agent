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
- `id42-work[mcp,postgres,web]` installed in the image, attached over stdio only when `ID42_WORK_ENABLED=true`
- SQLite by default, with an optional `postgres` Compose profile
- `scripts/doctor.sh` and a CI smoke test that checks that the gateway becomes healthy, that `id42-work mcp serve` answers on stdio, and that Postgres can take an approval and one reconciliation

Out of this version: backup and restore, and a standalone API.

## v0.2 — governed agent

The agent still decides what it wants to do. ID42 Work governs what is allowed to run. The stdio attachment is in the image. The default factory registers `echo`, `note.list`, and `note`. Executions are stored in SQLite on `id42-work-data`, or in Postgres when that profile is selected. What is still open:

- Works and policies supplied by the operator, beyond those three. Set `ID42_WORK_FACTORY` to replace the default. `id42_agent.mcp_stub:server` keeps the empty in-memory server
- Approvals and reconciliation in the optional `id42-work-web` profile, against the same store and authority key. The nanobot WebUI still has no approval controls. Decisions stay outside the MCP server. `scripts/work-approve.sh` runs `id42-work approvals` for the default factory. `scripts/work-reconcile.sh` runs `id42-work reconcile`. The note provider probes an uncertain append on resume. The server still has no approve, reject, or reconcile tool
- The MCP server remains the policy point for tools that have side effects: what may run, against which records, and with which audit trail
- Keep the loopback deployment working with `ID42_WORK_ENABLED=false`

## v0.3 — business connectors

Connectors behind the same MCP boundary, not inside the agent image.

- Systems of record the operator actually uses (mail, calendar, files, internal APIs), each as a separate server or tool set
- Credentials stay with the connector, not in the agent workspace
- The personal v0.1 path remains usable with local Ollama and no connector installed
