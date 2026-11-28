# The image named "upstream" is the pinned nanobot Dockerfile, passed by
# Compose as an additional build context (service:nanobot-upstream).
# id42-work needs mcp>=2.3. nanobot v0.4.2 requires mcp<2. A second virtualenv
# keeps those pins apart. The postgres extra (psycopg) and the web extra
# (the operator UI) stay in that same virtualenv. Only the id42-work console
# script is placed on PATH; `python` stays the nanobot interpreter.
FROM upstream

USER root

COPY docker/id42_agent /opt/id42-agent/id42_agent

ENV PYTHONPATH=/opt/id42-agent
ENV ID42_WORK_VENV=/opt/id42-work/venv

# Full commit of ID42Studio/id42-work. Override with --build-arg ID42_WORK_REF.
ARG ID42_WORK_REF=ae07623aa67c0b5d0535cb58f6a2e2b204736e50

RUN set -eu; \
    case "$ID42_WORK_REF" in \
      ""|-*|*[!A-Za-z0-9._-]*) echo "id42-agent: invalid ID42_WORK_REF" >&2; exit 1 ;; \
    esac; \
    uv venv "$ID42_WORK_VENV"; \
    uv pip install --python "$ID42_WORK_VENV/bin/python" --no-cache \
      "id42-work[mcp,postgres,web] @ git+https://github.com/ID42Studio/id42-work.git@${ID42_WORK_REF}"; \
    ln -sfn "$ID42_WORK_VENV/bin/id42-work" /usr/local/bin/id42-work; \
    printf '%s\n' "$ID42_WORK_REF" > /opt/id42-agent/ID42_WORK_REF; \
    mkdir -p /var/lib/id42-work; \
    chmod 700 /var/lib/id42-work; \
    chown -R nanobot:nanobot /opt/id42-work /opt/id42-agent /var/lib/id42-work; \
    ID42_WORK_STORE=/tmp/id42-work-build.sqlite PYTHONPATH=/opt/id42-agent \
      "$ID42_WORK_VENV/bin/python" -m id42_agent.build_check; \
    "$ID42_WORK_VENV/bin/id42-work" approvals approve --help >/dev/null; \
    "$ID42_WORK_VENV/bin/id42-work" reconcile abandon --help >/dev/null; \
    "$ID42_WORK_VENV/bin/id42-work" web serve --help >/dev/null; \
    rm -f /opt/id42-agent/id42_agent/build_check.py \
      /opt/id42-agent/id42_agent/__pycache__/build_check*.pyc \
      /tmp/id42-work-build.sqlite /tmp/id42-work-build.sqlite-wal /tmp/id42-work-build.sqlite-shm \
      /tmp/id42-notes-legacy.jsonl /tmp/id42-notes-legacy.jsonl.lock \
      /tmp/id42-notes-build.jsonl /tmp/id42-notes-build.jsonl.lock \
      /tmp/id42-notes-dir.lock \
      /var/lib/id42-work/authority.key /var/lib/id42-work/authority.key.lock \
      /tmp/notes.jsonl /tmp/notes.jsonl.lock; \
    rmdir /tmp/id42-notes-dir; \
    if [ -e /var/lib/id42-work/authority.key ] || [ -e /var/lib/id42-work/authority.key.lock ]; then \
      echo "id42-agent: authority key material survived the image build" >&2; \
      exit 1; \
    fi

USER root
