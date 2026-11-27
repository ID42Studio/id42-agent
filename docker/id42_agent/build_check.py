"""Image build check for the default factory and note reconciliation.

Runs once while the image is built, then the Dockerfile removes this file.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import id42_agent.mcp_host as host
import id42_agent.mcp_stub as stub
from id42_work import ActorAuthority, CapabilityCall, ReconciliationVerdict


def _call(operation: object) -> CapabilityCall:
    return CapabilityCall(
        execution_id=uuid4(),
        action_id="append-note",
        operation_id=operation,
        actor_id=host.ACTOR_ID,
        tenant_id=host.TENANT_ID,
    )


def main() -> None:
    server = host.server()
    listed = asyncio.run(server.list_works())
    ids = sorted(item["id"] for item in listed["works"])
    if ids != sorted(host.DEFAULT_WORK_IDS):
        raise SystemExit(f"works {ids}")
    if type(stub.server()).__name__ != "WorkMcpServer":
        raise SystemExit("stub factory did not return a WorkMcpServer")
    actor = host.issue_agent(ActorAuthority(host.AUTHORITY_ID))
    if actor.actor_id != host.ACTOR_ID or actor.has_grant("reconcile:note"):
        raise SystemExit("execution actor holds reconcile:note")
    if actor.has_grant("approve:note") or not actor.has_grant(host.NOTES_APPEND):
        raise SystemExit("execution actor grants are wrong")

    legacy_path = Path("/tmp/id42-notes-legacy.jsonl")
    legacy_path.write_text('{"text":"legacy"}\n', encoding="utf-8")
    legacy = host.NoteStore(legacy_path)
    listed_notes = asyncio.run(legacy.invoke(host.NOTES_READ, {"limit": 20}, _call(uuid4())))
    if listed_notes != {"notes": ["legacy"], "total": 1}:
        raise SystemExit(f"legacy note did not list: {listed_notes}")
    missed = asyncio.run(legacy.reconcile(_call(uuid4())))
    if missed.verdict is not ReconciliationVerdict.NOT_APPLIED:
        raise SystemExit(f"legacy file reconciled as {missed.verdict}")

    notes_path = Path("/tmp/id42-notes-build.jsonl")
    store = host.NoteStore(notes_path)
    operation = uuid4()
    empty = asyncio.run(store.reconcile(_call(operation)))
    if empty.verdict is not ReconciliationVerdict.NOT_APPLIED:
        raise SystemExit(f"missing file reconciled as {empty.verdict}")
    first = asyncio.run(store.invoke(host.NOTES_APPEND, {"text": "built"}, _call(operation)))
    second = asyncio.run(store.invoke(host.NOTES_APPEND, {"text": "built"}, _call(operation)))
    if first != second or notes_path.read_text(encoding="utf-8").count("\n") != 1:
        raise SystemExit("append was not idempotent")
    applied = asyncio.run(store.reconcile(_call(operation)))
    if applied.verdict is not ReconciliationVerdict.APPLIED or applied.result != first:
        raise SystemExit(f"applied probe was {applied}")
    notes_path.write_text(notes_path.read_text(encoding="utf-8") + "{not-json\n", encoding="utf-8")
    corrupt = asyncio.run(store.reconcile(_call(operation)))
    if corrupt.verdict is not ReconciliationVerdict.UNKNOWN:
        raise SystemExit(f"corrupt file reconciled as {corrupt.verdict}")
    blocked = Path("/tmp/id42-notes-dir")
    blocked.mkdir(exist_ok=True)
    unreadable = asyncio.run(host.NoteStore(blocked).reconcile(_call(operation)))
    if unreadable.verdict is not ReconciliationVerdict.UNKNOWN:
        raise SystemExit(f"unreadable file reconciled as {unreadable.verdict}")


if __name__ == "__main__":
    main()
