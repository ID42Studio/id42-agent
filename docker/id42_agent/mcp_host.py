"""Default host factory for ``id42-work mcp serve``.

The process that runs the command is the host. It registers three works and
stores executions outside the agent workspace and outside any home directory.
``ID42_WORK_STORAGE`` selects the backend. Empty or ``sqlite`` opens
``SqliteStorage`` (the default). ``postgres`` opens ``PostgresStorage`` from
the ``postgres`` extra, using ``ID42_WORK_DATABASE_URL``. The authority key
file stays on the data volume either way:

- ``echo`` returns its input. No capability, no approval.
- ``note.list`` reads notes already stored. Read-only, no approval.
- ``note`` appends one note through the side-effecting capability
  ``notes.append``. Autonomy is supervised, so the call waits for approval.
  The MCP client cannot approve. ``id42-work approvals`` decides in another
  process. It seals ``OPERATOR_ID`` with only that approval's reviewer grant
  (``approve:note`` for this work). ``--resume`` continues as the host actor
  issued here.

``notes.append`` stores the capability ``operation_id`` on the note. That id
is the call's idempotency key. ``reconcile`` looks it up and does not write:

- the id is in a readable file: ``APPLIED``, with the stored append result
- the file is readable and the id is absent: ``NOT_APPLIED``
- the file is missing: ``NOT_APPLIED``
- the file is unreadable or a line is not a note: ``UNKNOWN``

A line that only has ``text`` is still listed. It cannot prove a later call.
Resume probes ``UNCERTAIN`` calls. ``APPLIED`` is replayed. ``NOT_APPLIED``
retries the same operation id and asks for approval again. The execution
actor is not given ``reconcile:note``. ``id42-work reconcile`` seals that
grant for ``OPERATOR_ID`` when an operator decides, and only that grant.

The SQLite file is ``/var/lib/id42-work/work.sqlite`` unless
``ID42_WORK_STORE`` is an absolute path outside ``/home`` and ``/root``.
``ID42_WORK_STORE`` is ignored when the backend is Postgres. Compose mounts
a named volume at ``/var/lib/id42-work``. That directory holds the note file
and the authority key for both backends, and the SQLite file for the default.
``id42_agent.mcp_stub:server`` is the empty in-memory factory. It does not
use this key file.

The HMAC key lives in one file on that volume, mode ``0600``. The default
path is ``/var/lib/id42-work/authority.key``. ``ID42_WORK_AUTHORITY_KEY_PATH``
overrides it and must be absolute. ``ActorAuthority`` does not read the
environment; this module does, through ``authority_key_path_from_env``.
The file is created on first use. A symlink, a mode other than ``0600``,
and a world-writable directory are refused. Nothing here prints key bytes
or an actor credential.

Executions still record ``actor_id`` and ``tenant_id``. The approvals
command and the reconcile command load this same factory, so they seal
``OPERATOR_ID`` with the same keys. A credential issued in one process
verifies in the next. This module does not seal the operator. Each
command does, with only the grant that decision needs.

The MCP server passes ``session_actor=credential_session_actor`` with
this factory's ``issue`` as the fallback. Stdio has no credential header,
so every stdio call is that one sealed actor, and the server pins the
identity for the process. HTTP requires ``Id42-Actor-Credential`` and
pins the first verified identity of each session. ``host_actor`` stays
the factory actor, which is what ``--resume`` uses. Listing tools return
one page (``limit`` and ``cursor``).
"""

from __future__ import annotations

import atexit
import fcntl
import json
import os
import re
from collections.abc import Callable, Mapping
from datetime import timedelta
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from id42_work import (
    ActorAuthority,
    AuthorityKeyError,
    AutonomyLevel,
    CapabilityCall,
    CapabilityRegistry,
    CapabilitySpec,
    ExecutionContext,
    ExecutionLimits,
    Policy,
    ReconciliationProbe,
    ReconciliationVerdict,
    SqliteStorage,
    StaticPolicyResolver,
    StorageError,
    TrustedActor,
    Work,
    WorkRegistry,
    WorkRunner,
    authority_key_path_from_env,
)
from id42_work.mcp import WorkMcpServer, credential_session_actor
from pydantic import BaseModel, Field, JsonValue

STORE_DIRECTORY = Path("/var/lib/id42-work")
DEFAULT_STORE = STORE_DIRECTORY / "work.sqlite"
STORAGE_SQLITE = "sqlite"
STORAGE_POSTGRES = "postgres"
_STORAGE_ENV = "ID42_WORK_STORAGE"
_DATABASE_ENV = "ID42_WORK_DATABASE_URL"
_DSN_USERINFO = re.compile(r"://[^/\s@]*:[^/\s@]*@")
_DSN_PASSWORD = re.compile(r"(?i)\bpassword\s*=\s*('[^']*'|\"[^\"]*\"|\S+)")
# One file for the MCP server and for `id42-work approvals` / `reconcile`.
# Keep the shell default in scripts/lib/common.sh in sync with this path.
DEFAULT_AUTHORITY_KEY = STORE_DIRECTORY / "authority.key"
NOTES_NAME = "notes.jsonl"
MAX_NOTE_CHARS = 2000
MAX_NOTES_BYTES = 1_048_576
ACTOR_ID = "id42-agent"
# scripts/work-approve.sh and scripts/work-reconcile.sh pass this id to
# `--actor`. Each command seals it. This module does not. Approvals seal
# only the reviewer grant. Reconcile seals only reconcile:<work_id>.
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
if OPERATOR_ID == ACTOR_ID or any(
    grant.startswith(("approve:", "reconcile:")) for grant in _AGENT_GRANTS
):
    raise RuntimeError("the execution actor must not approve or reconcile its own work")
_FORBIDDEN_ROOTS = (Path("/home"), Path("/root"))
_LIMITS = ExecutionLimits(timeout=timedelta(seconds=30))
_POSTGRES_TIMEOUT_SECONDS = 10.0


class _Closable(Protocol):
    @property
    def schema_version(self) -> int: ...

    def close(self) -> None: ...


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


def _note_record(item: object, line_number: int) -> dict[str, JsonValue]:
    """One stored note.

    ``operation_id`` is the capability call's idempotency key. Lines written
    before that field existed have ``text`` and still list. They do not match
    a later reconcile. A line that is not a note is corrupt.
    """
    if not isinstance(item, dict):
        raise RuntimeError(f"note store line {line_number} is not a note")
    text = item.get("text")
    if not isinstance(text, str):
        raise RuntimeError(f"note store line {line_number} is not a note")
    operation_id = item.get("operation_id")
    if operation_id is not None and not isinstance(operation_id, str):
        raise RuntimeError(f"note store line {line_number} is not a note")
    byte_count = item.get("byte_count")
    if byte_count is None:
        byte_count = len(text.encode("utf-8"))
    if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count < 0:
        raise RuntimeError(f"note store line {line_number} is not a note")
    record: dict[str, JsonValue] = {"text": text, "byte_count": byte_count}
    if isinstance(operation_id, str):
        record["operation_id"] = operation_id
    return record


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

    async def reconcile(self, call: CapabilityCall) -> ReconciliationProbe:
        """Prove whether this operation id was appended. Does not write."""
        operation_id = str(call.operation_id)
        try:
            records = self._locked(self._load_unlocked)
        except (OSError, RuntimeError, UnicodeError, ValueError):
            return ReconciliationProbe(verdict=ReconciliationVerdict.UNKNOWN)
        if not isinstance(records, list):
            return ReconciliationProbe(verdict=ReconciliationVerdict.UNKNOWN)
        for item in records:
            if not isinstance(item, dict) or item.get("operation_id") != operation_id:
                continue
            byte_count = item.get("byte_count")
            if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count < 0:
                return ReconciliationProbe(verdict=ReconciliationVerdict.UNKNOWN)
            return ReconciliationProbe(
                verdict=ReconciliationVerdict.APPLIED,
                result={"appended": True, "byte_count": byte_count},
            )
        return ReconciliationProbe(verdict=ReconciliationVerdict.NOT_APPLIED)

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
                records.append(_note_record(item, line_number))
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
    _reject_home(resolved, "ID42_WORK_STORE must not live under /home or /root")
    return resolved


def storage_backend() -> str:
    """``sqlite`` or ``postgres``. An empty value is SQLite."""
    raw = os.environ.get(_STORAGE_ENV, "").strip().lower()
    if raw in {"", STORAGE_SQLITE}:
        return STORAGE_SQLITE
    if raw == STORAGE_POSTGRES:
        return STORAGE_POSTGRES
    raise RuntimeError("ID42_WORK_STORAGE must be sqlite or postgres")


def _database_url_error(value: str) -> str | None:
    """Return an error when ``value`` is not a Postgres target, else None.

    The message does not include the value. A URL must not contain whitespace.
    A libpq connection string may. A socket path under ``/home`` or ``/root``
    is refused.
    """
    if not value:
        return "ID42_WORK_DATABASE_URL is required when ID42_WORK_STORAGE=postgres"
    if any(char in value for char in "\r\n"):
        return "ID42_WORK_DATABASE_URL must be a single line"
    if "://" in value:
        if any(char.isspace() for char in value):
            return "ID42_WORK_DATABASE_URL must be a postgres URL or a libpq connection string"
        parsed = urlparse(value)
        if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
            return "ID42_WORK_DATABASE_URL must name a postgres host"
        return None
    hosts: list[str] = []
    for part in value.split():
        if "=" not in part:
            return "ID42_WORK_DATABASE_URL must be a postgres URL or a libpq connection string"
        key, _, raw = part.partition("=")
        if key.lower() in {"host", "hostaddr"}:
            hosts.append(raw.strip("'\""))
    if not hosts:
        return "ID42_WORK_DATABASE_URL must name a postgres host"
    for host in hosts:
        path = Path(host)
        if path.is_absolute():
            try:
                _reject_home(path, "ID42_WORK_DATABASE_URL must not use a socket under /home or /root")
            except RuntimeError:
                return "ID42_WORK_DATABASE_URL must not use a socket under /home or /root"
    return None


def database_url() -> str:
    """Postgres target from the environment. The value is not logged."""
    raw = os.environ.get(_DATABASE_ENV, "").strip()
    error = _database_url_error(raw)
    if error:
        raise RuntimeError(error)
    return raw


def _public_storage_error(exc: BaseException) -> str:
    """Describe a storage failure without the URL or its password."""
    text = str(exc)
    url = os.environ.get(_DATABASE_ENV, "").strip()
    if url:
        text = text.replace(url, "[redacted]")
        password = urlparse(url).password if "://" in url else None
        if password:
            text = text.replace(password, "[redacted]")
    text = _DSN_USERINFO.sub("://[redacted]@", text)
    text = _DSN_PASSWORD.sub("password=[redacted]", text)
    text = " ".join(text.split())
    if not text:
        text = "cannot open postgres storage"
    return text[:300]


def _prepare_directory(directory: Path) -> None:
    """Create a store directory. chmod only a directory this process created."""
    created = not directory.exists()
    directory.mkdir(parents=True, exist_ok=True)
    if created:
        os.chmod(directory, 0o700)
    if not os.access(directory, os.W_OK | os.X_OK):
        raise RuntimeError(f"id42-work store is not writable: {directory}")


def _open_postgres() -> _Closable:
    error = _database_url_error(os.environ.get(_DATABASE_ENV, "").strip())
    if error:
        raise RuntimeError(error)
    try:
        from id42_work.storage.postgres import PostgresStorage
    except ImportError as exc:
        raise RuntimeError(
            "postgres storage needs id42-work[postgres] in /opt/id42-work/venv"
        ) from exc
    try:
        return PostgresStorage(database_url(), timeout=_POSTGRES_TIMEOUT_SECONDS)
    except StorageError as exc:
        raise RuntimeError(_public_storage_error(exc)) from None


def open_storage() -> _Closable:
    """Open the backend selected by ``ID42_WORK_STORAGE``. The caller closes it."""
    if storage_backend() == STORAGE_SQLITE:
        path = store_file()
        _prepare_directory(path.parent)
        storage = SqliteStorage(path)
        _mode_600_db(path)
        return storage
    _prepare_directory(STORE_DIRECTORY)
    return _open_postgres()


def storage_status() -> tuple[str, int]:
    """Open the selected backend, read its schema version, and close it.

    The result is ``(backend, schema_version)``. A connection failure raises
    ``RuntimeError`` and does not include the database URL.
    """
    kind = storage_backend()
    storage = open_storage()
    try:
        version = storage.schema_version
    except Exception as exc:
        if kind == STORAGE_POSTGRES:
            raise RuntimeError(_public_storage_error(exc)) from None
        raise
    finally:
        storage.close()
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise RuntimeError(f"id42-work schema is {version!r}")
    if kind == STORAGE_SQLITE:
        _mode_600_db(store_file())
    return kind, version


def authority_key_file() -> Path:
    """Absolute path of the HMAC key file. An unset env uses the volume default.

    The final path component is not resolved, so a symlink at that name stays
    a symlink and ``ActorAuthority`` can refuse it. The parent is resolved.
    """
    try:
        chosen = authority_key_path_from_env()
    except AuthorityKeyError as exc:
        raise RuntimeError("ID42_WORK_AUTHORITY_KEY_PATH must be an absolute path") from exc
    path = DEFAULT_AUTHORITY_KEY if chosen is None else Path(chosen)
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise RuntimeError("ID42_WORK_AUTHORITY_KEY_PATH must be an absolute file path")
    parent = path.parent
    resolved_parent = parent.resolve() if parent.exists() else parent
    _reject_home(
        resolved_parent,
        "authority key must not live under /home or /root",
    )
    _reject_home(path, "authority key must not live under /home or /root")
    if parent.exists():
        return resolved_parent / path.name
    return path


def _reject_home(path: Path, message: str) -> None:
    for root in _FORBIDDEN_ROOTS:
        if path == root or root in path.parents:
            raise RuntimeError(
                f"{message} (the agent workspace is inside /home/nanobot)"
            )


def _inside_store(directory: Path) -> bool:
    store = STORE_DIRECTORY.resolve() if STORE_DIRECTORY.exists() else STORE_DIRECTORY
    return directory == store or store in directory.parents


def _prepare_key_directory(directory: Path) -> None:
    """Create a key directory inside the store. Refuse one others can write."""
    if not directory.exists():
        if not _inside_store(directory):
            raise RuntimeError(f"authority key directory does not exist: {directory}")
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
    elif _inside_store(directory) and directory.stat().st_mode & 0o002:
        try:
            os.chmod(directory, 0o700)
        except OSError as exc:
            raise RuntimeError(
                f"authority key directory is writable by others: {directory}"
            ) from exc
    if not directory.is_dir():
        raise RuntimeError(f"authority key directory is not a directory: {directory}")
    if directory.stat().st_mode & 0o002:
        raise RuntimeError(f"authority key directory is writable by others: {directory}")
    if not os.access(directory, os.W_OK | os.X_OK):
        raise RuntimeError(f"authority key directory is not writable: {directory}")


def open_authority(*, create: bool) -> ActorAuthority:
    """Authority backed by the shared key file. ``create`` writes it if missing."""
    path = authority_key_file()
    _prepare_key_directory(path.parent)
    return ActorAuthority(AUTHORITY_ID, key_path=path, create_key_file=create)


def rotate_authority_key() -> str:
    """Retire the current signing key and return the new key id.

    The id is not secret. Key bytes are not returned and must not be logged.
    An in-memory authority cannot be rotated; this factory always has a file.
    """
    try:
        return open_authority(create=False).rotate_keys()
    except AuthorityKeyError as exc:
        raise RuntimeError(f"cannot rotate the authority key ({exc.reason})") from None


def notes_file(db_path: Path | None = None) -> Path:
    """JSONL notes next to the SQLite file, or in the store directory for Postgres."""
    if db_path is not None:
        return db_path.parent / NOTES_NAME
    if storage_backend() == STORAGE_SQLITE:
        return store_file().parent / NOTES_NAME
    return STORE_DIRECTORY / NOTES_NAME


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
            "this server with id42-work approvals. resume_execution does not decide. "
            "If the append is left UNCERTAIN, resume probes the note file by operation id. "
            "A person closes a call the file cannot prove with id42-work reconcile."
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


def open_host() -> tuple[WorkRunner, ActorAuthority, _Closable]:
    """Open the durable runner. The caller closes the storage."""
    if storage_backend() == STORAGE_SQLITE:
        notes = notes_file(store_file())
    else:
        notes = STORE_DIRECTORY / NOTES_NAME
    storage = open_storage()
    registry = WorkRegistry()
    for work, executor in _works():
        registry.register(work, executor=executor)
    capabilities = CapabilityRegistry()
    capabilities.add(NoteStore(notes))
    authority = open_authority(create=True)
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


def _close_storage(storage: _Closable) -> None:
    storage.close()


def server() -> WorkMcpServer:
    """Host factory. The sealed actor is issued here, not by the MCP client.

    Stdio keeps one process actor. ``credential_session_actor`` falls back to
    ``issue`` when the transport has no header, and the server pins that
    identity. An HTTP caller must present a credential this authority verifies.
    """
    runner, authority, storage = open_host()
    atexit.register(_close_storage, storage)

    def issue() -> TrustedActor:
        return issue_agent(authority)

    return WorkMcpServer(
        runner,
        authority=authority,
        issue_actor=issue,
        session_actor=credential_session_actor(fallback=issue),
    )
