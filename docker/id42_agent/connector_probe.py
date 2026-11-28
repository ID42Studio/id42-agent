"""Register the Odoo and GitHub connectors against in-process fakes.

No socket is allowed. The fakes stand in for the connectors' own test doubles,
which are not installed with the packages. Run with the id42-work interpreter.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import sys
from pathlib import Path

import httpx

import id42_agent.mcp_host as host
from id42_agent.connectors import configured_work_ids
from id42_work import ExecutionStatus


class _Blocked(OSError):
    """A network call the probe refused."""


def _block_network() -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise _Blocked("connector probe blocked a network call")

    socket.create_connection = fail  # type: ignore[assignment]
    socket.socket.connect = fail  # type: ignore[method-assign]
    socket.socket.connect_ex = fail  # type: ignore[method-assign]


class _GitHub:
    """Repository read. A POST is recorded and would be a failed supervised write."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "number": 1,
                    "title": "Governed",
                    "state": "open",
                    "html_url": "https://github.com/acme/widgets/issues/1",
                    "user": {"login": "ada"},
                    "body": "",
                    "labels": [],
                },
            )
        return httpx.Response(
            200,
            json={
                "id": 7,
                "name": "widgets",
                "full_name": "acme/widgets",
                "private": False,
                "description": None,
                "default_branch": "main",
                "html_url": "https://github.com/acme/widgets",
            },
        )


def _private_dir() -> Path:
    root = Path("/tmp/id42-connector-probe")
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    os.environ["ID42_WORK_STORAGE"] = "sqlite"
    os.environ.pop("ID42_WORK_DATABASE_URL", None)
    os.environ["ID42_WORK_STORE"] = str(root / "work.sqlite")
    os.environ["ID42_WORK_AUTHORITY_KEY_PATH"] = str(root / "authority.key")
    os.environ["ID42_WORK_GITHUB_OPERATION_LOG"] = str(root / "github.sqlite")
    return root


def _clear_connector_env() -> None:
    for name in (
        "ID42_WORK_ODOO_ENABLED",
        "ID42_WORK_GITHUB_ENABLED",
        "ODOO_URL",
        "ODOO_DB",
        "ODOO_USER",
        "ODOO_API_KEY",
        "ODOO_API_KEY_FILE",
        "ODOO_PROTOCOL",
        "ODOO_TIMEOUT",
        "ID42_WORK_ODOO_READ_MODELS",
        "ID42_WORK_ODOO_WRITE_MODELS",
        "ID42_WORK_ODOO_READ_FIELDS",
        "ID42_WORK_GITHUB_REPOSITORIES",
        "GITHUB_TOKEN",
        "ID42_WORK_GITHUB_TOKEN_FILE",
    ):
        os.environ.pop(name, None)


def _ids(runner: object) -> list[str]:
    works = runner.registered_works()  # type: ignore[attr-defined]
    return [work.id for work in works]


def _error_text(execution: object) -> str:
    info = getattr(execution, "error", None)
    if info is None:
        return str(getattr(execution, "status_reason", "") or "")
    parts = [str(info.message)]
    parts.extend(str(cause.message) for cause in info.causes)
    return "\n".join(parts)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


async def _run(runner: object, **kwargs: object) -> object:
    return await runner.run(**kwargs)  # type: ignore[attr-defined]


def main() -> None:
    _block_network()
    root = _private_dir()
    try:
        _clear_connector_env()
        os.environ["ID42_WORK_ODOO_ENABLED"] = "false"
        os.environ["ID42_WORK_GITHUB_ENABLED"] = "false"
        os.environ["ODOO_URL"] = "http://127.0.0.1:9"
        os.environ["GITHUB_TOKEN"] = "probe-token-not-secret"
        _require("id42_work_odoo" not in sys.modules, "odoo was imported before it was enabled")
        _require("id42_work_github" not in sys.modules, "github was imported before it was enabled")
        runner, _authority, storage = host.open_host()
        try:
            found = _ids(runner)
            _require(found == ["echo", "note", "note.list"], f"flags off registered {found}")
        finally:
            storage.close()
            host_close()
        _require("id42_work_odoo" not in sys.modules, "odoo was imported while disabled")
        _require("id42_work_github" not in sys.modules, "github was imported while disabled")

        _clear_connector_env()
        os.environ["ID42_WORK_ODOO_ENABLED"] = "true"
        os.environ["ID42_WORK_ODOO_READ_MODELS"] = "res.partner"
        try:
            host.open_host()
        except RuntimeError as exc:
            _require("ODOO_URL" in str(exc), f"odoo without credentials: {exc}")
        else:
            raise SystemExit("odoo without credentials opened a host")
        host_close()

        _clear_connector_env()
        os.environ["ID42_WORK_GITHUB_ENABLED"] = "true"
        os.environ["ID42_WORK_GITHUB_REPOSITORIES"] = "acme/widgets"
        os.environ["GITHUB_TOKEN"] = "probe-token-not-secret"
        os.environ["ID42_WORK_GITHUB_TOKEN_FILE"] = str(root / "missing-token")
        try:
            host.open_host()
        except RuntimeError as exc:
            _require("not both" in str(exc), f"github token and file together: {exc}")
        else:
            raise SystemExit("github accepted a token and a token file")
        host_close()

        os.environ.pop("GITHUB_TOKEN", None)
        os.environ.pop("ID42_WORK_GITHUB_TOKEN_FILE", None)
        try:
            host.open_host()
        except RuntimeError as exc:
            _require("GITHUB_TOKEN" in str(exc), f"github without a token: {exc}")
        else:
            raise SystemExit("github without a token opened a host")
        host_close()

        _arm_connectors(root)
        fake = _load_fake_odoo()()
        fake.records["res.partner"] = {1: {"id": 1, "name": "Acme"}}
        github = _GitHub()
        runner, authority, storage = host.open_host(
            odoo_transport=fake,
            github_transport=httpx.MockTransport(github),
        )
        try:
            found = _ids(runner)
            expected = list(configured_work_ids())
            _require(found == expected, f"works {found} != {expected}")
            actor = host.issue_agent(authority)
            for grant in (
                "odoo.record.search_read",
                "odoo.record.create",
                "github.repo.get",
                "github.issue.create",
            ):
                _require(actor.has_grant(grant), f"missing grant {grant}")
            for grant in (
                "odoo.record.write",
                "odoo.record.read",
                "odoo.message.post",
                "github.issue.comment",
                "github.label.add",
                "github.pull.comment",
                "approve:odoo.create",
                "approve:github.issue.create",
                "reconcile:note",
                "reconcile:odoo.create",
            ):
                _require(not actor.has_grant(grant), f"unexpected grant {grant}")

            denied_odoo = asyncio.run(
                _run(
                    runner,
                    work_id="odoo.search",
                    version="1.0.0",
                    input_data={"model": "sale.order", "name": "Acme"},
                    actor=actor,
                )
            )
            _require(
                denied_odoo.status is ExecutionStatus.FAILED
                and "allow-list" in _error_text(denied_odoo),
                "a model outside the allow-list was not refused",
            )
            _require(not fake.calls, "a refused model still called Odoo")

            denied_github = asyncio.run(
                _run(
                    runner,
                    work_id="github.repo.get",
                    version="1.0.0",
                    input_data={"repository": "other/repo"},
                    actor=actor,
                )
            )
            _require(
                denied_github.status is ExecutionStatus.FAILED
                and "allow-list" in _error_text(denied_github),
                "a repository outside the allow-list was not refused",
            )
            _require(not github.calls, "a refused repository still called GitHub")

            looked = asyncio.run(
                _run(
                    runner,
                    work_id="odoo.search",
                    version="1.0.0",
                    input_data={"model": "res.partner", "name": "Acme"},
                    actor=actor,
                )
            )
            outcome = looked.raise_for_status()
            _require(
                looked.status is ExecutionStatus.COMPLETED
                and outcome.output.count == 1
                and outcome.output.names == ["Acme"],
                "odoo search did not complete against the fake",
            )
            _require(
                all(call[1] != "load" for call in fake.calls),
                "odoo search wrote a record",
            )

            repo = asyncio.run(
                _run(
                    runner,
                    work_id="github.repo.get",
                    version="1.0.0",
                    input_data={"repository": "acme/widgets"},
                    actor=actor,
                )
            )
            repo_outcome = repo.raise_for_status()
            _require(
                repo.status is ExecutionStatus.COMPLETED and repo_outcome.output.name == "widgets",
                "github repo.get did not complete against the fake",
            )
            _require(all(method != "POST" for method, _path in github.calls), "github read sent a POST")

            waiting_odoo = asyncio.run(
                _run(
                    runner,
                    work_id="odoo.create",
                    version="1.0.0",
                    input_data={"model": "res.partner", "name": "Ada"},
                    actor=actor,
                    idempotency_key="odoo-create-1",
                )
            )
            _require(
                waiting_odoo.status is ExecutionStatus.WAITING_APPROVAL,
                f"odoo create status {waiting_odoo.status}",
            )
            _require(
                all(method != "load" for _model, method, _args, _kw in fake.calls),
                "odoo create ran before approval",
            )

            before_posts = [method for method, _path in github.calls]
            waiting_github = asyncio.run(
                _run(
                    runner,
                    work_id="github.issue.create",
                    version="1.0.0",
                    input_data={"repository": "acme/widgets", "title": "Governed"},
                    actor=actor,
                    idempotency_key="github-issue-1",
                )
            )
            _require(
                waiting_github.status is ExecutionStatus.WAITING_APPROVAL,
                f"github issue status {waiting_github.status}",
            )
            after_posts = [method for method, _path in github.calls]
            _require(before_posts == after_posts and "POST" not in after_posts, "github issue ran before approval")
        finally:
            storage.close()
            host_close()
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("connector-probe-ok")


def host_close() -> None:
    from id42_agent.connectors import close_providers

    close_providers()


def _arm_connectors(root: Path) -> None:
    del root
    os.environ["ID42_WORK_ODOO_ENABLED"] = "true"
    os.environ["ODOO_URL"] = "http://odoo.internal"
    os.environ["ODOO_DB"] = "probe"
    os.environ["ODOO_USER"] = "probe"
    os.environ["ODOO_API_KEY"] = "odoo-probe-key"
    os.environ["ID42_WORK_ODOO_READ_MODELS"] = "res.partner"
    os.environ["ID42_WORK_ODOO_WRITE_MODELS"] = "res.partner"
    os.environ["ID42_WORK_GITHUB_ENABLED"] = "true"
    os.environ["ID42_WORK_GITHUB_REPOSITORIES"] = "acme/widgets"
    os.environ["GITHUB_TOKEN"] = "probe-token-not-secret"
    os.environ.pop("ID42_WORK_GITHUB_TOKEN_FILE", None)


def _load_fake_odoo() -> type:
    """Build a FakeOdoo equivalent. The connector's tests are not on PYTHONPATH."""
    from collections.abc import Mapping

    from id42_work_odoo.errors import OdooRpcError

    class FakeOdoo:
        def __init__(self) -> None:
            self.authenticated = False
            self.calls: list[tuple[str, str, list[object], dict[str, object]]] = []
            self.records: dict[str, dict[int, dict[str, object]]] = {}
            self.uid = 7

        async def version(self) -> Mapping[str, object]:
            return {"server_version": "18.0", "server_version_info": [18, 0, 0, "final", 0, ""]}

        async def authenticate(self) -> int:
            self.authenticated = True
            return self.uid

        async def execute_kw(
            self,
            model: str,
            method: str,
            args: list[object] | tuple[object, ...] | object,
            kwargs: Mapping[str, object] | None = None,
        ) -> object:
            arg_list = list(args) if isinstance(args, (list, tuple)) else [args]
            kw = dict(kwargs or {})
            self.calls.append((model, method, arg_list, kw))
            if method == "fields_get":
                return {"name": {"type": "char"}}
            if method == "search_read":
                domain = arg_list[0] if arg_list else []
                rows = list(self.records.get(model, {}).values())
                if isinstance(domain, list):
                    rows = [row for row in rows if _row_matches(row, domain)]
                fields = kw.get("fields")
                if isinstance(fields, list):
                    names = [name for name in fields if isinstance(name, str)]
                    return [{name: row.get(name) for name in names} for row in rows]
                return rows
            raise OdooRpcError(f"fake has no method {method}")

    def _row_matches(row: Mapping[str, object], domain: list[object]) -> bool:
        if len(domain) != 1 or not isinstance(domain[0], (list, tuple)) or len(domain[0]) != 3:
            return True
        field, operator, expected = domain[0]
        actual = row.get(str(field))
        if operator == "ilike" and isinstance(actual, str) and isinstance(expected, str):
            return expected.lower() in actual.lower()
        return actual == expected

    return FakeOdoo


if __name__ == "__main__":
    main()
