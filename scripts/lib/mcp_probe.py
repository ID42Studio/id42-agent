#!/usr/bin/env python3
"""Speak to id42-work's stdio MCP server.

Starts ``id42-work mcp serve --factory <module:callable>`` and the initialize
handshake (protocol 2025-03-26). Does not open a socket.

Commands:

- ``probe`` (default) checks ``tools/list`` and ``list_works``. The default
  factory must advertise echo, note, and note.list.
- ``persist-start`` runs echo and note, then prints their execution ids.
- ``persist-check ECHO_ID NOTE_ID`` lists executions and requires those ids.
- ``start-note TEXT KEY`` starts one note and prints its id and status.
- ``execution-status EXECUTION_ID`` prints the stored status of one execution.
- ``resume-note EXECUTION_ID`` resumes one execution and prints the same fields.
- ``list-notes`` runs ``note.list`` and prints each stored note.
"""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping

_FACTORY = os.environ.get("ID42_MCP_FACTORY", "id42_agent.mcp_host:server")
_DEFAULT_FACTORY = "id42_agent.mcp_host:server"
# Keep in sync with docker/id42_agent/mcp_host.py DEFAULT_WORK_IDS.
_DEFAULT_WORKS = ("echo", "note", "note.list")
_PROTOCOL = "2025-03-26"
_DEADLINE_S = 40
_EXPECTED_TOOLS = (
    "list_works",
    "start_execution",
    "get_execution",
    "resume_execution",
    "list_executions",
    "cancel_execution",
)
_ECHO_TEXT = "smoke-persist"
_NOTE_TEXT = "smoke-note"
_NOTE_KEY = "smoke-note"


def _fail(message: str, stderr: bytes) -> None:
    text = stderr.decode("utf-8", errors="replace").strip()
    if text:
        print(text[-2000:], file=sys.stderr)
    print(f"id42-agent: {message}", file=sys.stderr)
    raise SystemExit(1)


def _write(proc: subprocess.Popen[bytes], payload: Mapping[str, object]) -> None:
    if proc.stdin is None:
        raise RuntimeError("stdin is closed")
    line = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
    proc.stdin.write(line)
    proc.stdin.flush()


def _message_from_line(line: bytes, request_id: int) -> dict[str, object] | None:
    stripped = line.strip()
    if not stripped.startswith(b"{"):
        return None
    try:
        message = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(message, dict) or message.get("id") != request_id:
        return None
    return message


def _read_matching(proc: subprocess.Popen[bytes], request_id: int, deadline: float) -> dict[str, object]:
    if proc.stdout is None:
        raise RuntimeError("stdout is closed")
    fd = proc.stdout.fileno()
    pending = bytearray()
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], min(1.0, deadline - time.monotonic()))
        if not ready:
            if proc.poll() is not None:
                break
            continue
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        pending.extend(chunk)
        while True:
            newline = pending.find(b"\n")
            if newline < 0:
                break
            line = bytes(pending[:newline])
            del pending[: newline + 1]
            message = _message_from_line(line, request_id)
            if message is not None:
                return message
    raise TimeoutError(f"no response for request {request_id}")


def _stop(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


class _Session:
    def __init__(self) -> None:
        self.stderr_chunks: list[bytes] = []
        self.proc = subprocess.Popen(
            ["id42-work", "mcp", "serve", "--factory", _FACTORY],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self.deadline = time.monotonic() + _DEADLINE_S
        self._next_id = 1

        def _drain() -> None:
            if self.proc.stderr is None:
                return
            data = self.proc.stderr.read()
            if data:
                self.stderr_chunks.append(data[-4000:])

        self._drainer = threading.Thread(target=_drain, daemon=True)
        self._drainer.start()

    def stderr(self) -> bytes:
        return b"".join(self.stderr_chunks)

    def fail(self, message: str) -> None:
        # The child can exit and close stdout before this thread has read
        # stderr. Stop it and join the reader first, or the refusal is lost.
        self.close()
        _fail(message, self.stderr())

    def close(self) -> None:
        _stop(self.proc)
        self._drainer.join(timeout=1)

    def _request(self, method: str, params: Mapping[str, object] | None) -> dict[str, object]:
        request_id = self._next_id
        self._next_id += 1
        payload: dict[str, object] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = dict(params)
        _write(self.proc, payload)
        message = _read_matching(self.proc, request_id, self.deadline)
        if "error" in message:
            self.fail(f"{method} failed: {message['error']}")
        return message

    def initialize(self) -> None:
        message = self._request(
            "initialize",
            {
                "protocolVersion": _PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "id42-agent-doctor", "version": "0"},
            },
        )
        result = message.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("protocolVersion"), str):
            self.fail("initialize returned no protocolVersion")
        _write(self.proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})

    def tool(self, name: str, arguments: Mapping[str, object]) -> dict[str, object]:
        message = self._request(
            "tools/call",
            {"name": name, "arguments": dict(arguments)},
        )
        return _tool_payload(message, self.fail)


def _content_text(result: Mapping[str, object]) -> str:
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    chunks: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
            chunks.append(item["text"])
    return "\n".join(chunks)


def _tool_payload(
    message: Mapping[str, object],
    fail: object,
) -> dict[str, object]:
    result = message.get("result")
    if not isinstance(result, dict):
        fail("tool result is not an object")
        raise AssertionError("unreachable")
    if result.get("isError") is True:
        detail = _content_text(result) or "tool error"
        fail(detail)
        raise AssertionError("unreachable")
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    text = _content_text(result)
    if text:
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError:
            loaded = None
        if isinstance(loaded, dict):
            return loaded
    if any(key in result for key in ("works", "executions", "execution_id")):
        return result
    fail("tool result had no JSON payload")
    raise AssertionError("unreachable")


def _work_ids(payload: Mapping[str, object], session: _Session) -> list[str]:
    works = payload.get("works")
    if not isinstance(works, list):
        session.fail("list_works result did not include works")
    ids: list[str] = []
    for item in works:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            ids.append(item["id"])
    return ids


def _require_default_works(ids: list[str], session: _Session) -> None:
    if _FACTORY != _DEFAULT_FACTORY:
        return
    found = sorted(ids)
    expected = sorted(_DEFAULT_WORKS)
    if found != expected:
        session.fail(f"list_works returned {found}, expected {expected}")


def probe() -> None:
    session = _Session()
    try:
        session.initialize()
        listed = session._request("tools/list", {})
        tools = listed.get("result") if isinstance(listed.get("result"), dict) else {}
        names = [
            item.get("name")
            for item in tools.get("tools", [])
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        ]
        missing = [name for name in _EXPECTED_TOOLS if name not in names]
        if missing:
            session.fail(f"tools/list missing {', '.join(missing)}")
        payload = session.tool("list_works", {})
        ids = _work_ids(payload, session)
        _require_default_works(ids, session)
        print("works=" + ",".join(sorted(ids)))
    except (TimeoutError, OSError, BrokenPipeError) as exc:
        session.fail(str(exc))
    finally:
        session.close()
    print("mcp-stdio-ok")


def persist_start() -> None:
    session = _Session()
    echo_id = ""
    note_id = ""
    try:
        session.initialize()
        echo = session.tool(
            "start_execution",
            {
                "work_id": "echo",
                "version": "1.0.0",
                "input_data": {"text": _ECHO_TEXT},
            },
        )
        if echo.get("status") != "COMPLETED" or echo.get("work_id") != "echo":
            session.fail(f"echo execution did not complete: {echo.get('status')!r}")
        outcome = echo.get("outcome")
        output = outcome.get("output") if isinstance(outcome, dict) else None
        if not isinstance(output, dict) or output.get("text") != _ECHO_TEXT:
            session.fail("echo output did not match the input")
        echo_id = str(echo.get("execution_id") or "")
        note = session.tool(
            "start_execution",
            {
                "work_id": "note",
                "version": "1.0.0",
                "input_data": {"text": _NOTE_TEXT},
                "idempotency_key": _NOTE_KEY,
            },
        )
        if note.get("status") != "WAITING_APPROVAL" or note.get("work_id") != "note":
            session.fail(f"note execution was not waiting for approval: {note.get('status')!r}")
        note_id = str(note.get("execution_id") or "")
        if not echo_id or not note_id:
            session.fail("start_execution returned no execution id")
    except (TimeoutError, OSError, BrokenPipeError) as exc:
        session.fail(str(exc))
    finally:
        session.close()
    print(f"execution_id={echo_id}")
    print(f"note_execution_id={note_id}")
    print("note_status=WAITING_APPROVAL")


def start_note(text: str, key: str) -> None:
    session = _Session()
    note_id = ""
    status = ""
    try:
        session.initialize()
        note = session.tool(
            "start_execution",
            {
                "work_id": "note",
                "version": "1.0.0",
                "input_data": {"text": text},
                "idempotency_key": key,
            },
        )
        note_id = str(note.get("execution_id") or "")
        status = str(note.get("status") or "")
        if not note_id or not status:
            session.fail("start_execution returned no execution id or status")
    except (TimeoutError, OSError, BrokenPipeError) as exc:
        session.fail(str(exc))
    finally:
        session.close()
    print(f"execution_id={note_id}")
    print(f"status={status}")


def _print_execution(payload: Mapping[str, object]) -> None:
    status = str(payload.get("status") or "")
    work_id = str(payload.get("work_id") or "")
    if not status or not work_id:
        raise RuntimeError("execution payload had no status")
    reason = payload.get("status_reason")
    flag = "true" if payload.get("requires_reconciliation") is True else "false"
    print(f"status={status}")
    print(f"work_id={work_id}")
    print(f"requires_reconciliation={flag}")
    print(f"status_reason={reason if isinstance(reason, str) else ''}")
    outcome = payload.get("outcome")
    output = outcome.get("output") if isinstance(outcome, dict) else None
    if isinstance(output, dict) and "appended" in output:
        print(f"appended={output.get('appended')}")


def execution_status(execution_id: str) -> None:
    session = _Session()
    try:
        session.initialize()
        payload = session.tool("get_execution", {"execution_id": execution_id})
        _print_execution(payload)
    except (TimeoutError, OSError, BrokenPipeError, RuntimeError) as exc:
        session.fail(str(exc))
    finally:
        session.close()


def resume_note(execution_id: str) -> None:
    session = _Session()
    try:
        session.initialize()
        payload = session.tool("resume_execution", {"execution_id": execution_id})
        _print_execution(payload)
    except (TimeoutError, OSError, BrokenPipeError, RuntimeError) as exc:
        session.fail(str(exc))
    finally:
        session.close()


def list_notes() -> None:
    session = _Session()
    try:
        session.initialize()
        payload = session.tool(
            "start_execution",
            {
                "work_id": "note.list",
                "version": "1.0.0",
                "input_data": {"limit": 50},
            },
        )
        if payload.get("status") != "COMPLETED":
            session.fail(f"note.list did not complete: {payload.get('status')!r}")
        outcome = payload.get("outcome")
        output = outcome.get("output") if isinstance(outcome, dict) else None
        notes = output.get("notes") if isinstance(output, dict) else None
        total = output.get("total") if isinstance(output, dict) else None
        if not isinstance(notes, list) or isinstance(total, bool) or not isinstance(total, int):
            session.fail("note.list returned no notes")
        print(f"total={total}")
        for note in notes:
            if not isinstance(note, str):
                session.fail("note.list returned a note that is not text")
            print(f"note={note}")
    except (TimeoutError, OSError, BrokenPipeError) as exc:
        session.fail(str(exc))
    finally:
        session.close()


def persist_check(echo_id: str, note_id: str) -> None:
    session = _Session()
    try:
        session.initialize()
        payload = session.tool("list_executions", {})
        executions = payload.get("executions")
        if not isinstance(executions, list):
            session.fail("list_executions returned no executions list")
        by_id: dict[str, dict[str, object]] = {}
        for item in executions:
            if isinstance(item, dict) and isinstance(item.get("execution_id"), str):
                by_id[item["execution_id"]] = item
        echo = by_id.get(echo_id)
        note = by_id.get(note_id)
        if echo is None or note is None:
            session.fail("execution ids from before the restart are not listed")
        if echo.get("work_id") != "echo" or echo.get("status") != "COMPLETED":
            session.fail(f"echo execution did not survive: {echo.get('status')!r}")
        if note.get("work_id") != "note" or note.get("status") != "WAITING_APPROVAL":
            session.fail(f"note execution did not survive: {note.get('status')!r}")
    except (TimeoutError, OSError, BrokenPipeError) as exc:
        session.fail(str(exc))
    finally:
        session.close()
    print("persist-ok")


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "probe"
    if command == "probe":
        probe()
    elif command == "persist-start":
        persist_start()
    elif command == "persist-check":
        if len(sys.argv) != 4:
            _fail("usage: mcp_probe.py persist-check ECHO_ID NOTE_ID", b"")
        persist_check(sys.argv[2], sys.argv[3])
    elif command == "start-note":
        if len(sys.argv) != 4:
            _fail("usage: mcp_probe.py start-note TEXT KEY", b"")
        start_note(sys.argv[2], sys.argv[3])
    elif command == "execution-status":
        if len(sys.argv) != 3:
            _fail("usage: mcp_probe.py execution-status EXECUTION_ID", b"")
        execution_status(sys.argv[2])
    elif command == "resume-note":
        if len(sys.argv) != 3:
            _fail("usage: mcp_probe.py resume-note EXECUTION_ID", b"")
        resume_note(sys.argv[2])
    elif command == "list-notes":
        if len(sys.argv) != 2:
            _fail("usage: mcp_probe.py list-notes", b"")
        list_notes()
    else:
        _fail(f"unknown command {command}", b"")


if __name__ == "__main__":
    main()
