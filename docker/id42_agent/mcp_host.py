"""Default host factory for ``id42-work mcp serve``.

The process that runs the command is the host. It registers three works and
stores executions in SQLite on a dedicated directory, outside the agent
workspace and outside any home directory:

- ``echo`` returns its input. No capability, no approval.
- ``note.list`` reads notes already stored. Read-only, no approval.
- ``note`` appends one note through the side-effecting capability
  ``notes.append``. Autonomy is supervised, so the call waits for approval.
  The MCP client cannot approve. ``id42-work approvals`` decides in another
  process. It seals ``OPERATOR_ID`` with only that approval's reviewer grant
  (``approve:note`` for this work). ``--resume`` continues as the host actor
  issued here.

The database file is ``/var/lib/id42-work/work.sqlite`` unless
``ID42_WORK_STORE`` is an absolute path outside ``/home`` and ``/root``.
Compose mounts a named volume there. The file survives a container restart.
``id42_agent.mcp_stub:server`` is the empty in-memory factory.

Actor seals are not stored. id42-work keeps the sealing key in process
memory. Executions record ``actor_id`` and ``tenant_id``, so a new process
that issues those same ids can resume. This factory does not seal the
operator. The approvals command does, from the authority on the server.
"""

from __future__ import annotations

import atexit
import fcntl
import json
import os
from collections.abc import Callable, Mapping
from datetime import timedelta
from pathlib import Path

from id42_work import (
    ActorAuthority,
    AutonomyLevel,
    CapabilityCall,
    CapabilityRegistry,
    CapabilitySpec,
    ExecutionContext,
    ExecutionLimits,
    Policy,
    SqliteStorage,
    StaticPolicyResolver,
    TrustedActor,
    Work,
    WorkRegistry,
    WorkRunner,
)
from id42_work.mcp import WorkMcpServer
from pydantic import BaseModel, Field, JsonValue

STORE_DIRECTORY = Path("/var/lib/id42-work")
DEFAULT_STORE = STORE_DIRECTORY / "work.sqlite"
NOTES_NAME = "notes.jsonl"
MAX_NOTE_CHARS = 2000
MAX_NOTES_BYTES = 1_048_576
ACTOR_ID = "id42-agent"
# scripts/work-approve.sh passes this id to `id42-work approvals --actor`.
# The command seals it. This module does not.
OPERATOR_ID = "id42-operator"
TENANT_ID = "local"
AUTHORITY_ID = "id42-agent"
NOTES_APPEND = "notes.append"
NOTES_READ = "notes.read"
WORK_ECHO = "echo"
WORK_NOTE = "note"
WORK_NOTE_LIST = "note.list"
WORK_VERSION = "1.0.0"
# Keep in sync with scripts/lib/mcp_probe.py (_DEFAULT_WORKS).
DEFAULT_WORK_IDS = (WORK_ECHO, WORK_NOTE, WORK_NOTE_LIST)
_AGENT_GRANTS = frozenset({NOTES_APPEND, NOTES_READ})
if OPERATOR_ID == ACTOR_ID:
    raise RuntimeError("OPERATOR_ID and ACTOR_ID must differ")
_FORBIDDEN_ROOTS = (Path("/home"), Path("/root"))
_LIMITS = ExecutionLimits(timeout=timedelta(seconds=30))


class EchoInput(BaseModel):
    text: str = Field(max_length=4000)


class EchoOutput(BaseModel):
    text: str


class NoteInput(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_NOTE_CHARS)


class NoteOutput(BaseModel):
    appended: bool
    byte_count: int


class NoteListInput(BaseModel):
    limit: int = Field(default=20, ge=1, le=50)


class NoteListOutput(BaseModel):
    notes: list[str]
    total: int


class Echo:
    async def execute(self, input_data: EchoInput, context: ExecutionContext) -> EchoOutput:
        del context
        return EchoOutput(text=input_data.text)


class AppendNote:
    async def execute(self, input_data: NoteInput, context: ExecutionContext) -> NoteOutput:
        _require_note_text(input_data.text)
        result = await context.invoke(
            NOTES_APPEND,
            action_id="append-note",
            params={"text": input_data.text},
        )
        byte_count = _byte_count(result)
        return NoteOutput(appended=True, byte_count=byte_count)


class ListNotes:
    async def execute(self, input_data: NoteListInput, context: ExecutionContext) -> NoteListOutput:
        result = await context.invoke(
            NOTES_READ,
            action_id="read-notes",
            params={"limit": input_data.limit},
        )
        if not isinstance(result, dict):
            raise RuntimeError("notes.read returned no object")
        notes = result.get("notes")
        total = result.get("total")
        if not isinstance(notes, list) or not all(isinstance(item, str) for item in notes):
            raise RuntimeError("notes.read returned no note list")
        if isinstance(total, bool) or not isinstance(total, int):
            raise RuntimeError("notes.read returned no total")
        return NoteListOutput(notes=list(notes), total=total)


def _require_note_text(text: str) -> None:
    if not text.strip() or len(text) > MAX_NOTE_CHARS or "\x00" in text:
        raise ValueError("note text must be 1 to 2000 non-blank characters")


def _byte_count(result: object) -> int:
    if not isinstance(result, dict) or result.get("appended") is not True:
        raise RuntimeError("notes.append did not store the note")
    value = result.get("byte_count")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError("notes.append returned no byte_count")
    return value


class NoteStore:
    """Append-only notes beside the SQLite file. Paths come from the host, not the client."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock_path = path.with_name(path.name + ".lock")

    def capabilities(self) -> list[CapabilitySpec]:
        return [
            CapabilitySpec(
                name=NOTES_READ,
                side_effecting=False,
                description="Read notes already stored on this host.",
            ),
            CapabilitySpec(
                name=NOTES_APPEND,
                side_effecting=True,
                description="Append one note to the host store.",
            ),
        ]

    async def invoke(
        self, capability: str, params: Mapping[str, object], call: CapabilityCall
    ) -> JsonValue:
        if capability == NOTES_READ:
            return self._read(params)
        if capability == NOTES_APPEND:
            return self._append(params, call)
        raise RuntimeError(f"unknown capability {capability}")

    def _read(self, params: Mapping[str, object]) -> dict[str, JsonValue]:
        limit = params.get("limit", 20)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise RuntimeError("limit must be an integer from 1 to 50")
        records = self._locked(self._load_unlocked)
        tail = records[-limit:]
        return {"notes": [item["text"] for item in tail], "total": len(records)}

    def _append(self, params: Mapping[str, object], call: CapabilityCall) -> dict[str, JsonValue]:
        text = params.get("text")
        if not isinstance(text, str):
            raise RuntimeError("note text must be a string")
        _require_note_text(text)
        operation_id = str(call.operation_id)
        byte_count = len(text.encode("utf-8"))

        def write() -> dict[str, JsonValue]:
            for item in self._load_unlocked():
                if item.get("operation_id") == operation_id:
                    return {"appended": True, "byte_count": item["byte_count"]}
            line = json.dumps(
                {"operation_id": operation_id, "text": text, "byte_count": byte_count},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8") + b"\n"
            current = self._path.stat().st_size if self._path.exists() else 0
            if current + len(line) > MAX_NOTES_BYTES:
                raise RuntimeError("note store is full")
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("ab") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            _mode_600(self._path)
            return {"appended": True, "byte_count": byte_count}

        return self._locked(write)

    def _load_unlocked(self) -> list[dict[str, JsonValue]]:
        if not self._path.exists():
            return []
        records: list[dict[str, JsonValue]] = []
        with self._path.open("r", encoding="utf-8") as handle:
            for line_number, raw in enumerate(handle, start=1):
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    item = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(f"note store line {line_number} is not JSON") from exc
                text = item.get("text") if isinstance(item, dict) else None
                operation_id = item.get("operation_id") if isinstance(item, dict) else None
                byte_count = item.get("byte_count") if isinstance(item, dict) else None
                if (
                    not isinstance(text, str)
                    or not isinstance(operation_id, str)
                    or isinstance(byte_count, bool)
                    or not isinstance(byte_count, int)
                ):
                    raise RuntimeError(f"note store line {line_number} is not a note")
                records.append(
                    {"text": text, "operation_id": operation_id, "byte_count": byte_count}
                )
        return records

    def _locked(self, fn: Callable[[], JsonValue]) -> JsonValue:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock_path.open("a", encoding="utf-8") as handle:
            _mode_600(self._lock_path)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                return fn()
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def store_file() -> Path:
    """SQLite path for this process. Refuses the workspace and any home directory."""
    raw = os.environ.get("ID42_WORK_STORE", "").strip()
    path = Path(raw) if raw else DEFAULT_STORE
    if not path.is_absolute():
        raise RuntimeError("ID42_WORK_STORE must be an absolute path")
    resolved = path.resolve()
    for root in _FORBIDDEN_ROOTS:
        if resolved == root or root in resolved.parents:
            raise RuntimeError(
                "ID42_WORK_STORE must not live under /home or /root "
                "(the agent workspace is inside /home/nanobot)"
            )
    return resolved


def notes_file(db_path: Path | None = None) -> Path:
    """JSONL notes next to the SQLite file."""
    return (db_path or store_file()).parent / NOTES_NAME


def issue_agent(authority: ActorAuthority) -> TrustedActor:
    """Actor the MCP server issues. It can run works. It cannot approve them.

    ``id42-work approvals --resume`` calls this through ``WorkMcpServer.host_actor``.
    The operator (``OPERATOR_ID``) is sealed by that command, not here.
    """
    return authority.issue(
        actor_id=ACTOR_ID,
        tenant_id=TENANT_ID,
        grants=_AGENT_GRANTS,
        ttl=timedelta(minutes=5),
    )


def _policies() -> dict[tuple[str, str], Policy]:
    return {
        (WORK_ECHO, WORK_VERSION): Policy(
            id="echo",
            version=WORK_VERSION,
            autonomy=AutonomyLevel.AUTONOMOUS,
            allowed_capabilities=frozenset(),
            execution_limits=_LIMITS,
        ),
        (WORK_NOTE, WORK_VERSION): Policy(
            id="note",
            version=WORK_VERSION,
            autonomy=AutonomyLevel.SUPERVISED,
            allowed_capabilities=frozenset({NOTES_APPEND}),
            execution_limits=_LIMITS,
        ),
        (WORK_NOTE_LIST, WORK_VERSION): Policy(
            id="note.list",
            version=WORK_VERSION,
            autonomy=AutonomyLevel.AUTONOMOUS,
            allowed_capabilities=frozenset({NOTES_READ}),
            execution_limits=_LIMITS,
        ),
    }


def _works() -> tuple[tuple[Work, object], ...]:
    echo = Work(
        id=WORK_ECHO,
        version=WORK_VERSION,
        name="Echo",
        description=(
            "Return the input text unchanged. No capability is called and no approval is required."
        ),
        input_schema=EchoInput,
        output_schema=EchoOutput,
        required_capabilities=frozenset(),
        autonomy_ceiling=AutonomyLevel.AUTONOMOUS,
    )
    note = Work(
        id=WORK_NOTE,
        version=WORK_VERSION,
        name="Append a note",
        description=(
            "Append one note to the host store outside the agent workspace. "
            "This calls the side-effecting capability notes.append and waits for approval. "
            "Pass an idempotency_key. There is no approve tool. A person decides outside "
            "this server with id42-work approvals. resume_execution does not decide."
        ),
        input_schema=NoteInput,
        output_schema=NoteOutput,
        required_capabilities=frozenset({NOTES_APPEND}),
        autonomy_ceiling=AutonomyLevel.SUPERVISED,
    )
    listing = Work(
        id=WORK_NOTE_LIST,
        version=WORK_VERSION,
        name="List notes",
        description=(
            "Return the most recent notes stored by the note work. Read-only. No approval."
        ),
        input_schema=NoteListInput,
        output_schema=NoteListOutput,
        required_capabilities=frozenset({NOTES_READ}),
        autonomy_ceiling=AutonomyLevel.AUTONOMOUS,
    )
    return (
        (echo, Echo()),
        (note, AppendNote()),
        (listing, ListNotes()),
    )


def open_host() -> tuple[WorkRunner, ActorAuthority, SqliteStorage]:
    """Open the durable runner. The caller closes the storage."""
    path = store_file()
    # chmod only a directory this process created. The volume root is mode 700
    # from the image and the entrypoint; chmod of an existing parent would
    # also hit shared directories such as /tmp when ID42_WORK_STORE is a file there.
    created = not path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    if created:
        os.chmod(path.parent, 0o700)
    if not os.access(path.parent, os.W_OK | os.X_OK):
        raise RuntimeError(f"id42-work store is not writable: {path.parent}")
    storage = SqliteStorage(path)
    _mode_600_db(path)
    registry = WorkRegistry()
    for work, executor in _works():
        registry.register(work, executor=executor)
    capabilities = CapabilityRegistry()
    capabilities.add(NoteStore(notes_file(path)))
    authority = ActorAuthority(AUTHORITY_ID)
    runner = WorkRunner(
        registry=registry,
        policies=StaticPolicyResolver(_policies()),
        capabilities=capabilities,
        actor_authority=authority,
        storage=storage,
    )
    return runner, authority, storage


def _mode_600(path: Path) -> None:
    if path.exists():
        os.chmod(path, 0o600)


def _mode_600_db(path: Path) -> None:
    # WAL mode also creates sibling -wal and -shm files. They hold committed rows.
    for suffix in ("", "-wal", "-shm"):
        _mode_600(Path(str(path) + suffix))


def _close_storage(storage: SqliteStorage) -> None:
    storage.close()


def server() -> WorkMcpServer:
    """Host factory. The sealed actor is issued here, not by the MCP client."""
    runner, authority, storage = open_host()
    atexit.register(_close_storage, storage)

    def issue() -> TrustedActor:
        return issue_agent(authority)

    return WorkMcpServer(runner, authority=authority, issue_actor=issue)
