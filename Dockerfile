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

# Full commits. Override with --build-arg. A branch name is not a pin.
ARG ID42_WORK_REF=ae07623aa67c0b5d0535cb58f6a2e2b204736e50
ARG ID42_WORK_ODOO_REF=3797544d8a3580657548d000110915578efe87c3
ARG ID42_WORK_GITHUB_REF=2e2a23d8f64d6c2313523bbf7ee24554fb3a4cb3

RUN set -eu; \
    valid_ref() { \
      case "$1" in \
        ""|-*|*[!A-Za-z0-9._-]*) echo "id42-agent: invalid $2" >&2; exit 1 ;; \
      esac; \
    }; \
    valid_ref "$ID42_WORK_REF" ID42_WORK_REF; \
    valid_ref "$ID42_WORK_ODOO_REF" ID42_WORK_ODOO_REF; \
    valid_ref "$ID42_WORK_GITHUB_REF" ID42_WORK_GITHUB_REF; \
    uv venv "$ID42_WORK_VENV"; \
    uv pip install --python "$ID42_WORK_VENV/bin/python" --no-cache \
      "id42-work[mcp,postgres,web] @ git+https://github.com/ID42Studio/id42-work.git@${ID42_WORK_REF}"; \
    uv pip install --python "$ID42_WORK_VENV/bin/python" --no-cache --no-deps \
      "id42-work-odoo @ git+https://github.com/ID42Studio/id42-work-odoo.git@${ID42_WORK_ODOO_REF}" \
      "id42-work-github @ git+https://github.com/ID42Studio/id42-work-github.git@${ID42_WORK_GITHUB_REF}"; \
    uv pip install --python "$ID42_WORK_VENV/bin/python" --no-cache 'httpx>=0.28,<1'; \
    "$ID42_WORK_VENV/bin/python" -c 'import httpx, id42_work_github, id42_work_odoo; from importlib.metadata import version; assert version("id42-work")=="0.1.0rc2"'; \
    ln -sfn "$ID42_WORK_VENV/bin/id42-work" /usr/local/bin/id42-work; \
    printf '%s\n' "$ID42_WORK_REF" > /opt/id42-agent/ID42_WORK_REF; \
    printf '%s\n' "$ID42_WORK_ODOO_REF" > /opt/id42-agent/ID42_WORK_ODOO_REF; \
    printf '%s\n' "$ID42_WORK_GITHUB_REF" > /opt/id42-agent/ID42_WORK_GITHUB_REF; \
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
