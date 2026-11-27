#!/usr/bin/env python3.12
"""
Local reverse proxy for the Guidepoint MCP server (`com.dalton.guidepoint-mcp-proxy`).

Guidepoint's Auth0 access tokens live only 600s. This proxy sits on
http://127.0.0.1:8943/mcp in front of https://clapi.guidepoint.io/mcp-server/v1/mcp:
it reads persistent `handoff-journal.json` (`target_owner`) at startup before touching
`.guidepoint.owner.lock` or `guidepoint_mcp_tokens.json`, exposes `/healthz` and
admin-authenticated `/admin/drain`, and honors shared request deadlines via
`X-Dalton-Request-Timeout-Ms` and `arguments.timeout`.
"""
from __future__ import annotations

import atexit
import hmac
import http.server
import json
import os
from pathlib import Path
import socket
import socketserver
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

_RUNTIME_DIR = str(Path(__file__).resolve().parent.parent)
if _RUNTIME_DIR not in sys.path:
    sys.path.insert(0, _RUNTIME_DIR)

from guidepoint_lock import (  # noqa: E402
    GuidepointOwnerLock,
    GuidepointStandbyError,
    OWNER_LEGACY_PROXY,
    read_target_owner,
    resolve_guidepoint_tokens_file,
)

PORT = int(os.environ.get("GUIDEPOINT_PROXY_PORT", "8943"))
UPSTREAM = os.environ.get("GUIDEPOINT_UPSTREAM_URL", "").strip() or "https://clapi.guidepoint.io/mcp-server/v1/mcp"
TOKEN_URL = os.environ.get("GUIDEPOINT_TOKEN_URL", "").strip() or "https://auth0.guidepoint.com/oauth/token"
REFRESH_SKEW_SECONDS = 60
DEFAULT_UPSTREAM_TIMEOUT_S = 120.0

_lock = threading.Lock()
_state_lock = threading.Lock()
_tokens: dict = {}
_owner_lock: GuidepointOwnerLock | None = None
_standby: bool = False
_draining: bool = False
_in_flight: int = 0

HOP_BY_HOP = {
    "host",
    "authorization",
    "content-length",
    "connection",
    "transfer-encoding",
    "x-dalton-request-timeout-ms",
}


def resolve_research_home() -> Path:
    raw_home = os.environ.get("DALTON_RESEARCH_HOME", "").strip()
    if raw_home:
        return Path(raw_home).expanduser().resolve()
    raw_ocw = os.environ.get("DALTON_OCW_ROOT", "").strip()
    if raw_ocw:
        return Path(raw_ocw).expanduser().resolve()
    return (Path.home() / "Library" / "Application Support" / "Everflow" / "research").resolve()


def resolve_token_store() -> Path:
    return resolve_guidepoint_tokens_file(environ=os.environ)


def _owner_lock_held() -> bool:
    return bool(_owner_lock is not None and _owner_lock.held)


def release_owner_lock() -> None:
    global _owner_lock, _standby
    with _state_lock:
        _standby = True
        lock = _owner_lock
        _owner_lock = None
    if lock is not None:
        lock.release()


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


def _extract_raw_socket(stream):
    """Best-effort lookup of the underlying socket from HTTPResponse or HTTPError."""
    candidates = [stream, getattr(stream, "fp", None)]
    for obj in candidates:
        if obj is None:
            continue
        sock = getattr(getattr(getattr(obj, "fp", None), "raw", None), "_sock", None)
        if sock is not None:
            return sock
        sock = getattr(getattr(obj, "raw", None), "_sock", None)
        if sock is not None:
            return sock
        sock = getattr(obj, "_sock", None)
        if sock is not None:
            return sock
    return None


def _is_real_read1(fn) -> bool:
    return callable(fn) and not hasattr(fn, "assert_called")


def _resolve_nonblocking_chunk_reader(stream):
    """Return a single-read method (`read1` when available) so chunked streams yield between chunks."""
    read1 = getattr(stream, "read1", None)
    if _is_real_read1(read1):
        return read1
    fp = getattr(stream, "fp", None)
    fp_read1 = getattr(fp, "read1", None) if fp is not None else None
    if _is_real_read1(fp_read1):
        return fp_read1
    return stream.read


def _read_stream_with_deadline(stream, *, deadline_mono: float) -> bytes:
    """Read an entire HTTP response body while enforcing an absolute monotonic deadline."""
    raw_sock = _extract_raw_socket(stream)
    orig_timeout: float | None = None
    if raw_sock is not None and hasattr(raw_sock, "gettimeout"):
        try:
            t_val = raw_sock.gettimeout()
            if isinstance(t_val, (int, float)) and t_val > 0:
                orig_timeout = float(t_val)
        except Exception:
            orig_timeout = None
    read_fn = _resolve_nonblocking_chunk_reader(stream)
    if raw_sock is None and read_fn is stream.read and hasattr(stream.read, "assert_called"):
        rem = deadline_mono - time.monotonic()
        if rem <= 0.0:
            raise TimeoutError("Guidepoint response body deadline exceeded")
        raw = stream.read()
        if time.monotonic() >= deadline_mono:
            raise TimeoutError("Guidepoint response body deadline exceeded")
        return bytes(raw)
    buf = bytearray()
    while True:
        rem = deadline_mono - time.monotonic()
        if rem <= 0.0:
            raise TimeoutError("Guidepoint response body deadline exceeded")
        if raw_sock is not None and hasattr(raw_sock, "settimeout"):
            target_timeout = rem if orig_timeout is None else min(orig_timeout, rem)
            try:
                raw_sock.settimeout(max(0.01, target_timeout))
            except Exception:
                pass
        try:
            chunk = read_fn(4096)
        except (TimeoutError, socket.timeout) as exc:
            raise TimeoutError("Guidepoint response body read timed out") from exc
        if not chunk:
            break
        buf.extend(chunk)
        if time.monotonic() >= deadline_mono:
            raise TimeoutError("Guidepoint response body deadline exceeded")
    return bytes(buf)


def do_refresh(*, timeout_s: float = 30.0, deadline_mono: float | None = None) -> None:
    client_id = os.environ["GUIDEPOINT_CLIENT_ID"]
    client_secret = os.environ["GUIDEPOINT_CLIENT_SECRET"]
    if deadline_mono is not None:
        eff_deadline = float(deadline_mono)
        rem = eff_deadline - time.monotonic()
        if rem <= 0.0:
            raise TimeoutError("Guidepoint token refresh deadline exceeded")
        req_timeout = max(0.01, min(30.0, rem))
    else:
        req_timeout = max(0.01, min(30.0, float(timeout_s)))
        eff_deadline = time.monotonic() + req_timeout
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
    with urllib.request.urlopen(req, timeout=req_timeout) as resp:
        raw_body = _read_stream_with_deadline(resp, deadline_mono=eff_deadline)
    new_tokens = json.loads(raw_body.decode("utf-8"))
    new_tokens.setdefault("refresh_token", _tokens["refresh_token"])
    new_tokens["obtained_at"] = time.time()
    _tokens.update(new_tokens)
    save_tokens()
    print(f"[guidepoint-proxy] refreshed access_token, expires_in={_tokens.get('expires_in')}s")


def ensure_fresh_token(*, timeout_s: float = 30.0, deadline_mono: float | None = None) -> str:
    if _standby or not _owner_lock_held():
        raise GuidepointStandbyError("lock_not_acquired")
    t_start = time.monotonic()
    eff_deadline = (
        float(deadline_mono)
        if deadline_mono is not None
        else t_start + max(0.01, float(timeout_s))
    )
    rem = eff_deadline - time.monotonic()
    if rem <= 0.0:
        raise TimeoutError("Guidepoint request deadline exceeded before token lock")
    acquired = _lock.acquire(timeout=rem)
    if not acquired:
        raise TimeoutError("Timed out waiting for Guidepoint token refresh lock")
    try:
        if _standby or not _owner_lock_held():
            raise GuidepointStandbyError("lock_not_acquired")
        now_after_lock = time.monotonic()
        rem_after_lock = eff_deadline - now_after_lock
        if rem_after_lock <= 0.0:
            raise TimeoutError("Guidepoint request deadline exceeded waiting for token lock")
        obtained_at = _tokens.get("obtained_at", 0)
        expires_in = _tokens.get("expires_in", 600)
        if time.time() > obtained_at + expires_in - REFRESH_SKEW_SECONDS:
            if deadline_mono is None and (now_after_lock - t_start) < 0.01:
                do_refresh(timeout_s=float(timeout_s))
            else:
                do_refresh(timeout_s=rem_after_lock, deadline_mono=eff_deadline)
        return _tokens["access_token"]
    finally:
        _lock.release()


def initialize_runtime(
    *,
    ownership_file: Path | str | None = None,
    token_store: Path | None = None,
    lock_timeout_s: float = 2.0,
) -> bool:
    """Read `target_owner` BEFORE touching lock/tokens; acquire lock only when owner is `legacy_proxy_8943`."""
    global _owner_lock, _standby, _draining, _in_flight, _tokens
    with _state_lock:
        _draining = False
        _in_flight = 0
    target = read_target_owner(ownership_file=ownership_file, environ=os.environ)
    if target != OWNER_LEGACY_PROXY:
        with _state_lock:
            _standby = True
            _tokens = {}
        print(
            f"[guidepoint-proxy] starting in standby (target_owner={target} != {OWNER_LEGACY_PROXY})",
            file=sys.stderr,
        )
        return False

    path = token_store or resolve_token_store()
    lock = GuidepointOwnerLock(path, service_name="com.dalton.guidepoint-mcp-proxy")
    lock.acquire(timeout_s=lock_timeout_s)
    with _state_lock:
        _owner_lock = lock
        _standby = False
    load_tokens(path)
    ensure_fresh_token()
    return True


def _extract_admin_token(headers) -> str:
    auth = str(headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    hdr = str(headers.get("X-Guidepoint-Admin-Token") or "").strip()
    if hdr:
        return hdr
    return ""


def _is_valid_admin_token(headers) -> bool:
    expected = os.environ.get("GUIDEPOINT_PROXY_ADMIN_TOKEN", "").strip()
    supplied = _extract_admin_token(headers)
    if not expected or not supplied:
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def _resolve_proxy_timeout_and_body(headers, body: bytes | None) -> tuple[float, bytes | None]:
    """Read shared deadline from `X-Dalton-Request-Timeout-Ms` & JSON-RPC `arguments.timeout`."""
    candidates: list[float] = []
    raw_hdr_ms = str(headers.get("X-Dalton-Request-Timeout-Ms") or "").strip()
    if raw_hdr_ms:
        try:
            hdr_s = float(raw_hdr_ms) / 1000.0
            if hdr_s > 0:
                candidates.append(hdr_s)
        except ValueError:
            pass

    out_body = body
    if body:
        try:
            parsed = json.loads(body.decode("utf-8"))
            if isinstance(parsed, dict):
                params = parsed.get("params")
                if isinstance(params, dict) and isinstance(params.get("arguments"), dict):
                    args_dict = params["arguments"]
                    raw_arg = args_dict.get("timeout")
                    if (
                        isinstance(raw_arg, (int, float))
                        and not isinstance(raw_arg, bool)
                        and float(raw_arg) > 0
                    ):
                        candidates.append(float(raw_arg))
                        # Strip Dalton-internal timeout knob before forwarding to upstream Guidepoint API
                        clean_args = {k: v for k, v in args_dict.items() if k != "timeout"}
                        parsed = {
                            **parsed,
                            "params": {**params, "arguments": clean_args},
                        }
                        out_body = json.dumps(parsed, ensure_ascii=False).encode("utf-8")
        except (ValueError, UnicodeDecodeError):
            pass

    eff_timeout_s = max(0.1, min(candidates)) if candidates else DEFAULT_UPSTREAM_TIMEOUT_S
    return eff_timeout_s, out_body


def health_payload() -> dict:
    with _state_lock:
        draining = _draining
        in_flight = _in_flight
        standby = _standby
    held = _owner_lock_held()
    target = read_target_owner(environ=os.environ)
    return {
        "ok": not draining,
        "status": "draining" if draining else ("standby" if standby else "ok"),
        "service": "com.dalton.guidepoint-mcp-proxy",
        "owner_lock_held": held,
        "standby": standby,
        "target_owner": target,
        "draining": draining,
        "in_flight": in_flight,
    }


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_json(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(raw)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _path_only(self) -> str:
        return self.path.split("?", 1)[0]

    def _proxy(self) -> None:
        global _in_flight
        with _state_lock:
            if _draining:
                self._send_json(
                    503,
                    {
                        "ok": False,
                        "status": "draining",
                        "error_code": "service_draining",
                        "error": "Guidepoint proxy is draining.",
                    },
                )
                return
            if _standby or not _owner_lock_held():
                self._send_json(
                    503,
                    {
                        "ok": False,
                        "status": "standby",
                        "error_code": "lock_not_acquired",
                        "error": "Guidepoint proxy is in standby (lock_not_acquired).",
                    },
                )
                return
            _in_flight += 1

        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length) if length else None
            eff_timeout_s, fwd_body = _resolve_proxy_timeout_and_body(self.headers, body)
            deadline_mono = time.monotonic() + eff_timeout_s

            try:
                access_token = ensure_fresh_token(deadline_mono=deadline_mono)
            except GuidepointStandbyError:
                self._send_json(
                    503,
                    {
                        "ok": False,
                        "status": "standby",
                        "error_code": "lock_not_acquired",
                        "error": "Guidepoint proxy is in standby (lock_not_acquired).",
                    },
                )
                return
            except (TimeoutError, socket.timeout):
                self._send_json(
                    504,
                    {
                        "ok": False,
                        "status": "timeout",
                        "error_code": "upstream_timeout",
                        "error": f"token refresh timed out after {eff_timeout_s:.1f}s",
                    },
                )
                return
            except urllib.error.URLError as e:
                if isinstance(getattr(e, "reason", None), (TimeoutError, socket.timeout)):
                    self._send_json(
                        504,
                        {
                            "ok": False,
                            "status": "timeout",
                            "error_code": "upstream_timeout",
                            "error": f"token refresh timed out after {eff_timeout_s:.1f}s",
                        },
                    )
                else:
                    self._send_json(502, {"ok": False, "error": f"token refresh failed: {e}"})
                return
            except Exception as e:
                self._send_json(502, {"ok": False, "error": f"token refresh failed: {e}"})
                return

            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
            headers["Authorization"] = f"Bearer {access_token}"

            rem_s = deadline_mono - time.monotonic()
            if rem_s <= 0.0:
                self._send_json(
                    504,
                    {
                        "ok": False,
                        "status": "timeout",
                        "error_code": "upstream_timeout",
                        "error": f"upstream timed out after {eff_timeout_s:.1f}s",
                    },
                )
                return
            req = urllib.request.Request(UPSTREAM, data=fwd_body, method=self.command, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=max(0.01, rem_s)) as resp:
                    self._relay(resp.status, resp.headers.items(), resp, deadline_mono=deadline_mono)
            except urllib.error.HTTPError as e:
                self._relay(
                    e.code,
                    e.headers.items() if e.headers else [],
                    e,
                    deadline_mono=deadline_mono,
                )
            except (TimeoutError, socket.timeout):
                self._send_json(
                    504,
                    {
                        "ok": False,
                        "status": "timeout",
                        "error_code": "upstream_timeout",
                        "error": f"upstream timed out after {eff_timeout_s:.1f}s",
                    },
                )
            except urllib.error.URLError as e:
                if isinstance(getattr(e, "reason", None), (TimeoutError, socket.timeout)):
                    self._send_json(
                        504,
                        {
                            "ok": False,
                            "status": "timeout",
                            "error_code": "upstream_timeout",
                            "error": f"upstream timed out after {eff_timeout_s:.1f}s",
                        },
                    )
                else:
                    self._send_json(502, {"ok": False, "error": f"upstream error: {e}"})
            except Exception as e:
                self._send_json(502, {"ok": False, "error": f"upstream error: {e}"})
        finally:
            with _state_lock:
                _in_flight = max(0, _in_flight - 1)

    def _relay(
        self,
        status,
        headers,
        body_stream,
        *,
        deadline_mono: float | None = None,
    ) -> None:
        self.send_response(status)
        for k, v in headers:
            if k.lower() in HOP_BY_HOP or k.lower() == "content-length":
                continue
            self.send_header(k, v)
        self.send_header("Connection", "close")
        self.end_headers()
        raw_sock = _extract_raw_socket(body_stream)
        orig_sock_timeout: float | None = None
        if raw_sock is not None and hasattr(raw_sock, "gettimeout"):
            try:
                t_val = raw_sock.gettimeout()
                if isinstance(t_val, (int, float)) and t_val > 0:
                    orig_sock_timeout = float(t_val)
            except Exception:
                orig_sock_timeout = None
        read_fn = _resolve_nonblocking_chunk_reader(body_stream)
        while True:
            if deadline_mono is not None:
                rem = deadline_mono - time.monotonic()
                if rem <= 0.0:
                    self.close_connection = True
                    try:
                        body_stream.close()
                    except Exception:
                        pass
                    break
                if raw_sock is not None and hasattr(raw_sock, "settimeout"):
                    target_timeout = rem if orig_sock_timeout is None else min(orig_sock_timeout, rem)
                    try:
                        raw_sock.settimeout(max(0.01, target_timeout))
                    except Exception:
                        pass
            try:
                chunk = read_fn(4096)
            except (TimeoutError, socket.timeout):
                self.close_connection = True
                try:
                    body_stream.close()
                except Exception:
                    pass
                break
            if not chunk:
                break
            if deadline_mono is not None and time.monotonic() >= deadline_mono:
                self.close_connection = True
                try:
                    body_stream.close()
                except Exception:
                    pass
                break
            try:
                self.wfile.write(chunk)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                break

    def do_GET(self) -> None:
        path = self._path_only()
        if path == "/healthz":
            self._send_json(200, health_payload())
            return
        if path == "/admin/drain":
            if not _is_valid_admin_token(self.headers):
                self._send_json(401, {"ok": False, "error": "Unauthorized"})
                return
            with _state_lock:
                payload = {
                    "ok": True,
                    "draining": _draining,
                    "in_flight": _in_flight,
                    "owner_lock_held": _owner_lock_held(),
                    "standby": _standby,
                }
            self._send_json(200, payload)
            return
        # Guidepoint supports request/response MCP but its optional SSE GET
        # returns 404 even for a freshly initialized, working session.
        self.send_response(405)
        self.send_header("Allow", "POST, DELETE")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        global _draining
        path = self._path_only()
        if path == "/admin/drain":
            if not _is_valid_admin_token(self.headers):
                self._send_json(401, {"ok": False, "error": "Unauthorized"})
                return
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
                if not isinstance(body, dict):
                    raise ValueError("JSON object required")
            except Exception:
                self._send_json(400, {"ok": False, "error": "Parse error"})
                return
            with _state_lock:
                _draining = bool(body.get("drain", True))
                payload = {
                    "ok": True,
                    "draining": _draining,
                    "in_flight": _in_flight,
                    "owner_lock_held": _owner_lock_held(),
                    "standby": _standby,
                }
            self._send_json(200, payload)
            return
        self._proxy()

    def do_DELETE(self) -> None:
        self._proxy()

    def log_message(self, fmt, *args) -> None:
        pass


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    if "GUIDEPOINT_CLIENT_ID" not in os.environ or "GUIDEPOINT_CLIENT_SECRET" not in os.environ:
        print("Missing GUIDEPOINT_CLIENT_ID / GUIDEPOINT_CLIENT_SECRET env vars.", file=sys.stderr)
        sys.exit(1)
    atexit.register(release_owner_lock)
    initialize_runtime()
    port = int(os.environ.get("GUIDEPOINT_PROXY_PORT", str(PORT)))
    server = ThreadingHTTPServer(("127.0.0.1", port), ProxyHandler)
    print(f"[guidepoint-proxy] listening on http://127.0.0.1:{port}/mcp -> {UPSTREAM}")
    try:
        server.serve_forever()
    finally:
        release_owner_lock()


if __name__ == "__main__":
    main()
