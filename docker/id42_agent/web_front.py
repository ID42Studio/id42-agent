"""Run the operator UI on loopback and forward the operator-network port to it.

``id42-work web serve`` accepts only a loopback bind. This process starts the
UI on ``127.0.0.1:8768`` and copies bytes from the address on the operator
network, port 8767, to that listener. Nothing listens on the store network.
A second container, attached only to the operator network, publishes 8767 on
the host loopback. The operator token stays in the environment. It is not a
process argument.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time

from id42_agent.operator_net import operator_address

INTERNAL_HOST = "127.0.0.1"
INTERNAL_PORT = 8768
LISTEN_PORT = 8767
_FACTORY = "id42_agent.mcp_host:server"


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


def _handle(client: socket.socket) -> None:
    try:
        upstream = socket.create_connection((INTERNAL_HOST, INTERNAL_PORT), timeout=10)
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


def _wait_until_listening(process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit("id42-agent: operator UI exited before it listened")
        try:
            with socket.create_connection((INTERNAL_HOST, INTERNAL_PORT), timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)
    raise SystemExit("id42-agent: operator UI did not listen on 127.0.0.1")


def _serve(process: subprocess.Popen[bytes], listen_host: str) -> None:
    server = socket.create_server((listen_host, LISTEN_PORT), reuse_port=False)
    server.settimeout(1.0)

    def stop(signum: int, _frame: object) -> None:
        del signum
        server.close()
        if process.poll() is None:
            process.terminate()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while process.poll() is None:
        try:
            client, _address = server.accept()
        except TimeoutError:
            continue
        except OSError:
            break
        threading.Thread(target=_handle, args=(client,), daemon=True).start()
    raise SystemExit("id42-agent: operator UI exited")


def main() -> None:
    if not os.environ.get("ID42_WORK_WEB_TOKEN") and not os.environ.get("ID42_WORK_WEB_TOKEN_FILE"):
        print(
            "id42-agent: set ID42_WORK_WEB_TOKEN or ID42_WORK_WEB_TOKEN_FILE",
            file=sys.stderr,
        )
        raise SystemExit(1)
    command = [
        "id42-work",
        "web",
        "serve",
        "--factory",
        _FACTORY,
        "--host",
        INTERNAL_HOST,
        "--port",
        str(INTERNAL_PORT),
    ]
    process = subprocess.Popen(command)  # noqa: S603 - fixed argv, token stays in the environment
    try:
        _wait_until_listening(process)
        _serve(process, operator_address())
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    main()
