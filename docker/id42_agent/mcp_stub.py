"""Empty in-memory host factory for ``id42-work mcp serve``.

Opt in with ``ID42_WORK_FACTORY=id42_agent.mcp_stub:server``. The factory
registers no works and keeps execution state in process memory, so a restart
drops it. The default factory is ``id42_agent.mcp_host:server``.

Stdio still has one sealed actor for the process. ``session_actor`` falls
back to that actor because stdio has no credential header.
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
from id42_work.mcp import WorkMcpServer, credential_session_actor


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

    return WorkMcpServer(
        runner,
        authority=authority,
        issue_actor=issue,
        session_actor=credential_session_actor(fallback=issue),
    )
