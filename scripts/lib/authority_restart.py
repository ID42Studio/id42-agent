#!/usr/bin/env python3
"""Prove an operator seal still verifies after the MCP host process exits.

The MCP server and this script load ``id42_agent.mcp_host``. That factory
keeps one HMAC key file. Commands:

- ``seal EXECUTION_ID`` approves the waiting note, writes the operator and
  host credentials next to the key file, and prints ids only.
- ``verify APPROVAL_ID`` loads the key file in this process and checks those
  credentials, plus the stored approval.
- ``refuse`` exits 0 when the key file is rejected for its permissions.
- ``wipe`` deletes the credential files and leaves the key file.

Key bytes and credential text are never printed.
"""

from __future__ import annotations

import asyncio
import os
import stat
import sys
from datetime import timedelta
from pathlib import Path
from uuid import UUID

from id42_work import ActorAuthority, ApprovalStatus, AuthorityKeyError, SqliteStorage, TrustedActor
from id42_work.approvals.console import console_from_server

import id42_agent.mcp_host as host

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _fail(message: str) -> None:
    print(f"id42-agent: {message}", file=sys.stderr)
    raise SystemExit(1)


def _credential_paths() -> tuple[Path, Path]:
    parent = host.authority_key_file().parent
    return parent / ".authority-reviewer", parent / ".authority-actor"


def _write_private(path: Path, text: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        _fail(f"cannot store an actor credential ({exc.errno})")
    try:
        os.fchmod(fd, 0o600)
        payload = text.encode("utf-8")
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                _fail("cannot store an actor credential")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_private(path: Path) -> str:
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    except OSError as exc:
        _fail(f"cannot read an actor credential ({exc.errno})")
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            _fail("actor credential file permissions are too open")
        data = os.read(fd, info.st_size)
    finally:
        os.close(fd)
    return data.decode("utf-8")


def _key_mode() -> str:
    path = host.authority_key_file()
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        _fail("authority key file is not a regular file")
    return f"{stat.S_IMODE(info.st_mode):03o}"


def seal(execution_id: str) -> None:
    """Approve one waiting note and keep the seals that allowed it."""
    try:
        parsed = UUID(execution_id)
    except ValueError:
        _fail("execution id must be a UUID")
    server = host.server()
    console = console_from_server(server)
    pending = [
        row
        for row in console.list_pending_sync()
        if row.execution.execution_id == parsed and row.approval.status.value == "PENDING"
    ]
    if len(pending) != 1:
        _fail(f"expected one pending approval, found {len(pending)}")
    approval = pending[0].approval
    reviewer = server.authority.issue(
        actor_id=host.OPERATOR_ID,
        tenant_id=pending[0].execution.tenant_id,
        grants=frozenset({approval.reviewer_capability}),
        ttl=timedelta(minutes=5),
    )
    decision = console.approve_sync(
        approval.approval_id,
        reviewer=reviewer,
        reason="cross-process",
        resume=True,
    )
    actor = server.host_actor()
    reviewer_path, actor_path = _credential_paths()
    _write_private(reviewer_path, reviewer.to_credential())
    _write_private(actor_path, actor.to_credential())
    status = decision.execution.status
    print(f"signing_key_id={server.authority.signing_key_id}")
    print(f"approval_id={decision.approval.approval_id}")
    print(f"execution_id={decision.execution.execution_id}")
    print(f"decision_actor_id={decision.approval.decision_actor_id}")
    print(f"execution_status={getattr(status, 'value', status)}")
    print(f"key_mode={_key_mode()}")
    print("sealed")


def verify(approval_id: str) -> None:
    """Check the previous seals and the stored approval with a new authority."""
    try:
        parsed = UUID(approval_id)
    except ValueError:
        _fail("approval id must be a UUID")
    key_path = host.authority_key_file()
    try:
        authority = ActorAuthority(host.AUTHORITY_ID, key_path=key_path)
    except AuthorityKeyError as exc:
        _fail(f"authority key file was refused ({exc.reason})")
    reviewer_path, actor_path = _credential_paths()
    reviewer = authority.verify(TrustedActor.from_credential(_read_private(reviewer_path)))
    actor = authority.verify(TrustedActor.from_credential(_read_private(actor_path)))
    if reviewer.actor_id != host.OPERATOR_ID or reviewer.actor_id == actor.actor_id:
        _fail("reviewer is not the operator")
    if not reviewer.has_grant("approve:note") or actor.has_grant("approve:note"):
        _fail("grants did not survive the restart")
    if actor.actor_id != host.ACTOR_ID or not actor.has_grant(host.NOTES_APPEND):
        _fail("host actor did not survive the restart")
    storage = SqliteStorage(host.store_file())
    try:
        approval = asyncio.run(storage.approvals.get(parsed))
    finally:
        storage.close()
    if approval is None or approval.status is not ApprovalStatus.GRANTED:
        _fail("stored approval is not granted")
    if approval.decision_actor_id != reviewer.actor_id:
        _fail("stored approval was not decided by the verified operator")
    print(f"signing_key_id={authority.signing_key_id}")
    print(f"decision_actor_id={reviewer.actor_id}")
    print(f"approval_status={approval.status.value}")
    print(f"key_mode={_key_mode()}")
    print("verified")


def refuse() -> None:
    """Exit 0 only when this process refuses the key file for its mode."""
    key_path = host.authority_key_file()
    try:
        ActorAuthority(host.AUTHORITY_ID, key_path=key_path)
    except AuthorityKeyError as exc:
        print(f"refused reason={exc.reason}")
        if exc.reason != "permissions":
            raise SystemExit(1)
        return
    _fail("authority key file was accepted")


def wipe() -> None:
    """Remove credential files. The key file stays."""
    for path in _credential_paths():
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            _fail(f"cannot remove an actor credential ({exc.errno})")
    print("wiped")


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "seal":
        if len(sys.argv) != 3:
            _fail("usage: authority_restart.py seal EXECUTION_ID")
        seal(sys.argv[2])
    elif command == "verify":
        if len(sys.argv) != 3:
            _fail("usage: authority_restart.py verify APPROVAL_ID")
        verify(sys.argv[2])
    elif command == "refuse":
        if len(sys.argv) != 2:
            _fail("usage: authority_restart.py refuse")
        refuse()
    elif command == "wipe":
        if len(sys.argv) != 2:
            _fail("usage: authority_restart.py wipe")
        wipe()
    else:
        _fail(f"unknown command {command}")


if __name__ == "__main__":
    main()
