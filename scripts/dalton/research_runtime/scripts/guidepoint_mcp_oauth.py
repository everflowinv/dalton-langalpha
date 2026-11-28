#!/usr/bin/env python3.12
"""
Guidepoint MCP OAuth 2.0 Authorization Code flow (loopback redirect, RFC 8252 style).

Usage:
    /opt/homebrew/bin/python3.12 scripts/dalton/research_runtime/scripts/guidepoint_mcp_oauth.py

Flow:
    1. Starts a local HTTP server on http://localhost:8942/callback
    2. Opens the Auth0 authorization URL in the default browser
    3. User logs in via Guidepoint 360 (email/password/MFA)
    4. Auth0 redirects back to the local server with ?code=...
    5. Script exchanges the code for access_token/refresh_token via Auth0 token endpoint
    6. Tokens are atomically saved (0600) to GUIDEPOINT_TOKENS_FILE or
       $DALTON_RESEARCH_HOME/config/guidepoint_mcp_tokens.json

Reads GUIDEPOINT_CLIENT_ID / GUIDEPOINT_CLIENT_SECRET from the environment, or
falls back to GUIDEPOINT_ENV_FILE / $DALTON_RESEARCH_HOME/config/guidepoint_mcp.env.
"""
from __future__ import annotations

import http.server
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import webbrowser

PORT = 8942
CALLBACK_PATH = "/callback"
REDIRECT_URI = f"http://localhost:{PORT}{CALLBACK_PATH}"

AUTH_URL = "https://auth0.guidepoint.com/authorize"
TOKEN_URL = "https://auth0.guidepoint.com/oauth/token"
MCP_SERVER_URL = "https://clapi.guidepoint.io/mcp-server/v1/mcp"
SCOPE = "openid profile email offline_access"

_result: dict[str, str] = {}


def resolve_research_home() -> Path:
    raw_home = os.environ.get("DALTON_RESEARCH_HOME", "").strip()
    if raw_home:
        return Path(raw_home).expanduser().resolve()
    return (Path.home() / "Library" / "Application Support" / "Everflow" / "research").resolve()


def resolve_env_file() -> Path:
    override = os.environ.get("GUIDEPOINT_ENV_FILE", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return resolve_research_home() / "config" / "guidepoint_mcp.env"


def resolve_token_store() -> Path:
    override = os.environ.get("GUIDEPOINT_TOKENS_FILE", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return resolve_research_home() / "config" / "guidepoint_mcp_tokens.json"


def _read_client_credentials_from_env_file(env_path: Path) -> tuple[str | None, str | None]:
    if not env_path.is_file():
        return None, None
    cid: str | None = None
    csec: str | None = None
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip("'").strip('"')
        if k == "GUIDEPOINT_CLIENT_ID" and v:
            cid = v
        elif k == "GUIDEPOINT_CLIENT_SECRET" and v:
            csec = v
    return cid, csec


def save_tokens(tokens: dict, token_store: Path | None = None) -> Path:
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
            json.dump(tokens, f, indent=2)
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
    return path


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self.send_response(404)
            self.end_headers()
            return

        qs = urllib.parse.parse_qs(parsed.query)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()

        if "code" in qs:
            _result["code"] = qs["code"][0]
            self.wfile.write(b"<html><body>Authorization received. You can close this tab.</body></html>")
        else:
            _result["error"] = qs.get("error_description", qs.get("error", ["unknown_error"]))[0]
            self.wfile.write(b"<html><body>Authorization failed. Check the terminal.</body></html>")

        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def log_message(self, fmt, *args) -> None:
        pass


def exchange_code_for_token(code: str, client_id: str, client_secret: str) -> dict:
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }).encode()

    req = urllib.request.Request(
        TOKEN_URL,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def main() -> None:
    _result.clear()
    client_id = os.environ.get("GUIDEPOINT_CLIENT_ID")
    client_secret = os.environ.get("GUIDEPOINT_CLIENT_SECRET")
    if not client_id or not client_secret:
        file_cid, file_csec = _read_client_credentials_from_env_file(resolve_env_file())
        client_id = client_id or file_cid
        client_secret = client_secret or file_csec
    if not client_id or not client_secret:
        print("Missing GUIDEPOINT_CLIENT_ID / GUIDEPOINT_CLIENT_SECRET env vars.", file=sys.stderr)
        print("Get these from Guidepoint sales/contact once the callback URL below is whitelisted.", file=sys.stderr)
        print(f"Callback URL to whitelist: {REDIRECT_URI}", file=sys.stderr)
        sys.exit(1)

    auth_params = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "audience": MCP_SERVER_URL,
    })
    auth_url = f"{AUTH_URL}?{auth_params}"

    server = http.server.HTTPServer(("localhost", PORT), CallbackHandler)
    print(f"Listening on {REDIRECT_URI}")
    print(f"Opening browser for authorization...\n{auth_url}")
    webbrowser.open(auth_url)
    server.serve_forever()

    if "error" in _result:
        print(f"Authorization failed: {_result['error']}", file=sys.stderr)
        sys.exit(1)

    code = _result["code"]
    print("Got authorization code, exchanging for tokens...")
    tokens = exchange_code_for_token(code, client_id, client_secret)
    tokens["obtained_at"] = time.time()

    saved_path = save_tokens(tokens)
    print(f"Tokens saved to {saved_path}")
    print(f"access_token expires_in: {tokens.get('expires_in')}s")
    print("refresh_token stored — use it to mint new access tokens without re-login.")


if __name__ == "__main__":
    main()
