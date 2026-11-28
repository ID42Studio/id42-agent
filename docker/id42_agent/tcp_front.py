"""Forward one TCP port to an upstream host.

The publisher container is attached only to the operator network. Compose
publishes its listen port on the host loopback. No secret is read here.
"""

from __future__ import annotations

import os
import signal
import socket
import sys
import threading

_LISTEN_HOST = "0.0.0.0"  # noqa: S104 - this container has only the operator network


def _pipe(source: socket.socket, destination: socket.socket) -> None:
    try:
        while True:
            chunk = source.recv(65536)
            if not chunk:
                break
            destination.sendall(chunk)
    except OSError:
        pass
    finally:
        try:
            destination.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _handle(client: socket.socket, upstream_host: str, upstream_port: int) -> None:
    try:
        upstream = socket.create_connection((upstream_host, upstream_port), timeout=10)
    except OSError:
        client.close()
        return
    upstream.settimeout(None)
    client.settimeout(None)
    left = threading.Thread(target=_pipe, args=(client, upstream), daemon=True)
    right = threading.Thread(target=_pipe, args=(upstream, client), daemon=True)
    left.start()
    right.start()
    left.join()
    right.join()
    client.close()
    upstream.close()


def main() -> None:
    upstream_host = os.environ.get("ID42_FRONT_UPSTREAM", "").strip()
    if not upstream_host:
        print("id42-agent: ID42_FRONT_UPSTREAM is empty", file=sys.stderr)
        raise SystemExit(1)
    try:
        upstream_port = int(os.environ.get("ID42_FRONT_UPSTREAM_PORT", "8767"))
        listen_port = int(os.environ.get("ID42_FRONT_LISTEN_PORT", "8767"))
    except ValueError:
        print("id42-agent: forwarder ports must be integers", file=sys.stderr)
        raise SystemExit(1) from None
    server = socket.create_server((_LISTEN_HOST, listen_port), reuse_port=False)
    server.settimeout(1.0)

    def stop(signum: int, _frame: object) -> None:
        del signum
        server.close()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while True:
        try:
            client, _address = server.accept()
        except TimeoutError:
            continue
        except OSError:
            break
        threading.Thread(
            target=_handle,
            args=(client, upstream_host, upstream_port),
            daemon=True,
        ).start()


if __name__ == "__main__":
    main()
