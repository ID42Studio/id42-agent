#!/usr/bin/env python3
"""Ask id42-work's stdio MCP server to list its tools.

Starts ``id42-work mcp serve --factory <module:callable>`` and speaks the
initialize handshake (protocol 2025-03-26). Exits 0 when ``list_works`` is
advertised and a call returns a works list. Does not open a socket.
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
_PROTOCOL = "2025-03-26"
_DEADLINE_S = 20
_EXPECTED = (
    "list_works",
    "start_execution",
    "get_execution",
    "resume_execution",
    "list_executions",
    "cancel_execution",
)


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


def main() -> None:
    stderr_chunks: list[bytes] = []
    proc = subprocess.Popen(
        ["id42-work", "mcp", "serve", "--factory", _FACTORY],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )

    def _drain() -> None:
        if proc.stderr is None:
            return
        data = proc.stderr.read()
        if data:
            stderr_chunks.append(data[-4000:])

    drainer = threading.Thread(target=_drain, daemon=True)
    drainer.start()
    deadline = time.monotonic() + _DEADLINE_S
    try:
        _write(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": _PROTOCOL,
                    "capabilities": {},
                    "clientInfo": {"name": "id42-agent-doctor", "version": "0"},
                },
            },
        )
        initialized = _read_matching(proc, 1, deadline)
        if "error" in initialized:
            _fail(f"initialize failed: {initialized['error']}", b"".join(stderr_chunks))
        result = initialized.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("protocolVersion"), str):
            _fail("initialize returned no protocolVersion", b"".join(stderr_chunks))
        _write(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        _write(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listed = _read_matching(proc, 2, deadline)
        if "error" in listed:
            _fail(f"tools/list failed: {listed['error']}", b"".join(stderr_chunks))
        tools = (listed.get("result") or {}) if isinstance(listed.get("result"), dict) else {}
        names = [
            item.get("name")
            for item in tools.get("tools", [])
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        ]
        missing = [name for name in _EXPECTED if name not in names]
        if missing:
            _fail(f"tools/list missing {', '.join(missing)}", b"".join(stderr_chunks))
        _write(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "list_works", "arguments": {}},
            },
        )
        called = _read_matching(proc, 3, deadline)
        if "error" in called:
            _fail(f"list_works failed: {called['error']}", b"".join(stderr_chunks))
        body = json.dumps(called.get("result"))
        if '"works"' not in body:
            _fail("list_works result did not include works", b"".join(stderr_chunks))
    except (TimeoutError, OSError, BrokenPipeError) as exc:
        _fail(str(exc), b"".join(stderr_chunks))
    finally:
        _stop(proc)
        drainer.join(timeout=1)
    print("mcp-stdio-ok")


if __name__ == "__main__":
    main()
