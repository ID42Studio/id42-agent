"""Image build check for the default factory and note reconciliation.

Runs once while the image is built, then the Dockerfile removes this file.
"""

from __future__ import annotations

import asyncio
import os
import stat
import subprocess
from pathlib import Path
from uuid import uuid4

import id42_agent.mcp_host as host
import id42_agent.mcp_stub as stub
from id42_work import (
    ActorAuthority,
    AuthorityKeyError,
    CapabilityCall,
    ReconciliationVerdict,
    TrustedActor,
)


def _call(operation: object) -> CapabilityCall:
    return CapabilityCall(
        execution_id=uuid4(),
        action_id="append-note",
        operation_id=operation,
        actor_id=host.ACTOR_ID,
        tenant_id=host.TENANT_ID,
    )


def _check_authority_key(server: object) -> None:
    """The image build must refuse a loose key file and must not keep the key."""
    key = host.authority_key_file()
    info = key.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SystemExit("authority key file is not a regular file")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise SystemExit(f"authority key mode is {stat.S_IMODE(info.st_mode):03o}")
    authority = getattr(server, "authority", None)
    issue = getattr(server, "host_actor", None)
    if authority is None or not callable(issue):
        raise SystemExit("factory did not expose the authority")
    credential = issue().to_credential()
    try:
        reloaded = ActorAuthority(host.AUTHORITY_ID, key_path=key)
        verified = reloaded.verify(TrustedActor.from_credential(credential))
    finally:
        del credential
    if verified.actor_id != host.ACTOR_ID:
        raise SystemExit("reloaded authority rejected the host actor")
    os.chmod(key, 0o644)
    try:
        ActorAuthority(host.AUTHORITY_ID, key_path=key)
    except AuthorityKeyError as exc:
        if exc.reason != "permissions":
            raise SystemExit(f"loose authority key failed as {exc.reason}") from exc
    else:
        raise SystemExit("loose authority key was accepted")
    finally:
        # Later calls in this process reload the file. Put the mode back.
        # main() deletes the file so the image layer does not contain key bytes.
        if key.exists():
            os.chmod(key, 0o600)


def _check_postgres_extra() -> None:
    """The id42-work venv has PostgresStorage. The nanobot venv does not."""
    import psycopg
    from id42_work.storage.postgres import PostgresStorage

    if PostgresStorage.__name__ != "PostgresStorage":
        raise SystemExit("postgres extra did not export PostgresStorage")
    del psycopg
    nanobot = "/app/.venv/bin/python"
    for statement in ("import psycopg", "import id42_work"):
        completed = subprocess.run(
            [nanobot, "-c", statement],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode == 0:
            raise SystemExit(f"nanobot virtualenv can run {statement}")


def main() -> None:
    _check_postgres_extra()
    server = host.server()
    _check_authority_key(server)
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
    host.DEFAULT_AUTHORITY_KEY.unlink(missing_ok=True)
    Path(str(host.DEFAULT_AUTHORITY_KEY) + ".lock").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
