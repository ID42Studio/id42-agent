#!/bin/sh
# Publish the operator UI on the host loopback.
# This container joins only the operator network. It forwards bytes to
# id42-work-web and does not receive the operator token or the data volume.
set -eu

export PYTHONPATH="/opt/id42-agent${PYTHONPATH:+:$PYTHONPATH}"
exec setpriv --reuid=nanobot --regid=nanobot --init-groups \
    /opt/id42-work/venv/bin/python -m id42_agent.tcp_front
