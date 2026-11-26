#!/bin/sh
# Fail closed when the data volume has no config yet.
# Upstream docker bootstrap would otherwise create a config that leaves
# tools.exec.enable on and tools.restrictToWorkspace off. id42-agent renders
# that file once, from scripts/bootstrap.sh, and does not overwrite it later.
set -eu

dir="${HOME:-/home/nanobot}/.nanobot"
if [ ! -f "$dir/config.json" ]; then
    echo "id42-agent: $dir/config.json is missing." >&2
    echo "Run scripts/bootstrap.sh (or 'make init') before starting this container." >&2
    exit 1
fi

exec /usr/local/bin/entrypoint.sh "$@"
