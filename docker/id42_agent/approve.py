"""Operator decisions for the default id42-work factory.

id42-work's MCP server and CLI cannot approve or reject. This module opens
the same SQLite file in a separate process and issues a reviewer that is not
the execution actor. It is not an MCP tool. Run it with the id42-work
interpreter, not nanobot's:

    python -m id42_agent.approve pending
    python -m id42_agent.approve approve APPROVAL_ID [--reason TEXT]
    python -m id42_agent.approve reject APPROVAL_ID --reason TEXT

``approve`` grants the decision and then resumes the execution as the host
actor, which is what runs the side effect. ``reject`` ends the execution.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from uuid import UUID

from id42_work import ExecutionStatus

from id42_agent.mcp_host import issue_agent, issue_operator, open_host

_WAITING = frozenset({ExecutionStatus.WAITING_APPROVAL, ExecutionStatus.WAITING_REVIEW})


def _fail(message: str) -> None:
    print(f"id42-agent: {message}", file=sys.stderr)
    raise SystemExit(1)


def _preview_text(approval: object) -> str:
    preview = getattr(approval, "params_preview", None)
    text = ""
    if isinstance(preview, dict) and isinstance(preview.get("text"), str):
        text = preview["text"]
    return text.replace("\n", "\\n").replace("\r", "\\r")


async def _pending() -> None:
    runner, _authority, storage = open_host()
    try:
        found = False
        for execution in await runner.list_executions():
            if execution.status not in _WAITING:
                continue
            for approval in await runner.approvals.list_pending(execution.execution_id):
                found = True
                print(
                    "pending"
                    f" execution_id={execution.execution_id}"
                    f" approval_id={approval.approval_id}"
                    f" work_id={execution.work_id}"
                    f" gate={approval.gate.value}"
                    f" capability={approval.capability}"
                    f" text={_preview_text(approval)}"
                )
        if not found:
            print("pending-none")
    finally:
        storage.close()


async def _approve(approval_id: UUID, reason: str | None) -> None:
    runner, authority, storage = open_host()
    try:
        approval = await runner.approvals.get(approval_id)
        if approval is None:
            _fail(f"approval {approval_id} does not exist")
        decided = await runner.approvals.approve(
            approval_id, reviewer=issue_operator(authority), reason=reason
        )
        finished = await runner.resume(approval.execution_id, actor=issue_agent(authority))
        print(
            "approved"
            f" approval_id={decided.approval_id}"
            f" execution_id={finished.execution_id}"
            f" status={finished.status.value}"
        )
        if finished.status is not ExecutionStatus.COMPLETED:
            _fail(f"execution resumed to {finished.status.value}")
    finally:
        storage.close()


async def _reject(approval_id: UUID, reason: str) -> None:
    runner, authority, storage = open_host()
    try:
        approval = await runner.approvals.get(approval_id)
        if approval is None:
            _fail(f"approval {approval_id} does not exist")
        decided = await runner.approvals.reject(
            approval_id, reviewer=issue_operator(authority), reason=reason
        )
        execution = await runner.get(approval.execution_id)
        status = execution.status.value if execution is not None else "missing"
        print(
            "rejected"
            f" approval_id={decided.approval_id}"
            f" execution_id={approval.execution_id}"
            f" status={status}"
        )
        if execution is None or execution.status is not ExecutionStatus.REJECTED:
            _fail(f"execution is {status}")
    finally:
        storage.close()


def _approval_id(raw: str) -> UUID:
    try:
        return UUID(raw)
    except ValueError:
        _fail("approval id must be a UUID")
        raise AssertionError("unreachable")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="id42_agent.approve")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("pending")
    approve = sub.add_parser("approve")
    approve.add_argument("approval_id")
    approve.add_argument("--reason", default=None)
    reject = sub.add_parser("reject")
    reject.add_argument("approval_id")
    reject.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "pending":
            asyncio.run(_pending())
        elif args.command == "approve":
            asyncio.run(_approve(_approval_id(args.approval_id), args.reason))
        elif args.command == "reject":
            asyncio.run(_reject(_approval_id(args.approval_id), args.reason))
        else:
            parser.error("unknown command")
    except SystemExit:
        raise
    except Exception as exc:
        _fail(str(exc))


if __name__ == "__main__":
    main()
