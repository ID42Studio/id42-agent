"""Security baseline for the running nanobot-gateway container.

A compliant container publishes 8765 and 18790 on loopback, drops every
capability except CHOWN, SETGID, and SETUID, sets no-new-privileges, and
does not mount the Docker socket or a host home directory. The read-only
entrypoint script is the one allowed bind from a checkout, including when
that checkout lives under /home (GitHub runners and typical developer clones).

Named volumes are not host home directories. nanobot-data is mounted at
/home/nanobot/.nanobot. id42-work-data is mounted at /var/lib/id42-work,
outside the agent workspace and outside any home directory.
"""

from __future__ import annotations

import json
import subprocess
import sys

ENTRYPOINT_DEST = "/id42/entrypoint.sh"
ENTRYPOINT_SUFFIX = "/docker/entrypoint.sh"
ALLOWED_CAPS = frozenset({"CHOWN", "SETGID", "SETUID"})
LOOPBACK = frozenset({"127.0.0.1", "::1"})
PUBLISHED = ("8765/tcp", "18790/tcp")


def _caps(items) -> set[str]:
    names = set()
    for item in items or []:
        name = str(item).upper()
        if name.startswith("CAP_"):
            name = name[4:]
        names.add(name)
    return names


def _host_home(source: str) -> bool:
    if source == "/root" or source.startswith("/root/"):
        return True
    parts = source.split("/")
    return len(parts) >= 3 and parts[1] == "home" and bool(parts[2])


def mount_errors(source: str, destination: str) -> list[str]:
    errors = []
    blob = f"{source} {destination}"
    if "docker.sock" in blob:
        errors.append("docker socket is mounted")
    if destination in {"/home", "/root", "/home/nanobot"}:
        errors.append(f"home directory is mounted ({source} -> {destination})")
        return errors
    # Named volumes are mounted inside the container. Their source is a
    # Docker volume path, not a host home: nanobot-data at
    # /home/nanobot/.nanobot, id42-work-data at /var/lib/id42-work.
    # Reject a bind of the host home (or anything under it) except the
    # single read-only entrypoint script.
    if _host_home(source):
        allowed = (
            destination == ENTRYPOINT_DEST and source.endswith(ENTRYPOINT_SUFFIX)
        )
        if not allowed:
            errors.append(f"home directory is mounted ({source} -> {destination})")
    return errors


def security_errors(info: dict) -> list[str]:
    host = info["HostConfig"]
    errors: list[str] = []
    bindings = host.get("PortBindings") or {}
    for port in PUBLISHED:
        items = bindings.get(port) or []
        if not items:
            errors.append(f"{port} is not published")
            continue
        for item in items:
            ip = item.get("HostIp") or ""
            if ip not in LOOPBACK:
                errors.append(f"{port} is published on {ip or 'all interfaces'}")
    for mount in info.get("Mounts") or []:
        errors.extend(
            mount_errors(mount.get("Source") or "", mount.get("Destination") or "")
        )
    dropped = _caps(host.get("CapDrop"))
    added = _caps(host.get("CapAdd"))
    if "ALL" not in dropped:
        errors.append(f"cap_drop is {sorted(dropped)}")
    extra = added - ALLOWED_CAPS
    if extra:
        errors.append(f"unexpected capabilities: {sorted(extra)}")
    security = " ".join(host.get("SecurityOpt") or [])
    if "no-new-privileges" not in security:
        errors.append("no-new-privileges is not set")
    return errors


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: baseline.py CONTAINER", file=sys.stderr)
        return 2
    raw = subprocess.check_output(["docker", "inspect", argv[1]], text=True)
    errors = security_errors(json.loads(raw)[0])
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
