#!/usr/bin/env python3
"""SIGKILL a governed note append, then read it back from a new process.

Host code for the smoke test. Not an MCP tool and not part of the factory.
The child process is killed with SIGKILL at a journal point that has already
been committed, so the crash does not depend on a timer.

Modes:

- ``probe-after`` writes the note line, then SIGKILL, while the journal is
  still ``IN_FLIGHT`` and the execution is ``RUNNING``. The next process
  resumes. The note file proves the call, so the probe is ``APPLIED``, the
  line is not written again, and the execution reaches ``COMPLETED``.
- ``probe-before`` SIGKILL before the line is written. The probe is
  ``NOT_APPLIED``. The previous approval was consumed, so the retry waits
  for a new one and does not call ``notes.append``.
- ``operator-after`` and ``operator-before`` are the same kills. The next
  process makes the note file unreadable, so the probe is ``UNKNOWN`` and the
  call becomes ``UNCERTAIN``. ``scripts/work-reconcile.sh`` then decides.
- ``validating`` SIGKILL after ``notes.append`` has returned and the execution
  has been stored as ``VALIDATING``, before the validation report. The next
  process replays the journal and does not call ``notes.append`` again.

A killed process skips ``finally``, so a SQLite resume claim can outlive it.
The recovery process runs in the same PID namespace and takes that claim
because the pid is dead. Postgres drops the advisory lock when the connection
closes.

Commands:

- ``drive MODE TEXT KEY`` runs the child, then recovery, and prints the report
- ``child MODE TEXT KEY`` is the process that is killed
- ``recover MODE`` resumes or marks the call uncertain
- ``inspect EXECUTION_ID`` prints the stored state
- ``resume EXECUTION_ID`` resumes as the host actor and prints that state
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from uuid import UUID

from id42_work import ApprovalStatus, EventType, EvidenceType, WorkRunner
from id42_work.approvals.console import ApprovalConsole
from id42_work.events.models import WorkEvent
from id42_work.evidence.models import Evidence

import id42_agent.mcp_host as host

_MARKER = host.STORE_DIRECTORY / "crash-marker.json"
_ACTION = "append-note"
_ORIGINAL_APPEND = host.NoteStore._append
_SCRIPT = str(Path(sys.argv[0]).resolve())
_TIMEOUT_S = 90


def _fail(message: str) -> None:
    print(f"id42-agent: {message}", file=sys.stderr)
    raise SystemExit(1)


def _scrub(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace")
    redacted: list[str] = []
    for line in text.splitlines():
        lowered = line.lower()
        if "postgres://" in lowered or "postgresql://" in lowered or "id42a1." in line:
            redacted.append("[redacted]")
            continue
        redacted.append(line)
    return "\n".join(redacted)[-2000:]


def _reject_secrets(raw: bytes) -> None:
    text = raw.decode("utf-8", errors="replace")
    lowered = text.lower()
    if "id42a1." in text or "postgres://" in lowered or "postgresql://" in lowered:
        _fail("a crash process printed a credential or a database URL")


def _run(args: list[str]) -> tuple[bytes, bytes, int]:
    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = proc.communicate(timeout=_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        _reject_secrets(stdout)
        _reject_secrets(stderr)
        _fail(f"{args[1]} timed out\n{_scrub(stderr)}")
    code = proc.returncode
    if code is None:
        _fail(f"{args[1]} did not exit")
    return stdout, stderr, code


def _write_marker(payload: Mapping[str, object]) -> None:
    _MARKER.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"
    temporary = _MARKER.with_name("crash-marker.json.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(temporary, 0o600)
    os.replace(temporary, _MARKER)
    directory = os.open(_MARKER.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _read_marker(mode: str) -> dict[str, object]:
    if not _MARKER.is_file():
        _fail("crash marker is missing")
    payload = json.loads(_MARKER.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("mode") != mode:
        _fail("crash marker does not match this recovery")
    return payload


def _kill_point(mode: str) -> str:
    if mode.endswith("-before"):
        return "before"
    if mode.endswith("-after"):
        return "after"
    if mode == "validating":
        return "validating"
    _fail(f"unknown crash mode {mode}")
    return ""


def _mark_call(mode: str, text: str, execution_id: UUID, operation_id: UUID) -> None:
    _write_marker(
        {
            "byte_count": len(text.encode("utf-8")),
            "execution_id": str(execution_id),
            "mode": mode,
            "operation_id": str(operation_id),
            "text": text,
        }
    )


def _install_append_kill(mode: str) -> None:
    point = _kill_point(mode)

    def _append(
        self: host.NoteStore, params: Mapping[str, object], call: object
    ) -> dict[str, object]:
        text = params.get("text")
        execution_id = getattr(call, "execution_id", None)
        operation_id = getattr(call, "operation_id", None)
        if not isinstance(text, str) or not isinstance(execution_id, UUID):
            raise RuntimeError("crash hook saw no note call")
        if not isinstance(operation_id, UUID):
            raise RuntimeError("crash hook saw no operation id")
        if point == "before":
            _mark_call(mode, text, execution_id, operation_id)
            os.kill(os.getpid(), signal.SIGKILL)
        result = _ORIGINAL_APPEND(self, params, call)
        if point == "after":
            _mark_call(mode, text, execution_id, operation_id)
            os.kill(os.getpid(), signal.SIGKILL)
        return result

    host.NoteStore._append = _append  # type: ignore[method-assign]


def _install_validation_kill(mode: str, text: str) -> None:
    async def _finish(self: WorkRunner, execution: object, *_args: object, **_kwargs: object) -> object:
        parsed = getattr(execution, "execution_id", None)
        if not isinstance(parsed, UUID):
            raise RuntimeError("validation crash saw no execution")
        journal = await self._rec.storage.executions.list_journal(parsed)
        matched = [entry for entry in journal if entry.action_id == _ACTION]
        if len(matched) != 1:
            raise RuntimeError("validation crash saw no append-note journal entry")
        _mark_call(mode, text, parsed, matched[0].operation_id)
        os.kill(os.getpid(), signal.SIGKILL)
        raise RuntimeError("process survived SIGKILL")

    WorkRunner._finish_validation = _finish  # type: ignore[method-assign]


def _note_count(operation_id: str) -> int:
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
            if isinstance(item, dict) and item.get("operation_id") == operation_id:
                count += 1
    return count


def _event_count(events: tuple[WorkEvent, ...], event_type: EventType) -> int:
    count = 0
    for event in events:
        if event.event_type is not event_type:
            continue
        if event_type in {EventType.ACTION_INVOKED, EventType.ACTION_REPLAYED}:
            if event.payload.get("action_id") != _ACTION:
                continue
        count += 1
    return count


def _report_count(evidence: tuple[Evidence, ...]) -> int:
    return sum(1 for item in evidence if item.evidence_type is EvidenceType.VALIDATION_REPORT)


def _probe_verdict(evidence: tuple[Evidence, ...]) -> str:
    found = ""
    for item in evidence:
        if item.evidence_type is not EvidenceType.RECONCILIATION_DECISION:
            continue
        if item.metadata.get("source") != "provider":
            continue
        if item.metadata.get("action_id") != _ACTION:
            continue
        decision = item.metadata.get("decision")
        if isinstance(decision, str):
            found = decision
    return found or "none"


async def _snapshot(runner: WorkRunner, execution_id: UUID) -> dict[str, str]:
    execution = await runner.get(execution_id)
    if execution is None:
        _fail("execution is missing")
    journal = await runner.journal(execution_id)
    matched = [entry for entry in journal if entry.action_id == _ACTION]
    if len(matched) != 1:
        _fail(f"expected one append-note journal entry, found {len(matched)}")
    entry = matched[0]
    events = await runner.events(execution_id)
    evidence = await runner.evidence(execution_id)
    approvals = await runner.approvals_for(execution_id)
    note_approvals = [item for item in approvals if item.action_id == _ACTION]
    pending = [item for item in note_approvals if item.status is ApprovalStatus.PENDING]
    if len(pending) > 1:
        _fail("more than one pending note approval")
    operation = str(entry.operation_id)
    fields = {
        "status": execution.status.value,
        "status_reason": execution.status_reason or "",
        "requires_reconciliation": "true" if execution.requires_reconciliation else "false",
        "journal_status": entry.status.value,
        "operation_id": operation,
        "operation_notes": str(_note_count(operation)),
        "invoked": str(_event_count(events, EventType.ACTION_INVOKED)),
        "replayed": str(_event_count(events, EventType.ACTION_REPLAYED)),
        "validation_started": str(_event_count(events, EventType.VALIDATION_STARTED)),
        "validation_report": str(_report_count(evidence)),
        "probe": _probe_verdict(evidence),
        "approval_consumed": str(
            sum(1 for item in note_approvals if item.status is ApprovalStatus.CONSUMED)
        ),
        "approval_granted": str(
            sum(1 for item in note_approvals if item.status is ApprovalStatus.GRANTED)
        ),
        "approval_pending": str(len(pending)),
        "pending_approval_id": str(pending[0].approval_id) if pending else "",
        "outcome": "true" if execution.outcome is not None else "false",
    }
    return fields


def _print_fields(prefix: str, fields: Mapping[str, str]) -> None:
    for key, value in fields.items():
        print(f"{prefix}_{key}={value}", flush=True)


async def _child(mode: str, text: str, key: str) -> None:
    point = _kill_point(mode)
    if point == "validating":
        _install_validation_kill(mode, text)
    else:
        _install_append_kill(mode)
    runner, authority, storage = host.open_host()
    try:
        actor = host.issue_agent(authority)
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
            if approval.status is ApprovalStatus.PENDING
        ]
        if len(pending) != 1:
            _fail(f"expected one pending approval, found {len(pending)}")
        console = ApprovalConsole(
            runner,
            authority=authority,
            resume_actor=lambda: host.issue_agent(authority),
        )
        await console.approve(
            pending[0].approval_id,
            actor_id=host.OPERATOR_ID,
            reason="crash",
            resume=True,
        )
    finally:
        storage.close()
    _fail("process survived the crash hook")


def _withhold_notes(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
    os.chmod(path, 0)


def _restore_notes(path: Path) -> None:
    if path.exists():
        os.chmod(path, 0o600)


async def _recover(mode: str) -> None:
    marker = _read_marker(mode)
    execution_raw = marker.get("execution_id")
    operation_raw = marker.get("operation_id")
    byte_count = marker.get("byte_count")
    if not isinstance(execution_raw, str) or not isinstance(operation_raw, str):
        _fail("crash marker has no ids")
    if isinstance(byte_count, bool) or not isinstance(byte_count, int):
        _fail("crash marker has no byte_count")
    try:
        execution_id = UUID(execution_raw)
    except ValueError:
        _fail("crash marker execution id is not a UUID")
    runner, authority, storage = host.open_host()
    notes = host.notes_file()
    try:
        before = await _snapshot(runner, execution_id)
        if before["operation_id"] != operation_raw:
            _fail("journal operation id does not match the crash marker")
        actor = host.issue_agent(authority)
        if mode.startswith("operator-"):
            _withhold_notes(notes)
            try:
                await runner.resume(execution_id, actor=actor)
            finally:
                _restore_notes(notes)
        else:
            await runner.resume(execution_id, actor=actor)
        after = await _snapshot(runner, execution_id)
    finally:
        _restore_notes(notes)
        storage.close()
    if notes.exists():
        notes_mode = f"{notes.stat().st_mode & 0o777:03o}"
    else:
        notes_mode = "absent"
    print(f"execution_id={execution_id}", flush=True)
    print(f"operation_id={operation_raw}", flush=True)
    print(f"byte_count={byte_count}", flush=True)
    print(f"notes_mode={notes_mode}", flush=True)
    _print_fields("before", before)
    _print_fields("after", after)


async def _inspect(execution_id: UUID) -> None:
    runner, _authority, storage = host.open_host()
    try:
        fields = await _snapshot(runner, execution_id)
    finally:
        storage.close()
    print(f"execution_id={execution_id}", flush=True)
    _print_fields("state", fields)


async def _resume(execution_id: UUID) -> None:
    runner, authority, storage = host.open_host()
    try:
        await runner.resume(execution_id, actor=host.issue_agent(authority))
        fields = await _snapshot(runner, execution_id)
    finally:
        storage.close()
    print(f"execution_id={execution_id}", flush=True)
    _print_fields("state", fields)


def _parse_execution_id(raw: str) -> UUID:
    try:
        return UUID(raw)
    except ValueError:
        _fail("execution id must be a UUID")
        raise AssertionError("unreachable") from None


def drive(mode: str, text: str, key: str) -> None:
    _kill_point(mode)
    stdout, stderr, code = _run([sys.executable, _SCRIPT, "child", mode, text, key])
    _reject_secrets(stdout)
    _reject_secrets(stderr)
    if code != -signal.SIGKILL:
        _fail(f"child exited {code}, expected SIGKILL\n{_scrub(stderr)}")
    print("child_signal=9", flush=True)
    stdout, stderr, code = _run([sys.executable, _SCRIPT, "recover", mode])
    _reject_secrets(stdout)
    _reject_secrets(stderr)
    if code != 0:
        _fail(f"recover exited {code}\n{_scrub(stderr)}")
    sys.stdout.buffer.write(stdout)


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "drive":
        if len(sys.argv) != 5:
            _fail("usage: process_crash.py drive MODE TEXT KEY")
        drive(sys.argv[2], sys.argv[3], sys.argv[4])
    elif command == "child":
        if len(sys.argv) != 5:
            _fail("usage: process_crash.py child MODE TEXT KEY")
        asyncio.run(_child(sys.argv[2], sys.argv[3], sys.argv[4]))
    elif command == "recover":
        if len(sys.argv) != 3:
            _fail("usage: process_crash.py recover MODE")
        asyncio.run(_recover(sys.argv[2]))
    elif command == "inspect":
        if len(sys.argv) != 3:
            _fail("usage: process_crash.py inspect EXECUTION_ID")
        asyncio.run(_inspect(_parse_execution_id(sys.argv[2])))
    elif command == "resume":
        if len(sys.argv) != 3:
            _fail("usage: process_crash.py resume EXECUTION_ID")
        asyncio.run(_resume(_parse_execution_id(sys.argv[2])))
    else:
        _fail(f"unknown command {command}")


if __name__ == "__main__":
    main()
