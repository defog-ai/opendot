"""Connect a model CLI's stdio MCP client to the host gateway's unix socket.

This file is copied into each step's run folder and runs inside the container
with the image's python3, so it uses the standard library only:

    python3 /opendot/mcp/bridge.py /opendot/mcp/<connector>.sock

Bytes from stdin go to the socket and bytes from the socket go to stdout. The
bridge holds no login and adds nothing: the host decides what each call may do.
"""

import os
import socket
import sys
import threading

CHUNK = 65536


def _stdin_to_socket(sock: socket.socket) -> None:
    source = sys.stdin.buffer
    try:
        while True:
            data = source.read1(CHUNK)
            if not data:
                break
            sock.sendall(data)
    except OSError:
        pass
    try:
        sock.shutdown(socket.SHUT_WR)
    except OSError:
        pass


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write("usage: bridge.py <socket path>\n")
        return 2
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(argv[1])
    except OSError as exc:
        sys.stderr.write(f"opendot bridge: cannot reach the host gateway at {argv[1]}: {exc}\n")
        return 1
    threading.Thread(target=_stdin_to_socket, args=(sock,), daemon=True).start()
    out = sys.stdout.buffer
    try:
        while True:
            data = sock.recv(CHUNK)
            if not data:
                break
            out.write(data)
            out.flush()
    except (OSError, BrokenPipeError):
        return 0
    finally:
        sock.close()
    return 0


if __name__ == "__main__":
    code = main(sys.argv)
    sys.stdout.flush()
    os._exit(code)
