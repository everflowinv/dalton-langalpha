#!/usr/bin/env python3.12
"""
Local reverse proxy for the Guidepoint MCP server.

Guidepoint's Auth0 access tokens live only 600s. This proxy sits on
http://127.0.0.1:8943/mcp in front of https://clapi.guidepoint.io/mcp-server/v1/mcp:
it holds the refresh_token, mints a fresh access_token whenever the current one
is about to expire, and forwards every request/response otherwise.

Requires GUIDEPOINT_CLIENT_ID / GUIDEPOINT_CLIENT_SECRET in the environment
and an existing guidepoint_mcp_tokens.json (run guidepoint_mcp_oauth.py once
to seed it).
"""
from __future__ import annotations

import http.server
import json
import os
from pathlib import Path
import socketserver
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

PORT = int(os.environ.get("GUIDEPOINT_PROXY_PORT", "8943"))
UPSTREAM = "https://clapi.guidepoint.io/mcp-server/v1/mcp"
TOKEN_URL = "https://auth0.guidepoint.com/oauth/token"
REFRESH_SKEW_SECONDS = 60

_lock = threading.Lock()
_tokens: dict = {}

HOP_BY_HOP = {"host", "authorization", "content-length", "connection", "transfer-encoding"}


def resolve_research_home() -> Path:
    raw_home = os.environ.get("DALTON_RESEARCH_HOME", "").strip()
    if raw_home:
        return Path(raw_home).expanduser().resolve()
    raw_ocw = os.environ.get("DALTON_OCW_ROOT", "").strip()
    if raw_ocw:
        return Path(raw_ocw).expanduser().resolve()
    return (Path.home() / "Library" / "Application Support" / "Everflow" / "research").resolve()


def resolve_token_store() -> Path:
    override = os.environ.get("GUIDEPOINT_TOKENS_FILE", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return resolve_research_home() / "config" / "guidepoint_mcp_tokens.json"


def load_tokens(token_store: Path | None = None) -> None:
    global _tokens
    path = token_store or resolve_token_store()
    if not path.exists():
        print(
            f"No token store at {path}. Run guidepoint_mcp_oauth.py first.",
            file=sys.stderr,
        )
        sys.exit(1)
    with path.open("r", encoding="utf-8") as f:
        _tokens = json.load(f)
    if "access_token" not in _tokens or "refresh_token" not in _tokens:
        print("Token store is missing access_token/refresh_token.", file=sys.stderr)
        sys.exit(1)


def save_tokens(token_store: Path | None = None) -> None:
    path = token_store or resolve_token_store()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=".guidepoint_mcp_tokens.",
        suffix=".tmp",
    )
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(_tokens, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


def do_refresh() -> None:
    client_id = os.environ["GUIDEPOINT_CLIENT_ID"]
    client_secret = os.environ["GUIDEPOINT_CLIENT_SECRET"]
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": _tokens["refresh_token"],
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        new_tokens = json.load(resp)
    new_tokens.setdefault("refresh_token", _tokens["refresh_token"])
    new_tokens["obtained_at"] = time.time()
    _tokens.update(new_tokens)
    save_tokens()
    print(f"[guidepoint-proxy] refreshed access_token, expires_in={_tokens.get('expires_in')}s")


def ensure_fresh_token() -> str:
    with _lock:
        obtained_at = _tokens.get("obtained_at", 0)
        expires_in = _tokens.get("expires_in", 600)
        if time.time() > obtained_at + expires_in - REFRESH_SKEW_SECONDS:
            do_refresh()
        return _tokens["access_token"]


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _proxy(self) -> None:
        try:
            access_token = ensure_fresh_token()
        except Exception as e:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(f"token refresh failed: {e}".encode())
            return

        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else None

        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
        headers["Authorization"] = f"Bearer {access_token}"

        req = urllib.request.Request(UPSTREAM, data=body, method=self.command, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                self._relay(resp.status, resp.headers.items(), resp)
        except urllib.error.HTTPError as e:
            self._relay(e.code, e.headers.items() if e.headers else [], e)
        except Exception as e:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(f"upstream error: {e}".encode())

    def _relay(self, status, headers, body_stream) -> None:
        self.send_response(status)
        for k, v in headers:
            if k.lower() in HOP_BY_HOP or k.lower() == "content-length":
                continue
            self.send_header(k, v)
        self.send_header("Connection", "close")
        self.end_headers()
        while True:
            chunk = body_stream.read(4096)
            if not chunk:
                break
            try:
                self.wfile.write(chunk)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                break

    def do_GET(self) -> None:
        # Guidepoint supports request/response MCP but its optional SSE GET
        # returns 404 even for a freshly initialized, working session. Advertise
        # unsupported GET as MCP expects; POST/DELETE still reach upstream with
        # their original error semantics.
        self.send_response(405)
        self.send_header("Allow", "POST, DELETE")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        self._proxy()

    def do_DELETE(self) -> None:
        self._proxy()

    def log_message(self, fmt, *args) -> None:
        pass


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def main() -> None:
    if "GUIDEPOINT_CLIENT_ID" not in os.environ or "GUIDEPOINT_CLIENT_SECRET" not in os.environ:
        print("Missing GUIDEPOINT_CLIENT_ID / GUIDEPOINT_CLIENT_SECRET env vars.", file=sys.stderr)
        sys.exit(1)
    load_tokens()
    ensure_fresh_token()
    port = int(os.environ.get("GUIDEPOINT_PROXY_PORT", str(PORT)))
    server = ThreadingHTTPServer(("127.0.0.1", port), ProxyHandler)
    print(f"[guidepoint-proxy] listening on http://127.0.0.1:{port}/mcp -> {UPSTREAM}")
    server.serve_forever()


if __name__ == "__main__":
    main()
