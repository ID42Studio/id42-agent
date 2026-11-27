"""Default host factory for ``id42-work mcp serve``.

The process that runs the command is the host. This factory registers no works
and keeps execution state in process memory. It lets the server start on stdio
and answer ``list_works`` with an empty list. Replace it with a module that
registers the works, policies and actor for this machine. The MCP client never
supplies an actor, a grant or an approval.

Storage does not survive a restart of the server process.
"""

from datetime import timedelta

from id42_work import (
    ActorAuthority,
    CapabilityRegistry,
    MemoryStorage,
    StaticPolicyResolver,
    TrustedActor,
    WorkRegistry,
    WorkRunner,
)
from id42_work.mcp import WorkMcpServer


def server() -> WorkMcpServer:
    """Build a WorkMcpServer with no registered works."""
    authority = ActorAuthority("id42-agent")
    runner = WorkRunner(
        registry=WorkRegistry(),
        policies=StaticPolicyResolver({}),
        capabilities=CapabilityRegistry(),
        actor_authority=authority,
        storage=MemoryStorage(),
    )

    def issue() -> TrustedActor:
        return authority.issue(
            actor_id="id42-agent",
            grants=(),
            ttl=timedelta(minutes=5),
        )

    return WorkMcpServer(runner, authority=authority, issue_actor=issue)
