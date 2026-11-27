#!/usr/bin/env python3
"""Plant an UNCERTAIN note call, and read the note file back.

Host code for the smoke test. Not an MCP tool. `crash before` dies inside
notes.append before the line is stored. `crash after` stores the line, then
dies. id42-work records that as UNCERTAIN. A later resume, without this
patch, probes the file.

Commands:

- ``crash before|after TEXT KEY``
- ``inspect EXECUTION_ID TEXT``
- ``seed-legacy TEXT``
- ``corrupt-notes``
- ``repair-notes``
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Mapping

from id42_work import UncertainOutcomeError
from id42_work.approvals.console import ApprovalConsole

import id42_agent.mcp_host as host

_ORIGINAL_APPEND = host.NoteStore._append


def _fail(message: str) -> None:
    print(f"id42-agent: {message}", file=sys.stderr)
    raise SystemExit(1)


def _install_crash(mode: str) -> None:
    def _append(
        self: host.NoteStore, params: Mapping[str, object], call: object
    ) -> dict[str, object]:
        if mode == "before":
            raise UncertainOutcomeError("note store crashed before the append")
        _ORIGINAL_APPEND(self, params, call)
        raise UncertainOutcomeError("note store crashed after the append")

    host.NoteStore._append = _append  # type: ignore[method-assign]


def _note_count(text: str) -> int:
    path = host.notes_file()
    if not path.exists():
        return 0
    count = 0
    with path.open("r", encoding="utf-8") as handle:
        for raw in handle:
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                item = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and item.get("text") == text:
                count += 1
    return count


def _print_state(execution: object, text: str) -> None:
    status = getattr(execution, "status", None)
    status_value = getattr(status, "value", "")
    requires = bool(getattr(execution, "requires_reconciliation", False))
    print(f"execution_id={execution.execution_id}")
    print(f"status={status_value}")
    print(f"requires_reconciliation={'true' if requires else 'false'}")
    print(f"status_reason={getattr(execution, 'status_reason', None) or ''}")
    print(f"actor_id={execution.actor_id}")
    print(f"note_count={_note_count(text)}")


async def _crash(mode: str, text: str, key: str) -> None:
    if mode not in {"before", "after"}:
        _fail("crash mode must be before or after")
    _install_crash(mode)
    runner, authority, storage = host.open_host()
    try:
        actor = host.issue_agent(authority)
        if actor.has_grant("reconcile:note") or actor.actor_id != host.ACTOR_ID:
            _fail("execution actor can reconcile note")
        execution = await runner.run(
            work_id=host.WORK_NOTE,
            version=host.WORK_VERSION,
            input_data={"text": text},
            actor=actor,
            idempotency_key=key,
        )
        if execution.status.value != "WAITING_APPROVAL":
            _fail(f"note did not wait for approval: {execution.status.value}")
        pending = [
            approval
            for approval in await runner.approvals_for(execution.execution_id)
            if approval.status.value == "PENDING"
        ]
        if len(pending) != 1:
            _fail(f"expected one pending approval, found {len(pending)}")
        console = ApprovalConsole(
            runner,
            authority=authority,
            resume_actor=lambda: host.issue_agent(authority),
        )
        decision = await console.approve(
            pending[0].approval_id,
            actor_id=host.OPERATOR_ID,
            reason="crash",
            resume=True,
        )
        journal = await runner.journal(decision.execution.execution_id)
        matched = [entry for entry in journal if entry.action_id == "append-note"]
        if len(matched) != 1:
            _fail(f"expected one append-note journal entry, found {len(matched)}")
        entry = matched[0]
        _print_state(decision.execution, text)
        print(f"action_id={entry.action_id}")
        print(f"journal_status={entry.status.value}")
        print(f"capability={entry.capability}")
        print(f"resumed={'true' if decision.resumed else 'false'}")
    finally:
        storage.close()


async def _inspect(execution_id: str, text: str) -> None:
    runner, _authority, storage = host.open_host()
    try:
        from uuid import UUID

        try:
            parsed = UUID(execution_id)
        except ValueError:
            _fail("execution id must be a UUID")
        execution = await runner.get(parsed)
        if execution is None:
            _fail("execution is missing")
        journal = await runner.journal(parsed)
        matched = [entry for entry in journal if entry.action_id == "append-note"]
        if len(matched) != 1:
            _fail(f"expected one append-note journal entry, found {len(matched)}")
        entry = matched[0]
        _print_state(execution, text)
        print(f"action_id={entry.action_id}")
        print(f"journal_status={entry.status.value}")
        print(f"capability={entry.capability}")
    finally:
        storage.close()


def _seed_legacy(text: str) -> None:
    path = host.notes_file()
    line = json.dumps({"text": text}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        handle.write(line + b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(path, 0o600)
    print(f"seeded={text}")


def _corrupt_notes() -> None:
    path = host.notes_file()
    with path.open("ab") as handle:
        handle.write(b"{not-json\n")
        handle.flush()
        os.fsync(handle.fileno())
    print("notes-corrupt")


def _repair_notes() -> None:
    path = host.notes_file()
    kept: list[str] = []
    with path.open("r", encoding="utf-8") as handle:
        for raw in handle:
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                item = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                kept.append(stripped)
    path.write_text("".join(line + "\n" for line in kept), encoding="utf-8")
    os.chmod(path, 0o600)
    print(f"notes-repaired={len(kept)}")


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "crash":
        if len(sys.argv) != 5:
            _fail("usage: reconcile_crash.py crash before|after TEXT KEY")
        asyncio.run(_crash(sys.argv[2], sys.argv[3], sys.argv[4]))
    elif command == "inspect":
        if len(sys.argv) != 4:
            _fail("usage: reconcile_crash.py inspect EXECUTION_ID TEXT")
        asyncio.run(_inspect(sys.argv[2], sys.argv[3]))
    elif command == "seed-legacy":
        if len(sys.argv) != 3:
            _fail("usage: reconcile_crash.py seed-legacy TEXT")
        _seed_legacy(sys.argv[2])
    elif command == "corrupt-notes":
        if len(sys.argv) != 2:
            _fail("usage: reconcile_crash.py corrupt-notes")
        _corrupt_notes()
    elif command == "repair-notes":
        if len(sys.argv) != 2:
            _fail("usage: reconcile_crash.py repair-notes")
        _repair_notes()
    else:
        _fail(f"unknown command {command}")


if __name__ == "__main__":
    main()
