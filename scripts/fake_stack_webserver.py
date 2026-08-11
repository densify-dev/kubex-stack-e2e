#!/usr/bin/env python3
"""Tiny HTTP server used to capture Kubex stack uploads in CI."""

from __future__ import annotations

import base64
import io
import json
import os
import tarfile
import zipfile
from copy import deepcopy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from time import time
from urllib.parse import urlparse


STATE_LOCK = Lock()
STATE: dict[str, list[dict[str, object]]] = {
    "requests": [],
    "uploads": [],
}

def _extract_archive_members(body: bytes) -> list[str]:
    members: list[str] = []

    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            members.extend(archive.namelist())
            return members
    except zipfile.BadZipFile:
        pass

    try:
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:*") as archive:
            members.extend(member.name for member in archive.getmembers() if member.name)
            return members
    except tarfile.TarError:
        pass

    return members


def _request_entry(handler: BaseHTTPRequestHandler, body: bytes) -> dict[str, object]:
    headers = {key: value for key, value in handler.headers.items()}
    entry: dict[str, object] = {
        "method": handler.command,
        "path": urlparse(handler.path).path,
        "query": urlparse(handler.path).query,
        "headers": headers,
        "body_size": len(body),
        "body_b64": base64.b64encode(body).decode("ascii"),
        "timestamp": int(time()),
    }
    if body:
        archive_members = _extract_archive_members(body)
        if archive_members:
            entry["archive_members"] = archive_members
        try:
            entry["body_text"] = body.decode("utf-8")
        except UnicodeDecodeError:
            pass
    return entry


def _record_request(handler: BaseHTTPRequestHandler, body: bytes) -> dict[str, object]:
    entry = _request_entry(handler, body)
    with STATE_LOCK:
        STATE["requests"].append(entry)
        if body:
            STATE["uploads"].append(entry)
    return entry


class Handler(BaseHTTPRequestHandler):
    server_version = "kubex-stack-webserver/1.0"

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length) if length > 0 else b""

    def _send_json(self, status: HTTPStatus, payload: object) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_empty(self, status: HTTPStatus) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/healthz":
            self._send_empty(HTTPStatus.OK)
            return
        if path == "/debug/state":
            with STATE_LOCK:
                snapshot = deepcopy(STATE)
            self._send_json(HTTPStatus.OK, snapshot)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        self._handle_write()

    def do_PUT(self) -> None:  # noqa: N802
        self._handle_write()

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle_write()

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle_write()

    def _handle_write(self) -> None:
        path = urlparse(self.path).path
        if path == "/debug/reset":
            with STATE_LOCK:
                for value in STATE.values():
                    value.clear()
            self._send_empty(HTTPStatus.NO_CONTENT)
            return

        body = self._read_body()
        _record_request(self, body)
        if path.endswith("/authorize"):
            self._send_json(
                HTTPStatus.OK,
                {
                    "apiToken": "stack-validation-token",
                    "expires": int((time() + 300) * 1000),
                    "status": int(HTTPStatus.OK),
                },
            )
            return
        self._send_json(
            HTTPStatus.OK,
            {"status": int(HTTPStatus.OK), "path": path, "bytes": len(body)},
        )


def main() -> int:
    listen_host = os.getenv("STACK_WEB_HOST", "0.0.0.0")
    listen_port = int(os.getenv("STACK_WEB_PORT", "8080"))
    server = ThreadingHTTPServer((listen_host, listen_port), Handler)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
