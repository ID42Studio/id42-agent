#!/bin/sh
# Start the id42-work operator UI as uid 1000.
# The UI binds 127.0.0.1. docker/id42_agent/web_front.py forwards the
# published port to that listener. The token is an environment variable
# or a mode 0600 file. It is not a process argument and is not printed.
set -eu

store=/var/lib/id42-work
if [ -d "$store" ]; then
    chmod 700 "$store" 2>/dev/null || true
    chown nanobot:nanobot "$store"
fi

if [ -z "${ID42_WORK_WEB_TOKEN:-}" ] && [ -z "${ID42_WORK_WEB_TOKEN_FILE:-}" ]; then
    echo "id42-agent: set ID42_WORK_WEB_TOKEN or ID42_WORK_WEB_TOKEN_FILE before starting the operator UI." >&2
    exit 1
fi
# An empty file path would override the token. Drop blank values.
if [ -z "${ID42_WORK_WEB_TOKEN_FILE:-}" ]; then
    unset ID42_WORK_WEB_TOKEN_FILE
fi
if [ -z "${ID42_WORK_WEB_TOKEN:-}" ]; then
    unset ID42_WORK_WEB_TOKEN
fi

export PYTHONPATH="/opt/id42-agent${PYTHONPATH:+:$PYTHONPATH}"
# The module switches to uid 1000. This process has no DAC_OVERRIDE, so it
# cannot search the mode 0700 store after the chown above.
if ! /opt/id42-work/venv/bin/python -m id42_agent.materialize; then
    echo "id42-agent: could not store inline secrets as files." >&2
    exit 1
fi
exec setpriv --reuid=nanobot --regid=nanobot --init-groups \
    /opt/id42-work/venv/bin/python -m id42_agent.web_front
