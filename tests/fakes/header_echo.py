#!/usr/bin/env python3
"""Lightweight HTTP request-header echo server for edge header-stripping probes.

Returns every received request header as JSON so CI checks can prove Caddy
``edge`` stripped ``X-Service-Token`` and ``X-User-Id`` before forwarding.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from typing import Any


def build_echo_payload(
    method: str,
    path: str,
    header_items: Iterable[tuple[str, str]],
) -> dict[str, Any]:
    """Build deterministic JSON echo payload from received HTTP metadata."""
    raw_headers = [[str(k), str(v)] for k, v in header_items]
    lower_headers: dict[str, str] = {}
    for k, v in raw_headers:
        lk = k.lower()
        if lk in lower_headers:
            lower_headers[lk] = f"{lower_headers[lk]}, {v}"
        else:
            lower_headers[lk] = v
    return {
        "method": method.upper(),
        "path": path,
        "headers": lower_headers,
        "raw_headers": raw_headers,
    }


def _make_handler() -> type[BaseHTTPRequestHandler]:
    class HeaderEchoHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _handle(self) -> None:
            path_only = self.path.split("?", 1)[0]
            if self.command == "GET" and path_only == "/health":
                self._send_json(200, {"status": "ok"})
                return
            length = int(self.headers.get("Content-Length") or "0")
            if length > 0:
                self.rfile.read(length)
            payload = build_echo_payload(
                self.command,
                self.path,
                self.headers.items(),
            )
            self._send_json(200, payload)

        def do_GET(self) -> None:  # noqa: N802
            self._handle()

        def do_POST(self) -> None:  # noqa: N802
            self._handle()

        def do_PUT(self) -> None:  # noqa: N802
            self._handle()

        def do_HEAD(self) -> None:  # noqa: N802
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            print(f"[header-echo] {self.address_string()} - {format % args}", flush=True)

    return HeaderEchoHandler


def main() -> None:
    parser = argparse.ArgumentParser(description="HTTP header echo probe server")
    parser.add_argument("--host", default=os.getenv("HEADER_ECHO_HOST", "0.0.0.0"))
    parser.add_argument(
        "--port", type=int, default=int(os.getenv("HEADER_ECHO_PORT", "8098"))
    )
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), _make_handler())
    print(f"[header-echo] Listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
