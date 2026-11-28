"""Optional Odoo and GitHub works for the default factory.

Both stay unregistered unless the matching flag is true. Credentials come from
the environment or from a mode 0600 file. This module does not import either
connector until that flag is on, so a missing package cannot break the
default works.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

WORK_ODOO_SEARCH = "odoo.search"
WORK_ODOO_CREATE = "odoo.create"
WORK_GITHUB_REPO = "github.repo.get"
WORK_GITHUB_ISSUE = "github.issue.create"
WORK_VERSION = "1.0.0"

ODOO_SEARCH_READ = "odoo.record.search_read"
ODOO_CREATE = "odoo.record.create"
GITHUB_REPO_GET = "github.repo.get"
GITHUB_ISSUE_CREATE = "github.issue.create"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}
_FORBIDDEN_ROOTS = (Path("/home"), Path("/root"))
_DEFAULT_OPERATION_LOG = "/var/lib/id42-work/github-operations.sqlite"
_PROVIDERS: list[object] = []


class _ClosableProvider(Protocol):
    async def aclose(self) -> None: ...


def flag_enabled(name: str, environ: Mapping[str, str] | None = None) -> bool:
    """True only for an explicit true/yes/on/1. Empty is false."""
    env = os.environ if environ is None else environ
    value = env.get(name, "").strip().lower()
    if value in _FALSE:
        return False
    if value in _TRUE:
        return True
    raise RuntimeError(f"{name} must be true or false")


def configured_work_ids(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Work ids the factory registers for this environment, sorted."""
    env = os.environ if environ is None else environ
    ids = ["echo", "note", "note.list"]
    if flag_enabled("ID42_WORK_ODOO_ENABLED", env):
        ids.append(WORK_ODOO_SEARCH)
        if _csv(env.get("ID42_WORK_ODOO_WRITE_MODELS", ""), "ID42_WORK_ODOO_WRITE_MODELS"):
            ids.append(WORK_ODOO_CREATE)
    if flag_enabled("ID42_WORK_GITHUB_ENABLED", env):
        ids.extend((WORK_GITHUB_REPO, WORK_GITHUB_ISSUE))
    return tuple(sorted(ids))


def execution_grants(environ: Mapping[str, str] | None = None) -> frozenset[str]:
    """Capability names the execution actor needs for the registered connector works.

    Approve and reconcile grants are never included. Other connector
    capabilities stay ungranted.
    """
    env = os.environ if environ is None else environ
    grants: set[str] = set()
    if flag_enabled("ID42_WORK_ODOO_ENABLED", env):
        grants.add(ODOO_SEARCH_READ)
        if _csv(env.get("ID42_WORK_ODOO_WRITE_MODELS", ""), "ID42_WORK_ODOO_WRITE_MODELS"):
            grants.add(ODOO_CREATE)
    if flag_enabled("ID42_WORK_GITHUB_ENABLED", env):
        grants.update({GITHUB_REPO_GET, GITHUB_ISSUE_CREATE})
    return frozenset(grants)


def install(
    registry: object,
    capabilities: object,
    policies: dict[tuple[str, str], object],
    *,
    odoo_transport: object | None = None,
    github_transport: object | None = None,
) -> None:
    """Register connector works when their flags are on. Transports are for tests."""
    if flag_enabled("ID42_WORK_ODOO_ENABLED"):
        _install_odoo(registry, capabilities, policies, transport=odoo_transport)
    if flag_enabled("ID42_WORK_GITHUB_ENABLED"):
        _install_github(registry, capabilities, policies, transport=github_transport)


def close_providers() -> None:
    """Close connector clients opened by :func:`install`."""
    import asyncio

    pending = list(_PROVIDERS)
    _PROVIDERS.clear()

    async def _close() -> None:
        for item in pending:
            closer = getattr(item, "aclose", None)
            if closer is not None:
                await closer()

    if pending:
        try:
            asyncio.run(_close())
        except RuntimeError:
            pass


def _install_odoo(
    registry: object,
    capabilities: object,
    policies: dict[tuple[str, str], object],
    *,
    transport: object | None,
) -> None:
    try:
        from datetime import timedelta

        from id42_work import AutonomyLevel, ExecutionContext, ExecutionLimits, Policy, Work
        from id42_work_odoo import OdooConfig, OdooConfigError, OdooProvider
        from pydantic import BaseModel, Field
    except ImportError as exc:
        raise RuntimeError(
            "ID42_WORK_ODOO_ENABLED is true but id42-work-odoo is not installed "
            "in /opt/id42-work/venv"
        ) from exc

    read_models = _csv(os.environ.get("ID42_WORK_ODOO_READ_MODELS", ""), "ID42_WORK_ODOO_READ_MODELS")
    if not read_models:
        raise RuntimeError("ID42_WORK_ODOO_READ_MODELS is required when Odoo is enabled")
    write_models = _csv(
        os.environ.get("ID42_WORK_ODOO_WRITE_MODELS", ""),
        "ID42_WORK_ODOO_WRITE_MODELS",
    )
    missing = [model for model in write_models if model not in read_models]
    if missing:
        raise RuntimeError("ID42_WORK_ODOO_WRITE_MODELS must also be listed in ID42_WORK_ODOO_READ_MODELS")
    read_fields = _read_fields(os.environ.get("ID42_WORK_ODOO_READ_FIELDS", ""), read_models)
    for model in read_models:
        allowed = read_fields.get(model) if read_fields else None
        if allowed is not None and "name" not in allowed:
            raise RuntimeError("ID42_WORK_ODOO_READ_FIELDS must include name for each listed model")
    try:
        config = OdooConfig.from_env()
    except OdooConfigError as exc:
        raise RuntimeError(str(exc)) from None

    class SearchIn(BaseModel):
        model: str = Field(min_length=1, max_length=128)
        name: str = Field(min_length=1, max_length=200)
        limit: int = Field(default=20, ge=1, le=80)

    class SearchOut(BaseModel):
        count: int
        names: list[str]

    class CreateIn(BaseModel):
        model: str = Field(min_length=1, max_length=128)
        name: str = Field(min_length=1, max_length=200)

    class CreateOut(BaseModel):
        model: str
        record_id: int

    class SearchRecords:
        async def execute(self, input_data: SearchIn, context: ExecutionContext) -> SearchOut:
            payload = await context.invoke(
                ODOO_SEARCH_READ,
                action_id="search",
                params={
                    "model": input_data.model,
                    "domain": [["name", "ilike", input_data.name]],
                    "fields": ["name"],
                    "limit": input_data.limit,
                },
            )
            if not isinstance(payload, dict):
                raise RuntimeError("odoo search returned no object")
            records = payload.get("records")
            if not isinstance(records, list):
                raise RuntimeError("odoo search returned no records")
            names: list[str] = []
            for row in records:
                if isinstance(row, dict) and isinstance(row.get("name"), str):
                    names.append(row["name"])
            return SearchOut(count=len(records), names=names)

    class CreateRecord:
        async def execute(self, input_data: CreateIn, context: ExecutionContext) -> CreateOut:
            payload = await context.invoke(
                ODOO_CREATE,
                action_id="create",
                params={"model": input_data.model, "values": {"name": input_data.name}},
            )
            if not isinstance(payload, dict):
                raise RuntimeError("odoo create returned no object")
            ids = payload.get("ids")
            if (
                not isinstance(ids, list)
                or len(ids) != 1
                or isinstance(ids[0], bool)
                or not isinstance(ids[0], int)
            ):
                raise RuntimeError("odoo create returned no record id")
            return CreateOut(model=str(payload.get("model") or input_data.model), record_id=ids[0])

    provider = OdooProvider(
        config,
        read_models=read_models,
        write_models=write_models,
        read_fields=read_fields,
        message_models=[],
        transport=transport,  # type: ignore[arg-type]
    )
    _PROVIDERS.append(provider)
    capabilities.add(provider)  # type: ignore[attr-defined]
    limits = ExecutionLimits(timeout=timedelta(seconds=30))
    search = Work(
        id=WORK_ODOO_SEARCH,
        version=WORK_VERSION,
        name="Search Odoo records",
        description=(
            "Search one allow-listed Odoo model by name. Read-only. "
            "An empty allow-list denies every model. No unlink."
        ),
        input_schema=SearchIn,
        output_schema=SearchOut,
        required_capabilities=frozenset({ODOO_SEARCH_READ}),
        autonomy_ceiling=AutonomyLevel.AUTONOMOUS,
    )
    registry.register(search, executor=SearchRecords())  # type: ignore[attr-defined]
    policies[(WORK_ODOO_SEARCH, WORK_VERSION)] = Policy(
        id=WORK_ODOO_SEARCH,
        version=WORK_VERSION,
        autonomy=AutonomyLevel.AUTONOMOUS,
        allowed_capabilities=frozenset({ODOO_SEARCH_READ}),
        execution_limits=limits,
    )
    if not write_models:
        return
    create = Work(
        id=WORK_ODOO_CREATE,
        version=WORK_VERSION,
        name="Create an Odoo record",
        description=(
            "Create one record with a name on an allow-listed model. "
            "Supervised: the call waits for approval. There is no unlink."
        ),
        input_schema=CreateIn,
        output_schema=CreateOut,
        required_capabilities=frozenset({ODOO_CREATE}),
        autonomy_ceiling=AutonomyLevel.SUPERVISED,
    )
    registry.register(create, executor=CreateRecord())  # type: ignore[attr-defined]
    policies[(WORK_ODOO_CREATE, WORK_VERSION)] = Policy(
        id=WORK_ODOO_CREATE,
        version=WORK_VERSION,
        autonomy=AutonomyLevel.SUPERVISED,
        allowed_capabilities=frozenset({ODOO_CREATE}),
        execution_limits=limits,
    )


def _install_github(
    registry: object,
    capabilities: object,
    policies: dict[tuple[str, str], object],
    *,
    transport: object | None,
) -> None:
    try:
        from datetime import timedelta

        from id42_work import AutonomyLevel, ExecutionContext, ExecutionLimits, Policy, Work
        from id42_work_github import GithubConfig, GithubConfigError, GithubProvider
        from pydantic import BaseModel, Field
    except ImportError as exc:
        raise RuntimeError(
            "ID42_WORK_GITHUB_ENABLED is true but id42-work-github is not installed "
            "in /opt/id42-work/venv"
        ) from exc

    repositories = _csv(
        os.environ.get("ID42_WORK_GITHUB_REPOSITORIES", ""),
        "ID42_WORK_GITHUB_REPOSITORIES",
    )
    if not repositories:
        raise RuntimeError("ID42_WORK_GITHUB_REPOSITORIES is required when GitHub is enabled")
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    token_file = os.environ.get("ID42_WORK_GITHUB_TOKEN_FILE", "").strip()
    if token and token_file:
        raise RuntimeError("set GITHUB_TOKEN or ID42_WORK_GITHUB_TOKEN_FILE, not both")
    if not token and not token_file:
        raise RuntimeError("GITHUB_TOKEN or ID42_WORK_GITHUB_TOKEN_FILE is required when GitHub is enabled")
    log_path = os.environ.get("ID42_WORK_GITHUB_OPERATION_LOG", "").strip() or _DEFAULT_OPERATION_LOG
    _absolute_file(log_path, "ID42_WORK_GITHUB_OPERATION_LOG")
    payload: dict[str, object] = {
        "repositories": repositories,
        "operation_log": log_path,
        "timeout_seconds": 20,
        "trust_env": False,
    }
    if token_file:
        _absolute_file(token_file, "ID42_WORK_GITHUB_TOKEN_FILE")
        payload["token_file"] = token_file
        payload["token_env"] = None
    try:
        config = GithubConfig.model_validate(payload)
    except (GithubConfigError, ValueError) as exc:
        raise RuntimeError("GitHub connector settings were refused") from exc

    class RepoIn(BaseModel):
        repository: str = Field(min_length=1, max_length=200)

    class RepoOut(BaseModel):
        name: str
        full_name: str

    class IssueIn(BaseModel):
        repository: str = Field(min_length=1, max_length=200)
        title: str = Field(min_length=1, max_length=200)
        body: str = Field(default="", max_length=4000)

    class IssueOut(BaseModel):
        number: int
        html_url: str

    class ReadRepo:
        async def execute(self, input_data: RepoIn, context: ExecutionContext) -> RepoOut:
            payload_out = await context.invoke(
                GITHUB_REPO_GET,
                action_id="read",
                params={"repository": input_data.repository},
            )
            if not isinstance(payload_out, dict):
                raise RuntimeError("github repo.get returned no object")
            name = payload_out.get("name")
            full_name = payload_out.get("full_name")
            if not isinstance(name, str) or not isinstance(full_name, str):
                raise RuntimeError("github repo.get returned no name")
            return RepoOut(name=name, full_name=full_name)

    class OpenIssue:
        async def execute(self, input_data: IssueIn, context: ExecutionContext) -> IssueOut:
            created = await context.invoke(
                GITHUB_ISSUE_CREATE,
                action_id="open",
                params={
                    "repository": input_data.repository,
                    "title": input_data.title,
                    "body": input_data.body,
                },
            )
            if not isinstance(created, dict):
                raise RuntimeError("github issue.create returned no object")
            number = created.get("number")
            html_url = created.get("html_url")
            if isinstance(number, bool) or not isinstance(number, int) or not isinstance(html_url, str):
                raise RuntimeError("github issue.create returned no issue")
            return IssueOut(number=number, html_url=html_url)

    try:
        provider = GithubProvider(config, transport=transport)  # type: ignore[arg-type]
    except GithubConfigError as exc:
        raise RuntimeError("GitHub connector settings were refused") from exc
    _PROVIDERS.append(provider)
    capabilities.add(provider)  # type: ignore[attr-defined]
    limits = ExecutionLimits(timeout=timedelta(seconds=30))
    read = Work(
        id=WORK_GITHUB_REPO,
        version=WORK_VERSION,
        name="Read a GitHub repository",
        description=(
            "Read one repository on the allow-list. Read-only. "
            "A repository that is not listed is refused before any request is sent."
        ),
        input_schema=RepoIn,
        output_schema=RepoOut,
        required_capabilities=frozenset({GITHUB_REPO_GET}),
        autonomy_ceiling=AutonomyLevel.AUTONOMOUS,
    )
    issue = Work(
        id=WORK_GITHUB_ISSUE,
        version=WORK_VERSION,
        name="Open a GitHub issue",
        description=(
            "Open one issue on an allow-listed repository. Supervised: the call waits "
            "for approval. This work cannot merge, close, delete, push, or label."
        ),
        input_schema=IssueIn,
        output_schema=IssueOut,
        required_capabilities=frozenset({GITHUB_ISSUE_CREATE}),
        autonomy_ceiling=AutonomyLevel.SUPERVISED,
    )
    registry.register(read, executor=ReadRepo())  # type: ignore[attr-defined]
    registry.register(issue, executor=OpenIssue())  # type: ignore[attr-defined]
    policies[(WORK_GITHUB_REPO, WORK_VERSION)] = Policy(
        id=WORK_GITHUB_REPO,
        version=WORK_VERSION,
        autonomy=AutonomyLevel.AUTONOMOUS,
        allowed_capabilities=frozenset({GITHUB_REPO_GET}),
        execution_limits=limits,
    )
    policies[(WORK_GITHUB_ISSUE, WORK_VERSION)] = Policy(
        id=WORK_GITHUB_ISSUE,
        version=WORK_VERSION,
        autonomy=AutonomyLevel.SUPERVISED,
        allowed_capabilities=frozenset({GITHUB_ISSUE_CREATE}),
        execution_limits=limits,
    )


def _csv(raw: str, label: str) -> list[str]:
    if not raw.strip():
        return []
    items: list[str] = []
    for part in raw.split(","):
        item = part.strip()
        if not item:
            raise RuntimeError(f"{label} has an empty entry")
        if item in items:
            raise RuntimeError(f"{label} lists {item} more than once")
        items.append(item)
    return items


def _read_fields(raw: str, read_models: list[str]) -> dict[str, list[str]] | None:
    text = raw.strip()
    if not text:
        return None
    parsed: dict[str, list[str]] = {}
    for part in text.split(";"):
        model, separator, fields = part.partition(":")
        model = model.strip()
        if not separator or not model or not fields.strip():
            raise RuntimeError(
                "ID42_WORK_ODOO_READ_FIELDS must be model:field,field;model:field"
            )
        if model not in read_models:
            raise RuntimeError(
                "ID42_WORK_ODOO_READ_FIELDS names a model that is not in ID42_WORK_ODOO_READ_MODELS"
            )
        names = _csv(fields, "ID42_WORK_ODOO_READ_FIELDS")
        if model in parsed:
            raise RuntimeError(f"ID42_WORK_ODOO_READ_FIELDS lists {model} more than once")
        parsed[model] = names
    return parsed


def _absolute_file(value: str, label: str) -> None:
    path = Path(value)
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise RuntimeError(f"{label} must be an absolute file path")
    for root in _FORBIDDEN_ROOTS:
        if path == root or root in path.parents:
            raise RuntimeError(f"{label} must not live under /home or /root")
