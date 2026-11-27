# The image named "upstream" is the pinned nanobot Dockerfile, passed by
# Compose as an additional build context (service:nanobot-upstream).
# id42-work needs mcp>=2.3. nanobot v0.4.2 requires mcp<2. A second virtualenv
# keeps those pins apart. Only the id42-work console script is placed on PATH;
# `python` stays the nanobot interpreter.
FROM upstream

USER root

COPY docker/id42_agent /opt/id42-agent/id42_agent

ENV PYTHONPATH=/opt/id42-agent
ENV ID42_WORK_VENV=/opt/id42-work/venv

# Full commit of ID42Studio/id42-work. Override with --build-arg ID42_WORK_REF.
ARG ID42_WORK_REF=07eaa2a0787c206398b1296091fea1317f1ff27b

RUN set -eu; \
    case "$ID42_WORK_REF" in \
      ""|-*|*[!A-Za-z0-9._-]*) echo "id42-agent: invalid ID42_WORK_REF" >&2; exit 1 ;; \
    esac; \
    uv venv "$ID42_WORK_VENV"; \
    uv pip install --python "$ID42_WORK_VENV/bin/python" --no-cache \
      "id42-work[mcp] @ git+https://github.com/ID42Studio/id42-work.git@${ID42_WORK_REF}"; \
    ln -sfn "$ID42_WORK_VENV/bin/id42-work" /usr/local/bin/id42-work; \
    printf '%s\n' "$ID42_WORK_REF" > /opt/id42-agent/ID42_WORK_REF; \
    chown -R nanobot:nanobot /opt/id42-work /opt/id42-agent; \
    PYTHONPATH=/opt/id42-agent "$ID42_WORK_VENV/bin/python" -c 'import id42_agent.mcp_host as host; server = host.server(); assert type(server).__name__ == "WorkMcpServer"'

USER root
