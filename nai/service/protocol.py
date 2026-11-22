"""Tiny JSON-lines control protocol over a local Unix socket."""
from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

MAX_MESSAGE = 4 * 1024 * 1024  # logs and reports can be sizeable


def request(path: Path, command: str, **args: Any) -> dict[str, Any]:
    """Send one request and return the parsed response."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(30.0)
        sock.connect(str(path))
        payload = json.dumps({"cmd": command, "args": args}).encode("utf-8") + b"\n"
        sock.sendall(payload)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_MESSAGE:
                raise RuntimeError("control response too large")
            if b"\n" in chunk:
                break
    data = b"".join(chunks).split(b"\n", 1)[0]
    if not data:
        raise RuntimeError("empty control response")
    response = json.loads(data)
    if not isinstance(response, dict):
        raise RuntimeError("invalid control response")
    return response


def read_request(conn: socket.socket) -> dict[str, Any] | None:
    """Read a single newline-terminated JSON request from a server connection."""
    buffer = b""
    conn.settimeout(5.0)
    while b"\n" not in buffer and len(buffer) < MAX_MESSAGE:
        chunk = conn.recv(65536)
        if not chunk:
            return None
        buffer += chunk
    line = buffer.split(b"\n", 1)[0]
    if not line:
        return None
    data = json.loads(line)
    if not isinstance(data, dict) or "cmd" not in data:
        raise ValueError("request must contain 'cmd'")
    return data


def write_response(conn: socket.socket, *, ok: bool, data: Any = None, error: str | None = None) -> None:
    payload = {"ok": ok, "data": data, "error": error}
    conn.sendall(json.dumps(payload, default=str).encode("utf-8") + b"\n")
