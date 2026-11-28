"""Find this container's address on the operator network.

The operator UI forwarder binds that address and no other. A miss refuses
to start. It does not listen on every interface.
"""

from __future__ import annotations

import ipaddress
import os
import socket

DEFAULT_SUBNET = "10.42.76.0/24"


def operator_address(cidr: str | None = None) -> str:
    """Return the local IPv4 address that belongs to ``cidr``."""
    raw = (cidr if cidr is not None else os.environ.get("ID42_OPERATOR_SUBNET", "")).strip()
    if not raw:
        raw = DEFAULT_SUBNET
    try:
        network = ipaddress.ip_network(raw, strict=False)
    except ValueError:
        raise SystemExit("id42-agent: operator subnet is not a network") from None
    if network.version != 4 or network.prefixlen >= 31:
        raise SystemExit("id42-agent: operator subnet must be an IPv4 network")
    probe = str(next(network.hosts()))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        try:
            sock.connect((probe, 9))
            local = sock.getsockname()[0]
        except OSError:
            raise SystemExit(
                "id42-agent: operator UI could not find an address on the operator network"
            ) from None
    finally:
        sock.close()
    try:
        address = ipaddress.ip_address(local)
    except ValueError:
        raise SystemExit(
            "id42-agent: operator UI could not find an address on the operator network"
        ) from None
    if address not in network:
        raise SystemExit(
            "id42-agent: operator UI refused to listen outside the operator network"
        )
    return str(address)
