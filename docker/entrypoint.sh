#!/bin/sh
# Fail closed when the data volume has no config yet.
# Upstream docker bootstrap would otherwise create a config that leaves
# tools.exec.enable on and tools.restrictToWorkspace off. id42-agent renders
# that file once, from scripts/bootstrap.sh, and does not overwrite it later.
set -eu

# The id42-work store is a named volume, not the agent workspace. Root can
# chown it (CAP_CHOWN). chmod only succeeds while root still owns the
# directory; after the first boot it is already mode 700 and owned by nanobot.
# The authority key file is created later by the factory, as uid 1000, mode
# 0600. Do not create it here. This process is root and must not write key
# material.
store=/var/lib/id42-work
if [ -d "$store" ]; then
    chmod 700 "$store" 2>/dev/null || true
    chown nanobot:nanobot "$store"
fi

dir="${HOME:-/home/nanobot}/.nanobot"
if [ ! -f "$dir/config.json" ]; then
    echo "id42-agent: $dir/config.json is missing." >&2
    echo "Run scripts/bootstrap.sh (or 'make init') before starting this container." >&2
    exit 1
fi

# Inline database and connector secrets become mode 0600 files. config.json
# already holds the paths. This does not write the authority key. The module
# switches to uid 1000: this process has no DAC_OVERRIDE, so it cannot search
# the mode 0700 store after the chown above.
if ! /app/.venv/bin/python -m id42_agent.materialize; then
    echo "id42-agent: could not store inline secrets as files." >&2
    exit 1
fi

exec /usr/local/bin/entrypoint.sh "$@"
